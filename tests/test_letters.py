"""
Unit and integration tests for the letters API.
"""

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models import EmailSuppression, Letter, LetterStatus
from tests.conftest import CONFIRM_TOKEN, MANAGE_TOKEN, _letter


def _payload(**overrides) -> dict:
    base = {
        "recipient_email": "test@example.com",
        "sender_name": "Test User",
        "subject": "Hello Future",
        "body": "This is a test letter to my future self.",
        "deliver_at": (date.today() + timedelta(days=7)).isoformat(),
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------
# Creating
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_letter_returns_pending_and_masks_email(client):
    """A new letter is pending confirmation and never echoes the address."""
    response = await client.post("/api/letters", json=_payload())
    assert response.status_code == 201

    data = response.json()
    assert data["status"] == LetterStatus.PENDING
    assert data["recipient_email_masked"].endswith("@example.com")
    assert "test@example.com" not in response.text
    assert "body" not in data


@pytest.mark.asyncio
async def test_create_letter_sends_confirmation(client, _no_outbound_mail):
    """Creating a letter emails the recipient a confirmation link (P1)."""
    await client.post("/api/letters", json=_payload())
    assert _no_outbound_mail.call_count == 1


@pytest.mark.asyncio
async def test_create_letter_default_sender_name(client, db_session):
    """POST /api/letters uses 'Past You' as default sender name."""
    payload = _payload()
    payload.pop("sender_name")
    response = await client.post("/api/letters", json=payload)
    assert response.status_code == 201

    letter = (await db_session.execute(select(Letter))).scalars().one()
    assert letter.sender_name == "Past You"


@pytest.mark.asyncio
async def test_create_letter_sanitizes_body(client, db_session):
    """Script and event handlers are stripped before storage (E4)."""
    hostile = '<p>hi</p><script>alert(1)</script><img src=x onerror="alert(2)">'
    response = await client.post("/api/letters", json=_payload(body=hostile))
    assert response.status_code == 201

    letter = (await db_session.execute(select(Letter))).scalars().one()
    assert "<script>" not in letter.body
    assert "onerror" not in letter.body
    assert "<p>hi</p>" in letter.body


@pytest.mark.asyncio
async def test_create_letter_past_date_rejected(client):
    """POST /api/letters rejects delivery dates before the current date."""
    utc_yesterday = datetime.now(timezone.utc).date() - timedelta(days=1)
    response = await client.post(
        "/api/letters", json=_payload(deliver_at=utc_yesterday.isoformat())
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_create_letter_today_date_accepted(client):
    """Today's date is allowed; the hourly scheduler sends it."""
    utc_today = datetime.now(timezone.utc).date()
    response = await client.post(
        "/api/letters", json=_payload(deliver_at=utc_today.isoformat())
    )
    assert response.status_code == 201
    assert response.json()["deliver_at"] == utc_today.isoformat()


@pytest.mark.asyncio
async def test_create_letter_invalid_email(client):
    """POST /api/letters rejects invalid email addresses."""
    response = await client.post("/api/letters", json=_payload(recipient_email="nope"))
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_create_letter_rejects_unknown_timezone(client):
    response = await client.post("/api/letters", json=_payload(timezone_name="Mars/Olympus"))
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_create_letter_rejects_non_http_media_url(client):
    """javascript: URLs must never reach an <img src> in an email."""
    response = await client.post(
        "/api/letters", json=_payload(media_url="javascript:alert(1)")
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_create_letter_refuses_suppressed_address(client, db_session):
    """An unsubscribed address cannot be written to again (P5)."""
    db_session.add(EmailSuppression(email="test@example.com"))
    await db_session.commit()

    response = await client.post("/api/letters", json=_payload())
    assert response.status_code == 403


# --------------------------------------------------------------------------
# Confirming
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_confirm_schedules_letter(client, pending_letter):
    response = await client.post(
        f"/api/letters/{pending_letter.id}/confirm?token={CONFIRM_TOKEN}"
    )
    assert response.status_code == 200
    assert response.json()["status"] == LetterStatus.SCHEDULED


@pytest.mark.asyncio
async def test_confirm_rejects_bad_token(client, pending_letter):
    response = await client.post(
        f"/api/letters/{pending_letter.id}/confirm?token=wrong-token-aaaaaaaaaaaaaaaa"
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_confirm_token_is_single_use(client, pending_letter):
    first = await client.post(
        f"/api/letters/{pending_letter.id}/confirm?token={CONFIRM_TOKEN}"
    )
    assert first.status_code == 200
    second = await client.post(
        f"/api/letters/{pending_letter.id}/confirm?token={CONFIRM_TOKEN}"
    )
    assert second.status_code == 403


# --------------------------------------------------------------------------
# Reading status
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_letter_masks_recipient(client, sample_letter):
    """A letter id alone must not disclose the address (E1)."""
    response = await client.get(f"/api/letters/{sample_letter.id}")
    assert response.status_code == 200
    assert "future@example.com" not in response.text
    assert response.json()["recipient_email_masked"].endswith("@example.com")


@pytest.mark.asyncio
async def test_get_letter_not_found(client):
    response = await client.get("/api/letters/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404


# --------------------------------------------------------------------------
# Cancelling (P2)
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cancel_letter_with_token(client, sample_letter, db_session):
    response = await client.delete(f"/api/letters/{sample_letter.id}?token={MANAGE_TOKEN}")
    assert response.status_code == 200

    await db_session.refresh(sample_letter)
    assert sample_letter.status == LetterStatus.CANCELLED


@pytest.mark.asyncio
async def test_cancel_requires_valid_token(client, sample_letter):
    response = await client.delete(
        f"/api/letters/{sample_letter.id}?token=not-the-right-token-xxxxxxxx"
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_cannot_cancel_delivered_letter(client, db_session):
    letter = _letter(status=LetterStatus.SENT, sent_at=datetime.now(timezone.utc))
    db_session.add(letter)
    await db_session.commit()

    response = await client.delete(f"/api/letters/{letter.id}?token={MANAGE_TOKEN}")
    assert response.status_code == 409


# --------------------------------------------------------------------------
# Public feed (E1, E2, P4, R5)
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_public_feed_never_exposes_recipient(client, db_session):
    letter = _letter(
        is_public=True, status=LetterStatus.SENT, sent_at=datetime.now(timezone.utc)
    )
    db_session.add(letter)
    await db_session.commit()

    response = await client.get("/api/letters/feed/public")
    assert response.status_code == 200
    assert "future@example.com" not in response.text
    assert "recipient_email" not in response.text


@pytest.mark.asyncio
async def test_public_feed_honours_anonymity(client, db_session):
    """is_anonymous actually hides the sender name (E2)."""
    anon = _letter(
        is_public=True, is_anonymous=True, sender_name="Real Name",
        status=LetterStatus.SENT, sent_at=datetime.now(timezone.utc),
    )
    named = _letter(
        is_public=True, is_anonymous=False, sender_name="Openly Me",
        status=LetterStatus.SENT, sent_at=datetime.now(timezone.utc),
    )
    db_session.add_all([anon, named])
    await db_session.commit()

    items = (await client.get("/api/letters/feed/public")).json()["items"]
    names = {item["sender_name"] for item in items}
    assert names == {None, "Openly Me"}
    assert "Real Name" not in (await client.get("/api/letters/feed/public")).text


@pytest.mark.asyncio
async def test_public_feed_excludes_unsent_and_hidden(client, db_session):
    db_session.add_all([
        _letter(is_public=True, status=LetterStatus.SCHEDULED),
        _letter(is_public=True, status=LetterStatus.SENT,
                sent_at=datetime.now(timezone.utc), is_hidden=True),
        _letter(is_public=False, status=LetterStatus.SENT,
                sent_at=datetime.now(timezone.utc)),
    ])
    await db_session.commit()

    items = (await client.get("/api/letters/feed/public")).json()["items"]
    assert items == []


@pytest.mark.asyncio
async def test_public_feed_paginates(client, db_session):
    now = datetime.now(timezone.utc)
    for i in range(5):
        db_session.add(_letter(
            is_public=True, status=LetterStatus.SENT,
            subject=f"Letter {i}", sent_at=now - timedelta(minutes=i),
        ))
    await db_session.commit()

    first = (await client.get("/api/letters/feed/public?limit=2")).json()
    assert len(first["items"]) == 2
    assert first["next_cursor"]

    second = (await client.get(
        f"/api/letters/feed/public?limit=2&cursor={first['next_cursor']}"
    )).json()
    assert len(second["items"]) == 2
    first_subjects = {i["subject"] for i in first["items"]}
    second_subjects = {i["subject"] for i in second["items"]}
    assert first_subjects.isdisjoint(second_subjects)


# --------------------------------------------------------------------------
# Moderation and unsubscribe (P4, P5)
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reports_hide_letter_after_threshold(client, db_session):
    letter = _letter(
        is_public=True, status=LetterStatus.SENT, sent_at=datetime.now(timezone.utc)
    )
    db_session.add(letter)
    await db_session.commit()

    for reason in ("spam", "harassment", "other"):
        r = await client.post(
            f"/api/letters/{letter.id}/report", json={"reason": reason}
        )
        assert r.status_code == 200

    await db_session.refresh(letter)
    assert letter.is_hidden is True
    assert (await client.get("/api/letters/feed/public")).json()["items"] == []


@pytest.mark.asyncio
async def test_report_records_reason_and_details(client, db_session):
    """Moderation needs to know why something was flagged, not just how often."""
    from app.models import LetterReport

    letter = _letter(
        is_public=True, status=LetterStatus.SENT, sent_at=datetime.now(timezone.utc)
    )
    db_session.add(letter)
    await db_session.commit()

    r = await client.post(
        f"/api/letters/{letter.id}/report",
        json={"reason": "personal_info", "details": "  contains a home address  "},
    )
    assert r.status_code == 200

    reports = (await db_session.execute(select(LetterReport))).scalars().all()
    assert len(reports) == 1
    assert reports[0].reason == "personal_info"
    assert reports[0].details == "contains a home address"


@pytest.mark.asyncio
async def test_report_requires_a_known_reason(client, sample_letter):
    r = await client.post(
        f"/api/letters/{sample_letter.id}/report", json={"reason": "because"}
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_report_details_are_optional(client, db_session):
    letter = _letter(is_public=True, status=LetterStatus.SENT,
                     sent_at=datetime.now(timezone.utc))
    db_session.add(letter)
    await db_session.commit()

    r = await client.post(f"/api/letters/{letter.id}/report", json={"reason": "spam"})
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_unsubscribe_suppresses_and_cancels(client, db_session):
    from app.core.security import unsubscribe_token

    letter = _letter(recipient_email="stop@example.com", status=LetterStatus.SCHEDULED)
    db_session.add(letter)
    await db_session.commit()

    token = unsubscribe_token("stop@example.com")
    response = await client.post(
        f"/api/letters/unsubscribe?email=stop@example.com&t={token}"
    )
    assert response.status_code == 200

    await db_session.refresh(letter)
    assert letter.status == LetterStatus.CANCELLED

    suppressed = (await db_session.execute(select(EmailSuppression))).scalars().all()
    assert [s.email for s in suppressed] == ["stop@example.com"]


@pytest.mark.asyncio
async def test_unsubscribe_rejects_forged_token(client):
    response = await client.post(
        "/api/letters/unsubscribe?email=stop@example.com&t=forged"
    )
    assert response.status_code == 403


# --------------------------------------------------------------------------
# Retired endpoints (E3, E6)
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_open_relay_endpoint_is_gone(client):
    """The unauthenticated send-to-anyone route must not exist (E3)."""
    from app.main import app

    assert "/api/letters/test-email" not in app.openapi()["paths"]
    response = await client.post("/api/letters/test-email?email=a@b.com")
    # 405: the path shape matches /{letter_id}, which has no POST handler.
    assert response.status_code in (404, 405)


@pytest.mark.asyncio
async def test_debug_cors_endpoint_is_gone(client):
    response = await client.get("/api/debug/cors")
    assert response.status_code == 404
