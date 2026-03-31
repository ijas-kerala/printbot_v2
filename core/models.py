"""
core/models.py — All SQLAlchemy ORM models for PrintBot v3.

Single source of truth for the DB schema. Alembic autogenerates migrations
from this file — never edit the DB directly.

Model hierarchy:
  PrintJob ──< FileItem   (one job, many files)
  PrintJob ──  Coupon     (one job, zero or one coupon issued on failure)
  AdminSession            (standalone; no FK to jobs)
"""

from __future__ import annotations

import enum
import json
import math
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum as SAEnum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


# ── Base ──────────────────────────────────────────────────────────────────────

class Base(DeclarativeBase):
    pass


# ── Enum ──────────────────────────────────────────────────────────────────────

class JobStatus(str, enum.Enum):
    """
    Lifecycle states for a PrintJob.

    Transitions (happy path):
      UPLOADING → SETTINGS_PENDING → PAYMENT_PENDING → PAID → PROCESSING → PRINTING → COMPLETED

    Terminal error states: FAILED, EXPIRED
    """
    UPLOADING        = "uploading"
    SETTINGS_PENDING = "settings_pending"
    PAYMENT_PENDING  = "payment_pending"
    PAID             = "paid"
    PROCESSING       = "processing"
    PRINTING         = "printing"
    COMPLETED        = "completed"
    FAILED           = "failed"
    EXPIRED          = "expired"


# Active states used by is_active() — defined once to avoid duplication
_ACTIVE_STATUSES: frozenset[JobStatus] = frozenset({
    JobStatus.PAID,
    JobStatus.PROCESSING,
    JobStatus.PRINTING,
})


# ── Models ────────────────────────────────────────────────────────────────────

class PrintJob(Base):
    """
    Top-level record for a single user print session.

    One PrintJob contains 1-20 FileItems. The job drives the entire
    lifecycle from upload through payment to physical print completion.
    """
    __tablename__ = "print_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)  # UUID4 string
    session_token: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[JobStatus] = mapped_column(
        SAEnum(JobStatus, native_enum=False),
        default=JobStatus.UPLOADING,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    # Populated once job reaches COMPLETED/FAILED, triggers the cleanup scheduler
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # ── Payment ───────────────────────────────────────────────────────────────
    total_cost: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    razorpay_order_id: Mapped[Optional[str]] = mapped_column(
        String, nullable=True, index=True
    )
    razorpay_payment_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    paid_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # ── Print config ──────────────────────────────────────────────────────────
    copies: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    is_duplex: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    nup_layout: Mapped[int] = mapped_column(Integer, default=1, nullable=False)  # 1, 2, or 4

    # ── CUPS ──────────────────────────────────────────────────────────────────
    cups_job_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    failed_reason: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # ── Relations ─────────────────────────────────────────────────────────────
    files: Mapped[list[FileItem]] = relationship(
        "FileItem",
        back_populates="job",
        cascade="all, delete-orphan",
        order_by="FileItem.sort_order",
    )
    coupon: Mapped[Optional[Coupon]] = relationship(
        "Coupon",
        back_populates="job",
        uselist=False,
    )

    # ── Helper methods ────────────────────────────────────────────────────────

    def is_active(self) -> bool:
        """
        True while the job is actively consuming resources (paid, processing,
        or being sent to the printer). Used to decide whether to warn a user
        attempting to start a new session.
        """
        return self.status in _ACTIVE_STATUSES

    def total_pages_selected(self) -> int:
        """
        Sum of pages marked include=True across every FileItem in this job.

        Each FileItem stores its page_selection as a JSON list of dicts:
            [{"page": 1, "rotation": 0, "include": true}, ...]

        EDGE CASE: FileItem with no page_selection contributes all its pages
        (defensive: used during early upload before settings are confirmed).
        """
        total = 0
        for file_item in self.files:
            if not file_item.page_selection:
                # No selection recorded yet — fall back to the file's raw page count
                total += file_item.page_count or 0
                continue
            try:
                page_configs: list[dict] = json.loads(file_item.page_selection)
                total += sum(1 for p in page_configs if p.get("include", True))
            except (json.JSONDecodeError, TypeError):
                # FALLBACK: corrupt JSON means we can't know — count all pages
                total += file_item.page_count or 0
        return total

    def calculate_sheets(self) -> int:
        """
        Physical sheets of paper that will be consumed by this job.

        Formula:
          1. pages_per_side = nup_layout        (1, 2, or 4 logical pages per side)
          2. sides_per_sheet = 2 if duplex else 1
          3. logical_pages_per_sheet = pages_per_side * sides_per_sheet
          4. sheets_per_copy = ceil(total_selected / logical_pages_per_sheet)
          5. total_sheets = sheets_per_copy * copies

        EDGE CASE: 0 pages selected returns 0 (caller must guard against this).
        """
        total_pages = self.total_pages_selected()
        if total_pages == 0:
            return 0

        sides_per_sheet = 2 if self.is_duplex else 1
        logical_pages_per_sheet = self.nup_layout * sides_per_sheet
        sheets_per_copy = math.ceil(total_pages / logical_pages_per_sheet)
        return sheets_per_copy * self.copies

    def __repr__(self) -> str:
        return (
            f"<PrintJob id={self.id!r} status={self.status.value!r} "
            f"copies={self.copies} cost={self.total_cost}>"
        )


class FileItem(Base):
    """
    A single uploaded file within a PrintJob.

    After upload, each file is stored at uploads/<job_id>/<uuid>.<ext>.
    DOCX and image files get an additional converted_path pointing to their
    PDF rendition (produced by the background conversion task).
    """
    __tablename__ = "file_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("print_jobs.id"), nullable=False, index=True
    )
    original_name: Mapped[str] = mapped_column(String, nullable=False)
    stored_path: Mapped[str] = mapped_column(String, nullable=False)   # uploads/<job_id>/<uuid>.<ext>
    converted_path: Mapped[Optional[str]] = mapped_column(String, nullable=True)  # PDF after conversion
    page_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # JSON: [{"page": 1, "rotation": 90, "include": true}, ...]
    # NULL until the user visits the settings page and page configs are generated
    page_selection: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    job: Mapped[PrintJob] = relationship("PrintJob", back_populates="files")

    # ── Convenience ───────────────────────────────────────────────────────────

    def get_page_configs(self) -> list[dict]:
        """Return parsed page_selection or an empty list if not yet set / corrupt."""
        if not self.page_selection:
            return []
        try:
            return json.loads(self.page_selection)
        except (json.JSONDecodeError, TypeError):
            return []

    def effective_pdf_path(self) -> str:
        """
        Return the path that should be used for actual printing.
        Prefers converted_path (post-conversion PDF) over stored_path.
        """
        return self.converted_path or self.stored_path

    def __repr__(self) -> str:
        return (
            f"<FileItem id={self.id} job_id={self.job_id!r} "
            f"name={self.original_name!r} pages={self.page_count}>"
        )


class PricingRule(Base):
    """
    Tiered pricing configuration managed via the admin dashboard.

    Rules are matched by (total_pages, is_duplex). max_pages=NULL means
    the rule applies to all page counts above min_pages.

    EDGE CASE: Overlapping ranges for the same duplex type are caught
    at write time by the admin CRUD validator — not enforced at DB level.
    """
    __tablename__ = "pricing_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    min_pages: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    max_pages: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # NULL = unlimited
    is_duplex: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    price_per_page: Mapped[float] = mapped_column(Float, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    def __repr__(self) -> str:
        max_str = str(self.max_pages) if self.max_pages is not None else "∞"
        duplex_str = "duplex" if self.is_duplex else "simplex"
        return (
            f"<PricingRule id={self.id} [{self.min_pages}-{max_str} pages] "
            f"{duplex_str} ₹{self.price_per_page}/page active={self.is_active}>"
        )


class Coupon(Base):
    """
    Issued to a user when a paid job fails to print (compensation coupon).

    The coupon code is presented to the user on the error page so they can
    apply it to their next job. balance decreases as partial redemptions occur.
    """
    __tablename__ = "coupons"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(16), unique=True, index=True, nullable=False)
    balance: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    initial_amount: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    redeemed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    job_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("print_jobs.id"), nullable=True
    )

    job: Mapped[Optional[PrintJob]] = relationship("PrintJob", back_populates="coupon")

    def is_fully_redeemed(self) -> bool:
        return self.balance <= 0.0

    def __repr__(self) -> str:
        return (
            f"<Coupon id={self.id} code={self.code!r} "
            f"balance=₹{self.balance:.2f} job_id={self.job_id!r}>"
        )


class AdminSession(Base):
    """
    Persisted admin login sessions signed by itsdangerous.

    Storing sessions in DB (vs. pure cookie) allows server-side invalidation
    — useful if the admin PIN is compromised and needs immediate lockout.
    """
    __tablename__ = "admin_sessions"

    # SECURITY: token is the raw random value; the cookie holds itsdangerous-signed version
    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    def is_expired(self) -> bool:
        from datetime import timezone
        return datetime.now(timezone.utc).replace(tzinfo=None) > self.expires_at

    def __repr__(self) -> str:
        return (
            f"<AdminSession token={self.token[:8]}... "
            f"expires_at={self.expires_at.isoformat()}>"
        )
