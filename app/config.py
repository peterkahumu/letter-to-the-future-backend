"""
Application configuration using pydantic-settings.
All secrets and config are loaded from environment variables.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        # .env.local layers on top of .env, so you can point a local run at a
        # local database without editing (or risking) the deployed values.
        env_file=(".env", ".env.local"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    database_url: str

    # --- Mail transport -------------------------------------------------
    # "smtp" keeps the existing Gmail path; "resend" switches to the HTTP
    # API. See R8 — Gmail is not a sending platform and will suspend the
    # account for bulk sending.
    # "smtp" | "resend" | "console". "console" prints emails to the log
    # instead of sending them — the local development default.
    email_provider: str = "smtp"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from_name: str = "Letter to the Future"
    smtp_from_email: str = ""
    resend_api_key: str | None = None
    # Plain SMTP without TLS — for a local catcher like Mailpit, which has no
    # certificate. Never enable this against a real provider.
    smtp_insecure: bool = False

    frontend_url: str
    allowed_origin_regex: str | None = None
    debug: bool
    # Echo every SQL statement. Independent of `debug`, which only controls
    # the application log level — turning both on at once buries app logs.
    sql_echo: bool = False
    cloudinary_url: str | None = None

    # --- Security -------------------------------------------------------
    # Signs confirmation / manage / unsubscribe tokens. Generate with
    # `python -c "import secrets; print(secrets.token_urlsafe(48))"`.
    secret_key: str = "dev-only-insecure-key-change-me"

    # --- Abuse limits ---------------------------------------------------
    rate_limit_create: str = "5/hour"
    rate_limit_upload: str = "10/hour"
    rate_limit_report: str = "20/hour"
    max_upload_bytes: int = 10 * 1024 * 1024
    max_pending_per_recipient: int = 20

    # --- Delivery -------------------------------------------------------
    scheduler_batch_size: int = 100
    max_delivery_attempts: int = 5
    # Default OFF on purpose. A local .env usually points at the production
    # database, so defaulting this on means "run the app locally" silently
    # migrates production. Turn it on in the deploy environment only.
    run_migrations_on_startup: bool = False

    # --- Compliance -----------------------------------------------------
    # CAN-SPAM requires a real postal address in commercial mail.
    mailing_address: str = ""
    retention_note: str = (
        "Letters are stored until delivered, then kept for 12 months so you can "
        "read them again, unless you delete them sooner."
    )

    @property
    def async_database_url(self) -> str:
        """Ensure the database URL uses the asyncpg driver."""
        url = self.database_url
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql+asyncpg://", 1)
        elif url.startswith("postgresql://") and "+asyncpg" not in url:
            url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
        return url

    @property
    def sync_database_url(self) -> str:
        """Sync URL for Alembic, which does not speak asyncpg."""
        url = self.database_url
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql://", 1)
        return url.replace("+asyncpg", "")

    @property
    def primary_frontend_url(self) -> str:
        """First configured origin — used to build links inside emails."""
        first = self.frontend_url.split(",")[0].strip().rstrip("/")
        return first or "http://localhost:3000"

    @property
    def from_email(self) -> str:
        """Envelope sender; falls back to the SMTP username."""
        return self.smtp_from_email or self.smtp_user


@lru_cache
def get_settings() -> Settings:
    """Cached settings singleton."""
    return Settings()
