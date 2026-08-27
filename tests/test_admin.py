"""
Admin tests.

The most important assertions in this file are the negative ones: that the
admin surface is invisible to ordinary users, and that being an admin does
not come with a way to read anyone's journal.
"""

import pytest
from sqlalchemy import select

from app.models import DeliveryLog, EmailSuppression, Letter, LetterReport, LetterStatus, User, UserRole
from tests.conftest import auth_header

pytestmark = pytest.mark.asyncio

ADMIN_PATHS = [
    "/api/admin/stats",
    "/api/admin/users",
    "/api/admin/reports",
    "/api/admin/delivery/logs",
    "/api/admin/delivery/failed",
    "/api/admin/suppressions",
]


# --- Who can get in -----------------------------------------------------

@pytest.mark.parametrize("path", ADMIN_PATHS)
async def test_admin_routes_reject_anonymous_callers(client, path):
    assert (await client.get(path)).status_code == 401


@pytest.mark.parametrize("path", ADMIN_PATHS)
async def test_admin_routes_are_a_404_for_ordinary_users(client, user, path):
    """
    404 rather than 403 on purpose — a 403 tells someone probing that the
    admin surface is there and they simply lack the role.
    """
    assert (await client.get(path, headers=auth_header(user))).status_code == 404


@pytest.mark.parametrize("path", ADMIN_PATHS)
async def test_admin_routes_admit_admins(client, admin_user, path):
    assert (await client.get(path, headers=auth_header(admin_user))).status_code == 200


async def test_an_admin_is_also_an_ordinary_user(client, admin_user):
    """
    The role widens what they may do; it does not replace their account.
    An admin still has their own dashboard, letters, and journals.
    """
    assert (await client.get("/api/auth/me", headers=auth_header(admin_user))).status_code == 200
    assert (await client.get("/api/dashboard", headers=auth_header(admin_user))).status_code == 200

    created = await client.post(
        "/api/journals", json={"title": "Admin's own"}, headers=auth_header(admin_user)
    )
    assert created.status_code == 201


async def test_demotion_takes_effect_immediately(client, admin_user, db_session, user):
    """
    The role is read from the database on every request, not trusted from
    the token, so a token minted before a demotion stops working at once.
    """
    header = auth_header(admin_user)
    assert (await client.get("/api/admin/stats", headers=header)).status_code == 200

    # Another admin has to exist, or the last-admin guard blocks the change.
    user.role = UserRole.ADMIN
    admin_user.role = UserRole.USER
    await db_session.commit()

    assert (await client.get("/api/admin/stats", headers=header)).status_code == 404


# --- The privacy boundary -----------------------------------------------

async def test_admin_sees_journal_counts_but_never_content(
    client, admin_user, user, journal, entry
):
    """
    The line this feature must not cross. Administering the service means
    knowing that someone writes; it does not mean reading what they wrote.
    """
    resp = await client.get(f"/api/admin/users/{user.id}", headers=auth_header(admin_user))
    assert resp.status_code == 200
    body = resp.json()

    assert body["journals_count"] == 1
    assert body["entries_count"] == 1

    serialized = str(body)
    assert "hot tarmac" not in serialized       # the entry body
    assert "Landed in Nairobi" not in serialized  # the entry title
    assert "Travel 2026" not in serialized        # even the journal title


async def test_no_admin_route_returns_a_journal_entry(client, admin_user, user, journal, entry):
    """
    Swept across the whole admin surface rather than one endpoint, so a
    route added later that leaks an entry fails here.
    """
    for path in ADMIN_PATHS + [f"/api/admin/users/{user.id}"]:
        resp = await client.get(path, headers=auth_header(admin_user))
        assert resp.status_code == 200, path
        assert "hot tarmac" not in resp.text, f"{path} leaked an entry body"
        assert "Landed in Nairobi" not in resp.text, f"{path} leaked an entry title"


async def test_admin_cannot_reach_another_users_journals_through_the_journal_api(
    client, admin_user, journal, entry
):
    """
    Being an admin is not a key to the journal endpoints — those authorise
    on ownership alone, and an admin owns no more than anyone else.
    """
    assert (
        await client.get(f"/api/journals/{journal.id}", headers=auth_header(admin_user))
    ).status_code == 404
    assert (
        await client.get(
            f"/api/journals/entries/{entry.id}", headers=auth_header(admin_user)
        )
    ).status_code == 404


# --- Stats --------------------------------------------------------------

async def test_stats_counts_users_and_letters(client, admin_user, user, sample_letter):
    resp = await client.get("/api/admin/stats", headers=auth_header(admin_user))
    body = resp.json()

    assert body["users_total"] == 2
    assert body["admins_total"] == 1
    assert body["letters_total"] == 1
    assert body["letters_scheduled"] == 1
    # The fixture letter has no owner, so it counts as a guest letter.
    assert body["letters_guest"] == 1


# --- Users --------------------------------------------------------------

async def test_users_can_be_searched(client, admin_user, user, other_user):
    resp = await client.get(
        "/api/admin/users?q=someone.else", headers=auth_header(admin_user)
    )
    assert [u["email"] for u in resp.json()["items"]] == [other_user.email]


async def test_a_user_can_be_promoted(client, admin_user, user):
    resp = await client.patch(
        f"/api/admin/users/{user.id}",
        json={"role": "admin"},
        headers=auth_header(admin_user),
    )
    assert resp.status_code == 200
    assert resp.json()["role"] == "admin"


async def test_suspending_a_user_ends_their_sessions_now(client, admin_user, user):
    """
    A stateless JWT cannot be recalled, which is exactly why sessions are
    rows. Suspension has to bite immediately or it isn't suspension.
    """
    from tests.conftest import TEST_PASSWORD

    session = (
        await client.post(
            "/api/auth/login", json={"email": user.email, "password": TEST_PASSWORD}
        )
    ).json()

    await client.patch(
        f"/api/admin/users/{user.id}",
        json={"is_active": False},
        headers=auth_header(admin_user),
    )

    assert (
        await client.post(
            "/api/auth/refresh", json={"refresh_token": session["refresh_token"]}
        )
    ).status_code == 401
    # And the access token they already hold is dead too.
    assert (await client.get("/api/auth/me", headers=auth_header(user))).status_code == 403


async def test_an_admin_cannot_change_their_own_role(client, admin_user):
    resp = await client.patch(
        f"/api/admin/users/{admin_user.id}",
        json={"role": "user"},
        headers=auth_header(admin_user),
    )
    assert resp.status_code == 409


async def test_an_admin_cannot_suspend_themselves(client, admin_user):
    resp = await client.patch(
        f"/api/admin/users/{admin_user.id}",
        json={"is_active": False},
        headers=auth_header(admin_user),
    )
    assert resp.status_code == 409


async def test_the_last_admin_cannot_be_demoted(client, admin_user, db_session):
    """Otherwise the service can be locked out of its own admin surface."""
    second = User(
        email="second-admin@example.com",
        password_hash="x",
        display_name="Second",
        role=UserRole.ADMIN,
    )
    db_session.add(second)
    await db_session.commit()
    await db_session.refresh(second)

    # Two admins: demoting one is fine.
    assert (
        await client.patch(
            f"/api/admin/users/{second.id}",
            json={"role": "user"},
            headers=auth_header(admin_user),
        )
    ).status_code == 200

    # Now there is one left, and no one may remove it.
    other = User(email="third@example.com", password_hash="x", role=UserRole.ADMIN)
    db_session.add(other)
    await db_session.commit()
    await db_session.refresh(other)

    await client.patch(
        f"/api/admin/users/{admin_user.id}",
        json={"role": "user"},
        headers=auth_header(other),
    )
    resp = await client.patch(
        f"/api/admin/users/{other.id}", json={"role": "user"}, headers=auth_header(other)
    )
    assert resp.status_code == 409


async def test_deleting_a_user_detaches_their_letters(
    client, admin_user, user, sample_letter, db_session
):
    sample_letter.user_id = user.id
    await db_session.commit()

    resp = await client.delete(
        f"/api/admin/users/{user.id}", headers=auth_header(admin_user)
    )
    assert resp.status_code == 200

    letter = (
        await db_session.execute(select(Letter).where(Letter.id == sample_letter.id))
    ).scalar_one()
    assert letter.user_id is None
    assert letter.status == LetterStatus.SCHEDULED


async def test_an_admin_cannot_delete_themselves_from_here(client, admin_user):
    resp = await client.delete(
        f"/api/admin/users/{admin_user.id}", headers=auth_header(admin_user)
    )
    assert resp.status_code == 409


# --- Moderation ---------------------------------------------------------

async def test_the_report_queue_shows_flagged_letters(
    client, admin_user, sample_letter, db_session
):
    sample_letter.is_public = True
    sample_letter.reported_count = 2
    db_session.add(LetterReport(letter_id=sample_letter.id, reason="spam"))
    await db_session.commit()

    resp = await client.get("/api/admin/reports", headers=auth_header(admin_user))
    items = resp.json()
    assert len(items) == 1
    assert items[0]["reported_count"] == 2
    assert items[0]["reasons"] == ["spam"]
    # A public letter's body IS shown — a moderator cannot judge a report
    # without seeing what was reported. This is the deliberate contrast
    # with journals, which have no admin read path at all.
    assert "body" in items[0]


async def test_hiding_a_letter_keeps_it_scheduled_for_delivery(
    client, admin_user, sample_letter, db_session
):
    """
    Hiding takes something off the public board. It does not intercept
    someone's mail — publishing was optional, delivery is the promise.
    """
    resp = await client.post(
        f"/api/admin/letters/{sample_letter.id}/moderate",
        json={"hidden": True, "note": "off-topic"},
        headers=auth_header(admin_user),
    )
    assert resp.status_code == 200

    await db_session.refresh(sample_letter)
    assert sample_letter.is_hidden is True
    assert sample_letter.status == LetterStatus.SCHEDULED


async def test_restoring_a_letter_clears_its_report_count(
    client, admin_user, sample_letter, db_session
):
    """Otherwise the auto-hide threshold trips again on reports already dismissed."""
    sample_letter.is_hidden = True
    sample_letter.reported_count = 5
    await db_session.commit()

    await client.post(
        f"/api/admin/letters/{sample_letter.id}/moderate",
        json={"hidden": False},
        headers=auth_header(admin_user),
    )
    await db_session.refresh(sample_letter)
    assert sample_letter.is_hidden is False
    assert sample_letter.reported_count == 0


async def test_ordinary_users_cannot_moderate(client, user, sample_letter):
    resp = await client.post(
        f"/api/admin/letters/{sample_letter.id}/moderate",
        json={"hidden": True},
        headers=auth_header(user),
    )
    assert resp.status_code == 404


# --- Delivery health ----------------------------------------------------

async def test_delivery_logs_carry_a_masked_address(
    client, admin_user, sample_letter, db_session
):
    db_session.add(
        DeliveryLog(letter_id=sample_letter.id, outcome="accepted", detail="250 OK")
    )
    await db_session.commit()

    resp = await client.get("/api/admin/delivery/logs", headers=auth_header(admin_user))
    item = resp.json()[0]
    assert item["outcome"] == "accepted"
    assert item["subject"] == sample_letter.subject
    # Even an admin gets the masked form — the full address is not needed
    # to answer "did this go out".
    assert "•" in item["recipient_email_masked"]
    assert item["recipient_email_masked"] != sample_letter.recipient_email


async def test_a_failed_letter_can_be_requeued(client, admin_user, sample_letter, db_session):
    sample_letter.status = LetterStatus.FAILED
    sample_letter.attempts = 5
    sample_letter.last_error = "SMTP timeout"
    await db_session.commit()

    resp = await client.post(
        f"/api/admin/delivery/{sample_letter.id}/retry", headers=auth_header(admin_user)
    )
    assert resp.status_code == 200

    await db_session.refresh(sample_letter)
    assert sample_letter.status == LetterStatus.SCHEDULED
    assert sample_letter.attempts == 0
    assert sample_letter.last_error is None


async def test_only_failed_letters_can_be_requeued(client, admin_user, sample_letter):
    resp = await client.post(
        f"/api/admin/delivery/{sample_letter.id}/retry", headers=auth_header(admin_user)
    )
    assert resp.status_code == 409


async def test_a_suppression_can_be_lifted(client, admin_user, db_session):
    db_session.add(EmailSuppression(email="bounced@example.com", reason="hard_bounce"))
    await db_session.commit()

    resp = await client.delete(
        "/api/admin/suppressions/bounced@example.com", headers=auth_header(admin_user)
    )
    assert resp.status_code == 200
    assert (await db_session.execute(select(EmailSuppression))).scalars().all() == []
