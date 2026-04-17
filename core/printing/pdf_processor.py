"""
core/printing/pdf_processor.py — PDF manipulation and thumbnail generation.

Handles all PDF pipeline operations: thumbnail rendering, page selection,
rotation, N-up layout, image-to-PDF conversion, and multi-file merging.
Uses PyMuPDF (fitz) as the primary engine and img2pdf for lossless image conversion.
"""

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import List

import fitz  # PyMuPDF
import img2pdf

logger = logging.getLogger(__name__)

# A4 dimensions in PDF points (1 pt = 1/72 inch)
_A4_W: float = 595.0
_A4_H: float = 842.0


# ── Custom Exceptions ──────────────────────────────────────────────────────────

class PasswordProtectedError(Exception):
    """Raised when a PDF requires a password to open."""


class CorruptPDFError(Exception):
    """Raised when a PDF cannot be parsed (file is damaged or not a valid PDF)."""


class NoPagesSelectedError(Exception):
    """Raised when apply_page_settings is called with all pages excluded."""


# ── PDFProcessor ───────────────────────────────────────────────────────────────

class PDFProcessor:
    """Stateless collection of PDF pipeline operations.

    All methods are instance methods for consistency and testability, but
    none of them carry state — a single shared instance is safe across
    async handlers.
    """

    # ── Thumbnails ─────────────────────────────────────────────────────────────

    def generate_thumbnails(
        self,
        pdf_path: str,
        output_dir: str,
        width: int = 150,
    ) -> List[str]:
        """Render each page of a PDF as a PNG thumbnail.

        Args:
            pdf_path:   Absolute or relative path to the source PDF.
            output_dir: Directory where PNG files are written.
            width:      Target pixel width for each thumbnail.

        Returns:
            Sorted list of filenames (relative to output_dir), one per page.

        Raises:
            PasswordProtectedError: PDF requires a password.
            CorruptPDFError:        PDF cannot be parsed.
        """
        try:
            doc = fitz.open(pdf_path)
        except fitz.FileDataError as exc:
            logger.error("Corrupt PDF at %s: %s", pdf_path, exc)
            raise CorruptPDFError(f"Cannot open PDF: {pdf_path}") from exc
        except Exception as exc:
            logger.error("Unexpected error opening PDF %s: %s", pdf_path, exc)
            raise CorruptPDFError(f"Cannot open PDF: {pdf_path}") from exc

        # SECURITY: Refuse to render password-protected PDFs without credentials.
        if doc.needs_pass:
            doc.close()
            raise PasswordProtectedError(f"PDF is password-protected: {pdf_path}")

        os.makedirs(output_dir, exist_ok=True)
        paths: List[str] = []

        try:
            for i, page in enumerate(doc):
                # Scale so that the rendered width matches the requested pixel width.
                # EDGE CASE: page.rect.width could be 0 for malformed pages — guard it.
                page_width = page.rect.width or 1.0
                scale = width / page_width
                mat = fitz.Matrix(scale, scale)
                pix = page.get_pixmap(matrix=mat, alpha=False)
                filename = f"thumb_{i:03d}.png"
                out_path = os.path.join(output_dir, filename)
                pix.save(out_path)
                paths.append(filename)
                logger.debug("Thumbnail saved: %s", out_path)
        finally:
            doc.close()

        return paths

    # ── Page Settings + N-up ───────────────────────────────────────────────────

    def apply_page_settings(
        self,
        input_pdf: str,
        output_pdf: str,
        page_configs: List[dict],
        nup_layout: int,
        copies: int,
    ) -> int:
        """Apply per-page settings (rotation, inclusion) and N-up layout.

        Args:
            input_pdf:    Path to the source PDF.
            output_pdf:   Destination path for the processed PDF.
            page_configs: List of dicts with keys:
                            - page_idx (int): 0-based page index in source.
                            - rotation (int): 0 | 90 | 180 | 270.
                            - include  (bool): whether to include this page.
            nup_layout:   1 = normal, 2 = two pages side-by-side (landscape A4),
                          4 = 2×2 grid (portrait A4).
            copies:       Number of times to repeat the processed page set.

        Returns:
            Total number of physical pages in the output PDF.

        Raises:
            NoPagesSelectedError: All pages have include=False.
            ValueError:           nup_layout is not 1, 2, or 4.
        """
        if nup_layout not in (1, 2, 4):
            raise ValueError(f"nup_layout must be 1, 2, or 4; got {nup_layout}")

        included = [cfg for cfg in page_configs if cfg.get("include", True)]
        if not included:
            raise NoPagesSelectedError("No pages selected — all pages have include=False.")

        # Ensure the output directory exists before writing.
        out_dir = os.path.dirname(output_pdf)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)

        src = fitz.open(input_pdf)
        try:
            # Build an intermediate document containing only the included pages
            # with rotations applied.  Using insert_pdf per-page lets us set
            # rotation individually after insertion.
            intermediate = fitz.open()
            try:
                for cfg in included:
                    page_idx = cfg["page_idx"]
                    rotation = cfg.get("rotation", 0)
                    intermediate.insert_pdf(src, from_page=page_idx, to_page=page_idx)
                    # set_rotation normalises the value to {0,90,180,270}.
                    intermediate[-1].set_rotation(rotation)

                out_doc = self._build_nup(intermediate, nup_layout, copies)
                try:
                    out_doc.save(output_pdf, garbage=4, deflate=True)
                    physical_pages = out_doc.page_count
                    logger.info(
                        "apply_page_settings: %d logical → %d physical pages (nup=%d, copies=%d) → %s",
                        len(included),
                        physical_pages,
                        nup_layout,
                        copies,
                        output_pdf,
                    )
                    return physical_pages
                finally:
                    out_doc.close()
            finally:
                intermediate.close()
        finally:
            src.close()

    def _build_nup(
        self,
        src: fitz.Document,
        nup_layout: int,
        copies: int,
    ) -> fitz.Document:
        """Assemble output document from src, applying N-up and copies.

        Returns a new open fitz.Document — caller is responsible for closing it.
        """
        out = fitz.open()
        total = src.page_count

        if nup_layout == 1:
            for _ in range(copies):
                out.insert_pdf(src)
            return out

        # For 2-up: A4 landscape.  For 4-up: A4 portrait.
        if nup_layout == 2:
            sheet_w, sheet_h = _A4_H, _A4_W  # 842 × 595 landscape
            slots_fn = self._slots_2up
        else:  # 4-up
            sheet_w, sheet_h = _A4_W, _A4_H  # 595 × 842 portrait
            slots_fn = self._slots_4up

        pages_per_sheet = nup_layout

        for _ in range(copies):
            for sheet_start in range(0, total, pages_per_sheet):
                sheet = out.new_page(width=sheet_w, height=sheet_h)
                slots = slots_fn(sheet_w, sheet_h)
                for j, slot in enumerate(slots):
                    logical_idx = sheet_start + j
                    if logical_idx < total:
                        # EDGE CASE: show_pdf_page respects the page's set_rotation
                        # because we already baked it into intermediate.
                        sheet.show_pdf_page(slot, src, logical_idx)

        return out

    @staticmethod
    def _slots_2up(sheet_w: float, sheet_h: float) -> List[fitz.Rect]:
        """Two equal-width slots side by side (landscape)."""
        half = sheet_w / 2
        return [
            fitz.Rect(0, 0, half, sheet_h),
            fitz.Rect(half, 0, sheet_w, sheet_h),
        ]

    @staticmethod
    def _slots_4up(sheet_w: float, sheet_h: float) -> List[fitz.Rect]:
        """Four equal quadrant slots in a 2×2 grid (portrait)."""
        hw, hh = sheet_w / 2, sheet_h / 2
        return [
            fitz.Rect(0, 0, hw, hh),           # top-left
            fitz.Rect(hw, 0, sheet_w, hh),      # top-right
            fitz.Rect(0, hh, hw, sheet_h),      # bottom-left
            fitz.Rect(hw, hh, sheet_w, sheet_h), # bottom-right
        ]

    # ── Page Count ────────────────────────────────────────────────────────────

    def get_page_count(self, pdf_path: str) -> int:
        """Return the number of pages in a PDF.

        Raises:
            ValueError:      File is not a PDF.
            CorruptPDFError: File cannot be opened by PyMuPDF.
        """
        try:
            doc = fitz.open(pdf_path)
        except Exception as exc:
            logger.error("Cannot open file for page count %s: %s", pdf_path, exc)
            raise CorruptPDFError(f"Cannot open file: {pdf_path}") from exc

        try:
            if not doc.is_pdf:
                raise ValueError(f"File is not a PDF: {pdf_path}")
            return doc.page_count
        finally:
            doc.close()

    # ── Image → PDF ───────────────────────────────────────────────────────────

    def convert_image_to_pdf(self, image_path: str, output_path: str) -> str:
        """Convert a JPEG or PNG image to a single-page PDF.

        Uses img2pdf for lossless quality (no re-encoding of JPEG data).
        The ifvalid rotation flag skips broken EXIF rotation metadata common
        in phone camera images rather than raising an error.

        Args:
            image_path:  Path to the source JPEG or PNG.
            output_path: Destination path for the output PDF.

        Returns:
            output_path (unchanged).
        """
        out_dir = os.path.dirname(output_path)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)

        try:
            with open(output_path, "wb") as f:
                # FALLBACK: ifvalid silently skips invalid EXIF rotation instead
                # of raising — necessary for images from phones/cameras.
                f.write(img2pdf.convert(image_path, rotation=img2pdf.Rotation.ifvalid))
        except Exception as exc:
            logger.error("Image-to-PDF conversion failed for %s: %s", image_path, exc)
            raise

        logger.debug("Image converted to PDF: %s → %s", image_path, output_path)
        return output_path

    # ── DOCX → PDF ────────────────────────────────────────────────────────────

    def convert_docx_to_pdf(self, docx_path: str, output_path: str) -> str:
        """Convert a DOCX file to PDF using LibreOffice headless.

        LibreOffice preserves fonts, tables, images, and complex layouts far
        better than any pure-Python alternative.  It must be installed on the
        host system (``sudo apt install libreoffice``).

        Args:
            docx_path:   Path to the source .docx file.
            output_path: Destination path for the output PDF.

        Returns:
            output_path (unchanged).

        Raises:
            EnvironmentError: LibreOffice is not installed / not on PATH.
            RuntimeError:     LibreOffice exited with a non-zero return code.
        """
        if shutil.which("libreoffice") is None:
            raise EnvironmentError(
                "LibreOffice is not installed. "
                "Run: sudo apt install libreoffice"
            )

        out_dir = os.path.dirname(os.path.abspath(output_path))
        os.makedirs(out_dir, exist_ok=True)

        # LibreOffice writes <stem>.pdf into --outdir; we rename to output_path
        # afterwards so callers always get exactly the path they asked for.
        result = subprocess.run(
            [
                "libreoffice", "--headless",
                "--convert-to", "pdf",
                "--outdir", out_dir,
                os.path.abspath(docx_path),
            ],
            capture_output=True,
            timeout=60,
        )

        if result.returncode != 0:
            stderr = result.stderr.decode(errors="replace").strip()
            raise RuntimeError(f"LibreOffice conversion failed: {stderr}")

        # LibreOffice names the output file after the input stem
        lo_output = Path(out_dir) / (Path(docx_path).stem + ".pdf")
        if not lo_output.exists():
            raise RuntimeError(
                f"LibreOffice reported success but output PDF not found at {lo_output}"
            )

        # Rename to the caller-specified path if it differs
        if lo_output != Path(output_path):
            lo_output.rename(output_path)

        logger.debug("DOCX converted to PDF: %s → %s", docx_path, output_path)
        return output_path

    # ── Merge ─────────────────────────────────────────────────────────────────

    def merge_pdfs(self, pdf_paths: List[str], output_path: str) -> str:
        """Merge multiple PDF files into a single PDF in the given order.

        Args:
            pdf_paths:   Ordered list of PDF paths to merge.
            output_path: Destination path for the merged PDF.

        Returns:
            output_path (unchanged).
        """
        out_dir = os.path.dirname(output_path)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)

        out = fitz.open()
        try:
            for path in pdf_paths:
                src = fitz.open(path)
                try:
                    out.insert_pdf(src)
                finally:
                    src.close()

            out.save(output_path, garbage=4, deflate=True)
            logger.info("Merged %d PDFs → %s (%d pages)", len(pdf_paths), output_path, out.page_count)
        finally:
            out.close()

        return output_path


# Module-level singleton — import `pdf_processor` everywhere instead of
# instantiating PDFProcessor() in multiple places.
pdf_processor = PDFProcessor()
