"""
web/routers/webhooks.py — Razorpay webhook receiver.

Routes
------
POST /webhooks/razorpay  → Handle Razorpay async payment events.

Supported events
----------------
  order.paid          — Primary path (Orders API). Fired when an order is fully
                        paid. Contains both order entity and payment entity.
  payment.captured    — Fallback path. Fired when a payment transitions to
                        "captured" state. Always contains the payment entity
                        with an order_id back-reference.
  payment_link.paid   — Legacy path for Payment Links API (kept for safety).

Idempotency
-----------
All three event handlers call mark_job_paid(), which is idempotent — if the
job has already been transitioned by verify-payment (or a previous webhook
delivery), the function logs the skip and returns False.  No duplicate actions.

Security
--------
Every incoming request is verified with HMAC-SHA256 using RAZORPAY_WEBHOOK_SECRET
before any payload is processed.  Requests without a valid signature are silently
acknowledged with HTTP 200 to prevent Razorpay from treating them as delivery
failures and retrying indefinitely.

Return contract
---------------
This endpoint ALWAYS returns HTTP 200, even when:
  - The signature is missing or invalid  (security log + early return)
  - The payload is malformed JSON        (error log + return)
  - The job is not found in DB           (warning log + continue)
  - An unexpected exception is raised    (error log + return)

Razorpay retries failed deliveries with exponential backoff; returning a non-200
for a legitimate duplicate event would cause infinite retries and double-prints.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import PrintJob
from web.dependencies import get_db
from web.services.job_service import mark_job_paid
from web.services.razorpay_service import razorpay_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


# ── POST /webhooks/razorpay ────────────────────────────────────────────────────

@router.post("/razorpay")
async def razorpay_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
    x_razorpay_signature: Optional[str] = Header(default=None),
) -> JSONResponse:
    """
    Receive and process Razorpay webhook events.

    The raw request body must be read before any JSON parsing so that the
    HMAC-SHA256 signature can be computed over the exact bytes Razorpay sent.
    """
    body_bytes: bytes = await request.body()

    # ── Signature verification ─────────────────────────────────────────────────
    if not x_razorpay_signature:
        # SECURITY: No signature header — could be a probe or misconfigured sender.
        # Return 200 to avoid triggering Razorpay retry logic.
        logger.warning(
            "razorpay_webhook: request received with no X-Razorpay-Signature header "
            "(client=%s) — ignoring",
            request.client.host if request.client else "unknown",
        )
        return JSONResponse({"status": "ignored"})

    # SECURITY: verify HMAC-SHA256 before touching the payload
    sig_valid = razorpay_service.verify_webhook_signature(
        body_bytes=body_bytes,
        signature=x_razorpay_signature,
    )

    if not sig_valid:
        # SECURITY: Invalid signature — log prominently and return 200 so Razorpay
        # doesn't retry.  A genuine delivery failure would have a valid signature.
        logger.warning(
            "razorpay_webhook: INVALID signature (sig=%s…) — ignoring payload",
            x_razorpay_signature[:12] if x_razorpay_signature else "n/a",
        )
        return JSONResponse({"status": "ignored"})

    # ── Parse and dispatch ─────────────────────────────────────────────────────
    try:
        payload: dict[str, Any] = json.loads(body_bytes)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.error("razorpay_webhook: failed to parse JSON body: %s", exc)
        return JSONResponse({"status": "error_but_received"})

    event: str = payload.get("event", "")
    logger.info("razorpay_webhook: received event=%r", event)

    try:
        if event == "order.paid":
            await _handle_order_paid(db, payload)

        elif event == "payment.captured":
            await _handle_payment_captured(db, payload)

        elif event == "payment_link.paid":
            await _handle_payment_link_paid(db, payload)

        else:
            # Unrecognised event — log and acknowledge.  Razorpay sends many event
            # types; we only act on the payment-related ones.
            logger.debug("razorpay_webhook: unhandled event=%r — acknowledged", event)

    except Exception as exc:
        # EDGE CASE: unexpected error in a handler. Job status is unchanged unless
        # mark_job_paid() already committed before the error.  Return 200 to
        # prevent Razorpay from immediately retrying.
        logger.error(
            "razorpay_webhook: unhandled exception processing event=%r: %s",
            event, exc, exc_info=True,
        )
        return JSONResponse({"status": "error_but_received"})

    return JSONResponse({"status": "ok"})


# ── Event handlers ─────────────────────────────────────────────────────────────

async def _handle_order_paid(db: AsyncSession, payload: dict[str, Any]) -> None:
    """
    order.paid — Primary event from the Razorpay Orders API.

    Payload path: payload.payload.order.entity.id  → Razorpay order ID
    Payment ID  : payload.payload.payment.entity.id (may be absent if order
                  was paid via payment link rather than checkout JS)
    """
    try:
        order_entity: dict = payload["payload"]["order"]["entity"]
        order_id: str = order_entity["id"]
    except (KeyError, TypeError) as exc:
        logger.error("razorpay_webhook order.paid: malformed payload — %s", exc)
        return

    # Extract payment ID from the companion payment entity (if present)
    payment_id: Optional[str] = None
    try:
        payment_entity: dict = payload["payload"]["payment"]["entity"]
        payment_id = payment_entity.get("id")
    except (KeyError, TypeError):
        # EDGE CASE: payment entity absent in some legacy webhook formats
        pass

    job = await _find_job_by_order(db, order_id)
    if job is None:
        logger.warning(
            "razorpay_webhook order.paid: no job found for order_id=%s", order_id
        )
        return

    await mark_job_paid(
        db=db,
        job=job,
        payment_id=payment_id,
        source=f"webhook:order.paid:{order_id}",
    )


async def _handle_payment_captured(db: AsyncSession, payload: dict[str, Any]) -> None:
    """
    payment.captured — Fallback event (fired when payment reaches "captured" state).

    This event fires even when order.paid doesn't (e.g. certain UPI edge cases).
    Payload path: payload.payload.payment.entity
    """
    try:
        payment_entity: dict = payload["payload"]["payment"]["entity"]
        payment_id: str = payment_entity["id"]
        order_id: Optional[str] = payment_entity.get("order_id")
    except (KeyError, TypeError) as exc:
        logger.error("razorpay_webhook payment.captured: malformed payload — %s", exc)
        return

    if not order_id:
        logger.warning(
            "razorpay_webhook payment.captured: no order_id in payment entity "
            "(payment_id=%s) — cannot match to job",
            payment_id,
        )
        return

    job = await _find_job_by_order(db, order_id)
    if job is None:
        logger.warning(
            "razorpay_webhook payment.captured: no job for order_id=%s "
            "(payment_id=%s)",
            order_id, payment_id,
        )
        return

    await mark_job_paid(
        db=db,
        job=job,
        payment_id=payment_id,
        source=f"webhook:payment.captured:{payment_id}",
    )


async def _handle_payment_link_paid(db: AsyncSession, payload: dict[str, Any]) -> None:
    """
    payment_link.paid — Legacy Payment Links API event.

    In the v2 codebase payment links were used before switching to the Orders
    API.  This handler remains so that any lingering payment links still resolve
    correctly.
    Payload path: payload.payload.payment_link.entity.id → payment link ID
                  (stored as razorpay_order_id on the job for legacy links)
    """
    try:
        pl_entity: dict = payload["payload"]["payment_link"]["entity"]
        pl_id: str = pl_entity["id"]
    except (KeyError, TypeError) as exc:
        logger.error("razorpay_webhook payment_link.paid: malformed payload — %s", exc)
        return

    # Attempt to also extract payment ID if present
    payment_id: Optional[str] = None
    try:
        payments = pl_entity.get("payments", [])
        if payments:
            payment_id = payments[0].get("razorpay_payment_id")
    except (AttributeError, IndexError, TypeError):
        pass

    job = await _find_job_by_order(db, pl_id)
    if job is None:
        logger.warning(
            "razorpay_webhook payment_link.paid: no job for pl_id=%s", pl_id
        )
        return

    await mark_job_paid(
        db=db,
        job=job,
        payment_id=payment_id,
        source=f"webhook:payment_link.paid:{pl_id}",
    )


# ── Private helpers ────────────────────────────────────────────────────────────

async def _find_job_by_order(
    db: AsyncSession, order_id: str
) -> Optional[PrintJob]:
    """Look up a PrintJob by its Razorpay order ID. Returns None if not found."""
    result = await db.execute(
        select(PrintJob).where(PrintJob.razorpay_order_id == order_id)
    )
    return result.scalar_one_or_none()
