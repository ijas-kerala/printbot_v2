"""
web/main.py — FastAPI application factory and lifespan for PrintBot v3.

Startup sequence (in order):
  1. Create upload and log directories
  2. Run Alembic migrations (alembic upgrade head)
  3. Generate QR code from TUNNEL_URL
  4. Requeue any interrupted print jobs from before the last restart
  5. Start the print queue worker as a background asyncio task
  6. Start the periodic cleanup task (every 30 min)

Shutdown:
  - Worker and cleanup tasks are cancelled gracefully.

Routers included:
  upload, settings, payment, webhooks, status, admin, kiosk
"""

from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from core.config import settings
from core.database import AsyncSessionLocal

logger = logging.getLogger(__name__)

# ── Cleanup interval ───────────────────────────────────────────────────────────
_CLEANUP_INTERVAL_SECONDS: int = 30 * 60  # 30 minutes


# ── Lifespan ───────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    Manage startup and shutdown tasks for the PrintBot FastAPI application.

    All startup steps are non-fatal except a DB migration failure — if Alembic
    exits non-zero we log the error but continue (the existing schema may still
    be valid from a previous migration run).
    """
    # ── 1. Create runtime directories ─────────────────────────────────────────
    Path(settings.UPLOAD_DIR).mkdir(parents=True, exist_ok=True)
    Path("logs").mkdir(exist_ok=True)
    logger.info("Runtime directories ready (uploads=%s)", settings.UPLOAD_DIR)

    # ── 2. Run Alembic migrations ──────────────────────────────────────────────
    await _run_migrations()

    # ── 3. Generate QR code ────────────────────────────────────────────────────
    # Import here to avoid circular imports at module load time.
    from web.routers.kiosk import generate_qr_code  # noqa: PLC0415
    generate_qr_code()

    # ── 4. Requeue interrupted jobs ────────────────────────────────────────────
    from web.services.print_queue import print_queue  # noqa: PLC0415
    await print_queue.requeue_interrupted_jobs()

    # ── 5. Start print queue worker ────────────────────────────────────────────
    worker_task = asyncio.create_task(print_queue.worker(), name="print_queue_worker")
    logger.info("Print queue worker task started")

    # ── 6. Start periodic cleanup task ────────────────────────────────────────
    cleanup_task = asyncio.create_task(_cleanup_loop(), name="cleanup_loop")
    logger.info("Periodic cleanup task started (interval=%ds)", _CLEANUP_INTERVAL_SECONDS)

    yield  # Server is running

    # ── Shutdown ──────────────────────────────────────────────────────────────
    logger.info("Shutting down background tasks...")
    for task in (worker_task, cleanup_task):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    logger.info("PrintBot shutdown complete")


# ── Helpers ────────────────────────────────────────────────────────────────────

async def _run_migrations() -> None:
    """
    Shell out to `alembic upgrade head` to apply any pending migrations.

    Uses asyncio.create_subprocess_exec so the event loop is not blocked.
    Logs stdout/stderr at DEBUG/WARNING level respectively.
    Non-zero exit is logged as an error but does not abort startup.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "alembic", "upgrade", "head",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if stdout:
            logger.debug("alembic: %s", stdout.decode().strip())
        if stderr:
            # Alembic writes informational lines to stderr; only treat non-zero
            # exit as a real error.
            level = logging.ERROR if proc.returncode != 0 else logging.DEBUG
            logger.log(level, "alembic stderr: %s", stderr.decode().strip())

        if proc.returncode == 0:
            logger.info("Alembic migrations applied successfully")
        else:
            logger.error(
                "Alembic exited with code %d — the schema may be out of date",
                proc.returncode,
            )
    except Exception as exc:
        logger.error("Failed to run Alembic migrations: %s", exc, exc_info=True)


async def _cleanup_loop() -> None:
    """
    Periodic task: sweep expired jobs every _CLEANUP_INTERVAL_SECONDS.

    Runs immediately on first call so orphaned files from previous sessions are
    cleared at startup, then waits the full interval between subsequent runs.
    """
    from web.services.file_service import cleanup_expired_jobs  # noqa: PLC0415

    while True:
        try:
            async with AsyncSessionLocal() as db:
                deleted = await cleanup_expired_jobs(db)
                await db.commit()
            if deleted:
                logger.info("Cleanup sweep: removed %d expired job(s)", deleted)
        except Exception as exc:
            logger.error("Cleanup sweep failed: %s", exc, exc_info=True)

        await asyncio.sleep(_CLEANUP_INTERVAL_SECONDS)


# ── App factory ────────────────────────────────────────────────────────────────

def create_app() -> FastAPI:
    """
    Build and configure the FastAPI application.

    Separated from module-level instantiation so tests can call create_app()
    with a different lifespan or override settings before the app is built.
    """
    from web.routers import (  # noqa: PLC0415
        admin,
        kiosk,
        payment,
        settings as settings_router,
        status,
        upload,
        webhooks,
    )

    application = FastAPI(
        title="PrintBot v3",
        description="Self-service print vending machine",
        version="3.0.0",
        lifespan=lifespan,
        # Disable the automatic /docs and /redoc in production
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None if settings.is_production else "/redoc",
    )

    # ── Static files ───────────────────────────────────────────────────────────
    application.mount("/static", StaticFiles(directory="static"), name="static")

    # ── Routers ────────────────────────────────────────────────────────────────
    application.include_router(upload.router)
    application.include_router(settings_router.router)
    application.include_router(payment.router)
    application.include_router(webhooks.router)
    application.include_router(status.router)
    application.include_router(admin.router)
    application.include_router(kiosk.router)

    # ── Health endpoint ────────────────────────────────────────────────────────
    @application.get("/health", tags=["ops"])
    async def health_check() -> JSONResponse:
        """
        Lightweight liveness probe used by systemd / load balancers.

        Checks DB connectivity; does NOT check CUPS (too slow for a liveness probe).
        Returns 200 if healthy, 503 if the DB is unreachable.
        """
        components: dict = {}
        overall_ok = True

        # DB check
        try:
            async with AsyncSessionLocal() as session:
                await session.execute(text("SELECT 1"))
            components["database"] = "up"
        except Exception as exc:
            logger.error("Health check: DB down: %s", exc)
            components["database"] = f"down: {exc}"
            overall_ok = False

        http_status = 200 if overall_ok else 503
        return JSONResponse(
            status_code=http_status,
            content={"status": "ok" if overall_ok else "degraded", "components": components},
        )

    return application


# ── Module-level app instance (used by uvicorn web.main:app) ──────────────────
app = create_app()
