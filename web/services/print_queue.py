"""
web/services/print_queue.py — Asyncio-based print job processing queue.

A single background worker task processes print jobs one at a time, in the
order they are enqueued.  The worker is started via asyncio.create_task() in
web/main.py's lifespan handler.

Pipeline for each job:
  PAID → PROCESSING:
    1. Load job + files from DB
    2. Wait for any pending DOCX conversions (poll converted_path, 120s timeout)
    3. For each FileItem, apply page settings (rotate, N-up) via PDFProcessor
    4. If multiple files, merge processed PDFs in sort_order
    5. Submit final PDF to CUPS with retry
  PROCESSING → PRINTING:
    6. Store cups_job_id, update status
    7. Poll CUPS every 5s until completed/failed (10 min timeout)
  PRINTING → COMPLETED or FAILED:
    8. On success: set completed_at, schedule file deletion
    9. On failure: set failed_reason, generate compensation coupon

The worker creates its own DB sessions via AsyncSessionLocal — it runs outside
the FastAPI request lifecycle so Depends(get_db) is not available.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import string
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from core.config import settings
from core.database import AsyncSessionLocal
from core.models import Coupon, FileItem, JobStatus, PrintJob
from core.printing.cups_manager import cups_manager
from core.printing.pdf_processor import NoPagesSelectedError, pdf_processor
from web.services.file_service import schedule_deletion

logger = logging.getLogger(__name__)

# ── Timing constants ───────────────────────────────────────────────────────────

_CONVERSION_POLL_INTERVAL_S: float = 2.0    # How often to re-check converted_path
_CONVERSION_TIMEOUT_S: float = 120.0        # Give up waiting for DOCX conversion after 2 min
_CUPS_POLL_INTERVAL_S: float = 5.0          # How often to poll CUPS job state
_CUPS_TIMEOUT_S: float = 600.0             # 10 min max for a CUPS job to finish

# IPP job-state integers that indicate a terminal failure
_CUPS_FAILED_STATES: frozenset[int] = frozenset({6, 7, 8})  # stopped, cancelled, aborted
_CUPS_COMPLETED_STATE: int = 9

# Alphabet for coupon code generation (uppercase alpha + digits, no O/0/I/1 ambiguity)
_COUPON_ALPHABET: str = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_COUPON_CODE_LEN: int = 8


class PrintQueue:
    """
    Single-worker asyncio queue for print job processing.

    Only one job is processed at a time, ensuring the printer is never
    overwhelmed and job ordering is deterministic.
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._current_job_id: Optional[str] = None

    # ── Public interface ───────────────────────────────────────────────────────

    async def enqueue(self, job_id: str) -> None:
        """Add a job ID to the processing queue."""
        await self._queue.put(job_id)
        logger.info(
            "Job %s enqueued (queue depth now %d)", job_id, self._queue.qsize()
        )

    async def worker(self) -> None:
        """
        Infinite loop that processes jobs one at a time.

        Must be launched as a background task from the FastAPI lifespan:
            asyncio.create_task(print_queue.worker())

        Never raises — all exceptions inside process_job() are caught and logged
        so the worker loop continues running after any single job failure.
        """
        logger.info("Print queue worker started")
        while True:
            job_id = await self._queue.get()
            self._current_job_id = job_id
            try:
                await self.process_job(job_id)
            except Exception as exc:
                logger.error(
                    "Unhandled exception in process_job for %s: %s",
                    job_id, exc, exc_info=True,
                )
            finally:
                self._current_job_id = None
                self._queue.task_done()

    def get_queue_status(self) -> dict:
        """Return a snapshot of the queue's current state."""
        return {
            "current_job": self._current_job_id,
            "queued": self._queue.qsize(),
        }

    async def requeue_interrupted_jobs(self) -> None:
        """
        Re-enqueue any jobs that were left in an active state due to a server
        restart.  Should be called once during the FastAPI lifespan startup
        before asyncio.create_task(print_queue.worker()).

        Jobs in PAID, PROCESSING, or PRINTING are considered interrupted —
        they never reached COMPLETED or FAILED, so they need to be retried.
        """
        interrupted_statuses = (
            JobStatus.PAID,
            JobStatus.PROCESSING,
            JobStatus.PRINTING,
        )
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(PrintJob).where(
                    PrintJob.status.in_([s.value for s in interrupted_statuses])
                )
            )
            jobs: list[PrintJob] = list(result.scalars().all())

        if not jobs:
            logger.info("requeue_interrupted_jobs: no interrupted jobs found")
            return

        for job in jobs:
            logger.warning(
                "requeue_interrupted_jobs: re-enqueuing job %s (was in '%s')",
                job.id, job.status.value,
            )
            await self.enqueue(job.id)

    # ── Core pipeline ──────────────────────────────────────────────────────────

    async def process_job(self, job_id: str) -> None:
        """
        Execute the full pipeline for a single print job.

        Each major step opens its own DB session to write incremental progress.
        This way a crash mid-pipeline leaves the job in a clearly identifiable
        state (PROCESSING vs PRINTING) rather than still PAID.
        """
        logger.info("process_job: starting job %s", job_id)

        # ── Step 1: Load job + files, set status → PROCESSING ─────────────────
        job, files = await self._load_job(job_id)
        if job is None:
            # EDGE CASE: job vanished from DB between enqueue and processing
            logger.error("process_job: job %s not found in DB — aborting", job_id)
            return

        # ── Step 2: Wait for DOCX conversions to complete ─────────────────────
        try:
            files = await self._wait_for_conversions(job_id, files)
        except asyncio.TimeoutError:
            reason = "DOCX conversion timed out after 120s"
            logger.error("process_job: %s for job %s", reason, job_id)
            await self._fail_job(job_id, reason, issue_coupon=True)
            return

        # ── Step 3: Apply page settings for each file ─────────────────────────
        processed_paths: list[tuple[int, str]] = []  # (sort_order, path)
        for file_item in sorted(files, key=lambda f: f.sort_order):
            try:
                out_path = await self._apply_file_settings(job, file_item)
                processed_paths.append((file_item.sort_order, out_path))
            except NoPagesSelectedError:
                # EDGE CASE: all pages excluded for this file — skip it gracefully
                logger.warning(
                    "process_job: job %s file %d has 0 pages selected — skipping",
                    job_id, file_item.id,
                )
            except Exception as exc:
                reason = f"PDF processing failed for file {file_item.original_name!r}: {exc}"
                logger.error("process_job: %s (job %s)", reason, job_id, exc_info=True)
                await self._fail_job(job_id, reason, issue_coupon=True)
                return

        if not processed_paths:
            reason = "No pages selected across all files"
            logger.error("process_job: %s for job %s", reason, job_id)
            await self._fail_job(job_id, reason, issue_coupon=True)
            return

        # ── Step 4: Merge if multiple files ───────────────────────────────────
        sorted_paths = [p for _, p in sorted(processed_paths, key=lambda x: x[0])]
        if len(sorted_paths) > 1:
            try:
                final_pdf = await self._merge_files(job_id, sorted_paths)
            except Exception as exc:
                reason = f"PDF merge failed: {exc}"
                logger.error("process_job: %s (job %s)", reason, job_id, exc_info=True)
                await self._fail_job(job_id, reason, issue_coupon=True)
                return
        else:
            final_pdf = sorted_paths[0]

        # ── Step 5: Submit to CUPS ─────────────────────────────────────────────
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(PrintJob).where(PrintJob.id == job_id))
            job = result.scalar_one_or_none()
            if job is None:
                logger.error("process_job: job %s disappeared before CUPS submit", job_id)
                return
            printer = settings.DEFAULT_PRINTER

        def _submit() -> int:
            return cups_manager.submit_job(
                pdf_path=final_pdf,
                printer_name=printer,
                copies=job.copies,
                is_duplex=job.is_duplex,
            )

        cups_job_id: int = await asyncio.get_event_loop().run_in_executor(None, _submit)

        if cups_job_id == 0:
            # EDGE CASE: CUPS returned 0 — printer not found or CUPS is down
            reason = "CUPS submit failed — printer unavailable after retries"
            logger.error("process_job: %s (job %s)", reason, job_id)
            await self._fail_job(job_id, reason, issue_coupon=True)
            return

        # ── Step 6: Record cups_job_id, set status → PRINTING ─────────────────
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(PrintJob).where(PrintJob.id == job_id))
            job = result.scalar_one_or_none()
            if job is None:
                logger.error("process_job: job %s disappeared after CUPS submit", job_id)
                return
            job.cups_job_id = cups_job_id
            job.status = JobStatus.PRINTING
            await session.commit()
            logger.info(
                "process_job: job %s → PRINTING (cups_job_id=%d)", job_id, cups_job_id
            )

        # ── Step 7: Poll CUPS until done ───────────────────────────────────────
        elapsed = 0.0
        final_state: Optional[int] = None

        while elapsed < _CUPS_TIMEOUT_S:
            await asyncio.sleep(_CUPS_POLL_INTERVAL_S)
            elapsed += _CUPS_POLL_INTERVAL_S

            def _get_status() -> dict:
                return cups_manager.get_job_status(cups_job_id)

            status_dict: dict = await asyncio.get_event_loop().run_in_executor(
                None, _get_status
            )
            state = status_dict.get("state", 0)
            logger.debug(
                "process_job: CUPS poll job %d state=%s (%ds elapsed)",
                cups_job_id, status_dict.get("status"), int(elapsed),
            )

            if state == _CUPS_COMPLETED_STATE:
                final_state = state
                break
            if state in _CUPS_FAILED_STATES:
                final_state = state
                break
            # EDGE CASE: job purged from CUPS history before we could read it —
            # treat "unknown" state after >30s of printing as likely completed.
            if state == 0 and elapsed > 30:
                logger.warning(
                    "process_job: CUPS job %d returned unknown state after %.0fs — "
                    "assuming completed",
                    cups_job_id, elapsed,
                )
                final_state = _CUPS_COMPLETED_STATE
                break

        if final_state is None:
            # Timed out waiting for CUPS
            reason = f"CUPS job {cups_job_id} timed out after {int(_CUPS_TIMEOUT_S)}s"
            logger.error("process_job: %s (job %s)", reason, job_id)
            await self._fail_job(job_id, reason, issue_coupon=True)
            return

        # ── Step 8 / 9: Complete or fail ───────────────────────────────────────
        if final_state == _CUPS_COMPLETED_STATE:
            await self._complete_job(job_id)
        else:
            status_info = cups_manager.get_job_status(cups_job_id)
            reason = (
                f"CUPS job {cups_job_id} ended with state "
                f"'{status_info.get('status', 'unknown')}': "
                f"{status_info.get('state_reasons', '')}"
            )
            logger.error("process_job: %s (job %s)", reason, job_id)
            await self._fail_job(job_id, reason, issue_coupon=True)

    # ── Pipeline helpers ───────────────────────────────────────────────────────

    async def _load_job(
        self, job_id: str
    ) -> tuple[Optional[PrintJob], list[FileItem]]:
        """
        Fetch the PrintJob with its files eagerly loaded and transition it to
        PROCESSING.  Returns (None, []) if the job no longer exists.
        """
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(PrintJob)
                .options(selectinload(PrintJob.files))
                .where(PrintJob.id == job_id)
            )
            job: Optional[PrintJob] = result.scalar_one_or_none()
            if job is None:
                return None, []

            job.status = JobStatus.PROCESSING
            await session.commit()

            # Detach copies of the data we need — session will be closed after
            # this block, so we extract plain attributes.
            files: list[FileItem] = list(job.files)
            return job, files

    async def _wait_for_conversions(
        self, job_id: str, files: list[FileItem]
    ) -> list[FileItem]:
        """
        Poll the DB until every DOCX FileItem has a non-null converted_path, or
        raise asyncio.TimeoutError if the wait exceeds _CONVERSION_TIMEOUT_S.

        Returns the refreshed FileItem list (with converted_path populated).
        """
        file_ids_needing_conversion: list[int] = [
            f.id
            for f in files
            if f.stored_path.lower().endswith(".docx") and not f.converted_path
        ]

        if not file_ids_needing_conversion:
            return files

        logger.info(
            "process_job: waiting for DOCX conversion on job %s "
            "(file ids: %s)",
            job_id, file_ids_needing_conversion,
        )

        elapsed = 0.0
        while elapsed < _CONVERSION_TIMEOUT_S:
            await asyncio.sleep(_CONVERSION_POLL_INTERVAL_S)
            elapsed += _CONVERSION_POLL_INTERVAL_S

            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(FileItem).where(
                        FileItem.job_id == job_id
                    )
                )
                refreshed_files: list[FileItem] = list(result.scalars().all())

            still_pending = [
                f.id
                for f in refreshed_files
                if f.id in file_ids_needing_conversion and not f.converted_path
            ]
            if not still_pending:
                logger.info(
                    "process_job: all DOCX conversions complete for job %s", job_id
                )
                return refreshed_files

            logger.debug(
                "process_job: still waiting for conversion on file ids %s "
                "(%.0fs elapsed)",
                still_pending, elapsed,
            )

        raise asyncio.TimeoutError(
            f"DOCX conversion timed out after {_CONVERSION_TIMEOUT_S}s"
        )

    async def _apply_file_settings(
        self, job: PrintJob, file_item: FileItem
    ) -> str:
        """
        Apply per-page rotation and N-up layout to one FileItem, writing the
        processed PDF to uploads/<job_id>/processed_<file_id>.pdf.

        Returns the path to the processed PDF.

        Raises NoPagesSelectedError if all pages are excluded.
        """
        input_pdf = file_item.effective_pdf_path()
        output_pdf = str(
            Path(settings.UPLOAD_DIR) / job.id / f"processed_{file_item.id}.pdf"
        )
        page_configs = file_item.get_page_configs()

        # EDGE CASE: no page_selection recorded yet — include all pages with no rotation
        if not page_configs:
            import fitz
            doc = fitz.open(input_pdf)
            page_count = doc.page_count
            doc.close()
            page_configs = [
                {"page_idx": i, "rotation": 0, "include": True}
                for i in range(page_count)
            ]
        else:
            # Normalise key: the settings route stores "page" (1-based),
            # pdf_processor expects "page_idx" (0-based).
            normalised = []
            for cfg in page_configs:
                if "page_idx" not in cfg and "page" in cfg:
                    cfg = dict(cfg)
                    cfg["page_idx"] = cfg["page"] - 1
                normalised.append(cfg)
            page_configs = normalised

        def _apply() -> int:
            return pdf_processor.apply_page_settings(
                input_pdf=input_pdf,
                output_pdf=output_pdf,
                page_configs=page_configs,
                nup_layout=job.nup_layout,
                copies=1,  # copies are applied at CUPS level, not per-file
            )

        await asyncio.get_event_loop().run_in_executor(None, _apply)
        logger.debug(
            "process_job: applied settings for file %d → %s", file_item.id, output_pdf
        )
        return output_pdf

    async def _merge_files(self, job_id: str, pdf_paths: list[str]) -> str:
        """
        Merge a list of processed PDFs into a single file.

        Returns the path to the merged PDF.
        """
        merged_path = str(Path(settings.UPLOAD_DIR) / job_id / "final_merged.pdf")

        def _merge() -> str:
            return pdf_processor.merge_pdfs(pdf_paths, merged_path)

        result = await asyncio.get_event_loop().run_in_executor(None, _merge)
        logger.info(
            "process_job: merged %d PDFs for job %s → %s",
            len(pdf_paths), job_id, merged_path,
        )
        return result

    async def _complete_job(self, job_id: str) -> None:
        """Set job status to COMPLETED and schedule file deletion."""
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(PrintJob).where(PrintJob.id == job_id)
            )
            job: Optional[PrintJob] = result.scalar_one_or_none()
            if job is None:
                logger.error("_complete_job: job %s not found", job_id)
                return

            job.status = JobStatus.COMPLETED
            job.completed_at = datetime.now(timezone.utc).replace(tzinfo=None)
            await session.commit()
            logger.info("process_job: job %s → COMPLETED", job_id)

        # Schedule upload file deletion (runs via cleanup_expired_jobs on the
        # next cleanup cycle — schedule_deletion just sets expires_at).
        async with AsyncSessionLocal() as session:
            await schedule_deletion(job_id, session)
            await session.commit()

    async def _fail_job(
        self,
        job_id: str,
        reason: str,
        issue_coupon: bool = False,
    ) -> None:
        """
        Set job status to FAILED, record the failure reason, and optionally
        generate a compensation coupon worth the full job cost.
        """
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(PrintJob)
                .options(selectinload(PrintJob.coupon))
                .where(PrintJob.id == job_id)
            )
            job: Optional[PrintJob] = result.scalar_one_or_none()
            if job is None:
                logger.error("_fail_job: job %s not found", job_id)
                return

            job.status = JobStatus.FAILED
            job.failed_reason = reason[:500]  # guard against absurdly long strings

            if issue_coupon and job.total_cost > 0 and job.coupon is None:
                coupon = await self._generate_coupon(session, job)
                logger.info(
                    "_fail_job: issued coupon %r (₹%.2f) for failed job %s",
                    coupon.code, coupon.balance, job_id,
                )

            await session.commit()
            logger.error("process_job: job %s → FAILED (%s)", job_id, reason)

    async def _generate_coupon(
        self,
        session,  # AsyncSession — typed loosely to avoid circular import at runtime
        job: PrintJob,
    ) -> Coupon:
        """
        Create and persist a compensation Coupon for a failed job.

        The code is 8 characters drawn from an unambiguous alphabet.  On the
        rare collision (unique constraint violation), we retry up to 5 times.
        """
        max_attempts = 5
        for attempt in range(max_attempts):
            code = "".join(
                secrets.choice(_COUPON_ALPHABET) for _ in range(_COUPON_CODE_LEN)
            )
            # Check if code is already taken
            existing = await session.execute(
                select(Coupon).where(Coupon.code == code)
            )
            if existing.scalar_one_or_none() is not None:
                logger.debug("Coupon code collision on attempt %d: %r", attempt + 1, code)
                continue

            coupon = Coupon(
                code=code,
                balance=job.total_cost,
                initial_amount=job.total_cost,
                job_id=job.id,
            )
            session.add(coupon)
            # Don't commit here — the caller (_fail_job) commits the whole transaction
            return coupon

        # Extremely unlikely — generate a truly random fallback using token_hex
        code = secrets.token_hex(4).upper()
        coupon = Coupon(
            code=code,
            balance=job.total_cost,
            initial_amount=job.total_cost,
            job_id=job.id,
        )
        session.add(coupon)
        return coupon


# Module-level singleton — import `print_queue` everywhere; never instantiate PrintQueue elsewhere.
print_queue = PrintQueue()
