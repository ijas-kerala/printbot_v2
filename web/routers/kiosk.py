"""
web/routers/kiosk.py — Kiosk page and Server-Sent Events (SSE) status feed.

Endpoints:
  GET /kiosk         — Serve the fullscreen kiosk HTML page.
                       Restricted to localhost requests only; remote clients
                       receive a 403 so the kiosk is never exposed via the
                       Cloudflare tunnel.

  GET /kiosk/events  — SSE stream consumed by kiosk.js.
                       Emits a 'status' event every 3 seconds with current
                       machine state, and a keepalive comment every 15 seconds
                       to prevent proxy/browser timeouts.

QR code generation (called from web/main.py lifespan):
  generate_qr_code() — Creates static/icons/qr_code.png from settings.TUNNEL_URL.
                       Falls back gracefully if the qrcode library is missing
                       or the TUNNEL_URL is not yet configured.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import AsyncGenerator

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from core.config import settings
from core.database import AsyncSessionLocal
from web.routers.status import get_machine_status_data

logger = logging.getLogger(__name__)

router = APIRouter(tags=["kiosk"])

templates = Jinja2Templates(directory="web/templates")

# Localhost addresses that are allowed to load the kiosk page
_LOCAL_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "::1", "localhost"})

# SSE timing constants
_SSE_STATUS_INTERVAL_S: float = 3.0    # How often to push a status event
_SSE_KEEPALIVE_INTERVAL_S: float = 15.0  # How often to send a keepalive comment


# ── QR code generation (called from lifespan) ─────────────────────────────────

def generate_qr_code() -> None:
    """
    Generate a QR code PNG for settings.TUNNEL_URL and save it to
    settings.QR_CODE_PATH.

    Called once during the FastAPI lifespan startup so the kiosk has a fresh
    QR code without requiring a server restart after TUNNEL_URL changes.

    Does NOT raise — failures are logged as warnings so startup is never blocked
    by a missing qrcode dependency or a misconfigured path.
    """
    try:
        import qrcode  # type: ignore[import]
        from pathlib import Path

        tunnel_url = settings.TUNNEL_URL
        if not tunnel_url or tunnel_url == "https://print.example.com":
            logger.warning(
                "generate_qr_code: TUNNEL_URL is not configured — "
                "skipping QR code generation"
            )
            return

        qr_path = Path(settings.QR_CODE_PATH)
        qr_path.parent.mkdir(parents=True, exist_ok=True)

        img = qrcode.make(tunnel_url)
        img.save(str(qr_path))
        logger.info("generate_qr_code: QR code saved to %s", qr_path)

    except ImportError:
        logger.warning(
            "generate_qr_code: 'qrcode' package not installed — "
            "install with: pip install qrcode[pil]"
        )
    except Exception as exc:
        logger.warning("generate_qr_code: failed to generate QR code: %s", exc)


# ── Kiosk page ────────────────────────────────────────────────────────────────

@router.get("/kiosk", response_class=HTMLResponse)
async def serve_kiosk(request: Request) -> HTMLResponse:
    """
    Serve the fullscreen kiosk page.

    SECURITY: Only accessible from localhost.  The Chromium kiosk service hits
    this via http://localhost:8000/kiosk.  Any request arriving through the
    Cloudflare tunnel (or from another machine on the network) is rejected with
    a 403 so the kiosk UI is never exposed to the public internet.
    """
    client_host: str = request.client.host if request.client else ""

    # SECURITY: block non-local access
    if client_host not in _LOCAL_HOSTS:
        logger.warning(
            "serve_kiosk: refused request from non-local host %r", client_host
        )
        return HTMLResponse(
            content="Kiosk is only accessible from the local machine.",
            status_code=403,
        )

    tunnel_url = settings.TUNNEL_URL
    is_placeholder = (
        not tunnel_url or tunnel_url == "https://print.example.com"
    )

    return templates.TemplateResponse(
        "kiosk.html",
        {
            "request":        request,
            "tunnel_url":     tunnel_url,
            "is_placeholder": is_placeholder,
            "qr_code_path":   settings.QR_CODE_PATH,
        },
    )


# ── SSE event stream ───────────────────────────────────────────────────────────

@router.get("/kiosk/events")
async def kiosk_events(request: Request) -> StreamingResponse:
    """
    Server-Sent Events stream for the kiosk status panel.

    The Chromium kiosk page connects here via EventSource.  This endpoint
    streams machine status updates indefinitely until the client disconnects.

    Event format (SSE spec §9.2):
      event: status
      data: {"state": "...", "current_job_id": "...", ...}

    Keepalive format (SSE comment — ignored by EventSource, but keeps the
    connection alive through nginx/Cloudflare idle-connection timeouts):
      : keepalive

    The generator creates a new DB session for every poll cycle to avoid
    holding a connection open across async sleeps (SQLite + asyncio caveat).
    """
    return StreamingResponse(
        _sse_generator(request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # Disable nginx response buffering
            "Connection": "keep-alive",
        },
    )


async def _sse_generator(request: Request) -> AsyncGenerator[str, None]:
    """
    Async generator that yields SSE-formatted strings.

    Timing logic:
      - Status event every _SSE_STATUS_INTERVAL_S seconds (3s)
      - Keepalive comment every _SSE_KEEPALIVE_INTERVAL_S seconds (15s)

    The keepalive is implemented by counting elapsed time since the last
    keepalive rather than a separate timer task, keeping the generator simple.
    """
    last_keepalive = time.monotonic()

    try:
        while True:
            # ── Check for client disconnect before doing any work ──────────────
            if await request.is_disconnected():
                logger.debug("kiosk SSE: client disconnected — stopping generator")
                break

            # ── Fetch machine status with a fresh DB session each cycle ────────
            try:
                async with AsyncSessionLocal() as db:
                    status_data = await get_machine_status_data(db)
            except Exception as exc:
                logger.error("kiosk SSE: error fetching machine status: %s", exc)
                # FALLBACK: emit an error state so the kiosk doesn't go blank
                status_data = {
                    "state":          "error",
                    "current_job_id": None,
                    "queue_length":   0,
                    "printer_status": {"state": 0, "message": "unavailable", "state_reasons": []},
                }

            # Emit the status event
            payload = json.dumps(status_data)
            yield f"event: status\ndata: {payload}\n\n"

            # ── Keepalive comment (every 15s) ──────────────────────────────────
            now = time.monotonic()
            if now - last_keepalive >= _SSE_KEEPALIVE_INTERVAL_S:
                yield ": keepalive\n\n"
                last_keepalive = now

            # ── Sleep until the next status push ──────────────────────────────
            try:
                await asyncio.sleep(_SSE_STATUS_INTERVAL_S)
            except asyncio.CancelledError:
                # Client disconnected while we were sleeping
                logger.debug("kiosk SSE: sleep cancelled — client disconnected")
                break

    except asyncio.CancelledError:
        # Generator task was cancelled by the ASGI server on disconnect
        logger.debug("kiosk SSE: generator cancelled")
    except Exception as exc:
        logger.error("kiosk SSE: unhandled error in generator: %s", exc, exc_info=True)
