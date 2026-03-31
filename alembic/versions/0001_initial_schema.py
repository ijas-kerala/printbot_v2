"""Initial schema — all tables + default pricing rules.

Creates the complete PrintBot v3 database schema from scratch and seeds
two default pricing rules so the system is immediately operational after
a fresh install:

  - Simplex (single-sided): 1–∞ pages at ₹2.00/page
  - Duplex (double-sided):  1–∞ pages at ₹3.50/page

Revision ID: 0001
Revises:     (none — this is the root migration)
Create Date: 2026-03-31 00:00:00 UTC
"""

from __future__ import annotations

from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

# ── Migration identifiers ──────────────────────────────────────────────────────
revision: str = "0001"
down_revision: str | None = None
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _utcnow() -> str:
    """ISO-8601 UTC timestamp for seeded rows (server_default is not used in
    data inserts, so we provide an explicit value)."""
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" ", timespec="seconds")


# ── Upgrade ───────────────────────────────────────────────────────────────────

def upgrade() -> None:
    # ── print_jobs ────────────────────────────────────────────────────────────
    op.create_table(
        "print_jobs",
        sa.Column("id", sa.String(36), primary_key=True, nullable=False),
        sa.Column("session_token", sa.String(64), nullable=False),
        # JobStatus enum stored as VARCHAR (native_enum=False in models.py)
        sa.Column(
            "status",
            sa.String(32),
            nullable=False,
            server_default="uploading",
        ),
        sa.Column(
            "created_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
        ),
        sa.Column("expires_at", sa.DateTime, nullable=True),

        # Payment
        sa.Column("total_cost", sa.Float, nullable=False, server_default="0.0"),
        sa.Column("razorpay_order_id", sa.String, nullable=True),
        sa.Column("razorpay_payment_id", sa.String, nullable=True),
        sa.Column("paid_at", sa.DateTime, nullable=True),

        # Print config
        sa.Column("copies", sa.Integer, nullable=False, server_default="1"),
        sa.Column("is_duplex", sa.Boolean, nullable=False, server_default="0"),
        sa.Column("nup_layout", sa.Integer, nullable=False, server_default="1"),

        # CUPS tracking
        sa.Column("cups_job_id", sa.Integer, nullable=True),
        sa.Column("completed_at", sa.DateTime, nullable=True),
        sa.Column("failed_reason", sa.String, nullable=True),
    )

    # Index for fast lookup by Razorpay order ID (webhook path)
    op.create_index(
        "ix_print_jobs_razorpay_order_id",
        "print_jobs",
        ["razorpay_order_id"],
    )

    # ── file_items ────────────────────────────────────────────────────────────
    op.create_table(
        "file_items",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True, nullable=False),
        sa.Column(
            "job_id",
            sa.String(36),
            sa.ForeignKey("print_jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("original_name", sa.String, nullable=False),
        sa.Column("stored_path", sa.String, nullable=False),
        sa.Column("converted_path", sa.String, nullable=True),
        sa.Column("page_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("sort_order", sa.Integer, nullable=False, server_default="0"),
        # JSON: [{"page": 1, "rotation": 0, "include": true}, ...]
        sa.Column("page_selection", sa.Text, nullable=True),
    )

    op.create_index(
        "ix_file_items_job_id",
        "file_items",
        ["job_id"],
    )

    # ── pricing_rules ─────────────────────────────────────────────────────────
    op.create_table(
        "pricing_rules",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True, nullable=False),
        sa.Column("min_pages", sa.Integer, nullable=False, server_default="1"),
        # NULL means the rule applies to all pages above min_pages (unbounded)
        sa.Column("max_pages", sa.Integer, nullable=True),
        sa.Column("is_duplex", sa.Boolean, nullable=False, server_default="0"),
        sa.Column("price_per_page", sa.Float, nullable=False),
        sa.Column("description", sa.String, nullable=True),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="1"),
    )

    # ── coupons ───────────────────────────────────────────────────────────────
    op.create_table(
        "coupons",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True, nullable=False),
        sa.Column("code", sa.String(16), nullable=False, unique=True),
        sa.Column("balance", sa.Float, nullable=False, server_default="0.0"),
        sa.Column("initial_amount", sa.Float, nullable=False, server_default="0.0"),
        sa.Column(
            "created_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
        ),
        sa.Column("redeemed_at", sa.DateTime, nullable=True),
        sa.Column(
            "job_id",
            sa.String(36),
            sa.ForeignKey("print_jobs.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )

    op.create_index("ix_coupons_code", "coupons", ["code"], unique=True)

    # ── admin_sessions ────────────────────────────────────────────────────────
    op.create_table(
        "admin_sessions",
        sa.Column("token", sa.String(64), primary_key=True, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
        ),
        sa.Column("expires_at", sa.DateTime, nullable=False),
    )

    # ── Seed: default pricing rules ───────────────────────────────────────────
    # These two rules make the shop operational immediately after install.
    # Admins can adjust or replace them via the dashboard (Admin → Pricing tab).
    pricing_rules_table = sa.table(
        "pricing_rules",
        sa.column("min_pages", sa.Integer),
        sa.column("max_pages", sa.Integer),
        sa.column("is_duplex", sa.Boolean),
        sa.column("price_per_page", sa.Float),
        sa.column("description", sa.String),
        sa.column("is_active", sa.Boolean),
    )

    op.bulk_insert(
        pricing_rules_table,
        [
            {
                "min_pages": 1,
                "max_pages": None,       # NULL = unlimited (1–∞ pages)
                "is_duplex": False,
                "price_per_page": 2.0,
                "description": "Simplex (single-sided) — all pages",
                "is_active": True,
            },
            {
                "min_pages": 1,
                "max_pages": None,       # NULL = unlimited (1–∞ pages)
                "is_duplex": True,
                "price_per_page": 3.5,
                "description": "Duplex (double-sided) — all pages",
                "is_active": True,
            },
        ],
    )


# ── Downgrade ─────────────────────────────────────────────────────────────────

def downgrade() -> None:
    """Drop all tables in reverse dependency order.

    Order matters: tables with FKs pointing to a parent must be dropped
    before the parent, otherwise SQLite will raise an integrity error.
    """
    # Indexes are dropped automatically with their tables in SQLite,
    # but we drop them explicitly for portability.
    op.drop_index("ix_coupons_code", table_name="coupons")
    op.drop_index("ix_file_items_job_id", table_name="file_items")
    op.drop_index("ix_print_jobs_razorpay_order_id", table_name="print_jobs")

    op.drop_table("admin_sessions")
    op.drop_table("coupons")
    op.drop_table("pricing_rules")
    op.drop_table("file_items")
    op.drop_table("print_jobs")
