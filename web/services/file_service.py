"""
web/services/file_service.py — File upload storage, validation, and lifecycle management.

Handles streaming uploads to disk, magic byte / integrity validation, total-size
accounting, scheduled deletion timestamps, and the cleanup sweep that actually
removes expired job directories.

Design decisions:
- save_upload() returns an *unsaved* FileItem so the caller can persist it inside
  their own DB transaction (avoids partial writes on validation failure).
- validate_magic_bytes() is async for PDFs/images (aiofiles reads); DOCX uses
  the synchronous zipfile module which is acceptable — it reads only a few bytes
  of a local file and completes in microseconds.
- cleanup_expired_jobs() is idempotent and safe to call on every startup as well
  as on a periodic schedule.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import aiofiles
from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.models import FileItem, JobStatus, PrintJob

logger = logging.getLogger(__name__)

# ── Magic byte constants ───────────────────────────────────────────────────────

_MAGIC_PDF  = b"%PDF"
_MAGIC_PNG  = b"\x89PNG\r\n\x1a\n"
_MAGIC_JPEG = b"\xff\xd8"

# Normalised to lowercase dot-prefixed extension strings
_ALLOWED_EXTENSIONS: frozenset[str] = frozenset({".pdf", ".docx", ".jpg", ".jpeg", ".png"})

# Extension → canonical extension (used to normalise .jpeg → .jpg on disk)
_EXT_CANONICAL: dict[str, str] = {
    ".pdf":  ".pdf",
    ".docx": ".docx",
    ".jpg":  ".jpg",
    ".jpeg": ".jpg",
    ".png":  ".png",
}


# ── Public helpers ─────────────────────────────────────────────────────────────

async def save_upload(
    job_id: str,
    file: UploadFile,
    sort_order: int = 0,
) -> FileItem:
    """
    Stream *file* to ``uploads/<job_id>/<uuid>.<ext>``, validate its magic bytes,
    and return an **unsaved** FileItem ORM object.

    The caller is responsible for adding the object to their DB session and
    committing. Keeps this service DB-session-free.

    Raises:
        ValueError: extension not allowed, or magic bytes / integrity check fails.
        OSError: upload directory cannot be created or the file cannot be written.
    """
    original_name = file.filename or "upload"
    raw_ext = Path(original_name).suffix.lower()

    # EDGE CASE: no extension — attempt detection from magic bytes after streaming
    if raw_ext not in _ALLOWED_EXTENSIONS:
        if raw_ext:
            raise ValueError(
                f"File type {raw_ext!r} not accepted. "
                "Only PDF, DOCX, JPG, and PNG files are allowed."
            )
        # No extension at all: we will detect later; use a temp name
        raw_ext = ""

    ext = _EXT_CANONICAL.get(raw_ext, raw_ext)  # "" stays "" until detection below

    stored_filename = f"{uuid.uuid4().hex}{ext or '.bin'}"
    upload_dir = Path(settings.UPLOAD_DIR) / job_id
    upload_dir.mkdir(parents=True, exist_ok=True)
    stored_path = upload_dir / stored_filename

    # ── Stream to disk ────────────────────────────────────────────────────────
    bytes_written = 0
    try:
        async with aiofiles.open(stored_path, "wb") as out:
            while True:
                chunk = await file.read(65_536)  # 64 KB chunks
                if not chunk:
                    break
                bytes_written += len(chunk)
                if bytes_written > settings.MAX_UPLOAD_BYTES:
                    # Don't let a single file blow past the global cap
                    raise ValueError(
                        f"File exceeds the {settings.MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit."
                    )
                await out.write(chunk)
    except Exception:
        # Clean up partial write before re-raising
        stored_path.unlink(missing_ok=True)
        raise

    # ── Detect extension from magic bytes if none was declared ────────────────
    if not ext:
        detected = await _detect_extension_from_magic(stored_path)
        if detected is None:
            stored_path.unlink(missing_ok=True)
            raise ValueError(
                "Could not determine file type. "
                "Only PDF, DOCX, JPG, and PNG files are accepted."
            )
        ext = detected
        # Rename the file to have the correct extension
        new_path = stored_path.with_suffix(ext)
        stored_path.rename(new_path)
        stored_path = new_path

    # ── Validate magic bytes ──────────────────────────────────────────────────
    try:
        valid = await validate_magic_bytes(stored_path, ext)
    except Exception as exc:
        logger.error("Magic byte check raised an exception for %s: %s", stored_path, exc)
        stored_path.unlink(missing_ok=True)
        raise ValueError(f"File integrity check failed: {exc}") from exc

    if not valid:
        stored_path.unlink(missing_ok=True)
        raise ValueError(
            f"File content does not match its declared type ({ext!r}). "
            "The file may be corrupt or disguised."
        )

    # ── Post-magic integrity check ────────────────────────────────────────────
    try:
        await _check_file_integrity(stored_path, ext)
    except ValueError:
        stored_path.unlink(missing_ok=True)
        raise
    except Exception as exc:
        logger.error("Integrity check failed for %s: %s", stored_path, exc)
        stored_path.unlink(missing_ok=True)
        raise ValueError("File appears damaged or cannot be read.") from exc

    return FileItem(
        job_id=job_id,
        original_name=original_name,
        stored_path=str(stored_path),
        sort_order=sort_order,
        page_count=0,  # Populated later by pdf_processor / conversion_service
    )


async def validate_magic_bytes(file_path: Path | str, declared_ext: str) -> bool:
    """
    Read the first N bytes of *file_path* and verify they match the expected
    magic signature for *declared_ext*.

    Supported:
      PDF  — first 4 bytes == ``%PDF``
      JPEG — first 2 bytes == ``\\xFF\\xD8``
      PNG  — first 8 bytes == ``\\x89PNG\\r\\n\\x1a\\n``
      DOCX — valid ZIP archive containing ``word/document.xml``

    Returns ``True`` if valid, ``False`` if not.
    Raises ``OSError`` only if the file cannot be opened.
    """
    file_path = Path(file_path)
    ext = _EXT_CANONICAL.get(declared_ext.lower(), declared_ext.lower())

    if ext == ".pdf":
        async with aiofiles.open(file_path, "rb") as fh:
            header = await fh.read(4)
        return header == _MAGIC_PDF

    if ext == ".jpg":
        async with aiofiles.open(file_path, "rb") as fh:
            header = await fh.read(2)
        return header == _MAGIC_JPEG

    if ext == ".png":
        async with aiofiles.open(file_path, "rb") as fh:
            header = await fh.read(8)
        return header == _MAGIC_PNG

    if ext == ".docx":
        # zipfile is synchronous but touches only a few bytes of a local file —
        # acceptable overhead; no need for run_in_executor here.
        if not zipfile.is_zipfile(file_path):
            return False
        try:
            with zipfile.ZipFile(file_path, "r") as zf:
                return "word/document.xml" in zf.namelist()
        except zipfile.BadZipFile:
            return False

    # EDGE CASE: unknown extension reached — should not happen after save_upload guards
    logger.warning("validate_magic_bytes: unrecognised extension %r for %s", declared_ext, file_path)
    return False


async def get_total_job_size(job_id: str) -> int:
    """
    Return the total on-disk size (bytes) of all regular files under
    ``uploads/<job_id>/``. Returns 0 if the directory does not exist.
    """
    upload_dir = Path(settings.UPLOAD_DIR) / job_id
    if not upload_dir.exists():
        return 0
    return sum(
        p.stat().st_size for p in upload_dir.iterdir() if p.is_file()
    )


async def schedule_deletion(
    job_id: str,
    db: AsyncSession,
    delay_hours: Optional[int] = None,
) -> None:
    """
    Set ``expires_at`` on the PrintJob identified by *job_id* to mark it for
    future cleanup by ``cleanup_expired_jobs()``.

    Does **not** delete anything immediately. Idempotent — safe to call multiple
    times; subsequent calls overwrite the previous expiry timestamp.

    *delay_hours* defaults to ``settings.FILE_EXPIRY_HOURS`` (2 h post-completion).
    """
    hours = delay_hours if delay_hours is not None else settings.FILE_EXPIRY_HOURS

    result = await db.execute(select(PrintJob).where(PrintJob.id == job_id))
    job = result.scalar_one_or_none()
    if job is None:
        logger.warning("schedule_deletion: PrintJob %s not found — skipping", job_id)
        return

    job.expires_at = (
        datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=hours)
    )
    logger.info(
        "Scheduled file deletion for job %s in %dh (expires %s)",
        job_id, hours, job.expires_at.isoformat(),
    )


async def cleanup_expired_jobs(db: AsyncSession) -> int:
    """
    Delete upload directories and PrintJob DB records for all jobs whose
    ``expires_at`` timestamp has passed.

    Returns the number of jobs successfully cleaned up.

    EDGE CASE: If filesystem deletion fails for a job, the DB record is kept
    so the next cleanup cycle will retry. The error is always logged.
    EDGE CASE: Only COMPLETED / FAILED / EXPIRED jobs are eligible — never
    delete files for jobs that are still PAID / PROCESSING / PRINTING.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    # Only expire terminal-state jobs — never touch actively processing jobs
    _safe_to_expire = (
        JobStatus.COMPLETED,
        JobStatus.FAILED,
        JobStatus.EXPIRED,
        JobStatus.UPLOADING,        # orphaned upload sessions
        JobStatus.SETTINGS_PENDING, # abandoned before payment
        JobStatus.PAYMENT_PENDING,  # abandoned at payment step
    )

    result = await db.execute(
        select(PrintJob).where(
            PrintJob.expires_at.is_not(None),
            PrintJob.expires_at <= now,
            PrintJob.status.in_([s.value for s in _safe_to_expire]),
        )
    )
    expired_jobs: list[PrintJob] = list(result.scalars().all())

    deleted_count = 0
    for job in expired_jobs:
        upload_dir = Path(settings.UPLOAD_DIR) / job.id
        try:
            if upload_dir.exists():
                # Run in executor so shutil.rmtree doesn't block the event loop
                await asyncio.get_event_loop().run_in_executor(
                    None, shutil.rmtree, str(upload_dir)
                )
                logger.info("Deleted upload directory for expired job %s", job.id)
            await db.delete(job)
            deleted_count += 1
        except OSError as exc:
            # FALLBACK: keep DB record so next cycle retries the filesystem removal
            logger.error(
                "Failed to delete upload directory for job %s: %s — will retry next cycle",
                job.id, exc,
            )

    if deleted_count:
        logger.info("Cleanup complete: removed %d expired job(s)", deleted_count)

    return deleted_count


# ── Private helpers ────────────────────────────────────────────────────────────

async def _detect_extension_from_magic(file_path: Path) -> Optional[str]:
    """
    Try to identify a file's type by reading its leading bytes when no
    extension was declared by the client.  Returns the canonical extension
    string (e.g. '.pdf') or None if the type cannot be identified.
    """
    async with aiofiles.open(file_path, "rb") as fh:
        header = await fh.read(8)

    if header[:4] == _MAGIC_PDF:
        return ".pdf"
    if header[:2] == _MAGIC_JPEG:
        return ".jpg"
    if header[:8] == _MAGIC_PNG:
        return ".png"
    # DOCX (ZIP) starts with PK\x03\x04
    if header[:4] == b"PK\x03\x04":
        try:
            with zipfile.ZipFile(file_path, "r") as zf:
                if "word/document.xml" in zf.namelist():
                    return ".docx"
        except zipfile.BadZipFile:
            pass
    return None


async def _check_file_integrity(file_path: Path, ext: str) -> None:
    """
    Perform a deeper integrity check beyond magic bytes.

    PDF  — attempt to open with PyMuPDF; raises ValueError on corruption /
           password protection.
    JPG / PNG — attempt to open and verify with Pillow.
    DOCX — already validated structurally by validate_magic_bytes().

    This runs in an executor for the synchronous Pillow / fitz calls so the
    event loop is never blocked by potentially slow PDF parsing.
    """
    ext = _EXT_CANONICAL.get(ext.lower(), ext.lower())

    if ext == ".pdf":
        def _open_pdf() -> None:
            import fitz  # PyMuPDF
            try:
                doc = fitz.open(str(file_path))
            except fitz.FileDataError as exc:
                raise ValueError("File appears damaged and cannot be opened.") from exc
            if doc.is_encrypted:
                raise ValueError(
                    "Password-protected PDFs cannot be accepted. "
                    "Please remove the password before uploading."
                )
            doc.close()

        await asyncio.get_event_loop().run_in_executor(None, _open_pdf)

    elif ext in (".jpg", ".png"):
        def _verify_image() -> None:
            from PIL import Image, UnidentifiedImageError
            try:
                with Image.open(str(file_path)) as img:
                    img.verify()  # Raises on corrupt images
            except (UnidentifiedImageError, Exception) as exc:
                raise ValueError("Image file appears damaged or is not a valid JPEG/PNG.") from exc

        await asyncio.get_event_loop().run_in_executor(None, _verify_image)

    # DOCX integrity is already covered by the zipfile check in validate_magic_bytes
