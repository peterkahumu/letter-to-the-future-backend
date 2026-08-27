"""
Dashboard tests, and the letter-ownership rules accounts introduce.

The rule under test throughout: an account changes who can *see* a letter
listed. It never changes whether a letter can be rewritten.
"""

from datetime import date, timedelta

import pytest

from tests.conftest import auth_header

pytestmark = pytest.mark.asyncio


# --- Guests keep working ------------------------------------------------

async def test_a_guest_can_still_write_a_letter(client):
    """
    Accounts are additive. The flow that existed before them has to keep
    working exactly as it did, or adding accounts broke the front door.
    """
    resp = await client.post(
        "/api/letters",
        json={
            "recipient_email": "future@example.com",
            "sender_name": "Past Me",
            "subject": "Hello",
            "body": "<p>Hi there.</p>",
            "deliver_at": (date.today() + timedelta(days=10)).isoformat(),
            "timezone_name": "UTC",
        },
    )
    assert resp.status_code == 201


async def test_a_guest_letter_has_no_owner(client, db_session):
    from sqlalchemy import select

    from app.models import Letter

    await client.post(
        "/api/letters",
        json={
            "recipient_email": "future@example.com",
            "subject": "Hello",
            "body": "<p>Hi.</p>",
            "deliver_at": (date.today() + timedelta(days=10)).isoformat(),
        },
    )
    letter = (await db_session.execute(select(Letter))).scalars().one()
    assert letter.user_id is None


async def test_a_signed_in_writer_owns_their_letter(client, user, db_session):
    from sqlalchemy import select

    from app.models import Letter

    resp = await client.post(
        "/api/letters",
        json={
            "recipient_email": "future@example.com",
            "subject": "Hello",
            "body": "<p>Hi.</p>",
            "deliver_at": (date.today() + timedelta(days=10)).isoformat(),
        },
        headers=auth_header(user),
    )
    assert resp.status_code == 201

    letter = (await db_session.execute(select(Letter))).scalars().one()
    assert letter.user_id == user.id


async def test_an_invalid_token_does_not_block_writing(client):
    """
    The optional-user dependency must never raise. A stale token in a
    browser should degrade to writing as a guest, not to a 401 on compose.
    """
    resp = await client.post(
        "/api/letters",
        json={
            "recipient_email": "future@example.com",
            "subject": "Hello",
            "body": "<p>Hi.</p>",
            "deliver_at": (date.today() + timedelta(days=10)).isoformat(),
        },
        headers={"Authorization": "Bearer stale-and-invalid"},
    )
    assert resp.status_code == 201


# --- The dashboard ------------------------------------------------------

async def test_the_dashboard_needs_an_account(client):
    assert (await client.get("/api/dashboard")).status_code == 401


async def test_the_dashboard_summarises_what_you_wrote(
    client, user, db_session, sample_letter, journal, entry
):
    sample_letter.user_id = user.id
    await db_session.commit()

    resp = await client.get("/api/dashboard", headers=auth_header(user))
    assert resp.status_code == 200
    body = resp.json()

    assert body["user"]["email"] == user.email
    assert body["letters_total"] == 1
    assert body["letters_scheduled"] == 1
    assert body["journals_total"] == 1
    assert body["entries_total"] == 1
    assert body["next_delivery"] == sample_letter.deliver_at.isoformat()
    assert len(body["recent_letters"]) == 1
    assert len(body["recent_entries"]) == 1


async def test_the_dashboard_masks_the_recipient(client, user, db_session, sample_letter):
    sample_letter.user_id = user.id
    await db_session.commit()

    resp = await client.get("/api/dashboard", headers=auth_header(user))
    shown = resp.json()["recent_letters"][0]["recipient_email_masked"]
    assert "•" in shown
    assert shown != sample_letter.recipient_email


async def test_the_dashboard_only_counts_your_own(
    client, other_user, db_session, sample_letter, journal, entry, user
):
    sample_letter.user_id = user.id
    await db_session.commit()

    resp = await client.get("/api/dashboard", headers=auth_header(other_user))
    body = resp.json()
    assert body["letters_total"] == 0
    assert body["journals_total"] == 0
    assert body["entries_total"] == 0


async def test_my_letters_lists_only_your_own(
    client, user, other_user, db_session, sample_letter
):
    sample_letter.user_id = user.id
    await db_session.commit()

    mine = await client.get("/api/dashboard/letters", headers=auth_header(user))
    theirs = await client.get("/api/dashboard/letters", headers=auth_header(other_user))

    assert len(mine.json()) == 1
    assert theirs.json() == []


async def test_my_letters_can_be_filtered_by_status(client, user, db_session, sample_letter):
    sample_letter.user_id = user.id
    await db_session.commit()

    scheduled = await client.get(
        "/api/dashboard/letters?status=scheduled", headers=auth_header(user)
    )
    sent = await client.get("/api/dashboard/letters?status=sent", headers=auth_header(user))

    assert len(scheduled.json()) == 1
    assert sent.json() == []


async def test_an_unknown_status_filter_is_rejected(client, user):
    resp = await client.get(
        "/api/dashboard/letters?status=nonsense", headers=auth_header(user)
    )
    assert resp.status_code == 422


async def test_the_dashboard_offers_no_way_to_edit_a_letter(client, user):
    """
    Guards the concept itself. The dashboard lists letters; if it ever
    gained a write method, a letter from your past self would stop being
    one.
    """
    from app.main import app

    for path, spec in app.openapi()["paths"].items():
        if path.startswith("/api/dashboard"):
            assert set(spec.keys()) <= {"get"}, f"{path} gained a write method"

