"""Add printer_note column to print_jobs.

Stores a live printer/driver status message written by the queue worker
during the PRINTING phase, so the status API can surface it to users
without triggering an extra CUPS call on every poll.

Revision ID: 0002
Revises:     0001
Create Date: 2026-04-03 00:00:00 UTC
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "print_jobs",
        sa.Column("printer_note", sa.String(256), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("print_jobs", "printer_note")
