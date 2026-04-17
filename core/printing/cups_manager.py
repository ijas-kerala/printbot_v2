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

# ── IPP printer-state-reasons → (user message, severity) ──────────────────────
# Severity order for priority: error > warning > ok
# Printers that don't report these keywords just won't match any entry here,
# which resolves gracefully to "Ready" / "ok".
_REASON_MESSAGES: dict[str, tuple[str, str]] = {
    "media-needed":           ("Paper tray is empty", "error"),
    "media-empty":            ("Paper tray is empty", "error"),
    "media-low":              ("Paper is running low", "warning"),
    "media-jam":              ("Paper jam — clear the printer", "error"),
    "toner-low":              ("Toner is running low", "warning"),
    "toner-empty":            ("Toner is empty", "error"),
    "marker-supply-low":      ("Ink/toner is running low", "warning"),
    "marker-supply-empty":    ("Ink/toner is empty", "error"),
    "marker-waste-almost-full": ("Waste container almost full", "warning"),
    "marker-waste-full":      ("Waste container full", "error"),
    "door-open":              ("Printer door is open", "error"),
    "cover-open":             ("Printer cover is open", "error"),
    "input-tray-missing":     ("Paper tray is missing", "error"),
    "output-tray-missing":    ("Output tray is missing", "error"),
    "output-bin-full":        ("Output tray is full", "error"),
    "offline-report":         ("Printer is offline", "error"),
    "paused":                 ("Printer is paused", "warning"),
    "shutdown":               ("Printer has shut down", "error"),
    "connecting-to-device":   ("Connecting to printer…", "warning"),
    "none":                   ("Ready", "ok"),
}

# Severity rank for picking the most important reason when multiple are present
_SEVERITY_RANK: dict[str, int] = {"ok": 0, "warning": 1, "error": 2}

# ── IPP job-state integer → user-friendly progress message ────────────────────
_JOB_PROGRESS_MESSAGES: dict[int, str] = {
    3: "Waiting to print",
    4: "Print job is held",
    5: "Printing in progress",
    6: "Print job paused",
    7: "Print job cancelled",
    8: "Print job failed",
    9: "Finished",
}


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
                    "CUPS IPPError on attempt %d/%d printer=%r pdf=%r: %s",
                    attempt + 1, settings.CUPS_RETRY_ATTEMPTS, printer_name, pdf_path, exc,
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
          - ``status``         (str)  : human-readable state label
          - ``state``          (int)  : raw IPP state integer (0 if unknown)
          - ``state_reasons``  (str)  : comma-joined IPP state-reasons list
          - ``driver_message`` (str)  : printer-level message from the backend
                                        (e.g. "ccp send_data error, exit")

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

            driver_message: str = attrs.get("job-printer-state-message", "") or ""

            return {
                "status": _JOB_STATE_LABELS.get(state, f"state-{state}"),
                "state": state,
                "state_reasons": reasons_str,
                "driver_message": driver_message,
            }
        except cups.IPPError as exc:
            # EDGE CASE: job might have been purged from CUPS history after completion
            logger.warning("CUPS IPPError querying job %d: %s", cups_job_id, exc)
            return {"status": "unknown", "state": 0, "state_reasons": "", "driver_message": ""}
        except Exception as exc:
            logger.error("Unexpected error querying CUPS job %d: %s", cups_job_id, exc)
            return {"status": "unknown", "state": 0, "state_reasons": "", "driver_message": ""}

    # ── Printer status ─────────────────────────────────────────────────────────

    def get_printer_status(self, printer_name: Optional[str] = None) -> dict:
        """
        Return the current state of the configured (or specified) printer.

        Returns a dict with:
          - ``state``          (int)       : IPP printer-state integer (0 if unknown)
          - ``state_reasons``  (list[str]) : list of IPP printer-state-reasons
          - ``message``        (str)       : human-readable state label
          - ``driver_message`` (str)       : backend-level message from the printer
                                             driver (e.g. "ccp send_data error, exit")

        Falls back to ``{"state": 0, "state_reasons": [], "message": "unavailable",
        "driver_message": ""}`` if CUPS is unreachable.
        """
        target = printer_name or settings.DEFAULT_PRINTER
        if not target:
            return {"state": 0, "state_reasons": [], "message": "no printer configured", "driver_message": ""}

        try:
            conn = cups.Connection()
            printers: dict = conn.getPrinters()
            if target not in printers:
                logger.warning("CUPS printer %r not found in printer list", target)
                return {"state": 0, "state_reasons": [], "message": "printer not found", "driver_message": ""}

            printer_info: dict = printers[target]
            state: int = printer_info.get("printer-state", 0)
            reasons_raw = printer_info.get("printer-state-reasons", [])
            if isinstance(reasons_raw, str):
                reasons_raw = [reasons_raw] if reasons_raw else []

            driver_message: str = printer_info.get("printer-state-message", "") or ""

            return {
                "state": state,
                "state_reasons": reasons_raw,
                "message": _PRINTER_STATE_LABELS.get(state, f"state-{state}"),
                "driver_message": driver_message,
            }
        except cups.IPPError as exc:
            logger.error("CUPS IPPError getting printer status for %r: %s", target, exc)
            return {"state": 0, "state_reasons": [], "message": "unavailable", "driver_message": ""}
        except Exception as exc:
            logger.error("Unexpected error getting CUPS printer status: %s", exc)
            return {"state": 0, "state_reasons": [], "message": "unavailable", "driver_message": ""}

    # ── Online check ───────────────────────────────────────────────────────────

    def is_printer_online(self, printer_name: Optional[str] = None) -> bool:
        """
        Return True if the printer is in a state where it can accept new jobs
        (idle=3 or processing=4).  Returns False if stopped, unavailable, or
        if CUPS cannot be reached.
        """
        status = self.get_printer_status(printer_name)
        return status["state"] in _ONLINE_PRINTER_STATES

    # ── User-friendly printer health ───────────────────────────────────────────

    def get_printer_health(self, printer_name: Optional[str] = None) -> dict:
        """
        Return a user-friendly health summary for the configured printer.

        Parses IPP printer-state-reasons and picks the highest-severity reason
        to surface as a single actionable message.  Printers that report no
        reasons, or only "none", get "Ready" / "ok".

        Returns a dict with:
          - ``online``       (bool) : True when the printer can accept jobs
          - ``state_label``  (str)  : raw IPP label ("idle", "processing", etc.)
          - ``message``      (str)  : human-readable status for users
          - ``severity``     (str)  : "ok" | "warning" | "error"
          - ``raw_reasons``  (list) : raw IPP reason strings from the printer
        """
        raw = self.get_printer_status(printer_name)
        state: int = raw.get("state", 0)
        reasons: list[str] = raw.get("state_reasons", [])
        state_label: str = raw.get("message", "unavailable")

        online: bool = state in _ONLINE_PRINTER_STATES

        # Printer unreachable / not configured
        if state == 0:
            return {
                "online": False,
                "state_label": state_label,
                "message": state_label.replace("-", " ").capitalize(),
                "severity": "error",
                "raw_reasons": reasons,
            }

        # Printer stopped — hard offline regardless of reasons
        if state == 5:
            # Try to find a descriptive reason first
            best_msg, best_sev = "Printer stopped", "error"
            for reason in reasons:
                keyword = reason.split("-report")[0].split("-warning")[0]
                if keyword in _REASON_MESSAGES:
                    msg, sev = _REASON_MESSAGES[keyword]
                    if _SEVERITY_RANK.get(sev, 0) >= _SEVERITY_RANK.get(best_sev, 0):
                        best_msg, best_sev = msg, sev
            return {
                "online": False,
                "state_label": state_label,
                "message": best_msg,
                "severity": best_sev,
                "raw_reasons": reasons,
            }

        # Idle or processing — scan reasons for any issues
        best_msg = "Ready"
        best_sev = "ok"
        for reason in reasons:
            # IPP reasons can have "-report" or "-warning" suffixes; strip them
            # to normalise e.g. "toner-low-report" → "toner-low"
            keyword = reason
            for suffix in ("-report", "-warning", "-error"):
                if keyword.endswith(suffix):
                    keyword = keyword[: -len(suffix)]
                    break

            if keyword in _REASON_MESSAGES:
                msg, sev = _REASON_MESSAGES[keyword]
                if _SEVERITY_RANK.get(sev, 0) > _SEVERITY_RANK.get(best_sev, 0):
                    best_msg, best_sev = msg, sev
            elif keyword != "none" and keyword:
                # Unknown reason — flag as a generic warning so we don't hide it
                if best_sev == "ok":
                    best_msg = "Printer has an unknown condition"
                    best_sev = "warning"

        return {
            "online": online,
            "state_label": state_label,
            "message": best_msg,
            "severity": best_sev,
            "raw_reasons": reasons,
        }

    # ── User-friendly job progress ─────────────────────────────────────────────

    def get_job_progress(self, cups_job_id: int) -> dict:
        """
        Return a user-friendly progress summary for a CUPS job.

        Wraps get_job_status() with a plain-English message suitable for
        showing directly in the UI.

        Returns a dict with:
          - ``status``         (str) : IPP state label ("pending", "processing", etc.)
          - ``message``        (str) : human-readable progress message
          - ``state``          (int) : raw IPP job-state integer (0 if unknown)
          - ``driver_message`` (str) : backend-level driver message (pass-through)
        """
        raw = self.get_job_status(cups_job_id)
        state: int = raw.get("state", 0)
        message = _JOB_PROGRESS_MESSAGES.get(state, "Printing in progress")
        return {
            "status": raw.get("status", "unknown"),
            "message": message,
            "state": state,
            "driver_message": raw.get("driver_message", ""),
        }

    # ── Cancel job ─────────────────────────────────────────────────────────────

    def cancel_job(self, cups_job_id: int) -> bool:
        """
        Cancel a CUPS job by ID so it does not linger in the print queue.

        Should be called by the print queue worker whenever the app gives up
        on a job (timeout or terminal failure state) to prevent accumulation
        of stuck jobs that could block future prints.

        Returns True on success, False if the job was already gone or could
        not be cancelled.  Never raises — logs errors and returns False.
        """
        try:
            conn = cups.Connection()
            conn.cancelJob(cups_job_id, purge_job=False)
            logger.info("CUPS job %d cancelled successfully", cups_job_id)
            return True
        except cups.IPPError as exc:
            # Job may already have been purged or completed — not an error
            logger.warning("CUPS cancel job %d: IPPError %s", cups_job_id, exc)
            return False
        except Exception as exc:
            logger.error("CUPS cancel job %d: unexpected error %s", cups_job_id, exc)
            return False


# Module-level singleton — import `cups_manager` everywhere; never instantiate CupsManager elsewhere.
cups_manager = CupsManager()
