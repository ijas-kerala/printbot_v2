"""
web/routers/payment.py — Payment page and client-side payment verification.

Routes
------
GET  /payment/{order_id}  → Render payment.html with Razorpay checkout JS.
POST /verify-payment      → Verify Razorpay signature, mark job paid, enqueue.

Flow
----
1. After POST /api/settings/confirm, the settings router redirects the browser
   to GET /payment?order_id=<razorpay_order_id>.
2. payment.html auto-opens the Razorpay checkout modal.
3. On successful payment, Razorpay JS calls the handler callback, which
   POSTs to /verify-payment with the three Razorpay identifiers.
4. Server verifies the HMAC-SHA256 signature, transitions job to PAID,
   and enqueues the print job.
5. Client is redirected to /success?job_id=<id>.

In parallel, Razorpay fires a webhook to /webhooks/razorpay.  Both paths
use the shared mark_job_paid() helper, so exactly one will effect the
transition — the second call is a no-op.

Session validation
------------------
GET /payment/{order_id}: The pb_session cookie is verified to prove the caller
owns the job.  If the cookie is missing or expired, the page still renders if
the job exists — the order_id itself is an unguessable Razorpay UUID.  We log
a warning but do not block (UX: users sometimes clear cookies or switch tabs).

POST /verify-payment: No session check here — Razorpay sends the three IDs
back to the client, which forwards them to us.  The HMAC-SHA256 signature
verification is the authorisation mechanism; a tampered signature returns 400.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.models import JobStatus, PrintJob
from web.dependencies import get_db, verify_job_cookie
from web.services.job_service import mark_job_paid
from web.services.razorpay_service import razorpay_service

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="web/templates")

# Job statuses that mean payment is already complete — redirect to success
_PAID_STATUSES: frozenset[JobStatus] = frozenset({
    JobStatus.PAID,
    JobStatus.PROCESSING,
    JobStatus.PRINTING,
    JobStatus.COMPLETED,
})


# ── Pydantic models ────────────────────────────────────────────────────────────

class VerifyPaymentRequest(BaseModel):
    razorpay_payment_id: str = Field(..., min_length=1)
    razorpay_order_id: str = Field(..., min_length=1)
    razorpay_signature: str = Field(..., min_length=1)


# ── GET /payment/{order_id} ────────────────────────────────────────────────────

@router.get("/payment/{order_id}", response_class=HTMLResponse)
async def payment_page(
    request: Request,
    order_id: str,
    db: AsyncSession = Depends(get_db),
    pb_session: Optional[str] = Cookie(default=None, alias="pb_session"),
) -> HTMLResponse:
    """
    Render the Razorpay checkout page for a pending print job.

    The order_id in the URL is the Razorpay order ID stored on the PrintJob
    (razorpay_order_id), not the job's UUID.  This mirrors how the settings
    router issues the redirect: /payment?order_id=<razorpay_order_id>.
    """
    result = await db.execute(
        select(PrintJob).where(PrintJob.razorpay_order_id == order_id)
    )
    job = result.scalar_one_or_none()

    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Payment order not found.",
        )

    # Back-button / page-refresh guard: job already paid → go to success
    if job.status in _PAID_STATUSES:
        return RedirectResponse(
            url=f"/success?job_id={job.id}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    # Guard against landing here before settings were confirmed
    if job.status != JobStatus.PAYMENT_PENDING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This job is not awaiting payment.",
        )

    # SECURITY: verify session cookie ownership — warn but don't block.
    # The order_id is an unguessable Razorpay UUID so enumeration is infeasible,
    # but we still want to log anomalies (e.g. shared link, expired session).
    if pb_session:
        verified_job_id = verify_job_cookie(pb_session)
        if verified_job_id != job.id:
            logger.warning(
                "payment_page: pb_session job_id %s does not match order job_id %s "
                "(expired session or tab switch — proceeding)",
                verified_job_id, job.id,
            )
    else:
        logger.info(
            "payment_page: no pb_session cookie for job %s order %s "
            "(possible tab switch or cookie cleared)",
            job.id, order_id,
        )

    return templates.TemplateResponse(
        "payment.html",
        {
            "request": request,
            "job_id": job.id,
            "order_id": order_id,
            "amount": job.total_cost,
            "amount_paise": int(round(job.total_cost * 100)),
            "key_id": settings.RAZORPAY_KEY_ID,
            "is_mock_payment": settings.is_mock_payment,
            "shop_name": "PrintBot",
        },
    )


# ── POST /verify-payment ───────────────────────────────────────────────────────

@router.post("/verify-payment")
async def verify_payment(
    body: VerifyPaymentRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Verify Razorpay payment signature and transition job to PAID.

    Called by the Razorpay checkout JS handler on the payment page immediately
    after the user completes payment in the Razorpay modal.

    Returns:
        { "status": "ok",           "redirect": "/success?job_id=..." }  — newly paid
        { "status": "already_paid", "redirect": "/success?job_id=..." }  — idempotent
    """
    # Look up by Razorpay order ID
    result = await db.execute(
        select(PrintJob).where(
            PrintJob.razorpay_order_id == body.razorpay_order_id
        )
    )
    job = result.scalar_one_or_none()

    if job is None:
        logger.warning(
            "verify_payment: no job found for razorpay_order_id=%s",
            body.razorpay_order_id,
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Order not found.",
        )

    # Idempotency: already transitioned (e.g. webhook arrived first)
    if job.status in _PAID_STATUSES:
        logger.info(
            "verify_payment: job %s already in '%s' — returning already_paid",
            job.id, job.status.value,
        )
        return {"status": "already_paid", "redirect": f"/success?job_id={job.id}"}

    # SECURITY: verify HMAC-SHA256 signature from Razorpay checkout JS.
    # Run in executor because the Razorpay SDK is synchronous.
    sig_valid = await asyncio.get_event_loop().run_in_executor(
        None,
        lambda: razorpay_service.verify_payment_signature(
            razorpay_order_id=body.razorpay_order_id,
            razorpay_payment_id=body.razorpay_payment_id,
            razorpay_signature=body.razorpay_signature,
        ),
    )

    if not sig_valid:
        logger.warning(
            "verify_payment: signature verification FAILED for job %s "
            "(order=%s payment=%s)",
            job.id, body.razorpay_order_id, body.razorpay_payment_id,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Payment verification failed. Please contact support.",
        )

    # Mark paid and enqueue (idempotent helper — webhook may also call this)
    await mark_job_paid(
        db=db,
        job=job,
        payment_id=body.razorpay_payment_id,
        source="verify-payment",
    )

    logger.info(
        "verify_payment: job %s successfully verified and enqueued "
        "(payment=%s order=%s)",
        job.id, body.razorpay_payment_id, body.razorpay_order_id,
    )

    return {"status": "ok", "redirect": f"/success?job_id={job.id}"}
