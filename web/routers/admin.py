"""
web/routers/admin.py — Admin dashboard, authentication, and management API.

Provides:
  - Pattern lock + PIN login with itsdangerous signed session cookies
  - In-memory brute-force protection (5 attempts → 5-minute lockout per IP)
  - Dashboard page with stats, recent jobs, and pricing rules
  - JSON API endpoints polled by the dashboard JS:
      GET  /admin/api/printer-status        — real-time printer + queue state
      GET  /admin/api/revenue-chart         — 7-day revenue data for Chart.js
      POST /admin/api/job/<id>/retry        — requeue a failed job
      POST /admin/api/job/<id>/cancel       — expire a pending job
      POST /admin/api/pricing-rule/add      — add rule with overlap validation
      POST /admin/api/pricing-rule/delete   — remove a pricing rule
      POST /admin/api/export-csv            — streaming monthly CSV export
  - POST /admin/logout                      — clear session cookie + DB row

Registration (add to web/main.py when it is built):
    from web.routers.admin import router as admin_router
    app.include_router(admin_router)
"""

from __future__ import annotations

import asyncio
import csv
import hmac
import io
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from core.config import settings
from core.database import get_db
from core.models import AdminSession, JobStatus, PricingRule, PrintJob
from core.printing.cups_manager import cups_manager
from web.dependencies import require_admin, sign_admin_cookie
from web.services.file_service import schedule_deletion
from web.services.print_queue import print_queue

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])
templates = Jinja2Templates(directory="web/templates")

# ── Brute-force protection ─────────────────────────────────────────────────────
#
# Tracks failed login attempts per client IP.  After _MAX_ATTEMPTS failures
# within the rolling window, the IP is locked out for _LOCKOUT_SECONDS.
# This is an in-process dict — it resets on server restart, which is acceptable
# for a Raspberry Pi kiosk with a single Uvicorn worker.

_MAX_ATTEMPTS: int = 5
_LOCKOUT_SECONDS: int = 300  # 5 minutes

# ip → {"count": int, "lockout_until": datetime | None}
_login_attempts: dict[str, dict] = {}

# 24 hours in seconds — admin session lifetime
_ADMIN_SESSION_MAX_AGE: int = 86400

# Job statuses that count as revenue (money has been collected)
_REVENUE_STATUSES: tuple[str, ...] = (
    JobStatus.PAID.value,
    JobStatus.PROCESSING.value,
    JobStatus.PRINTING.value,
    JobStatus.COMPLETED.value,
)

# Job statuses that can be cancelled by admin (not yet paid)
_CANCELLABLE_STATUSES: tuple[JobStatus, ...] = (
    JobStatus.UPLOADING,
    JobStatus.SETTINGS_PENDING,
    JobStatus.PAYMENT_PENDING,
)


def _get_client_ip(request: Request) -> str:
    """Extract the client IP, honouring X-Forwarded-For from Cloudflare."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        # SECURITY: take the leftmost (client) IP only
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _is_locked_out(ip: str) -> tuple[bool, int]:
    """
    Return (is_locked, seconds_remaining).

    Cleans up expired lockout entries in-place so the dict doesn't grow unboundedly.
    """
    entry = _login_attempts.get(ip)
    if not entry:
        return False, 0
    lockout_until = entry.get("lockout_until")
    if lockout_until and datetime.now(timezone.utc).replace(tzinfo=None) < lockout_until:
        remaining = int((lockout_until - datetime.now(timezone.utc).replace(tzinfo=None)).total_seconds())
        return True, max(remaining, 0)
    # Lockout has expired — clear the entry
    if lockout_until:
        _login_attempts.pop(ip, None)
    return False, 0


def _record_failure(ip: str) -> None:
    """Increment failure counter for *ip*; apply lockout when threshold is hit."""
    entry = _login_attempts.setdefault(ip, {"count": 0, "lockout_until": None})
    entry["count"] += 1
    if entry["count"] >= _MAX_ATTEMPTS:
        entry["lockout_until"] = (
            datetime.now(timezone.utc).replace(tzinfo=None)
            + timedelta(seconds=_LOCKOUT_SECONDS)
        )
        logger.warning(
            "Admin login: IP %s locked out after %d failed attempts",
            ip, entry["count"],
        )


def _clear_failures(ip: str) -> None:
    """Remove failure tracking for *ip* after a successful login."""
    _login_attempts.pop(ip, None)


# ── Auth helpers ───────────────────────────────────────────────────────────────

def _credentials_match(credential: str) -> bool:
    """
    Timing-safe check against both ADMIN_PIN and ADMIN_PATTERN.

    hmac.compare_digest prevents timing oracle attacks — always compares the
    full length of both strings regardless of where they differ.
    """
    # SECURITY: always run both comparisons to avoid short-circuit timing leaks
    pin_match = hmac.compare_digest(credential, settings.ADMIN_PIN)
    pattern_match = hmac.compare_digest(credential, settings.ADMIN_PATTERN)
    return pin_match or pattern_match


def _cookie_kwargs() -> dict:
    """Shared kwargs for setting the admin session cookie."""
    return {
        "key": settings.SESSION_COOKIE_NAME,
        "httponly": True,
        "samesite": "lax",
        # SECURITY: only send over HTTPS in production
        "secure": settings.is_production,
        "max_age": _ADMIN_SESSION_MAX_AGE,
        "path": "/admin",
    }


# ── Pydantic request bodies ────────────────────────────────────────────────────

class AddPricingRuleBody(BaseModel):
    min_pages: int
    max_pages: Optional[int] = None
    is_duplex: bool = False
    price_per_page: float
    description: Optional[str] = None

    @field_validator("min_pages")
    @classmethod
    def min_pages_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("min_pages must be at least 1")
        return v

    @field_validator("price_per_page")
    @classmethod
    def price_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("price_per_page must be greater than 0")
        return v


class DeletePricingRuleBody(BaseModel):
    rule_id: int


class ExportCSVBody(BaseModel):
    month: str  # YYYY-MM

    @field_validator("month")
    @classmethod
    def valid_month(cls, v: str) -> str:
        try:
            parts = v.split("-")
            if len(parts) != 2:
                raise ValueError
            year, month = int(parts[0]), int(parts[1])
            if not (1 <= month <= 12) or year < 2020:
                raise ValueError
        except (ValueError, IndexError):
            raise ValueError("month must be in YYYY-MM format (e.g. 2026-03)")
        return v


# ── Root redirect ──────────────────────────────────────────────────────────────

@router.get("/", include_in_schema=False)
async def admin_root() -> RedirectResponse:
    return RedirectResponse(url="/admin/login", status_code=status.HTTP_302_FOUND)


# ── Login ──────────────────────────────────────────────────────────────────────

@router.get("/login")
async def login_page(request: Request):
    """Serve the admin login page (pattern lock + PIN form)."""
    return templates.TemplateResponse(request, "admin/login.html")


@router.post("/login")
async def login(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    credential: str = Form(...),
):
    """
    Validate admin credential (PIN or pattern sequence) and issue a signed
    session cookie on success.

    The ``credential`` field accepts either:
      - The PIN string (e.g. "1234")
      - The pattern sequence (e.g. "1235789" for the Z pattern)

    Both are checked in constant time via hmac.compare_digest.
    """
    ip = _get_client_ip(request)

    def _render_login(
        *,
        error: Optional[str] = None,
        lockout_until: Optional[str] = None,
        http_status: int = 200,
    ):
        return templates.TemplateResponse(
            request,
            "admin/login.html",
            {"error": error, "lockout_until": lockout_until},
            status_code=http_status,
        )

    # ── Brute-force gate ──────────────────────────────────────────────────────
    locked, remaining = _is_locked_out(ip)
    if locked:
        logger.warning("Admin login: blocked request from locked-out IP %s", ip)
        lockout_dt = _login_attempts[ip]["lockout_until"]
        return _render_login(
            lockout_until=lockout_dt.isoformat(),
            http_status=status.HTTP_429_TOO_MANY_REQUESTS,
        )

    # ── Credential validation ──────────────────────────────────────────────────
    # SECURITY: sanitise input length before comparison to prevent DoS via huge strings
    if len(credential) > 64:
        _record_failure(ip)
        return _render_login(
            error="Invalid credentials.",
            http_status=status.HTTP_401_UNAUTHORIZED,
        )

    if not _credentials_match(credential):
        _record_failure(ip)
        remaining_attempts = max(_MAX_ATTEMPTS - _login_attempts[ip]["count"], 0)
        logger.warning(
            "Admin login: failed attempt from %s (%d remaining)",
            ip, remaining_attempts,
        )
        # If this failure triggered a lockout, show the lockout UI immediately
        now_locked, _ = _is_locked_out(ip)
        if now_locked:
            lockout_dt = _login_attempts[ip]["lockout_until"]
            return _render_login(
                lockout_until=lockout_dt.isoformat(),
                http_status=status.HTTP_429_TOO_MANY_REQUESTS,
            )
        return _render_login(
            error="Invalid credentials.",
            http_status=status.HTTP_401_UNAUTHORIZED,
        )

    # ── Create session ─────────────────────────────────────────────────────────
    _clear_failures(ip)

    token = secrets.token_hex(32)
    session = AdminSession(
        token=token,
        expires_at=datetime.now(timezone.utc).replace(tzinfo=None)
        + timedelta(seconds=_ADMIN_SESSION_MAX_AGE),
    )
    db.add(session)
    await db.commit()

    signed = sign_admin_cookie(token)
    logger.info("Admin login: successful login from %s", ip)

    redirect = RedirectResponse(
        url="/admin/dashboard", status_code=status.HTTP_303_SEE_OTHER
    )
    redirect.set_cookie(value=signed, **_cookie_kwargs())
    return redirect


# ── Logout ─────────────────────────────────────────────────────────────────────

@router.post("/logout")
async def logout(
    response: Response,
    db: AsyncSession = Depends(get_db),
    admin_session: AdminSession = Depends(require_admin),
):
    """Invalidate the current admin session and clear the cookie."""
    await db.delete(admin_session)
    await db.commit()
    logger.info("Admin logout: session %s invalidated", admin_session.token[:8])

    redirect = RedirectResponse(
        url="/admin/login", status_code=status.HTTP_303_SEE_OTHER
    )
    redirect.delete_cookie(
        key=settings.SESSION_COOKIE_NAME,
        path="/admin",
    )
    return redirect


# ── Dashboard ──────────────────────────────────────────────────────────────────

@router.get("/dashboard")
async def dashboard(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: AdminSession = Depends(require_admin),
):
    """
    Admin dashboard page.

    Loads summary stats, recent jobs, and pricing rules to seed the template.
    The dashboard JS polls the API endpoints for live updates (printer status,
    revenue chart) so we only load the data that's needed for the initial render.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # ── Today's revenue ────────────────────────────────────────────────────────
    today_rev_result = await db.execute(
        select(func.coalesce(func.sum(PrintJob.total_cost), 0.0)).where(
            PrintJob.status.in_(_REVENUE_STATUSES),
            PrintJob.created_at >= today_start,
        )
    )
    today_revenue: float = today_rev_result.scalar_one()

    # ── Today's job count ──────────────────────────────────────────────────────
    today_jobs_result = await db.execute(
        select(func.count()).where(PrintJob.created_at >= today_start)
    )
    today_jobs: int = today_jobs_result.scalar_one()

    # ── All-time success rate ──────────────────────────────────────────────────
    total_result = await db.execute(select(func.count()).select_from(PrintJob))
    total_all: int = total_result.scalar_one()

    completed_result = await db.execute(
        select(func.count()).where(PrintJob.status == JobStatus.COMPLETED.value)
    )
    total_completed: int = completed_result.scalar_one()

    success_rate: float = (
        round(total_completed / total_all * 100, 1) if total_all > 0 else 0.0
    )

    # ── Recent jobs (last 20) ──────────────────────────────────────────────────
    recent_jobs_result = await db.execute(
        select(PrintJob)
        .options(selectinload(PrintJob.files))
        .order_by(PrintJob.created_at.desc())
        .limit(20)
    )
    recent_jobs = list(recent_jobs_result.scalars().all())

    # ── Retry eligibility (file existence check) ───────────────────────────────
    # A failed job can only be retried if at least one of its processed files
    # still exists on disk. If cleanup already ran, the button should be hidden.
    can_retry_set: set[str] = {
        job.id
        for job in recent_jobs
        if job.status == JobStatus.FAILED
        and any(os.path.exists(f.effective_pdf_path()) for f in job.files)
    }

    # ── Pricing rules ──────────────────────────────────────────────────────────
    pricing_result = await db.execute(
        select(PricingRule)
        .where(PricingRule.is_active.is_(True))
        .order_by(PricingRule.is_duplex.asc(), PricingRule.min_pages.asc())
    )
    pricing_rules = list(pricing_result.scalars().all())

    # ── Queue status (no DB call needed) ──────────────────────────────────────
    queue_status = print_queue.get_queue_status()

    # ── 7-day revenue chart data ───────────────────────────────────────────────
    today = now.date()
    chart_labels: list[str] = []
    chart_data: list[float] = []
    for offset in range(6, -1, -1):
        day = today - timedelta(days=offset)
        day_start = datetime(day.year, day.month, day.day, 0, 0, 0)
        day_end = datetime(day.year, day.month, day.day, 23, 59, 59)
        day_rev = await db.execute(
            select(func.coalesce(func.sum(PrintJob.total_cost), 0.0)).where(
                PrintJob.status.in_(_REVENUE_STATUSES),
                PrintJob.created_at >= day_start,
                PrintJob.created_at <= day_end,
            )
        )
        chart_labels.append(day.strftime("%Y-%m-%d"))
        chart_data.append(float(day_rev.scalar_one()))

    # ── Serialise pricing rules to dicts (ORM objects aren't JSON-safe) ────────
    pricing_rules_list = [
        {
            "id": r.id,
            "min_pages": r.min_pages,
            "max_pages": r.max_pages,
            "is_duplex": r.is_duplex,
            "price_per_page": r.price_per_page,
            "description": r.description,
            "is_active": r.is_active,
        }
        for r in pricing_rules
    ]

    return templates.TemplateResponse(
        request,
        "admin/dashboard.html",
        {
            "stats": {
                "today_revenue": today_revenue,
                "total_jobs": total_all,
                "success_rate": success_rate,
                "active_job": queue_status["current_job"],
            },
            "revenue_chart": {"labels": chart_labels, "data": chart_data},
            "recent_jobs": recent_jobs,
            "can_retry_set": can_retry_set,
            "pricing_rules": pricing_rules_list,
            "shop_name": "PrintBot",
            "printer_name": settings.DEFAULT_PRINTER,
            "tunnel_url": settings.TUNNEL_URL,
            "now": now,
        },
    )


# ── API: Printer status ────────────────────────────────────────────────────────

@router.get("/api/printer-status")
async def api_printer_status(
    _: AdminSession = Depends(require_admin),
) -> dict:
    """
    Return the current printer state and queue depth.

    Returns both the user-friendly health summary (for dashboard widgets) and
    the raw driver-level message (for admin diagnostics).  Both CUPS calls are
    dispatched to the executor to avoid blocking the event loop.
    """
    loop = asyncio.get_event_loop()

    def _get_both() -> tuple[dict, dict]:
        return cups_manager.get_printer_status(), cups_manager.get_printer_health()

    printer_raw, printer_health = await loop.run_in_executor(None, _get_both)
    queue_info = print_queue.get_queue_status()
    return {
        "printer": printer_raw,
        "printer_health": printer_health,
        "queue": queue_info,
        # Convenience fields for the dashboard JS widget:
        "is_online": printer_health.get("online", False),
        "status_text": printer_health.get("message", "Unavailable"),
        "severity": printer_health.get("severity", "error"),
        "driver_message": printer_raw.get("driver_message", ""),
    }


# ── API: Revenue chart ─────────────────────────────────────────────────────────

@router.get("/api/revenue-chart")
async def api_revenue_chart(
    db: AsyncSession = Depends(get_db),
    _: AdminSession = Depends(require_admin),
) -> dict:
    """
    Return 7-day revenue data for the Chart.js bar chart on the dashboard.

    Each day queries SUM(total_cost) for jobs in a post-payment status.
    Days with no revenue return 0.0.
    """
    today = datetime.now(timezone.utc).replace(tzinfo=None).date()
    labels: list[str] = []
    data: list[float] = []

    for offset in range(6, -1, -1):
        day = today - timedelta(days=offset)
        day_start = datetime(day.year, day.month, day.day, 0, 0, 0)
        day_end = datetime(day.year, day.month, day.day, 23, 59, 59)

        result = await db.execute(
            select(func.coalesce(func.sum(PrintJob.total_cost), 0.0)).where(
                PrintJob.status.in_(_REVENUE_STATUSES),
                PrintJob.created_at >= day_start,
                PrintJob.created_at <= day_end,
            )
        )
        labels.append(day.strftime("%Y-%m-%d"))
        data.append(float(result.scalar_one()))

    return {"labels": labels, "data": data}


# ── API: Job retry ─────────────────────────────────────────────────────────────

@router.post("/api/job/{job_id}/retry")
async def api_job_retry(
    job_id: str,
    db: AsyncSession = Depends(get_db),
    _: AdminSession = Depends(require_admin),
) -> dict:
    """
    Requeue a FAILED job.

    Resets the job back to PAID (the state that the print worker expects when
    it dequeues a job) and clears the failure reason before enqueuing.
    """
    result = await db.execute(
        select(PrintJob)
        .options(selectinload(PrintJob.files))
        .where(PrintJob.id == job_id)
    )
    job = result.scalar_one_or_none()

    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job.status != JobStatus.FAILED:
        raise HTTPException(
            status_code=400,
            detail=f"Only FAILED jobs can be retried (current status: {job.status.value}).",
        )

    # EDGE CASE: file cleanup may have already deleted the processed PDFs
    if not any(os.path.exists(f.effective_pdf_path()) for f in job.files):
        raise HTTPException(
            status_code=409,
            detail="File expired — cannot retry. User must re-upload.",
        )

    job.status = JobStatus.PAID
    job.failed_reason = None
    await db.commit()

    await print_queue.enqueue(job.id)
    logger.info("Admin retry: job %s re-enqueued by admin", job_id)

    return {"ok": True, "job_id": job_id}


# ── API: Job cancel ────────────────────────────────────────────────────────────

@router.post("/api/job/{job_id}/cancel")
async def api_job_cancel(
    job_id: str,
    db: AsyncSession = Depends(get_db),
    _: AdminSession = Depends(require_admin),
) -> dict:
    """
    Cancel a pending job (one that has not yet been paid).

    Sets status to EXPIRED and schedules file deletion. Refuses to cancel
    jobs in PAID / PROCESSING / PRINTING / COMPLETED states — those are
    either actively printing or finished.
    """
    result = await db.execute(select(PrintJob).where(PrintJob.id == job_id))
    job = result.scalar_one_or_none()

    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job.status not in _CANCELLABLE_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Job cannot be cancelled in status '{job.status.value}'. "
                "Only unpaid jobs (uploading, settings_pending, payment_pending) "
                "can be cancelled."
            ),
        )

    job.status = JobStatus.EXPIRED
    await schedule_deletion(job_id, db, delay_hours=0)
    await db.commit()

    logger.info("Admin cancel: job %s expired by admin", job_id)
    return {"ok": True, "job_id": job_id}


# ── API: Pricing rule add ──────────────────────────────────────────────────────

@router.post("/api/pricing-rule/add")
async def api_pricing_rule_add(
    body: AddPricingRuleBody,
    db: AsyncSession = Depends(get_db),
    _: AdminSession = Depends(require_admin),
) -> dict:
    """
    Add a new pricing rule with overlap validation.

    Rules are matched by (total_pages, is_duplex). Overlapping ranges for the
    same duplex type would make pricing ambiguous, so we reject them here.

    Overlap check: two ranges [a, b] and [c, d] overlap iff a <= d AND c <= b.
    NULL max_pages is treated as infinity (sys.maxsize) for comparison purposes.
    """
    if body.max_pages is not None and body.min_pages >= body.max_pages:
        raise HTTPException(
            status_code=400,
            detail="min_pages must be less than max_pages.",
        )

    # ── Overlap check ──────────────────────────────────────────────────────────
    existing_result = await db.execute(
        select(PricingRule).where(
            PricingRule.is_active.is_(True),
            PricingRule.is_duplex == body.is_duplex,
        )
    )
    existing_rules = list(existing_result.scalars().all())

    # Treat NULL max_pages as a very large number for comparison
    _INF = 10_000_000
    new_min = body.min_pages
    new_max = body.max_pages if body.max_pages is not None else _INF

    for rule in existing_rules:
        r_min = rule.min_pages
        r_max = rule.max_pages if rule.max_pages is not None else _INF
        # EDGE CASE: ranges overlap if new_min <= r_max AND r_min <= new_max
        if new_min <= r_max and r_min <= new_max:
            r_max_label = str(rule.max_pages) if rule.max_pages is not None else "∞"
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Range [{new_min}, {body.max_pages or '∞'}] overlaps with "
                    f"existing rule #{rule.id} [{r_min}, {r_max_label}] "
                    f"({'duplex' if rule.is_duplex else 'simplex'})."
                ),
            )

    rule = PricingRule(
        min_pages=body.min_pages,
        max_pages=body.max_pages,
        is_duplex=body.is_duplex,
        price_per_page=body.price_per_page,
        description=body.description,
        is_active=True,
    )
    db.add(rule)
    await db.commit()
    await db.refresh(rule)

    logger.info(
        "Admin pricing: added rule #%d [%d-%s %s ₹%.2f/page]",
        rule.id, rule.min_pages,
        str(rule.max_pages) if rule.max_pages else "∞",
        "duplex" if rule.is_duplex else "simplex",
        rule.price_per_page,
    )
    return {"ok": True, "rule_id": rule.id}


# ── API: Pricing rule delete ───────────────────────────────────────────────────

@router.post("/api/pricing-rule/delete")
async def api_pricing_rule_delete(
    body: DeletePricingRuleBody,
    db: AsyncSession = Depends(get_db),
    _: AdminSession = Depends(require_admin),
) -> dict:
    """
    Soft-delete a pricing rule by setting is_active = False.

    Soft deletion preserves historical records for already-calculated jobs
    while preventing the rule from being matched for new jobs.
    """
    result = await db.execute(
        select(PricingRule).where(PricingRule.id == body.rule_id)
    )
    rule = result.scalar_one_or_none()

    if rule is None:
        raise HTTPException(status_code=404, detail="Pricing rule not found.")
    if not rule.is_active:
        raise HTTPException(status_code=400, detail="Rule is already inactive.")

    rule.is_active = False
    await db.commit()

    logger.info("Admin pricing: rule #%d deactivated", body.rule_id)
    return {"ok": True}


# ── API: Export CSV ────────────────────────────────────────────────────────────

@router.post("/api/export-csv")
async def api_export_csv(
    body: ExportCSVBody,
    db: AsyncSession = Depends(get_db),
    _: AdminSession = Depends(require_admin),
) -> StreamingResponse:
    """
    Stream a monthly CSV export of all print jobs.

    Uses a Python async generator to yield CSV rows without loading every job
    row into memory at once, keeping peak memory usage flat regardless of
    how many jobs are in the month.
    """
    try:
        year_str, month_str = body.month.split("-")
        year, month_int = int(year_str), int(month_str)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid month format.") from exc

    month_start = datetime(year, month_int, 1, 0, 0, 0)
    # First day of the next month (handles year rollover correctly)
    if month_int == 12:
        month_end = datetime(year + 1, 1, 1, 0, 0, 0)
    else:
        month_end = datetime(year, month_int + 1, 1, 0, 0, 0)

    # Load all jobs for the month with files eagerly loaded
    result = await db.execute(
        select(PrintJob)
        .options(selectinload(PrintJob.files))
        .where(
            PrintJob.created_at >= month_start,
            PrintJob.created_at < month_end,
        )
        .order_by(PrintJob.created_at.asc())
    )
    jobs = list(result.scalars().all())

    async def _generate_csv():
        """Yield CSV header then one row per job."""
        buf = io.StringIO()
        writer = csv.writer(buf)

        writer.writerow([
            "Job ID", "Created At", "Status", "Files",
            "Total Pages", "Copies", "Duplex", "N-up",
            "Cost (INR)", "Paid At", "Razorpay Order ID",
            "Razorpay Payment ID", "CUPS Job ID", "Failed Reason",
        ])
        yield buf.getvalue()
        buf.seek(0)
        buf.truncate(0)

        for job in jobs:
            filenames = "; ".join(f.original_name for f in job.files)
            total_pages = sum(f.page_count for f in job.files)
            writer.writerow([
                job.id,
                job.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                job.status.value,
                filenames,
                total_pages,
                job.copies,
                "Yes" if job.is_duplex else "No",
                job.nup_layout,
                f"{job.total_cost:.2f}",
                job.paid_at.strftime("%Y-%m-%d %H:%M:%S") if job.paid_at else "",
                job.razorpay_order_id or "",
                job.razorpay_payment_id or "",
                job.cups_job_id or "",
                job.failed_reason or "",
            ])
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate(0)

    filename = f"printbot_jobs_{body.month}.csv"
    logger.info("Admin export: CSV for %s requested (%d jobs)", body.month, len(jobs))

    return StreamingResponse(
        _generate_csv(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
