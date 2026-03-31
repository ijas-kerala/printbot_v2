"""
core/printing/cups_manager.py — CUPS abstraction layer for PrintBot v3.

Wraps pycups (synchronous) with retry logic, structured status reporting,
and graceful degradation when the CUPS daemon is unavailable.

All public methods are synchronous — they are meant to be called from the
async print_queue worker via asyncio.get_event_loop().run_in_executor() so
that blocking CUPS socket I/O never stalls the event loop.

CUPS job-state integers follow IPP RFC 2911 §4.3.7:
  3 = pending, 4 = pending-held, 5 = processing,
  6 = processing-stopped, 7 = cancelled, 8 = aborted, 9 = completed
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import cups

from core.config import settings

logger = logging.getLogger(__name__)

# ── IPP job-state integer → readable string ────────────────────────────────────
_JOB_STATE_LABELS: dict[int, str] = {
    3: "pending",
    4: "pending-held",
    5: "processing",
    6: "processing-stopped",
    7: "cancelled",
    8: "aborted",
    9: "completed",
}

# IPP printer-state integers (RFC 2911 §4.4.11)
_PRINTER_STATE_LABELS: dict[int, str] = {
    3: "idle",
    4: "processing",
    5: "stopped",
}

# Printer states that indicate the printer can accept jobs
_ONLINE_PRINTER_STATES: frozenset[int] = frozenset({3, 4})


class CupsManager:
    """
    Thin wrapper around pycups that adds retry logic and structured responses.

    All methods create a fresh cups.Connection() on each call.  pycups
    connections are not thread-safe and are cheap to create, so opening one
    per call is safer than sharing a long-lived connection across executor threads.
    """

    # ── Submit ─────────────────────────────────────────────────────────────────

    def submit_job(
        self,
        pdf_path: str,
        printer_name: str,
        copies: int,
        is_duplex: bool,
    ) -> int:
        """
        Submit a PDF file to CUPS for printing.

        Retries up to ``settings.CUPS_RETRY_ATTEMPTS`` times with exponential
        backoff starting at ``settings.CUPS_RETRY_DELAY_SECONDS`` seconds.

        Args:
            pdf_path:     Absolute path to the final processed PDF.
            printer_name: CUPS printer name (must be registered in CUPS).
            copies:       Number of copies to print.
            is_duplex:    Whether to print on both sides of the paper.

        Returns:
            CUPS job ID (positive integer) on success.
            0 if all retry attempts are exhausted.
        """
        # Build IPP option dict
        options: dict[str, str] = {"copies": str(copies)}
        if is_duplex:
            # "two-sided-long-edge" = standard long-edge binding (portrait duplex)
            options["sides"] = "two-sided-long-edge"

        job_title = f"PrintBot-{pdf_path.split('/')[-1]}"
        last_exc: Optional[Exception] = None

        for attempt in range(settings.CUPS_RETRY_ATTEMPTS):
            try:
                conn = cups.Connection()
                job_id: int = conn.printFile(printer_name, pdf_path, job_title, options)
                logger.info(
                    "CUPS job submitted: id=%d printer=%r pdf=%r attempt=%d",
                    job_id, printer_name, pdf_path, attempt + 1,
                )
                return job_id
            except cups.IPPError as exc:
                last_exc = exc
                logger.warning(
                    "CUPS IPPError on attempt %d/%d for %r: %s",
                    attempt + 1, settings.CUPS_RETRY_ATTEMPTS, pdf_path, exc,
                )
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "CUPS submit error on attempt %d/%d for %r: %s",
                    attempt + 1, settings.CUPS_RETRY_ATTEMPTS, pdf_path, exc,
                )

            # Exponential backoff: 2s, 4s, 8s, …
            if attempt < settings.CUPS_RETRY_ATTEMPTS - 1:
                backoff = settings.CUPS_RETRY_DELAY_SECONDS * (2 ** attempt)
                logger.debug("Retrying CUPS submit in %ds …", backoff)
                time.sleep(backoff)

        logger.error(
            "CUPS submit failed after %d attempts for %r: %s",
            settings.CUPS_RETRY_ATTEMPTS, pdf_path, last_exc,
        )
        return 0

    # ── Job status ─────────────────────────────────────────────────────────────

    def get_job_status(self, cups_job_id: int) -> dict:
        """
        Query the current state of a CUPS job.

        Returns a dict with:
          - ``status``        (str)  : human-readable state label
          - ``state``         (int)  : raw IPP state integer (0 if unknown)
          - ``state_reasons`` (str)  : comma-joined IPP state-reasons list

        Never raises — returns an "unknown" sentinel dict if CUPS is unreachable
        or the job is not found.
        """
        try:
            conn = cups.Connection()
            # getJobAttributes returns a dict of IPP attributes for one job
            attrs: dict = conn.getJobAttributes(cups_job_id)
            state: int = attrs.get("job-state", 0)
            reasons_raw = attrs.get("job-state-reasons", "")
            if isinstance(reasons_raw, list):
                reasons_str = ", ".join(reasons_raw)
            else:
                reasons_str = str(reasons_raw)

            return {
                "status": _JOB_STATE_LABELS.get(state, f"state-{state}"),
                "state": state,
                "state_reasons": reasons_str,
            }
        except cups.IPPError as exc:
            # EDGE CASE: job might have been purged from CUPS history after completion
            logger.warning("CUPS IPPError querying job %d: %s", cups_job_id, exc)
            return {"status": "unknown", "state": 0, "state_reasons": ""}
        except Exception as exc:
            logger.error("Unexpected error querying CUPS job %d: %s", cups_job_id, exc)
            return {"status": "unknown", "state": 0, "state_reasons": ""}

    # ── Printer status ─────────────────────────────────────────────────────────

    def get_printer_status(self, printer_name: Optional[str] = None) -> dict:
        """
        Return the current state of the configured (or specified) printer.

        Returns a dict with:
          - ``state``         (int)       : IPP printer-state integer (0 if unknown)
          - ``state_reasons`` (list[str]) : list of IPP printer-state-reasons
          - ``message``       (str)       : human-readable state label

        Falls back to ``{"state": 0, "state_reasons": [], "message": "unavailable"}``
        if CUPS is unreachable.
        """
        target = printer_name or settings.DEFAULT_PRINTER
        if not target:
            return {"state": 0, "state_reasons": [], "message": "no printer configured"}

        try:
            conn = cups.Connection()
            printers: dict = conn.getPrinters()
            if target not in printers:
                logger.warning("CUPS printer %r not found in printer list", target)
                return {"state": 0, "state_reasons": [], "message": "printer not found"}

            printer_info: dict = printers[target]
            state: int = printer_info.get("printer-state", 0)
            reasons_raw = printer_info.get("printer-state-reasons", [])
            if isinstance(reasons_raw, str):
                reasons_raw = [reasons_raw] if reasons_raw else []

            return {
                "state": state,
                "state_reasons": reasons_raw,
                "message": _PRINTER_STATE_LABELS.get(state, f"state-{state}"),
            }
        except cups.IPPError as exc:
            logger.error("CUPS IPPError getting printer status for %r: %s", target, exc)
            return {"state": 0, "state_reasons": [], "message": "unavailable"}
        except Exception as exc:
            logger.error("Unexpected error getting CUPS printer status: %s", exc)
            return {"state": 0, "state_reasons": [], "message": "unavailable"}

    # ── Online check ───────────────────────────────────────────────────────────

    def is_printer_online(self, printer_name: Optional[str] = None) -> bool:
        """
        Return True if the printer is in a state where it can accept new jobs
        (idle=3 or processing=4).  Returns False if stopped, unavailable, or
        if CUPS cannot be reached.
        """
        status = self.get_printer_status(printer_name)
        return status["state"] in _ONLINE_PRINTER_STATES


# Module-level singleton — import `cups_manager` everywhere; never instantiate CupsManager elsewhere.
cups_manager = CupsManager()
