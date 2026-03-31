"""
web/services/job_service.py — PrintJob lifecycle helpers.

Centralises state-transition logic so that multiple entry points
(verify-payment route, Razorpay webhooks, admin retry) all go through
exactly the same code path.

Currently implemented:
  - mark_job_paid(): idempotent PAID transition + print queue enqueue

Future additions (other modules will extend this):
  - mark_job_processing(), mark_job_completed(), mark_job_failed()
  - get_or_create_job(), expire_stale_jobs()
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from core.models import JobStatus, PrintJob

logger = logging.getLogger(__name__)

# Statuses that indicate the job has already moved past the payment gate.
# mark_job_paid() treats these as "already done" and returns False.
_POST_PAYMENT_STATUSES: frozenset[JobStatus] = frozenset({
    JobStatus.PAID,
    JobStatus.PROCESSING,
    JobStatus.PRINTING,
    JobStatus.COMPLETED,
})


async def mark_job_paid(
    db: AsyncSession,
    job: PrintJob,
    payment_id: Optional[str] = None,
    source: str = "unknown",
) -> bool:
    """
    Transition *job* to PAID and enqueue it for printing.

    This is the single canonical path for marking a job paid — called by
    both the verify-payment route (user-facing) and the Razorpay webhook
    handler (server-to-server).  Both paths must produce identical results
    for any given job, regardless of which arrives first.

    Args:
        db:         Active async DB session (from Depends(get_db)).
        job:        The PrintJob ORM instance to transition.
        payment_id: Razorpay payment ID (razorpay_payment_id), optional.
                    Not present on coupon-paid jobs or payment_link flows.
        source:     Caller label for log messages (e.g. "verify-payment",
                    "webhook:order.paid").

    Returns:
        True  — job was newly marked PAID and enqueued.
        False — job was already in a post-payment status; nothing changed.

    Notes:
        - An explicit db.commit() is issued inside this function so that the
          PAID status is durably persisted before the print queue is notified.
          The caller's get_db() session will auto-commit on exit; the second
          commit is a no-op.
        - print_queue is imported lazily so this service can be imported before
          Module 7 (print_queue.py) is built.  The FALLBACK comment marks where
          the job would be missed if the queue is unavailable.
    """
    # ── Idempotency check ──────────────────────────────────────────────────────
    if job.status in _POST_PAYMENT_STATUSES:
        logger.info(
            "mark_job_paid [%s]: job %s already in '%s', skipping",
            source, job.id, job.status.value,
        )
        return False

    # ── Transition to PAID ────────────────────────────────────────────────────
    job.status = JobStatus.PAID
    job.paid_at = datetime.now(timezone.utc).replace(tzinfo=None)

    if payment_id:
        job.razorpay_payment_id = payment_id

    # Commit before enqueue: if the server crashes between these two steps the
    # startup requeue sweep (Module 7) will re-enqueue any PAID jobs found in DB.
    await db.commit()

    logger.info(
        "mark_job_paid [%s]: job %s → PAID (payment_id=%s)",
        source, job.id, payment_id or "n/a",
    )

    # ── Enqueue for printing ───────────────────────────────────────────────────
    try:
        from web.services.print_queue import print_queue  # noqa: PLC0415

        await print_queue.enqueue(job.id)
        logger.info("mark_job_paid [%s]: job %s enqueued", source, job.id)
    except ImportError:
        # FALLBACK: print_queue not yet wired (Module 7). The job is durably
        # PAID in the DB; the startup requeue sweep will pick it up on restart.
        logger.warning(
            "mark_job_paid [%s]: print_queue not available — job %s is PAID but "
            "not yet enqueued (will be picked up on next restart)",
            source, job.id,
        )
    except Exception as exc:
        # EDGE CASE: queue is initialised but enqueue raised (e.g. queue full).
        # Job is already PAID in DB — log and let the startup sweep recover it.
        logger.error(
            "mark_job_paid [%s]: enqueue failed for job %s: %s",
            source, job.id, exc,
        )

    return True
