"""
FastAPI application entry point.
Configures CORS, routers, and the hourly letter-sending scheduler.
"""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi_utilities import repeat_at

from app.config import get_settings
from app.routers import health, letters
from app.services.scheduler import check_and_send_letters

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(levelname)-8s │ %(name)s │ %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

settings = get_settings()


# Scheduler
@repeat_at(cron="0 * * * *")  # Every hour at minute 0
async def scheduled_letter_check() -> None:
    """Hourly check for letters that need to be sent."""
    await check_and_send_letters()


# Lifespan
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown lifecycle."""
    logger.info("🚀 Starting Letter to the Future API")
    logger.info(f"📡 CORS allowed origin: {settings.frontend_url}")

    # Kick off the scheduler in the background
    asyncio.create_task(scheduled_letter_check())

    yield

    logger.info("👋 Shutting down Letter to the Future API")


# App
app = FastAPI(
    title="Letter to the Future",
    description="Write letters to your future self. They'll be delivered by email on the date you choose.",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip().rstrip("/") for origin in settings.frontend_url.split(",")],
    allow_origin_regex=settings.allowed_origin_regex,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routers
app.include_router(health.router)
app.include_router(letters.router)
