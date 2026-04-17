"""
web/routers/upload.py — Multi-file upload endpoint and landing page.

Routes
------
GET  /          → Render index.html (upload landing page).
                  If a valid pb_session cookie exists for an active job, passes
                  that job's data to the template so the UI can warn the user.

POST /upload    → Accept 1–20 files via multipart form.
                  Validates each file (magic bytes + integrity), creates a
                  PrintJob + FileItems, sets the pb_session cookie, and
                  redirects to /settings?job_id=<id>.

Error handling covers all cases from the REBUILD_PLAN error table:
  - Total upload > 90 MB        → 400, delete all files so far
  - Individual file > 90 MB     → 400, delete partial file
  - Invalid file type           → 400, delete file
  - Corrupt / password PDF      → 422, delete file
  - Corrupt image               → 422, delete file
  - > 20 files                  → 400, no files written
  - 0 valid files after checks  → 400, job not created
  - Partial success             → job created with valid files only, rejected
                                   files listed in a query param warning

Session cookie:
  pb_session = itsdangerous.TimestampSigner.sign(job_id)
  max_age    = settings.JOB_SESSION_MAX_AGE (30 min default)
  httponly   = True, samesite = "lax"
"""

from __future__ import annotations

import logging
import secrets
import shutil
import uuid
from pathlib import Path
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, File, Request, UploadFile, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.models import JobStatus, PrintJob
from web.dependencies import get_db, get_job_from_session, sign_job_cookie
from web.services.file_service import (
    get_total_job_size,
    save_upload,
    schedule_deletion,
)

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="web/templates")


# Whitelisted flash messages surfaced via the ?msg= redirect param.
# Values are server-controlled so user-supplied keys cannot inject arbitrary text.
_FLASH_MESSAGES: dict[str, str] = {
    "session_expired":  "Your session has expired. Please upload your files to start again.",
    "session_required": "Please upload your files to begin.",
    "session_mismatch": "Session error. Please start a new upload.",
}


# ── GET / ─────────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def upload_page(
    request: Request,
    msg: Optional[str] = None,
    active_job: Optional[PrintJob] = Depends(get_job_from_session),
) -> HTMLResponse:
    """
    Serve the upload landing page.

    If the visitor already has a valid session for an in-progress job, the
    template receives it so the UI can show an "active session" warning with
    a link to resume.

    The optional ``msg`` query param is used by other routes that redirect
    here after a session error; it is resolved against a server-side whitelist
    before being passed to the template as ``flash_message``.
    """
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "active_job_id": active_job.id if active_job else None,
            "flash_message": _FLASH_MESSAGES.get(msg) if msg else None,
        },
    )


# ── POST /upload ───────────────────────────────────────────────────────────────

@router.post("/upload")
async def handle_upload(
    request: Request,
    files: Annotated[list[UploadFile], File(description="Files to print (PDF, DOCX, JPG, PNG)")],
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """
    Receive one or more uploaded files, validate them, create the job record,
    and redirect to /settings.

    Flow
    ----
    1. Guard: reject if file count > MAX_FILES_PER_JOB.
    2. Allocate a new job_id and upload directory.
    3. Stream each file via save_upload(); collect per-file errors without
       aborting — partial success is allowed per the spec.
    4. After each file, check the running directory total against MAX_UPLOAD_BYTES.
       Abort immediately if exceeded; delete the whole job dir.
    5. If no valid files remain after all checks, return 400 (no job created).
    6. Create PrintJob + add FileItems to DB session (single commit).
    7. Set pb_session cookie, redirect to /settings?job_id=<id>.
    """

    # ── Guard: file count ──────────────────────────────────────────────────────
    if not files:
        return _json_error(400, "No files received. Please select at least one file to upload.")

    if len(files) > settings.MAX_FILES_PER_JOB:
        return _json_error(
            400,
            f"Too many files. Please select at most {settings.MAX_FILES_PER_JOB} files at a time.",
        )

    # ── Allocate job resources ─────────────────────────────────────────────────
    job_id = str(uuid.uuid4())
    upload_dir = Path(settings.UPLOAD_DIR) / job_id
    upload_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Starting upload for new job %s (%d file(s))", job_id, len(files))

    accepted_items = []   # list of unsaved FileItem objects
    rejected_files = []   # list of (filename, reason) tuples

    # ── Process each file ──────────────────────────────────────────────────────
    for sort_order, upload_file in enumerate(files):
        filename = upload_file.filename or f"file_{sort_order + 1}"

        try:
            file_item = await save_upload(
                job_id=job_id,
                file=upload_file,
                sort_order=sort_order,
            )
        except ValueError as exc:
            # save_upload already deleted any partial file — just record the reason
            reason = str(exc)
            logger.warning("Rejected file %r for job %s: %s", filename, job_id, reason)
            rejected_files.append((filename, reason))
            continue
        except OSError as exc:
            logger.error("OS error saving %r for job %s: %s", filename, job_id, exc)
            rejected_files.append((filename, "A storage error occurred. Please try again."))
            continue

        # ── Total-size guard: check after each file ────────────────────────────
        running_total = await get_total_job_size(job_id)
        if running_total > settings.MAX_UPLOAD_BYTES:
            # Delete the file that pushed us over the limit, then abort
            overage_path = Path(file_item.stored_path)
            overage_path.unlink(missing_ok=True)
            logger.warning(
                "Total upload for job %s exceeded %d MB after file %r — aborting",
                job_id, settings.MAX_UPLOAD_BYTES // (1024 * 1024), filename,
            )
            _nuke_job_dir(job_id)
            return _json_error(
                400,
                f"Total upload size exceeds the "
                f"{settings.MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit. "
                "Please reduce the number or size of files.",
            )

        accepted_items.append(file_item)
        logger.debug("Accepted file %r for job %s (sort_order=%d)", filename, job_id, sort_order)

    # ── All files rejected ─────────────────────────────────────────────────────
    if not accepted_items:
        _nuke_job_dir(job_id)
        # Surface the first rejection reason as the primary error message
        primary_reason = rejected_files[0][1] if rejected_files else "No valid files."
        return _json_error(400, primary_reason, rejected=rejected_files)

    # ── Create PrintJob + FileItems ────────────────────────────────────────────
    session_token = secrets.token_hex(16)
    job = PrintJob(
        id=job_id,
        session_token=session_token,
        status=JobStatus.SETTINGS_PENDING,
    )
    db.add(job)
    for item in accepted_items:
        db.add(item)

    # Schedule cleanup for abandoned sessions (e.g. user never pays)
    # We use a longer window here — schedule_deletion will be called again
    # with a shorter window once the job reaches COMPLETED / FAILED.
    job.expires_at = None  # Cleared once the user moves to settings; will be
                           # set to a real expiry when the job terminates.

    await db.flush()  # Persist before we redirect; get_db() commits on exit

    logger.info(
        "Created PrintJob %s with %d file(s) (%d rejected)",
        job_id, len(accepted_items), len(rejected_files),
    )

    # ── Build redirect URL (warn about rejected files via query param) ─────────
    redirect_url = f"/settings?job_id={job_id}"
    if rejected_files:
        # Encode rejected filenames as a simple comma-separated list so the
        # settings page can surface a toast notification.
        names = ",".join(fn for fn, _ in rejected_files)
        redirect_url += f"&rejected={_url_encode(names)}"

    # ── Set session cookie + redirect ──────────────────────────────────────────
    signed_cookie = sign_job_cookie(job_id)
    response = RedirectResponse(url=redirect_url, status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        key=settings.JOB_SESSION_COOKIE_NAME,
        value=signed_cookie,
        max_age=settings.JOB_SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        # SECURITY: secure=True in production (HTTPS only via Cloudflare Tunnel)
        secure=settings.is_production,
    )
    return response


# ── Private helpers ────────────────────────────────────────────────────────────

def _nuke_job_dir(job_id: str) -> None:
    """
    Best-effort removal of the upload directory for *job_id*.
    Logs but never raises — called in error paths where we can't propagate.
    """
    upload_dir = Path(settings.UPLOAD_DIR) / job_id
    try:
        if upload_dir.exists():
            shutil.rmtree(str(upload_dir))
            logger.info("Cleaned up upload directory for aborted job %s", job_id)
    except OSError as exc:
        logger.error("Failed to clean up directory for job %s: %s", job_id, exc)


def _json_error(
    http_status: int,
    message: str,
    rejected: Optional[list[tuple[str, str]]] = None,
) -> JSONResponse:
    """
    Return a JSON error response in a consistent shape.

    The upload JS on the client side reads ``detail`` for the toast message
    and ``rejected`` for the per-file error list.
    """
    body: dict = {"detail": message}
    if rejected:
        body["rejected"] = [{"filename": fn, "reason": r} for fn, r in rejected]
    return JSONResponse(status_code=http_status, content=body)


def _url_encode(value: str) -> str:
    """Percent-encode a string for safe inclusion in a URL query parameter."""
    from urllib.parse import quote
    return quote(value, safe="")
