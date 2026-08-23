"""
Tests for the abuse and configuration controls.

These deliberately run with the rate limiter ENABLED — the rest of the suite
disables it, which is exactly why a slowapi wiring bug (missing `response`
parameter) reached a live smoke test instead of being caught here.
"""

from datetime import date

import pytest

from app.core.limiter import limiter


@pytest.fixture
def rate_limits_on():
    limiter.enabled = True
    limiter.reset()
    yield
    limiter.reset()
    limiter.enabled = False


def _payload(i: int) -> dict:
    return {
        "recipient_email": f"rl{i}@example.com",
        "sender_name": "Rate Limited",
        "subject": "Testing",
        "body": "Body text.",
        "deliver_at": date.today().isoformat(),
        "is_public": False,
        "is_anonymous": True,
        "timezone_name": "UTC",
    }


@pytest.mark.asyncio
async def test_rate_limited_endpoint_still_returns_201(client, rate_limits_on):
    """
    The decorated endpoint must work with limits on. slowapi injects
    X-RateLimit-* headers and raises unless the handler takes a `response`
    parameter — without it every create 500s in production.
    """
    response = await client.post("/api/letters", json=_payload(0))
    assert response.status_code == 201
    assert response.headers.get("x-ratelimit-limit")


@pytest.mark.asyncio
async def test_create_is_rate_limited(client, rate_limits_on):
    """Unthrottled creation is a free bulk mailer (E5)."""
    codes = []
    for i in range(8):
        response = await client.post("/api/letters", json=_payload(i))
        codes.append(response.status_code)

    assert 429 in codes, f"expected a 429 within 8 requests, got {codes}"
    assert codes[0] == 201


@pytest.mark.asyncio
async def test_upload_rejects_unsupported_type(client):
    """Uploads are type-capped so the endpoint isn't free file hosting (E5)."""
    files = {"file": ("evil.html", b"<html>x</html>", "text/html")}
    response = await client.post("/api/letters/upload", files=files)
    assert response.status_code in (415, 500)  # 500 only if Cloudinary unset


@pytest.mark.asyncio
async def test_pending_letters_capped_per_recipient(client, db_session):
    """Confirmation mail itself must not become a spam vector (E5)."""
    from app.config import get_settings
    from app.models import LetterStatus
    from tests.conftest import _letter

    settings = get_settings()
    for _ in range(settings.max_pending_per_recipient):
        db_session.add(_letter(
            recipient_email="flood@example.com", status=LetterStatus.PENDING
        ))
    await db_session.commit()

    payload = _payload(1)
    payload["recipient_email"] = "flood@example.com"
    response = await client.post("/api/letters", json=payload)
    assert response.status_code == 429


@pytest.mark.asyncio
async def test_health_answers_head(client):
    """Uptime monitors send HEAD; a GET-only route logs a 405 (P9)."""
    assert (await client.head("/health")).status_code == 200


@pytest.mark.asyncio
async def test_readiness_reports_stale_scheduler(client):
    """Silent delivery failure must surface as a 503 (P6)."""
    from datetime import datetime, timedelta, timezone

    from app.services import scheduler as sched

    original = sched.LAST_RUN_AT
    sched.LAST_RUN_AT = datetime.now(timezone.utc) - timedelta(hours=6)
    try:
        response = await client.get("/health/ready")
        assert response.status_code == 503
        assert response.json()["status"] == "degraded"
    finally:
        sched.LAST_RUN_AT = original
