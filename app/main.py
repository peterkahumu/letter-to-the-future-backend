"""
FastAPI application entry point.
Configures CORS, rate limiting, routers, and the hourly letter-sending scheduler.
"""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi_utilities import repeat_at
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.config import get_settings
from app.core.limiter import limiter
from app.core.logging import configure_logging
from app.routers import admin, auth, dashboard, health, journals, letters
from app.services.scheduler import check_and_send_letters

settings = get_settings()
configure_logging(debug=settings.debug)
logger = logging.getLogger(__name__)


# Error tracking (P7). No-op unless SENTRY_DSN is set.
def _init_sentry() -> None:
    import os

    dsn = os.getenv("SENTRY_DSN")
    if not dsn:
        return
    try:
        import sentry_sdk

        sentry_sdk.init(dsn=dsn, traces_sample_rate=0.1, send_default_pii=False)
        logger.info("🛰️  Sentry initialised")
    except ImportError:
        logger.warning("SENTRY_DSN is set but sentry-sdk is not installed.")


async def _run_migrations() -> None:
    """
    Bring the schema up to head on boot (R6).

    Guarded by a Postgres advisory lock so concurrent replicas don't race,
    and skipped entirely on non-Postgres (the test suite runs on SQLite).
    """
    from alembic import command
    from alembic.config import Config

    from app.database import engine

    if engine.dialect.name != "postgresql":
        return

    lock_key = 8_413_552_002
    conn = await engine.connect()
    try:
        got = await conn.exec_driver_sql(f"SELECT pg_try_advisory_lock({lock_key})")
        if not got.scalar():
            logger.info("🔒 Another replica is migrating — skipping.")
            return

        def _upgrade() -> None:
            # alembic/env.py resolves the URL from Settings itself.
            cfg = Config("alembic.ini")
            # Do not let alembic reconfigure logging out from under the app.
            cfg.attributes["configure_logger"] = False
            command.upgrade(cfg, "head")

        await asyncio.get_running_loop().run_in_executor(None, _upgrade)
        # Restore our handlers in case anything in the migration path still
        # reconfigured logging.
        configure_logging(debug=settings.debug)
        logger.info("🗃️  Migrations at head")
        await conn.exec_driver_sql(f"SELECT pg_advisory_unlock({lock_key})")
    except Exception as e:  # noqa: BLE001
        logger.error(f"❌ Migration failed: {e}")
        raise
    finally:
        await conn.close()


@repeat_at(cron="0 * * * *")  # Every hour at minute 0
async def scheduled_letter_check() -> None:
    """Hourly check for letters that need to be sent."""
    await check_and_send_letters()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown lifecycle."""
    configure_logging(debug=settings.debug)
    logger.info("🚀 Starting Letter to the Future API")
    logger.info(
        f"🔧 provider={settings.email_provider} "
        f"batch={settings.scheduler_batch_size} "
        f"max_attempts={settings.max_delivery_attempts} debug={settings.debug}"
    )
    _init_sentry()

    if settings.run_migrations_on_startup:
        await _run_migrations()
    else:
        logger.info("⏭️  Startup migrations disabled (RUN_MIGRATIONS_ON_STARTUP)")

    # Keep a reference — asyncio only holds a weak one, so a fire-and-forget
    # task can be garbage collected mid-flight (R7).
    app.state.scheduler_task = asyncio.create_task(scheduled_letter_check())

    yield

    task = getattr(app.state, "scheduler_task", None)
    if task and not task.done():
        task.cancel()
    logger.info("👋 Shutting down Letter to the Future API")


app = FastAPI(
    title="Letter to the Future",
    description="Write letters to your future self. They'll be delivered by email on the date you choose.",
    version="1.0.0",
    lifespan=lifespan,
)

# Rate limiting (E5)
app.state.limiter = limiter
app.add_middleware(SlowAPIMiddleware)


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content={"detail": "You're doing that too often. Try again in a little while."},
    )


allowed_origins = [o.strip().rstrip("/") for o in settings.frontend_url.split(",") if o.strip()]
logger.info(f"📡 CORS allowed origins: {allowed_origins}")

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_origin_regex=settings.allowed_origin_regex,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(auth.router)
app.include_router(dashboard.router)
app.include_router(journals.router)
app.include_router(letters.router)
app.include_router(admin.router)
