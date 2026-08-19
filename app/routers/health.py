"""
Health check router for deployment monitoring.
"""

from fastapi import APIRouter

from app.schemas import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse, summary="Health check")
async def health_check() -> dict:
    """Returns application health status."""
    return {"status": "ok", "version": "1.0.0"}
