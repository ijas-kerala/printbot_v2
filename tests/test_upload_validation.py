"""
tests/test_upload_validation.py — Magic byte validation and upload size limit tests.

Covers web/services/file_service.py:
  - validate_magic_bytes(): correct/incorrect headers for PDF, JPEG, PNG, DOCX
  - save_upload(): per-file size limit, disallowed extension, corrupt PDF rejection

Tests operate on real temporary files and mock UploadFile objects — no DB
session is required for the magic byte tests (they are pure filesystem ops).

The save_upload() tests that trigger _check_file_integrity() mock PyMuPDF/Pillow
so the test suite works in CI without a valid PDF renderer installed.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from fastapi import UploadFile

from web.services.file_service import validate_magic_bytes

# ── Magic byte constants (copied from file_service for test clarity) ───────────
_MAGIC_PDF  = b"%PDF-1.4 fake header content to fill out bytes"
_MAGIC_PNG  = b"\x89PNG\r\n\x1a\nfake png body"
_MAGIC_JPEG = b"\xff\xd8\xff\xe0fake jpeg body"
_MAGIC_DOCX_GARBAGE = b"PK\x03\x04this is not a real docx"


# ── Helpers ────────────────────────────────────────────────────────────────────

def _write(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path


def _make_valid_docx(path: Path) -> Path:
    """Create a minimal ZIP file that contains word/document.xml."""
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("word/document.xml", "<w:document/>")
        zf.writestr("[Content_Types].xml", "<Types/>")
    return path


def _make_zip_without_word(path: Path) -> Path:
    """Create a ZIP file that does NOT contain word/document.xml."""
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("some_other_file.xml", "<data/>")
    return path


# ── PDF magic byte tests ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_valid_pdf_magic_bytes(tmp_path: Path) -> None:
    path = _write(tmp_path / "doc.pdf", _MAGIC_PDF)
    assert await validate_magic_bytes(path, ".pdf") is True


@pytest.mark.asyncio
async def test_invalid_pdf_magic_bytes(tmp_path: Path) -> None:
    path = _write(tmp_path / "doc.pdf", b"\x00\x00garbage")
    assert await validate_magic_bytes(path, ".pdf") is False


@pytest.mark.asyncio
async def test_pdf_with_jpeg_header_fails(tmp_path: Path) -> None:
    """A JPEG disguised as PDF must fail the PDF magic check."""
    path = _write(tmp_path / "doc.pdf", _MAGIC_JPEG)
    assert await validate_magic_bytes(path, ".pdf") is False


# ── JPEG magic byte tests ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_valid_jpeg_magic_bytes(tmp_path: Path) -> None:
    path = _write(tmp_path / "photo.jpg", _MAGIC_JPEG)
    assert await validate_magic_bytes(path, ".jpg") is True


@pytest.mark.asyncio
async def test_valid_jpeg_with_jpeg_extension(tmp_path: Path) -> None:
    """.jpeg extension must be normalised to .jpg and accepted."""
    path = _write(tmp_path / "photo.jpeg", _MAGIC_JPEG)
    assert await validate_magic_bytes(path, ".jpeg") is True


@pytest.mark.asyncio
async def test_invalid_jpeg_magic_bytes(tmp_path: Path) -> None:
    path = _write(tmp_path / "photo.jpg", b"\x00\x00not a jpeg")
    assert await validate_magic_bytes(path, ".jpg") is False


# ── PNG magic byte tests ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_valid_png_magic_bytes(tmp_path: Path) -> None:
    path = _write(tmp_path / "image.png", _MAGIC_PNG)
    assert await validate_magic_bytes(path, ".png") is True


@pytest.mark.asyncio
async def test_invalid_png_magic_bytes_wrong_header(tmp_path: Path) -> None:
    path = _write(tmp_path / "image.png", b"\x89PNG wrong")  # first 4 match but not all 8
    assert await validate_magic_bytes(path, ".png") is False


@pytest.mark.asyncio
async def test_invalid_png_magic_bytes_garbage(tmp_path: Path) -> None:
    path = _write(tmp_path / "image.png", b"\xff\xd8garbage")
    assert await validate_magic_bytes(path, ".png") is False


# ── DOCX magic byte tests ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_valid_docx_magic_bytes(tmp_path: Path) -> None:
    path = _make_valid_docx(tmp_path / "doc.docx")
    assert await validate_magic_bytes(path, ".docx") is True


@pytest.mark.asyncio
async def test_invalid_docx_not_a_zip(tmp_path: Path) -> None:
    path = _write(tmp_path / "doc.docx", b"This is plain text, not a ZIP")
    assert await validate_magic_bytes(path, ".docx") is False


@pytest.mark.asyncio
async def test_invalid_docx_zip_without_word_document(tmp_path: Path) -> None:
    """A ZIP file that doesn't contain word/document.xml is not a valid DOCX."""
    path = _make_zip_without_word(tmp_path / "doc.docx")
    assert await validate_magic_bytes(path, ".docx") is False


@pytest.mark.asyncio
async def test_invalid_docx_corrupt_zip(tmp_path: Path) -> None:
    """Partial / corrupt ZIP must return False, not raise."""
    path = _write(tmp_path / "doc.docx", b"PK\x03\x04truncated")
    assert await validate_magic_bytes(path, ".docx") is False


# ── Unknown extension edge case ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_unknown_extension_returns_false(tmp_path: Path) -> None:
    """validate_magic_bytes must return False for an unrecognised extension."""
    path = _write(tmp_path / "file.xyz", b"random bytes")
    assert await validate_magic_bytes(path, ".xyz") is False


# ── save_upload() — extension and size tests ──────────────────────────────────

def _mock_upload(filename: str, content: bytes) -> UploadFile:
    """Build a minimal UploadFile mock that streams the given bytes."""
    mock = MagicMock(spec=UploadFile)
    mock.filename = filename
    # Simulate async chunked read: first call returns all content, second returns b""
    mock.read = AsyncMock(side_effect=[content, b""])
    return mock


@pytest.mark.asyncio
async def test_disallowed_extension_raises_value_error(tmp_path: Path) -> None:
    """Files with disallowed extensions must raise ValueError before any bytes are written."""
    from web.services.file_service import save_upload

    upload = _mock_upload("malware.exe", b"\x4d\x5a PE header")

    with patch("web.services.file_service.settings") as mock_settings:
        mock_settings.UPLOAD_DIR = str(tmp_path)
        mock_settings.MAX_UPLOAD_BYTES = 90 * 1024 * 1024

        with pytest.raises(ValueError, match="not accepted"):
            await save_upload(job_id="test-job-001", file=upload, sort_order=0)


@pytest.mark.asyncio
async def test_single_file_exceeds_size_limit_raises_value_error(tmp_path: Path) -> None:
    """A file that exceeds MAX_UPLOAD_BYTES must raise ValueError mid-stream."""
    from web.services.file_service import save_upload

    # Create a 1-byte-over-limit payload with a valid PDF header
    limit = 1024  # use a tiny limit so the test is fast
    oversized_content = b"%PDF" + b"x" * (limit + 1)

    upload = _mock_upload("big.pdf", oversized_content)
    # Simulate streaming: return content in one chunk, then EOF
    upload.read = AsyncMock(side_effect=[oversized_content, b""])

    with patch("web.services.file_service.settings") as mock_settings:
        mock_settings.UPLOAD_DIR = str(tmp_path)
        mock_settings.MAX_UPLOAD_BYTES = limit  # intentionally tiny

        with pytest.raises(ValueError, match="MB limit"):
            await save_upload(job_id="test-job-002", file=upload, sort_order=0)


# ── save_upload() — corrupt PDF rejection ─────────────────────────────────────

@pytest.mark.asyncio
async def test_corrupt_pdf_is_rejected(tmp_path: Path) -> None:
    """
    A file with a valid %PDF magic header but a corrupt body must be rejected
    during _check_file_integrity() via fitz.open().

    fitz is mocked so the test runs without a real PDF renderer.
    """
    from web.services.file_service import save_upload

    # Valid magic bytes, but garbage body
    corrupt_pdf_content = b"%PDF-1.4\n" + b"\x00" * 512

    upload = _mock_upload("corrupt.pdf", corrupt_pdf_content)
    upload.read = AsyncMock(side_effect=[corrupt_pdf_content, b""])

    with patch("web.services.file_service.settings") as mock_settings:
        mock_settings.UPLOAD_DIR = str(tmp_path)
        mock_settings.MAX_UPLOAD_BYTES = 90 * 1024 * 1024

        # Mock fitz.open inside _check_file_integrity to simulate a corrupt PDF
        import fitz

        with patch.object(fitz, "open", side_effect=fitz.FileDataError("corrupt")):
            with pytest.raises(ValueError, match="damaged|integrity"):
                await save_upload(job_id="test-job-003", file=upload, sort_order=0)


@pytest.mark.asyncio
async def test_password_protected_pdf_is_rejected(tmp_path: Path) -> None:
    """
    A password-protected PDF must be rejected with a clear error message.

    fitz is mocked so the test runs without a real encrypted PDF.
    """
    from web.services.file_service import save_upload

    encrypted_content = b"%PDF-1.4\n" + b"\x00" * 256

    upload = _mock_upload("locked.pdf", encrypted_content)
    upload.read = AsyncMock(side_effect=[encrypted_content, b""])

    with patch("web.services.file_service.settings") as mock_settings:
        mock_settings.UPLOAD_DIR = str(tmp_path)
        mock_settings.MAX_UPLOAD_BYTES = 90 * 1024 * 1024

        # Simulate an encrypted PDF: fitz.open succeeds but doc.is_encrypted is True
        mock_doc = MagicMock()
        mock_doc.is_encrypted = True
        mock_doc.close = MagicMock()

        import fitz

        with patch.object(fitz, "open", return_value=mock_doc):
            with pytest.raises(ValueError, match="[Pp]assword"):
                await save_upload(job_id="test-job-004", file=upload, sort_order=0)


# ── Total size enforcement (tested at router level via get_total_job_size) ─────

@pytest.mark.asyncio
async def test_get_total_job_size_empty_dir(tmp_path: Path) -> None:
    """get_total_job_size must return 0 for a directory with no files."""
    from web.services.file_service import get_total_job_size

    with patch("web.services.file_service.settings") as mock_settings:
        mock_settings.UPLOAD_DIR = str(tmp_path)
        (tmp_path / "test-empty-job").mkdir()
        total = await get_total_job_size("test-empty-job")

    assert total == 0


@pytest.mark.asyncio
async def test_get_total_job_size_sums_files(tmp_path: Path) -> None:
    """get_total_job_size must correctly sum the sizes of files in a job dir."""
    from web.services.file_service import get_total_job_size

    job_dir = tmp_path / "test-size-job"
    job_dir.mkdir()
    (job_dir / "a.pdf").write_bytes(b"x" * 100)
    (job_dir / "b.pdf").write_bytes(b"x" * 200)

    with patch("web.services.file_service.settings") as mock_settings:
        mock_settings.UPLOAD_DIR = str(tmp_path)
        total = await get_total_job_size("test-size-job")

    assert total == 300
