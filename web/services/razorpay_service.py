"""
web/services/razorpay_service.py — Razorpay API client wrapper for PrintBot v3.

Provides a thin synchronous wrapper around the Razorpay Python SDK for:
  - Creating Orders (UPI checkout flow)
  - Verifying payment signatures (Razorpay checkout JS callback)
  - Verifying webhook signatures (X-Razorpay-Signature header)
  - Fetching order details (status polling)

Mock mode is active when RAZORPAY_KEY_ID is empty or starts with "rzp_test_".
In mock mode all methods return deterministic fake responses so the full
checkout flow can be exercised locally without live credentials.

IMPORTANT: All methods are synchronous (the Razorpay SDK is sync-only).
Call them via asyncio.get_event_loop().run_in_executor() inside async routes
so the event loop is never blocked.
"""

from __future__ import annotations

import logging
import uuid
from typing import Optional

from core.config import settings

logger = logging.getLogger(__name__)


class RazorpayService:
    """
    Thin wrapper around the Razorpay SDK.

    A module-level singleton is exported as ``razorpay_service``.
    The class itself carries no per-request state, so the singleton is
    safe to use concurrently across all requests.
    """

    def __init__(self) -> None:
        self._client = None
        self.enabled: bool = False

        key_id = settings.RAZORPAY_KEY_ID
        key_secret = settings.RAZORPAY_KEY_SECRET

        if key_id and key_secret:
            try:
                import razorpay  # deferred: SDK may not be installed in dev/CI

                self._client = razorpay.Client(auth=(key_id, key_secret))
                self.enabled = True
                logger.info("RazorpayService: live mode (key prefix=%s…)", key_id[:12])
            except ImportError:
                logger.warning(
                    "RazorpayService: razorpay package not installed — mock mode"
                )
            except Exception as exc:
                logger.error(
                    "RazorpayService: SDK client init failed: %s — falling back to mock mode",
                    exc,
                )
        else:
            logger.warning(
                "RazorpayService: RAZORPAY_KEY_ID / KEY_SECRET not configured — mock mode"
            )

    # ── Orders ────────────────────────────────────────────────────────────────

    def create_order(
        self,
        amount: float,
        currency: str = "INR",
        receipt: Optional[str] = None,
        notes: Optional[dict] = None,
    ) -> dict:
        """
        Create a Razorpay Order and return the raw order dict.

        Args:
            amount:   Cost in **rupees** (NOT paise). Converted internally.
            currency: ISO-4217 currency code (default "INR").
            receipt:  Short reference string visible in Razorpay dashboard.
            notes:    Arbitrary key-value pairs stored with the order.

        Returns:
            Dict with at minimum ``{"id": "order_xxx", "amount": <paise>, ...}``.

        Raises:
            Exception: On Razorpay API error — caller wraps as HTTP 503.
        """
        amount_paise = int(round(amount * 100))

        if not self.enabled:
            mock_id = f"order_mock_{receipt or uuid.uuid4().hex[:8]}"
            logger.debug("RazorpayService (mock): create_order → %s (₹%.2f)", mock_id, amount)
            return {
                "id": mock_id,
                "amount": amount_paise,
                "currency": currency,
                "status": "created",
            }

        data: dict = {
            "amount": amount_paise,
            "currency": currency,
            "notes": notes or {},
        }
        if receipt:
            data["receipt"] = receipt

        try:
            order = self._client.order.create(data=data)
            logger.info(
                "RazorpayService: created order %s for ₹%.2f",
                order.get("id"),
                amount,
            )
            return order
        except Exception as exc:
            logger.error("RazorpayService: create_order failed: %s", exc)
            raise

    def fetch_order(self, order_id: str) -> Optional[dict]:
        """
        Fetch order details from Razorpay.

        Returns None on error — this is a non-critical polling path; callers
        should handle None gracefully rather than crashing.
        """
        if not self.enabled:
            return {"id": order_id, "status": "created"}

        try:
            return self._client.order.fetch(order_id)
        except Exception as exc:
            logger.error("RazorpayService: fetch_order(%s) failed: %s", order_id, exc)
            return None

    # ── Signature verification ────────────────────────────────────────────────

    def verify_payment_signature(
        self,
        razorpay_order_id: str,
        razorpay_payment_id: str,
        razorpay_signature: str,
    ) -> bool:
        """
        Verify the HMAC-SHA256 signature returned by the Razorpay checkout JS.

        Always returns True in mock mode — there is no real signature to verify.

        SECURITY: Returns False on any failure — never raises on bad signature.
        """
        # Mock mode: either no credentials or a test key that the frontend bypasses
        # with a fake signature — skip HMAC verification entirely.
        if not self.enabled or settings.is_mock_payment:
            return True

        try:
            self._client.utility.verify_payment_signature(
                {
                    "razorpay_order_id": razorpay_order_id,
                    "razorpay_payment_id": razorpay_payment_id,
                    "razorpay_signature": razorpay_signature,
                }
            )
            return True
        except Exception as exc:
            # SECURITY: Razorpay SDK raises SignatureVerificationError on mismatch
            logger.warning(
                "RazorpayService: payment signature verification failed: %s", exc
            )
            return False

    def verify_webhook_signature(
        self,
        body_bytes: bytes,
        signature: str,
    ) -> bool:
        """
        Verify the X-Razorpay-Signature header on an incoming webhook request.

        SECURITY: Returns False on any error — never raises.
        """
        if not self.enabled:
            return True

        try:
            self._client.utility.verify_webhook_signature(
                body_bytes.decode("utf-8"),
                signature,
                settings.RAZORPAY_WEBHOOK_SECRET,
            )
            return True
        except Exception as exc:
            logger.warning(
                "RazorpayService: webhook signature verification failed: %s", exc
            )
            return False


# Module-level singleton — import ``razorpay_service`` everywhere.
# Never instantiate RazorpayService() a second time.
razorpay_service = RazorpayService()
