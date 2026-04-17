"""
web/routers/settings.py — Print settings page and confirm endpoint.

Routes
------
GET  /settings                                        → Settings page (requires pb_session)
GET  /api/thumbnail/<job_id>/<file_item_id>/<page_num> → Serve pre-generated thumbnail PNG
POST /api/settings/confirm                            → Save settings, create Razorpay order

Flow
----
1. User arrives at /settings?job_id=<id> with a valid pb_session cookie (set by /upload).
2. Server generates thumbnails for any FileItem that doesn't have them yet, then renders
   settings.html with embedded pricing rules JSON.
3. User configures pages / copies / duplex / N-up and clicks "Confirm & Pay".
4. JS POSTs to /api/settings/confirm, which:
   a. Validates the session and page selection.
   b. Calculates price (sheets × price_per_sheet from pricing rules).
   c. Applies coupon discount if provided (immediately deducts coupon balance).
   d. If final cost > 0: creates Razorpay order and returns { order_id, amount, key_id }.
   e. If final cost == 0 (coupon covers all): marks job paid, enqueues, returns redirect URL.

Session validation
------------------
All routes that touch job data verify that the pb_session cookie's signed job_id
matches the job_id in the URL/body — preventing cross-job access.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from core.config import settings
from core.models import Coupon, FileItem, JobStatus, PricingRule, PrintJob
from core.printing.pdf_processor import CorruptPDFError, PasswordProtectedError, pdf_processor
from web.dependencies import get_db, get_job_from_session, require_job_session, verify_job_cookie
from web.services.razorpay_service import razorpay_service

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="web/templates")

# Sub-directory under uploads/<job_id>/ where thumbnails are stored
_THUMB_SUBDIR = "thumbs"


# ── Pydantic models (confirm endpoint) ────────────────────────────────────────

class PageConfig(BaseModel):
    idx: int = Field(..., ge=0, description="0-based page index")
    rotation: int = Field(0, description="Rotation in degrees: 0, 90, 180, or 270")
    include: bool = Field(True, description="Whether to include this page in the print")


class FileConfig(BaseModel):
    id: int = Field(..., description="FileItem primary key")
    pages: list[PageConfig]


class ConfirmRequest(BaseModel):
    job_id: str
    files: list[FileConfig]
    copies: int = Field(..., ge=1, le=99)
    is_duplex: bool = False
    nup_layout: int = Field(1, description="Pages per physical side: 1, 2, or 4")
    coupon_code: Optional[str] = None


# ── GET /settings ─────────────────────────────────────────────────────────────

_SETTINGS_PAID_STATUSES: frozenset[JobStatus] = frozenset({
    JobStatus.PAID,
    JobStatus.PROCESSING,
    JobStatus.PRINTING,
    JobStatus.COMPLETED,
})


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    job_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    session_job: Optional[PrintJob] = Depends(get_job_from_session),
) -> HTMLResponse:
    """
    Render the print settings page for the given job.

    Session validation is done manually so that missing/invalid sessions
    produce a friendly redirect to / rather than a raw 403 or 422 error.
    The query-param job_id is also optional for the same reason; any mismatch
    with the cookie-derived job redirects home.

    Thumbnails are generated on-demand for any FileItem that doesn't have them.
    This is blocking I/O (PyMuPDF), so it runs in an executor.
    """
    # Guard: job_id missing from URL
    if not job_id:
        return RedirectResponse(
            url="/?msg=session_required",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    # Guard: no valid session cookie
    if session_job is None:
        r = RedirectResponse(
            url="/?msg=session_expired",
            status_code=status.HTTP_303_SEE_OTHER,
        )
        r.delete_cookie(settings.JOB_SESSION_COOKIE_NAME)
        return r

    # SECURITY: cookie job_id must match the URL query param
    if session_job.id != job_id:
        return RedirectResponse(
            url="/?msg=session_mismatch",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    # Job already paid/processing/done — send the user to the status page
    if session_job.status in _SETTINGS_PAID_STATUSES:
        return RedirectResponse(
            url=f"/success?job_id={session_job.id}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    # Guard against accessing an already-confirmed job that is still pending payment
    if session_job.status == JobStatus.PAYMENT_PENDING and session_job.razorpay_order_id:
        return RedirectResponse(
            url=f"/payment?order_id={session_job.razorpay_order_id}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    if session_job.status not in (JobStatus.SETTINGS_PENDING, JobStatus.UPLOADING):
        # Only remaining cases are FAILED / EXPIRED — nothing useful to show
        r = RedirectResponse(
            url="/?msg=session_expired",
            status_code=status.HTTP_303_SEE_OTHER,
        )
        r.delete_cookie(settings.JOB_SESSION_COOKIE_NAME)
        return r

    # Fetch job with files (selectinload triggers one extra SQL for the relationship)
    result = await db.execute(
        select(PrintJob)
        .where(PrintJob.id == job_id)
        .options(selectinload(PrintJob.files))
    )
    job = result.scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")

    # Ensure thumbnails exist for every file
    files_context = []
    for file_item in job.files:
        ctx = await _ensure_thumbnails(file_item, job_id)
        files_context.append(ctx)

    # Fetch active pricing rules for client-side price calculator
    rules_result = await db.execute(
        select(PricingRule)
        .where(PricingRule.is_active.is_(True))
        .order_by(PricingRule.is_duplex, PricingRule.min_pages)
    )
    pricing_rules = rules_result.scalars().all()

    pricing_rules_list = [
        {
            "id": r.id,
            "min_pages": r.min_pages,
            "max_pages": r.max_pages,
            "is_duplex": r.is_duplex,
            "price_per_page": r.price_per_page,
        }
        for r in pricing_rules
    ]

    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "job": job,
            "job_id": job.id,
            "files": files_context,
            "pricing_rules": pricing_rules_list,
            "price_per_page": settings.PRICE_PER_PAGE,
            "razorpay_key_id": settings.RAZORPAY_KEY_ID,
            "is_mock_payment": settings.is_mock_payment,
        },
    )


# ── GET /api/thumbnail/<job_id>/<file_item_id>/<page_num> ────────────────────

@router.get("/api/thumbnail/{job_id}/{file_item_id}/{page_num}")
async def serve_thumbnail(
    job_id: str,
    file_item_id: int,
    page_num: int,
    pb_session: Optional[str] = Cookie(default=None, alias="pb_session"),
) -> FileResponse:
    """
    Serve a pre-generated thumbnail PNG for a specific page of a FileItem.

    SECURITY: Uses the pb_session cookie signature to prove the caller owns
    the job without a DB round-trip on every thumbnail request. UUID job_ids
    make enumeration attacks infeasible.

    Cache-Control: immutable because thumbnails never change after generation.
    """
    # SECURITY: verify cookie signature only (no DB hit — thumbnails are static)
    verified_job_id = verify_job_cookie(pb_session) if pb_session else None
    if verified_job_id != job_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied.",
        )

    if page_num < 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid page number.")

    thumb_path = (
        Path(settings.UPLOAD_DIR)
        / job_id
        / _THUMB_SUBDIR
        / str(file_item_id)
        / f"thumb_{page_num:03d}.png"
    )

    if not thumb_path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Thumbnail not found.")

    return FileResponse(
        path=str(thumb_path),
        media_type="image/png",
        headers={
            # Thumbnails are write-once; safe to cache forever in CDN / browser
            "Cache-Control": "public, max-age=31536000, immutable",
        },
    )


# ── POST /api/settings/confirm ────────────────────────────────────────────────

@router.post("/api/settings/confirm")
async def confirm_settings(
    body: ConfirmRequest,
    db: AsyncSession = Depends(get_db),
    session_job: PrintJob = Depends(require_job_session),
) -> dict:
    """
    Persist print settings, calculate price, apply coupon, and create a
    Razorpay order (or skip to free-print flow if coupon covers the full cost).

    Returns one of:
      { "status": "ok",   "order_id": ..., "amount": ..., "amount_paise": ..., "key_id": ... }
      { "status": "free", "redirect": "/success?job_id=..." }

    Edge cases handled:
    - job_id mismatch with session → 403
    - job already paid              → 409
    - 0 pages selected              → 400
    - nup_layout not in {1, 2, 4}   → 400
    - page index out of range       → 400
    - rotation not in {0,90,180,270}→ 400
    - invalid / exhausted coupon    → 400
    - pricing config gap            → 503
    - Razorpay order creation fails → 503
    """
    # SECURITY: cookie job_id must match the request body
    if session_job.id != body.job_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Job ID does not match your active session.",
        )

    # Idempotency guard — the job should not already be paid
    if session_job.status == JobStatus.PAID:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This job was already paid. Please check the status page.",
        )

    # Validate nup_layout early — catches bad input before expensive DB work
    if body.nup_layout not in (1, 2, 4):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid N-up layout. Must be 1, 2, or 4.",
        )

    if not body.files:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No file configuration submitted.",
        )

    # Fetch job with files (SQLAlchemy identity map returns same PrintJob instance)
    result = await db.execute(
        select(PrintJob)
        .where(PrintJob.id == body.job_id)
        .options(selectinload(PrintJob.files))
    )
    job = result.scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")

    file_map: dict[int, FileItem] = {f.id: f for f in job.files}

    # ── Validate and persist per-file page configs ─────────────────────────
    total_selected = 0

    for file_conf in body.files:
        file_item = file_map.get(file_conf.id)
        if file_item is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"File {file_conf.id} does not belong to this job.",
            )

        # Validate every page config for this file
        for page in file_conf.pages:
            if page.rotation not in (0, 90, 180, 270):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid rotation {page.rotation} for page {page.idx}. Must be 0, 90, 180, or 270.",
                )
            # EDGE CASE: page_count=0 means thumbnails/page count not yet known;
            # skip range check and trust the client's value in that state.
            if file_item.page_count > 0 and page.idx >= file_item.page_count:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f"Page index {page.idx} is out of range for "
                        f"'{file_item.original_name}' ({file_item.page_count} pages)."
                    ),
                )
            if page.include:
                total_selected += 1

        # Persist as the canonical page_selection JSON on the FileItem
        file_item.page_selection = json.dumps(
            [
                {"page_idx": p.idx, "rotation": p.rotation, "include": p.include}
                for p in file_conf.pages
            ]
        )

    # Guard: at least one page must be selected across all files
    if total_selected == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Please select at least one page to print.",
        )

    # Auto-correct: duplex makes no sense for a single page
    is_duplex = body.is_duplex
    if total_selected == 1 and is_duplex:
        is_duplex = False
        logger.info(
            "Auto-switched to simplex for single-page job %s", job.id
        )

    # ── Update job print config ────────────────────────────────────────────
    job.copies = body.copies
    job.is_duplex = is_duplex
    job.nup_layout = body.nup_layout

    # ── Calculate physical sheets ──────────────────────────────────────────
    # Formula: ceil(selected_pages / (nup_layout × sides_per_sheet)) × copies
    sides_per_sheet = 2 if is_duplex else 1
    logical_per_sheet = body.nup_layout * sides_per_sheet
    sheets_per_copy = math.ceil(total_selected / logical_per_sheet)
    total_sheets = sheets_per_copy * body.copies

    # ── Find matching pricing rule ─────────────────────────────────────────
    # Match: active, correct duplex mode, sheet count within [min_pages, max_pages]
    # Order by min_pages DESC so the most specific (highest-tier) rule wins.
    rules_result = await db.execute(
        select(PricingRule)
        .where(
            PricingRule.is_active.is_(True),
            PricingRule.is_duplex == is_duplex,
            PricingRule.min_pages <= total_sheets,
            or_(
                PricingRule.max_pages.is_(None),
                PricingRule.max_pages >= total_sheets,
            ),
        )
        .order_by(PricingRule.min_pages.desc())
    )
    rule = rules_result.scalars().first()

    if rule is None:
        # Distinguish: "no rules at all" (use fallback) vs "rules exist but none matched"
        any_rule_result = await db.execute(
            select(PricingRule.id).where(PricingRule.is_active.is_(True)).limit(1)
        )
        has_any_rule = any_rule_result.scalar_one_or_none() is not None

        if has_any_rule:
            # EDGE CASE: pricing config gap — admin needs to add a rule for this range
            logger.warning(
                "No pricing rule for job %s: %d sheets, duplex=%s",
                job.id, total_sheets, is_duplex,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Pricing unavailable for this job configuration. Please contact staff.",
            )

        # No rules configured at all → use the settings fallback price
        price_per_sheet: float = settings.PRICE_PER_PAGE
        logger.warning(
            "No pricing rules configured — falling back to PRICE_PER_PAGE=%.2f for job %s",
            price_per_sheet, job.id,
        )
    else:
        price_per_sheet = rule.price_per_page

    base_cost = round(total_sheets * price_per_sheet, 2)

    # ── Coupon application ─────────────────────────────────────────────────
    coupon_discount = 0.0

    if body.coupon_code:
        code_upper = body.coupon_code.strip().upper()

        coupon_result = await db.execute(
            select(Coupon).where(Coupon.code == code_upper)
        )
        coupon_obj: Optional[Coupon] = coupon_result.scalar_one_or_none()

        if coupon_obj is None or coupon_obj.balance <= 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid or expired coupon code.",
            )

        coupon_discount = min(coupon_obj.balance, base_cost)
        coupon_obj.balance = round(coupon_obj.balance - coupon_discount, 2)

        # Mark coupon as fully redeemed once balance reaches zero
        if coupon_obj.balance <= 0:
            coupon_obj.redeemed_at = datetime.now(timezone.utc).replace(tzinfo=None)

        logger.info(
            "Coupon %s applied to job %s: -₹%.2f (remaining balance: ₹%.2f)",
            code_upper, job.id, coupon_discount, coupon_obj.balance,
        )

    final_cost = max(0.0, round(base_cost - coupon_discount, 2))
    job.total_cost = final_cost

    # ── Free job: coupon covers full cost ──────────────────────────────────
    if final_cost <= 0:
        job.status = JobStatus.PAID
        job.razorpay_order_id = f"coupon_{job.id[:12]}"
        job.paid_at = datetime.now(timezone.utc).replace(tzinfo=None)

        # Enqueue for printing immediately
        try:
            from web.services.print_queue import print_queue  # noqa: PLC0415

            await print_queue.enqueue(job.id)
            logger.info("Free job %s enqueued for printing (coupon covered full cost)", job.id)
        except ImportError:
            # FALLBACK: print_queue not yet wired (Module 7); job is paid but not yet
            # enqueued — the startup requeue sweep will pick it up on next restart.
            logger.warning(
                "print_queue not available; job %s marked PAID but not enqueued — "
                "will be picked up on restart",
                job.id,
            )

        return {
            "status": "free",
            "redirect": f"/success?job_id={job.id}",
        }

    # ── Create Razorpay order ──────────────────────────────────────────────
    job.status = JobStatus.PAYMENT_PENDING

    try:
        # Run in executor: Razorpay SDK is blocking / sync
        order = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: razorpay_service.create_order(
                amount=final_cost,
                receipt=f"rcpt_{job.id[:20]}",
                notes={"job_id": job.id, "sheets": total_sheets},
            ),
        )
    except Exception as exc:
        # Roll back payment_pending status so the user can retry
        job.status = JobStatus.SETTINGS_PENDING
        logger.error("Razorpay order creation failed for job %s: %s", job.id, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Payment system error. Please try again.",
        )

    order_id: str = order.get("id", "") if isinstance(order, dict) else str(order)
    if not order_id:
        job.status = JobStatus.SETTINGS_PENDING
        logger.error("Razorpay returned empty order id for job %s: %r", job.id, order)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Payment system returned an invalid response. Please try again.",
        )

    job.razorpay_order_id = order_id

    logger.info(
        "Razorpay order %s created for job %s — ₹%.2f (%d sheets, %s)",
        order_id, job.id, final_cost, total_sheets,
        "duplex" if is_duplex else "simplex",
    )

    return {
        "status": "ok",
        "order_id": order_id,
        "amount": final_cost,
        # Razorpay checkout JS expects amount in paise
        "amount_paise": int(round(final_cost * 100)),
        "key_id": settings.RAZORPAY_KEY_ID,
    }


# ── POST /api/coupon/check ─────────────────────────────────────────────────────

class CouponCheckRequest(BaseModel):
    code: str
    job_id: str


@router.post("/api/coupon/check")
async def check_coupon(
    body: CouponCheckRequest,
    db: AsyncSession = Depends(get_db),
    session_job: PrintJob = Depends(require_job_session),
) -> dict:
    """
    Validate a coupon code and return its current redeemable balance.

    Does NOT redeem the coupon — redemption happens only in confirm_settings.
    This endpoint is safe to call speculatively from the settings page.

    Returns:
      { "valid": true,  "discount": float, "message": str }
      { "valid": false, "message": str }
    """
    # SECURITY: session must own this job
    if session_job.id != body.job_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Job ID does not match your active session.",
        )

    code = body.code.strip().upper()
    if not code:
        return {"valid": False, "message": "Enter a coupon code."}

    result = await db.execute(
        select(Coupon).where(Coupon.code == code)
    )
    coupon = result.scalar_one_or_none()

    if coupon is None:
        return {"valid": False, "message": "Coupon not found."}

    if coupon.balance <= 0:
        return {"valid": False, "message": "This coupon has already been used."}

    # Coupon is already attached to a *different* job that has been paid
    if coupon.job_id and coupon.job_id != body.job_id:
        other_result = await db.execute(
            select(PrintJob.status).where(PrintJob.id == coupon.job_id)
        )
        other_status = other_result.scalar_one_or_none()
        if other_status and other_status not in (JobStatus.SETTINGS_PENDING, JobStatus.UPLOADING):
            return {"valid": False, "message": "This coupon has already been applied."}

    logger.info("Coupon %s checked for job %s — balance ₹%.2f", code, body.job_id, coupon.balance)
    return {
        "valid": True,
        "discount": coupon.balance,
        "message": f"Coupon valid — ₹{coupon.balance:.2f} will be deducted.",
    }


# ── Private helpers ────────────────────────────────────────────────────────────

async def _ensure_thumbnails(file_item: FileItem, job_id: str) -> dict:
    """
    Verify that thumbnail PNGs exist on disk for *file_item*. Generate them
    via PyMuPDF if they are absent or incomplete.

    Thumbnails are stored at:
        uploads/<job_id>/thumbs/<file_item.id>/thumb_NNN.png

    Never raises — errors are captured in the returned dict so the settings
    page still renders even if one file's thumbnails are broken.

    Returns a template context dict:
        {
          "id": int,
          "original_name": str,
          "page_count": int,
          "thumb_urls": ["/api/thumbnail/<job_id>/<id>/<n>", ...],
          "conversion_pending": bool,   # True when DOCX/image not yet converted
          "error": str | None,
        }
    """
    thumb_dir = (
        Path(settings.UPLOAD_DIR) / job_id / _THUMB_SUBDIR / str(file_item.id)
    )

    effective_path = file_item.effective_pdf_path()
    is_usable_pdf = effective_path.lower().endswith(".pdf")

    # Files that need a background conversion step before we can render thumbnails
    raw_ext = Path(file_item.original_name).suffix.lower()
    conversion_pending = not is_usable_pdf and raw_ext in (".docx", ".jpg", ".jpeg", ".png")

    thumb_urls: list[str] = []
    error: Optional[str] = None
    page_count: int = file_item.page_count  # may be 0 if not yet determined

    if conversion_pending:
        # Images (JPG/PNG) can be converted to PDF inline right now.
        # DOCX still requires an external conversion tool (not implemented).
        if raw_ext in (".jpg", ".jpeg", ".png"):
            converted_pdf = str(
                Path(settings.UPLOAD_DIR) / job_id / f"converted_{file_item.id}.pdf"
            )
            try:
                await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: pdf_processor.convert_image_to_pdf(
                        file_item.stored_path, converted_pdf
                    ),
                )
                file_item.converted_path = converted_pdf
                effective_path = converted_pdf  # thumbnails must use the converted PDF
                is_usable_pdf = True
                conversion_pending = False
                logger.info(
                    "Converted image FileItem %d to PDF: %s (job %s)",
                    file_item.id, converted_pdf, job_id,
                )
            except Exception as exc:
                logger.error(
                    "Image-to-PDF conversion failed for FileItem %d (job %s): %s",
                    file_item.id, job_id, exc,
                )
                return {
                    "id": file_item.id,
                    "original_name": file_item.original_name,
                    "page_count": 0,
                    "thumb_urls": [],
                    "conversion_pending": False,
                    "error": "Could not convert image to PDF.",
                }
        elif raw_ext == ".docx":
            converted_pdf = str(
                Path(settings.UPLOAD_DIR) / job_id / f"converted_{file_item.id}.pdf"
            )
            try:
                await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: pdf_processor.convert_docx_to_pdf(
                        file_item.stored_path, converted_pdf
                    ),
                )
                file_item.converted_path = converted_pdf
                effective_path = converted_pdf
                is_usable_pdf = True
                conversion_pending = False
                logger.info(
                    "Converted DOCX FileItem %d to PDF: %s (job %s)",
                    file_item.id, converted_pdf, job_id,
                )
            except EnvironmentError as exc:
                logger.error("LibreOffice not available (job %s): %s", job_id, exc)
                return {
                    "id": file_item.id,
                    "original_name": file_item.original_name,
                    "page_count": 0,
                    "thumb_urls": [],
                    "conversion_pending": False,
                    "error": "DOCX conversion unavailable — LibreOffice is not installed.",
                }
            except Exception as exc:
                logger.error(
                    "DOCX conversion failed for FileItem %d (job %s): %s",
                    file_item.id, job_id, exc,
                )
                return {
                    "id": file_item.id,
                    "original_name": file_item.original_name,
                    "page_count": 0,
                    "thumb_urls": [],
                    "conversion_pending": False,
                    "error": "Could not convert DOCX to PDF.",
                }

    if is_usable_pdf:
        # Check whether existing thumbnails match the expected page count
        existing = sorted(thumb_dir.glob("thumb_*.png")) if thumb_dir.exists() else []
        needs_generation = not existing or (page_count > 0 and len(existing) != page_count)

        if needs_generation:
            try:
                generated: list[str] = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: pdf_processor.generate_thumbnails(
                        effective_path,
                        str(thumb_dir),
                        settings.THUMBNAIL_WIDTH,
                    ),
                )
                page_count = len(generated)

                # Persist updated page_count back to the FileItem (SQLAlchemy tracks the change)
                if file_item.page_count != page_count:
                    file_item.page_count = page_count
                    logger.info(
                        "Updated page_count for FileItem %d to %d (job %s)",
                        file_item.id, page_count, job_id,
                    )

            except PasswordProtectedError:
                error = "This PDF is password-protected and cannot be previewed."
                logger.warning(
                    "Password-protected PDF for FileItem %d (job %s)", file_item.id, job_id
                )
            except CorruptPDFError:
                error = "This PDF appears damaged and cannot be previewed."
                logger.error(
                    "Corrupt PDF for FileItem %d (job %s)", file_item.id, job_id
                )
            except Exception as exc:
                error = "Could not generate preview for this file."
                logger.error(
                    "Thumbnail generation error for FileItem %d (job %s): %s",
                    file_item.id, job_id, exc,
                )
        else:
            # Thumbnails already on disk — derive page_count from existing files
            # if the stored value is still 0 (edge case: restart mid-generation)
            if page_count == 0:
                page_count = len(existing)
                file_item.page_count = page_count

        if not error:
            thumb_urls = [
                f"/api/thumbnail/{job_id}/{file_item.id}/{i}"
                for i in range(page_count)
            ]

    return {
        "id": file_item.id,
        "original_name": file_item.original_name,
        "page_count": page_count,
        "thumb_urls": thumb_urls,
        "conversion_pending": False,
        "error": error,
    }
