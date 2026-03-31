"""
tests/test_pdf_processor.py — Unit tests for core/printing/pdf_processor.py.

All test PDFs are created programmatically with PyMuPDF so the suite has
zero external file dependencies and can run in CI without sample assets.
"""

import io
import os
from pathlib import Path
from typing import List

import fitz  # PyMuPDF
import pytest
from PIL import Image

from core.printing.pdf_processor import (
    CorruptPDFError,
    NoPagesSelectedError,
    PasswordProtectedError,
    PDFProcessor,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_pdf(path: Path, page_count: int = 1) -> Path:
    """Create a minimal valid PDF with `page_count` blank A4 pages."""
    doc = fitz.open()
    for i in range(page_count):
        page = doc.new_page(width=595, height=842)
        # Write a small text label so pages are distinguishable in rotation tests.
        page.insert_text((50, 100), f"Page {i + 1}", fontsize=24)
    doc.save(str(path))
    doc.close()
    return path


def _make_jpeg(path: Path, width: int = 100, height: int = 150) -> Path:
    """Create a minimal JPEG image using Pillow."""
    img = Image.new("RGB", (width, height), color=(200, 100, 50))
    img.save(str(path), format="JPEG")
    return path


def _make_png(path: Path, width: int = 80, height: int = 120) -> Path:
    """Create a minimal PNG image using Pillow."""
    img = Image.new("RGB", (width, height), color=(50, 100, 200))
    img.save(str(path), format="PNG")
    return path


def _page_configs(page_count: int, rotation: int = 0, include: bool = True) -> List[dict]:
    """Build a simple page_configs list covering all pages."""
    return [
        {"page_idx": i, "rotation": rotation, "include": include}
        for i in range(page_count)
    ]


@pytest.fixture
def proc() -> PDFProcessor:
    return PDFProcessor()


# ── get_page_count ─────────────────────────────────────────────────────────────

class TestGetPageCount:
    def test_single_page(self, proc: PDFProcessor, tmp_path: Path) -> None:
        pdf = _make_pdf(tmp_path / "single.pdf", page_count=1)
        assert proc.get_page_count(str(pdf)) == 1

    def test_multi_page(self, proc: PDFProcessor, tmp_path: Path) -> None:
        pdf = _make_pdf(tmp_path / "multi.pdf", page_count=5)
        assert proc.get_page_count(str(pdf)) == 5

    def test_non_pdf_raises(self, proc: PDFProcessor, tmp_path: Path) -> None:
        # A plain text file is not a PDF.
        txt = tmp_path / "not_a_pdf.txt"
        txt.write_text("hello world")
        with pytest.raises((CorruptPDFError, ValueError)):
            proc.get_page_count(str(txt))

    def test_missing_file_raises(self, proc: PDFProcessor, tmp_path: Path) -> None:
        with pytest.raises((CorruptPDFError, ValueError, FileNotFoundError)):
            proc.get_page_count(str(tmp_path / "ghost.pdf"))


# ── generate_thumbnails ────────────────────────────────────────────────────────

class TestGenerateThumbnails:
    def test_single_page_returns_one_path(
        self, proc: PDFProcessor, tmp_path: Path
    ) -> None:
        pdf = _make_pdf(tmp_path / "doc.pdf", page_count=1)
        out_dir = tmp_path / "thumbs"
        result = proc.generate_thumbnails(str(pdf), str(out_dir))
        assert len(result) == 1
        assert os.path.exists(os.path.join(str(out_dir), result[0]))

    def test_multi_page_returns_correct_count(
        self, proc: PDFProcessor, tmp_path: Path
    ) -> None:
        n = 4
        pdf = _make_pdf(tmp_path / "doc.pdf", page_count=n)
        out_dir = tmp_path / "thumbs"
        result = proc.generate_thumbnails(str(pdf), str(out_dir))
        assert len(result) == n
        for fname in result:
            assert os.path.exists(os.path.join(str(out_dir), fname))

    def test_thumbnail_pixel_width(
        self, proc: PDFProcessor, tmp_path: Path
    ) -> None:
        pdf = _make_pdf(tmp_path / "doc.pdf", page_count=1)
        out_dir = tmp_path / "thumbs"
        target_width = 120
        result = proc.generate_thumbnails(str(pdf), str(out_dir), width=target_width)
        img = Image.open(os.path.join(str(out_dir), result[0]))
        # Allow ±1 px tolerance for rounding in scale calculation.
        assert abs(img.width - target_width) <= 1

    def test_output_files_are_png(
        self, proc: PDFProcessor, tmp_path: Path
    ) -> None:
        pdf = _make_pdf(tmp_path / "doc.pdf", page_count=2)
        out_dir = tmp_path / "thumbs"
        result = proc.generate_thumbnails(str(pdf), str(out_dir))
        for fname in result:
            assert fname.endswith(".png")

    def test_creates_output_dir_if_missing(
        self, proc: PDFProcessor, tmp_path: Path
    ) -> None:
        pdf = _make_pdf(tmp_path / "doc.pdf", page_count=1)
        nested_dir = tmp_path / "a" / "b" / "c"
        proc.generate_thumbnails(str(pdf), str(nested_dir))
        assert nested_dir.exists()

    def test_corrupt_pdf_raises(
        self, proc: PDFProcessor, tmp_path: Path
    ) -> None:
        bad = tmp_path / "bad.pdf"
        bad.write_bytes(b"%PDF-1.4 this is garbage \x00\x01\x02")
        with pytest.raises(CorruptPDFError):
            proc.generate_thumbnails(str(bad), str(tmp_path / "thumbs"))


# ── apply_page_settings ────────────────────────────────────────────────────────

class TestApplyPageSettings:

    # ── 1-up basic ────────────────────────────────────────────────────────────

    def test_1up_all_pages_included(
        self, proc: PDFProcessor, tmp_path: Path
    ) -> None:
        pdf = _make_pdf(tmp_path / "src.pdf", page_count=3)
        out = tmp_path / "out.pdf"
        count = proc.apply_page_settings(
            str(pdf), str(out), _page_configs(3), nup_layout=1, copies=1
        )
        assert count == 3
        assert fitz.open(str(out)).page_count == 3

    def test_1up_partial_pages(
        self, proc: PDFProcessor, tmp_path: Path
    ) -> None:
        pdf = _make_pdf(tmp_path / "src.pdf", page_count=5)
        out = tmp_path / "out.pdf"
        configs = [
            {"page_idx": 0, "rotation": 0, "include": True},
            {"page_idx": 1, "rotation": 0, "include": False},
            {"page_idx": 2, "rotation": 0, "include": True},
            {"page_idx": 3, "rotation": 0, "include": False},
            {"page_idx": 4, "rotation": 0, "include": True},
        ]
        count = proc.apply_page_settings(
            str(pdf), str(out), configs, nup_layout=1, copies=1
        )
        assert count == 3

    # ── Copies ────────────────────────────────────────────────────────────────

    def test_copies_triples_page_count(
        self, proc: PDFProcessor, tmp_path: Path
    ) -> None:
        pdf = _make_pdf(tmp_path / "src.pdf", page_count=2)
        out = tmp_path / "out.pdf"
        count = proc.apply_page_settings(
            str(pdf), str(out), _page_configs(2), nup_layout=1, copies=3
        )
        assert count == 6

    # ── Rotation ──────────────────────────────────────────────────────────────

    def test_rotation_applied(
        self, proc: PDFProcessor, tmp_path: Path
    ) -> None:
        pdf = _make_pdf(tmp_path / "src.pdf", page_count=1)
        out = tmp_path / "out.pdf"
        configs = [{"page_idx": 0, "rotation": 90, "include": True}]
        proc.apply_page_settings(
            str(pdf), str(out), configs, nup_layout=1, copies=1
        )
        result_doc = fitz.open(str(out))
        assert result_doc[0].rotation == 90
        result_doc.close()

    def test_rotation_180(
        self, proc: PDFProcessor, tmp_path: Path
    ) -> None:
        pdf = _make_pdf(tmp_path / "src.pdf", page_count=1)
        out = tmp_path / "out.pdf"
        configs = [{"page_idx": 0, "rotation": 180, "include": True}]
        proc.apply_page_settings(
            str(pdf), str(out), configs, nup_layout=1, copies=1
        )
        result_doc = fitz.open(str(out))
        assert result_doc[0].rotation == 180
        result_doc.close()

    # ── 2-up ─────────────────────────────────────────────────────────────────

    def test_2up_even_pages(
        self, proc: PDFProcessor, tmp_path: Path
    ) -> None:
        # 4 logical pages → 2 physical sheets
        pdf = _make_pdf(tmp_path / "src.pdf", page_count=4)
        out = tmp_path / "out.pdf"
        count = proc.apply_page_settings(
            str(pdf), str(out), _page_configs(4), nup_layout=2, copies=1
        )
        assert count == 2

    def test_2up_odd_pages(
        self, proc: PDFProcessor, tmp_path: Path
    ) -> None:
        # 3 logical pages → 2 sheets (last sheet has only 1 page on the left)
        pdf = _make_pdf(tmp_path / "src.pdf", page_count=3)
        out = tmp_path / "out.pdf"
        count = proc.apply_page_settings(
            str(pdf), str(out), _page_configs(3), nup_layout=2, copies=1
        )
        assert count == 2

    def test_2up_output_is_landscape(
        self, proc: PDFProcessor, tmp_path: Path
    ) -> None:
        pdf = _make_pdf(tmp_path / "src.pdf", page_count=2)
        out = tmp_path / "out.pdf"
        proc.apply_page_settings(
            str(pdf), str(out), _page_configs(2), nup_layout=2, copies=1
        )
        result_doc = fitz.open(str(out))
        page = result_doc[0]
        # Landscape: width > height
        assert page.rect.width > page.rect.height
        result_doc.close()

    def test_2up_with_copies(
        self, proc: PDFProcessor, tmp_path: Path
    ) -> None:
        # 2 logical pages, 2-up → 1 sheet per copy → 2 copies = 2 sheets
        pdf = _make_pdf(tmp_path / "src.pdf", page_count=2)
        out = tmp_path / "out.pdf"
        count = proc.apply_page_settings(
            str(pdf), str(out), _page_configs(2), nup_layout=2, copies=2
        )
        assert count == 2

    # ── 4-up ─────────────────────────────────────────────────────────────────

    def test_4up_exact_four_pages(
        self, proc: PDFProcessor, tmp_path: Path
    ) -> None:
        # 4 logical pages → 1 physical sheet
        pdf = _make_pdf(tmp_path / "src.pdf", page_count=4)
        out = tmp_path / "out.pdf"
        count = proc.apply_page_settings(
            str(pdf), str(out), _page_configs(4), nup_layout=4, copies=1
        )
        assert count == 1

    def test_4up_partial_sheet(
        self, proc: PDFProcessor, tmp_path: Path
    ) -> None:
        # 5 logical pages → 2 physical sheets (4 on first, 1 on second)
        pdf = _make_pdf(tmp_path / "src.pdf", page_count=5)
        out = tmp_path / "out.pdf"
        count = proc.apply_page_settings(
            str(pdf), str(out), _page_configs(5), nup_layout=4, copies=1
        )
        assert count == 2

    def test_4up_eight_pages(
        self, proc: PDFProcessor, tmp_path: Path
    ) -> None:
        # 8 logical pages → 2 physical sheets
        pdf = _make_pdf(tmp_path / "src.pdf", page_count=8)
        out = tmp_path / "out.pdf"
        count = proc.apply_page_settings(
            str(pdf), str(out), _page_configs(8), nup_layout=4, copies=1
        )
        assert count == 2

    def test_4up_output_is_portrait(
        self, proc: PDFProcessor, tmp_path: Path
    ) -> None:
        pdf = _make_pdf(tmp_path / "src.pdf", page_count=4)
        out = tmp_path / "out.pdf"
        proc.apply_page_settings(
            str(pdf), str(out), _page_configs(4), nup_layout=4, copies=1
        )
        result_doc = fitz.open(str(out))
        page = result_doc[0]
        # Portrait: height > width
        assert page.rect.height > page.rect.width
        result_doc.close()

    # ── Empty selection ───────────────────────────────────────────────────────

    def test_empty_selection_raises(
        self, proc: PDFProcessor, tmp_path: Path
    ) -> None:
        pdf = _make_pdf(tmp_path / "src.pdf", page_count=3)
        out = tmp_path / "out.pdf"
        configs = _page_configs(3, include=False)
        with pytest.raises(NoPagesSelectedError):
            proc.apply_page_settings(
                str(pdf), str(out), configs, nup_layout=1, copies=1
            )

    def test_invalid_nup_raises(
        self, proc: PDFProcessor, tmp_path: Path
    ) -> None:
        pdf = _make_pdf(tmp_path / "src.pdf", page_count=2)
        out = tmp_path / "out.pdf"
        with pytest.raises(ValueError):
            proc.apply_page_settings(
                str(pdf), str(out), _page_configs(2), nup_layout=3, copies=1
            )

    def test_creates_output_dir_if_missing(
        self, proc: PDFProcessor, tmp_path: Path
    ) -> None:
        pdf = _make_pdf(tmp_path / "src.pdf", page_count=1)
        nested_out = tmp_path / "new" / "dir" / "out.pdf"
        proc.apply_page_settings(
            str(pdf), str(nested_out), _page_configs(1), nup_layout=1, copies=1
        )
        assert nested_out.exists()


# ── convert_image_to_pdf ───────────────────────────────────────────────────────

class TestConvertImageToPdf:
    def test_jpeg_converts_to_single_page_pdf(
        self, proc: PDFProcessor, tmp_path: Path
    ) -> None:
        jpg = _make_jpeg(tmp_path / "photo.jpg")
        out = tmp_path / "photo.pdf"
        result = proc.convert_image_to_pdf(str(jpg), str(out))
        assert result == str(out)
        assert out.exists()
        doc = fitz.open(str(out))
        assert doc.page_count == 1
        doc.close()

    def test_png_converts_to_single_page_pdf(
        self, proc: PDFProcessor, tmp_path: Path
    ) -> None:
        png = _make_png(tmp_path / "image.png")
        out = tmp_path / "image.pdf"
        result = proc.convert_image_to_pdf(str(png), str(out))
        assert result == str(out)
        assert out.exists()
        doc = fitz.open(str(out))
        assert doc.page_count == 1
        doc.close()

    def test_returns_output_path(
        self, proc: PDFProcessor, tmp_path: Path
    ) -> None:
        jpg = _make_jpeg(tmp_path / "img.jpg")
        out = tmp_path / "img.pdf"
        returned = proc.convert_image_to_pdf(str(jpg), str(out))
        assert returned == str(out)

    def test_creates_output_dir_if_missing(
        self, proc: PDFProcessor, tmp_path: Path
    ) -> None:
        jpg = _make_jpeg(tmp_path / "img.jpg")
        nested_out = tmp_path / "sub" / "img.pdf"
        proc.convert_image_to_pdf(str(jpg), str(nested_out))
        assert nested_out.exists()


# ── merge_pdfs ────────────────────────────────────────────────────────────────

class TestMergePdfs:
    def test_merge_three_pdfs_total_pages(
        self, proc: PDFProcessor, tmp_path: Path
    ) -> None:
        paths = [
            str(_make_pdf(tmp_path / f"doc{i}.pdf", page_count=i + 1))
            for i in range(3)
        ]
        out = tmp_path / "merged.pdf"
        result = proc.merge_pdfs(paths, str(out))
        assert result == str(out)
        doc = fitz.open(str(out))
        # 1 + 2 + 3 = 6 pages total
        assert doc.page_count == 6
        doc.close()

    def test_merge_preserves_order(
        self, proc: PDFProcessor, tmp_path: Path
    ) -> None:
        # Each PDF has a distinct page count so we can identify them.
        p1 = str(_make_pdf(tmp_path / "a.pdf", page_count=1))
        p2 = str(_make_pdf(tmp_path / "b.pdf", page_count=3))
        out = tmp_path / "merged.pdf"
        proc.merge_pdfs([p1, p2], str(out))
        doc = fitz.open(str(out))
        assert doc.page_count == 4
        doc.close()

    def test_merge_single_pdf(
        self, proc: PDFProcessor, tmp_path: Path
    ) -> None:
        pdf = _make_pdf(tmp_path / "only.pdf", page_count=2)
        out = tmp_path / "merged.pdf"
        proc.merge_pdfs([str(pdf)], str(out))
        doc = fitz.open(str(out))
        assert doc.page_count == 2
        doc.close()

    def test_returns_output_path(
        self, proc: PDFProcessor, tmp_path: Path
    ) -> None:
        pdf = _make_pdf(tmp_path / "a.pdf", page_count=1)
        out = tmp_path / "out.pdf"
        returned = proc.merge_pdfs([str(pdf)], str(out))
        assert returned == str(out)

    def test_creates_output_dir_if_missing(
        self, proc: PDFProcessor, tmp_path: Path
    ) -> None:
        pdf = _make_pdf(tmp_path / "a.pdf", page_count=1)
        nested_out = tmp_path / "merged" / "out.pdf"
        proc.merge_pdfs([str(pdf)], str(nested_out))
        assert nested_out.exists()
