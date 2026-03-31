"""
core/config.py — Application-wide settings loaded from .env via pydantic-settings.

All secrets, tunables, and path constants live here. Routes and services must
import `settings` from this module instead of reading env vars directly.
"""

from pydantic import computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # ── Razorpay ──────────────────────────────────────────────────────────────
    RAZORPAY_KEY_ID: str = ""
    RAZORPAY_KEY_SECRET: str = ""
    RAZORPAY_WEBHOOK_SECRET: str = ""

    # ── Admin auth ────────────────────────────────────────────────────────────
    ADMIN_PIN: str = "1234"
    # "Z" pattern on a 3×3 grid: top-left → top-right → centre → bottom-left → bottom-right
    ADMIN_PATTERN: str = "1235789"
    # SECURITY: random string used by itsdangerous to sign admin session cookies
    ADMIN_SESSION_SECRET: str = "change-me-in-production"

    # ── Admin session cookie ──────────────────────────────────────────────────
    SESSION_COOKIE_NAME: str = "pb_admin"

    # ── Job session cookie (user's print session) ─────────────────────────────
    JOB_SESSION_COOKIE_NAME: str = "pb_session"
    # SECURITY: separate secret for user job cookies; change in production
    JOB_SESSION_SECRET: str = "change-me-in-production"
    JOB_SESSION_MAX_AGE: int = 1800  # 30 minutes

    # ── Tunnel / public URL ───────────────────────────────────────────────────
    # Must point to the Cloudflare-proxied URL so Razorpay webhooks reach us
    TUNNEL_URL: str = "https://print.example.com"

    # ── Printer ───────────────────────────────────────────────────────────────
    DEFAULT_PRINTER: str = ""

    # ── Pricing fallback ──────────────────────────────────────────────────────
    # Used when no PricingRule row matches the job; avoids blocking checkout
    PRICE_PER_PAGE: float = 2.0

    # ── Upload limits ─────────────────────────────────────────────────────────
    MAX_UPLOAD_BYTES: int = 90 * 1024 * 1024   # 90 MB hard cap across all files
    MAX_FILES_PER_JOB: int = 20

    # ── Storage paths ─────────────────────────────────────────────────────────
    UPLOAD_DIR: str = "uploads"
    QR_CODE_PATH: str = "static/icons/qr_code.png"

    # ── Logging ───────────────────────────────────────────────────────────────
    LOG_LEVEL: str = "INFO"

    # ── Environment ───────────────────────────────────────────────────────────
    ENV: str = "development"   # "development" | "production"

    # ── File lifecycle ────────────────────────────────────────────────────────
    FILE_EXPIRY_HOURS: int = 2   # Hours after job completion before uploads are purged

    # ── Database ──────────────────────────────────────────────────────────────
    # Path to the SQLite file, relative to the working directory.
    # Alembic reads this via settings.DATABASE_URL too (see alembic/env.py).
    DATABASE_URL: str = "printbot.db"

    # ── CUPS retry ────────────────────────────────────────────────────────────
    CUPS_RETRY_ATTEMPTS: int = 3
    CUPS_RETRY_DELAY_SECONDS: int = 2

    # ── PDF processing ────────────────────────────────────────────────────────
    THUMBNAIL_WIDTH: int = 150  # Default pixel width for page thumbnails

    # ── Computed fields ───────────────────────────────────────────────────────

    @computed_field  # type: ignore[misc]
    @property
    def is_mock_payment(self) -> bool:
        """
        True when Razorpay is not configured for live transactions.

        Covers two cases:
          - RAZORPAY_KEY_ID is empty (local dev / CI with no .env)
          - RAZORPAY_KEY_ID starts with "rzp_test_" (Razorpay sandbox key)
        This flag gates real charge attempts so we never accidentally bill
        users during development.
        """
        return not self.RAZORPAY_KEY_ID or self.RAZORPAY_KEY_ID.startswith("rzp_test_")

    @computed_field  # type: ignore[misc]
    @property
    def is_production(self) -> bool:
        return self.ENV == "production"


# Module-level singleton — import `settings` everywhere, never instantiate again.
settings = Settings()
