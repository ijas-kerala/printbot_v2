"""
web/dependencies.py — Shared FastAPI dependency functions for PrintBot v3.

Contains:
  - get_db: re-exported async DB session generator (from core.database)
  - Job session helpers: sign/verify the pb_session cookie that ties a browser
    to its PrintJob for the duration of the upload → settings → payment flow.
  - get_job_from_session / require_job_session: FastAPI dependencies used by
    settings, payment, and status routers to validate ownership.
  - Admin session helpers: sign/verify the pb_admin cookie for admin login.
  - require_admin: FastAPI dependency that enforces admin authentication.

Session cookie design:
  Cookie name : settings.JOB_SESSION_COOKIE_NAME  ("pb_session")
  Cookie value: itsdangerous TimestampSigner.sign(job_id)  (base64-URL safe)
  Max-age     : settings.JOB_SESSION_MAX_AGE seconds (default 1800 = 30 min)

  The signature proves the server issued the cookie for exactly this job_id.
  PrintJob.session_token is a random hex token stored in the DB that lets us
  invalidate a session server-side (e.g. after the job is paid).

Admin session cookie design:
  Cookie name : settings.SESSION_COOKIE_NAME  ("pb_admin")
  Cookie value: itsdangerous TimestampSigner.sign(token)  (base64-URL safe)
  Max-age     : 86400 seconds (24 hours)

  Token is stored in the admin_sessions table, allowing server-side invalidation.
  Dual-layer check: cookie signature validity + DB row existence + expiry timestamp.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import Cookie, Depends, HTTPException, Response, status
from itsdangerous import BadSignature, SignatureExpired, TimestampSigner
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.database import get_db  # noqa: F401 — re-exported for caller convenience
from core.models import AdminSession, PrintJob

logger = logging.getLogger(__name__)

# 24 hours in seconds — max age for admin session cookies
_ADMIN_SESSION_MAX_AGE: int = 86400


# ── Cookie signing helpers ─────────────────────────────────────────────────────

def _make_job_signer() -> TimestampSigner:
    # SECURITY: uses JOB_SESSION_SECRET, distinct from the admin session secret
    return TimestampSigner(settings.JOB_SESSION_SECRET, sep=".")


def sign_job_cookie(job_id: str) -> str:
    """
    Return the signed cookie value for a job session.

    The value is an ASCII string safe to embed directly in a Set-Cookie header.
    """
    return _make_job_signer().sign(job_id).decode("ascii")


def verify_job_cookie(cookie_value: str) -> Optional[str]:
    """
    Verify the pb_session cookie signature and expiry.

    Returns the plain ``job_id`` string on success, or ``None`` if the
    signature is invalid, the token has expired, or the input is malformed.

    Never raises — designed to be used in both strict and lenient contexts.
    """
    try:
        raw = _make_job_signer().unsign(
            cookie_value, max_age=settings.JOB_SESSION_MAX_AGE
        )
        return raw.decode("ascii")
    except SignatureExpired:
        logger.debug("pb_session cookie expired")
        return None
    except BadSignature:
        # SECURITY: log but don't reveal details to the caller
        logger.warning("pb_session cookie had an invalid signature — possible tampering")
        return None
    except Exception as exc:
        logger.error("Unexpected error verifying pb_session cookie: %s", exc)
        return None


# ── FastAPI dependencies ───────────────────────────────────────────────────────

async def get_job_from_session(
    pb_session: Optional[str] = Cookie(default=None, alias="pb_session"),
    db: AsyncSession = Depends(get_db),
) -> Optional[PrintJob]:
    """
    Lenient dependency: parse the pb_session cookie and return the matching
    PrintJob, or ``None`` if the cookie is absent, invalid, or expired.

    Use on pages that want to *show* session context without hard-failing
    (e.g. the upload landing page showing an active-session warning).
    """
    if not pb_session:
        return None

    job_id = verify_job_cookie(pb_session)
    if not job_id:
        return None

    result = await db.execute(select(PrintJob).where(PrintJob.id == job_id))
    return result.scalar_one_or_none()


async def require_job_session(
    pb_session: Optional[str] = Cookie(default=None, alias="pb_session"),
    db: AsyncSession = Depends(get_db),
) -> PrintJob:
    """
    Strict dependency: raises HTTP 403 if the session cookie is absent,
    invalid, expired, or does not match any PrintJob in the DB.

    Use on settings / payment routes that must only be accessible to the
    browser that created the job.
    """
    if not pb_session:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No active print session. Please upload your files first.",
        )

    job_id = verify_job_cookie(pb_session)
    if not job_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your session has expired. Please start a new upload.",
        )

    result = await db.execute(select(PrintJob).where(PrintJob.id == job_id))
    job = result.scalar_one_or_none()
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Session references an unknown job. Please start a new upload.",
        )

    return job


# ── Admin session helpers ──────────────────────────────────────────────────────

def _make_admin_signer() -> TimestampSigner:
    # SECURITY: uses ADMIN_SESSION_SECRET, distinct from the job session secret
    return TimestampSigner(settings.ADMIN_SESSION_SECRET, sep=".")


def sign_admin_cookie(token: str) -> str:
    """
    Return the signed cookie value for an admin session token.

    The value is ASCII-safe for direct use in a Set-Cookie header.
    """
    return _make_admin_signer().sign(token).decode("ascii")


def verify_admin_cookie(cookie_value: str) -> Optional[str]:
    """
    Verify the pb_admin cookie signature and 24h expiry.

    Returns the plain ``token`` string on success, or ``None`` if the
    signature is invalid, the token has expired, or the input is malformed.

    Never raises — safe to call in both strict and lenient contexts.
    """
    try:
        raw = _make_admin_signer().unsign(
            cookie_value, max_age=_ADMIN_SESSION_MAX_AGE
        )
        return raw.decode("ascii")
    except SignatureExpired:
        logger.debug("pb_admin cookie expired")
        return None
    except BadSignature:
        # SECURITY: log but never reveal tamper details to the caller
        logger.warning("pb_admin cookie had an invalid signature — possible tampering")
        return None
    except Exception as exc:
        logger.error("Unexpected error verifying pb_admin cookie: %s", exc)
        return None


async def require_admin(
    response: Response,
    pb_admin: Optional[str] = Cookie(default=None, alias="pb_admin"),
    db: AsyncSession = Depends(get_db),
) -> AdminSession:
    """
    Strict admin dependency: validates the pb_admin cookie and returns the
    active AdminSession.

    Validation is intentionally dual-layer:
      1. itsdangerous signature + max_age check (tamper / expiry protection)
      2. DB row existence + AdminSession.is_expired() (server-side invalidation)

    On any failure, clears the stale cookie and redirects to /admin/login
    via HTTP 303. FastAPI raises this as an HTTPException so middleware and
    exception handlers can intercept it.
    """
    _redirect = HTTPException(
        status_code=status.HTTP_303_SEE_OTHER,
        detail="Admin session required",
        headers={"Location": "/admin/login"},
    )

    if not pb_admin:
        raise _redirect

    token = verify_admin_cookie(pb_admin)
    if not token:
        # SECURITY: clear the invalid cookie so the browser stops sending it
        response.delete_cookie(settings.SESSION_COOKIE_NAME)
        raise _redirect

    result = await db.execute(
        select(AdminSession).where(AdminSession.token == token)
    )
    session = result.scalar_one_or_none()

    if session is None or session.is_expired():
        response.delete_cookie(settings.SESSION_COOKIE_NAME)
        if session is not None:
            # EDGE CASE: expired row still in DB — clean it up proactively
            await db.delete(session)
            await db.commit()
        raise _redirect

    return session
