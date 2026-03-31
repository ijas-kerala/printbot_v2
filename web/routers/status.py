"""
web/routers/status.py — Job status polling API and overall machine status API.

Endpoints:
  GET /jobs/{job_id}/status   — Per-job status for the success page polling loop.
                                Returns current job state, human-readable text, CUPS
                                driver details when printing, and coupon code on failure.

  GET /api/machine-status     — Aggregated machine state for the kiosk SSE feed and
                                the admin dashboard.  Combines print_queue position,
                                recent DB activity, and live CUPS printer state.

The shared helper `get_machine_status_data()` is imported by kiosk.py to avoid
duplicating the machine-state derivation logic inside the SSE generator.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from core.models import JobStatus, PrintJob
from core.printing.cups_manager import cups_manager
from web.dependencies import get_db

logger = logging.getLogger(__name__)

router = APIRouter(tags=["status"])

# ── State text map ─────────────────────────────────────────────────────────────

_STATE_TEXT: dict[JobStatus, str] = {
    JobStatus.UPLOADING:        "Upload in progress...",
    JobStatus.SETTINGS_PENDING: "Configure your print settings.",
    JobStatus.PAYMENT_PENDING:  "Waiting for payment...",
    JobStatus.PAID:             "Payment received — preparing your print...",
    JobStatus.PROCESSING:       "Processing your document...",
    JobStatus.PRINTING:         "Your document is printing...",
    JobStatus.COMPLETED:        "Print complete! Collect your pages.",
    JobStatus.FAILED:           "Printing failed.",
    JobStatus.EXPIRED:          "This job has expired.",
}

# Statuses that indicate the job lifecycle is over (nothing more will change)
_DONE_STATUSES: frozenset[JobStatus] = frozenset({
    JobStatus.COMPLETED,
    JobStatus.FAILED,
    JobStatus.EXPIRED,
})

# Statuses that map to the kiosk "printing" state (user is actively waiting)
_PRINTING_LIKE_STATUSES: frozenset[JobStatus] = frozenset({
    JobStatus.PAID,
    JobStatus.PROCESSING,
    JobStatus.PRINTING,
})

# How far back to look for "recent" jobs when there is no active queue entry
_RECENT_JOB_WINDOW_SECONDS: int = 60


# ── Per-job status endpoint ────────────────────────────────────────────────────

@router.get("/jobs/{job_id}/status")
async def get_job_status(
    job_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """
    Return the current status of a single print job.

    Called every few seconds by success.js to drive the success-page polling UI.
    No cookie auth: the job_id in the URL is the implicit ownership proof since
    the success-page URL is only distributed via redirect from the payment flow.

    Response shape:
      {
        "status":        str,            # JobStatus value e.g. "printing"
        "state_text":    str,            # Human-readable description
        "is_done":       bool,           # True once COMPLETED / FAILED / EXPIRED
        "driver_status": dict | None,    # CUPS state info (only while PRINTING)
        "coupon_code":   str | None,     # Compensation coupon code (only if FAILED)
      }
    """
    # SECURITY: validate job_id shape before hitting the DB — prevents arbitrary
    # string lookups that could stress the index or expose timing differences.
    try:
        uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job not found.",
        )

    result = await db.execute(
        select(PrintJob)
        .options(selectinload(PrintJob.coupon))
        .where(PrintJob.id == job_id)
    )
    job: Optional[PrintJob] = result.scalar_one_or_none()

    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job not found.",
        )

    # ── CUPS driver status (only while actively printing) ─────────────────────
    driver_status: Optional[dict] = None
    if job.status == JobStatus.PRINTING and job.cups_job_id:
        cups_job_id = job.cups_job_id

        def _get_cups_status() -> dict:
            return cups_manager.get_job_status(cups_job_id)

        try:
            driver_status = await asyncio.get_event_loop().run_in_executor(
                None, _get_cups_status
            )
        except Exception as exc:
            # EDGE CASE: CUPS unavailable mid-print — log but don't break polling
            logger.warning(
                "get_job_status: failed to query CUPS for job %s: %s", job_id, exc
            )

    # ── Coupon code (only on failure, only if one was issued) ─────────────────
    coupon_code: Optional[str] = None
    if job.status == JobStatus.FAILED and job.coupon is not None:
        coupon_code = job.coupon.code

    return {
        "status":        job.status.value,
        "state_text":    _STATE_TEXT.get(job.status, job.status.value),
        "is_done":       job.status in _DONE_STATUSES,
        "driver_status": driver_status,
        "coupon_code":   coupon_code,
    }


# ── Machine status helper (shared with kiosk SSE) ─────────────────────────────

async def get_machine_status_data(db: AsyncSession) -> dict[str, Any]:
    """
    Build the aggregated machine-state dict used by both GET /api/machine-status
    and the kiosk SSE generator.

    State derivation priority (first match wins):
      1. Printer offline (IPP state 0 or 5)  → "error"
      2. Active queue entry in PAID/PROCESSING/PRINTING  → "printing"
      3. Recent job (last 60s) in UPLOADING   → "uploading"
      4. Recent job (last 60s) in SETTINGS_PENDING/PAYMENT_PENDING  → "payment_pending"
      5. Recent job completed in last 30s     → "completed"
      6. Otherwise                            → "idle"

    Args:
        db: An active AsyncSession (caller is responsible for lifecycle).

    Returns a dict with keys: state, current_job_id, queue_length, printer_status.
    """
    from web.services.print_queue import print_queue  # noqa: PLC0415 — lazy to avoid circular import at module load

    queue_snapshot = print_queue.get_queue_status()
    current_job_id: Optional[str] = queue_snapshot["current_job"]
    queue_length: int = queue_snapshot["queued"]

    # ── Printer status (synchronous CUPS call, offloaded to executor) ──────────
    def _get_printer() -> dict:
        return cups_manager.get_printer_status()

    try:
        printer_status: dict = await asyncio.get_event_loop().run_in_executor(
            None, _get_printer
        )
    except Exception as exc:
        logger.error("get_machine_status_data: CUPS printer query failed: %s", exc)
        printer_status = {"state": 0, "state_reasons": [], "message": "unavailable"}

    # ── Derive machine state ───────────────────────────────────────────────────
    machine_state: str

    # 1. Printer hard-offline takes highest priority — the user can't print at all
    if printer_status["state"] in (0, 5):
        machine_state = "error"

    # 2. Active print job in queue or currently processing
    elif current_job_id is not None:
        # Load the job to get its exact status (PAID / PROCESSING / PRINTING)
        result = await db.execute(
            select(PrintJob).where(PrintJob.id == current_job_id)
        )
        current_job: Optional[PrintJob] = result.scalar_one_or_none()
        if current_job and current_job.status in _PRINTING_LIKE_STATUSES:
            machine_state = "printing"
        else:
            # Job vanished or moved to terminal state since get_queue_status snapshot
            machine_state = "idle"

    else:
        # No active queue entry — inspect recent jobs to show transient states
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
            seconds=_RECENT_JOB_WINDOW_SECONDS
        )

        result = await db.execute(
            select(PrintJob)
            .where(PrintJob.created_at >= cutoff)
            .order_by(PrintJob.created_at.desc())
        )
        recent_jobs: list[PrintJob] = list(result.scalars().all())

        statuses_present: frozenset[JobStatus] = frozenset(
            j.status for j in recent_jobs
        )

        if JobStatus.UPLOADING in statuses_present:
            machine_state = "uploading"
        elif (
            JobStatus.PAYMENT_PENDING in statuses_present
            or JobStatus.SETTINGS_PENDING in statuses_present
        ):
            machine_state = "payment_pending"
        else:
            # EDGE CASE: check if a job just completed in the last 30s
            # so the kiosk shows "completed" briefly after the print finishes.
            completed_cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
                seconds=30
            )
            recently_completed = any(
                j.status == JobStatus.COMPLETED
                and j.completed_at is not None
                and j.completed_at >= completed_cutoff
                for j in recent_jobs
            )
            machine_state = "completed" if recently_completed else "idle"

    return {
        "state":          machine_state,
        "current_job_id": current_job_id,
        "queue_length":   queue_length,
        "printer_status": printer_status,
    }


# ── Machine status endpoint ────────────────────────────────────────────────────

@router.get("/api/machine-status")
async def get_machine_status(
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """
    Return the overall machine state.

    Used by:
      - Kiosk SSE generator (via direct call to get_machine_status_data)
      - Admin dashboard printer-status widget (polled every 10s)
      - Any future monitoring integrations

    No auth required — the response contains no user PII or secrets.
    """
    return await get_machine_status_data(db)
