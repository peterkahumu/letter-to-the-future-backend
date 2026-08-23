"""
Health and readiness endpoints for deployment monitoring.
"""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Response, status
from sqlalchemy import text

from app.database import AsyncSessionLocal
from app.schemas import HealthResponse
from app.services import scheduler

router = APIRouter(tags=["health"])

# Scheduler runs hourly; two missed passes means something is wrong.
SCHEDULER_STALE_AFTER = timedelta(hours=2, minutes=15)


@router.head("/health", include_in_schema=False)
@router.get("/health", response_model=HealthResponse, summary="Health check")
async def health_check() -> dict:
    """Liveness. Answers HEAD as well as GET — uptime monitors default to
    HEAD, which used to log a 405 every five minutes (P9)."""
    return {"status": "ok", "version": "1.0.0"}


@router.head("/health/ready", include_in_schema=False)
@router.get("/health/ready", summary="Readiness — database and delivery pipeline")
async def readiness(response: Response) -> dict:
    """
    Deep check for your uptime monitor to watch (P6).

    Returns 503 if the database is unreachable or the scheduler has stopped
    advancing, so silent delivery failure pages someone instead of sitting
    in the logs unnoticed.
    """
    problems: list[str] = []

    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
    except Exception as e:  # noqa: BLE001
        problems.append(f"database unreachable: {e}")

    last = scheduler.LAST_RUN_AT
    if last is None:
        scheduler_state = "not yet run"
    else:
        age = datetime.now(timezone.utc) - last
        scheduler_state = last.isoformat()
        if age > SCHEDULER_STALE_AFTER:
            problems.append(f"scheduler last ran {age} ago")

    if scheduler.LAST_RUN_ERROR:
        problems.append(f"last scheduler pass failed: {scheduler.LAST_RUN_ERROR}")

    if problems:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "degraded", "problems": problems, "scheduler_last_run": scheduler_state}

    return {"status": "ok", "scheduler_last_run": scheduler_state}
