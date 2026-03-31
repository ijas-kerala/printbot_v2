"""
tests/test_payment_idempotency.py — Payment idempotency tests for mark_job_paid().

Verifies that mark_job_paid() in web/services/job_service.py:
  - Correctly transitions a PAYMENT_PENDING job to PAID on first call
  - Returns False without mutating the job on any subsequent call
  - Enqueues the job for printing exactly once, regardless of call count
  - Stores razorpay_payment_id when provided
  - Handles all post-payment statuses (PAID, PROCESSING, PRINTING, COMPLETED)
    as already-done and skips gracefully

The print_queue.enqueue() is mocked via unittest.mock so tests don't require
a running asyncio queue or CUPS.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import JobStatus, PrintJob
from web.services.job_service import mark_job_paid


# mark_job_paid() imports print_queue lazily inside the function body:
#   from web.services.print_queue import print_queue
# The correct patch target is the singleton attribute on the source module so
# the local import inside mark_job_paid() picks up the Mock at lookup time.
_QUEUE_PATCH = "web.services.print_queue.print_queue"


# ── Happy-path tests ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_mark_job_paid_first_call_returns_true(
    async_db: AsyncSession, sample_job: PrintJob
) -> None:
    """First invocation must return True and transition job to PAID."""
    with patch(_QUEUE_PATCH) as mock_queue:
        mock_queue.enqueue = AsyncMock()
        result = await mark_job_paid(async_db, sample_job, source="test")

    assert result is True
    assert sample_job.status == JobStatus.PAID


@pytest.mark.asyncio
async def test_mark_job_paid_sets_paid_at(
    async_db: AsyncSession, sample_job: PrintJob
) -> None:
    """paid_at must be populated after the first successful call."""
    with patch(_QUEUE_PATCH) as mock_queue:
        mock_queue.enqueue = AsyncMock()
        await mark_job_paid(async_db, sample_job, source="test")

    assert sample_job.paid_at is not None
    assert isinstance(sample_job.paid_at, datetime)


@pytest.mark.asyncio
async def test_mark_job_paid_with_payment_id(
    async_db: AsyncSession, sample_job: PrintJob
) -> None:
    """razorpay_payment_id must be stored when a payment_id is provided."""
    with patch(_QUEUE_PATCH) as mock_queue:
        mock_queue.enqueue = AsyncMock()
        await mark_job_paid(async_db, sample_job, payment_id="pay_xyz123", source="test")

    assert sample_job.razorpay_payment_id == "pay_xyz123"


# ── Idempotency tests ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_mark_job_paid_second_call_returns_false(
    async_db: AsyncSession, sample_job: PrintJob
) -> None:
    """Second call on the same job must return False (no-op)."""
    with patch(_QUEUE_PATCH) as mock_queue:
        mock_queue.enqueue = AsyncMock()
        first = await mark_job_paid(async_db, sample_job, source="verify-payment")
        second = await mark_job_paid(async_db, sample_job, source="webhook")

    assert first is True
    assert second is False


@pytest.mark.asyncio
async def test_mark_job_paid_second_call_does_not_overwrite_paid_at(
    async_db: AsyncSession, sample_job: PrintJob
) -> None:
    """paid_at set on first call must not be overwritten by a second call."""
    with patch(_QUEUE_PATCH) as mock_queue:
        mock_queue.enqueue = AsyncMock()
        await mark_job_paid(async_db, sample_job, source="first")
        paid_at_after_first = sample_job.paid_at

        await mark_job_paid(async_db, sample_job, source="second")

    assert sample_job.paid_at == paid_at_after_first


@pytest.mark.asyncio
async def test_mark_job_paid_enqueues_exactly_once(
    async_db: AsyncSession, sample_job: PrintJob
) -> None:
    """print_queue.enqueue() must be called exactly once even when mark_job_paid
    is called twice (e.g. webhook race with verify-payment)."""
    with patch(_QUEUE_PATCH) as mock_queue:
        mock_queue.enqueue = AsyncMock()

        await mark_job_paid(async_db, sample_job, source="verify-payment")
        await mark_job_paid(async_db, sample_job, source="webhook")

        mock_queue.enqueue.assert_awaited_once_with(sample_job.id)


# ── Already post-payment status tests ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_mark_job_paid_already_paid_returns_false(
    async_db: AsyncSession, sample_job: PrintJob
) -> None:
    """A job already in PAID status must be treated as done."""
    sample_job.status = JobStatus.PAID

    with patch(_QUEUE_PATCH) as mock_queue:
        mock_queue.enqueue = AsyncMock()
        result = await mark_job_paid(async_db, sample_job, source="test")
        mock_queue.enqueue.assert_not_awaited()

    assert result is False


@pytest.mark.asyncio
async def test_mark_job_paid_already_processing_returns_false(
    async_db: AsyncSession, sample_job: PrintJob
) -> None:
    """A job in PROCESSING must not be re-paid."""
    sample_job.status = JobStatus.PROCESSING

    with patch(_QUEUE_PATCH) as mock_queue:
        mock_queue.enqueue = AsyncMock()
        result = await mark_job_paid(async_db, sample_job, source="test")

    assert result is False


@pytest.mark.asyncio
async def test_mark_job_paid_already_completed_returns_false(
    async_db: AsyncSession, sample_job: PrintJob
) -> None:
    """A job in COMPLETED must not be re-paid or re-enqueued."""
    sample_job.status = JobStatus.COMPLETED

    with patch(_QUEUE_PATCH) as mock_queue:
        mock_queue.enqueue = AsyncMock()
        result = await mark_job_paid(async_db, sample_job, source="test")
        mock_queue.enqueue.assert_not_awaited()

    assert result is False


@pytest.mark.asyncio
async def test_mark_job_paid_already_printing_returns_false(
    async_db: AsyncSession, sample_job: PrintJob
) -> None:
    """A job in PRINTING must not be re-paid."""
    sample_job.status = JobStatus.PRINTING

    with patch(_QUEUE_PATCH) as mock_queue:
        mock_queue.enqueue = AsyncMock()
        result = await mark_job_paid(async_db, sample_job, source="test")

    assert result is False
