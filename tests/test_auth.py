"""
Account tests.

The emphasis is on the parts that would be quietly wrong rather than
loudly broken: enumeration answers, token rotation, and whether revoking a
session actually revokes anything.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.core.auth import create_access_token, hash_refresh_token, verify_password
from app.core.security import hash_token, new_token
from app.models import RefreshToken, User, UserRole
from tests.conftest import TEST_PASSWORD, auth_header

pytestmark = pytest.mark.asyncio


# --- Registration -------------------------------------------------------

async def test_register_creates_an_unverified_account(client, db_session):
    resp = await client.post(
        "/api/auth/register",
        json={
            "email": "New.Person@Example.com",
            "password": "a-long-enough-password",
            "display_name": "New Person",
        },
    )
    assert resp.status_code == 201

    user = (
        await db_session.execute(select(User).where(User.email == "new.person@example.com"))
    ).scalar_one()
    # Stored lower-cased, whatever case they typed.
    assert user.email == "new.person@example.com"
    assert user.display_name == "New Person"
    assert user.role == UserRole.USER


async def test_register_never_stores_the_plaintext_password(client, db_session):
    await client.post(
        "/api/auth/register",
        json={"email": "hash@example.com", "password": "a-long-enough-password"},
    )
    user = (
        await db_session.execute(select(User).where(User.email == "hash@example.com"))
    ).scalar_one()

    assert user.password_hash != "a-long-enough-password"
    assert user.password_hash.startswith("$2b$")
    assert verify_password("a-long-enough-password", user.password_hash)


async def test_register_does_not_reveal_an_existing_account(client, user):
    """
    The whole point: signing up with a taken address must be indistinguishable
    from signing up with a free one, or this endpoint tells an attacker who
    has an account here.
    """
    fresh = await client.post(
        "/api/auth/register",
        json={"email": "brand-new@example.com", "password": "a-long-enough-password"},
    )
    taken = await client.post(
        "/api/auth/register",
        json={"email": user.email, "password": "a-different-password"},
    )

    assert fresh.status_code == taken.status_code == 201
    assert fresh.json() == taken.json()


async def test_register_with_taken_address_does_not_change_the_password(client, user, db_session):
    await client.post(
        "/api/auth/register",
        json={"email": user.email, "password": "attacker-chosen-password"},
    )
    await db_session.refresh(user)
    assert verify_password(TEST_PASSWORD, user.password_hash)


async def test_short_passwords_are_rejected(client):
    resp = await client.post(
        "/api/auth/register", json={"email": "short@example.com", "password": "abc"}
    )
    assert resp.status_code == 422


async def test_password_over_bcrypts_limit_is_rejected(client):
    """bcrypt ignores everything past 72 bytes — refuse rather than truncate."""
    resp = await client.post(
        "/api/auth/register",
        json={"email": "long@example.com", "password": "x" * 100},
    )
    assert resp.status_code == 422


# --- Login --------------------------------------------------------------

async def test_login_returns_a_token_pair(client, user):
    resp = await client.post(
        "/api/auth/login", json={"email": user.email, "password": TEST_PASSWORD}
    )
    assert resp.status_code == 200
    body = resp.json()

    assert body["token_type"] == "bearer"
    assert body["access_token"] and body["refresh_token"]
    assert body["user"]["email"] == user.email
    assert "password_hash" not in body["user"]


async def test_login_is_case_insensitive_on_the_address(client, user):
    resp = await client.post(
        "/api/auth/login",
        json={"email": user.email.upper(), "password": TEST_PASSWORD},
    )
    assert resp.status_code == 200


async def test_wrong_password_and_unknown_address_answer_alike(client, user):
    wrong = await client.post(
        "/api/auth/login", json={"email": user.email, "password": "not-the-password"}
    )
    missing = await client.post(
        "/api/auth/login", json={"email": "nobody@example.com", "password": "whatever-long"}
    )

    assert wrong.status_code == missing.status_code == 401
    assert wrong.json()["detail"] == missing.json()["detail"]


async def test_suspended_account_cannot_sign_in(client, user, db_session):
    user.is_active = False
    await db_session.commit()

    resp = await client.post(
        "/api/auth/login", json={"email": user.email, "password": TEST_PASSWORD}
    )
    assert resp.status_code == 403


async def test_login_records_the_time(client, user, db_session):
    assert user.last_login_at is None
    await client.post(
        "/api/auth/login", json={"email": user.email, "password": TEST_PASSWORD}
    )
    await db_session.refresh(user)
    assert user.last_login_at is not None


# --- Bearer tokens ------------------------------------------------------

async def test_me_requires_a_token(client):
    assert (await client.get("/api/auth/me")).status_code == 401


async def test_me_returns_the_signed_in_account(client, user):
    resp = await client.get("/api/auth/me", headers=auth_header(user))
    assert resp.status_code == 200
    assert resp.json()["email"] == user.email


async def test_a_garbage_token_is_rejected(client):
    resp = await client.get(
        "/api/auth/me", headers={"Authorization": "Bearer not-a-real-jwt"}
    )
    assert resp.status_code == 401


async def test_an_expired_token_is_rejected(client, user, monkeypatch):
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "access_token_expire_minutes", -1)
    token = create_access_token(user)

    resp = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401


async def test_a_token_for_a_deleted_account_is_rejected(client, user, db_session):
    """
    The user row is re-read on every request, so deletion takes effect at
    once rather than when the token happens to expire.
    """
    header = auth_header(user)
    await db_session.execute(User.__table__.delete().where(User.id == user.id))
    await db_session.commit()

    assert (await client.get("/api/auth/me", headers=header)).status_code == 401


async def test_a_token_for_a_suspended_account_is_rejected(client, user, db_session):
    header = auth_header(user)
    user.is_active = False
    await db_session.commit()

    assert (await client.get("/api/auth/me", headers=header)).status_code == 403


async def test_a_refresh_token_is_not_accepted_as_a_bearer_token(client, user):
    """
    Different credentials with different lifetimes — the `type` claim is
    what stops a long-lived refresh token being spent as an access token.
    """
    login = await client.post(
        "/api/auth/login", json={"email": user.email, "password": TEST_PASSWORD}
    )
    refresh = login.json()["refresh_token"]

    resp = await client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {refresh}"}
    )
    assert resp.status_code == 401


# --- Refresh rotation ---------------------------------------------------

async def test_refresh_rotates_the_token(client, user):
    login = (
        await client.post(
            "/api/auth/login", json={"email": user.email, "password": TEST_PASSWORD}
        )
    ).json()

    resp = await client.post(
        "/api/auth/refresh", json={"refresh_token": login["refresh_token"]}
    )
    assert resp.status_code == 200
    assert resp.json()["refresh_token"] != login["refresh_token"]


async def test_a_rotated_token_stops_working(client, user):
    login = (
        await client.post(
            "/api/auth/login", json={"email": user.email, "password": TEST_PASSWORD}
        )
    ).json()
    await client.post("/api/auth/refresh", json={"refresh_token": login["refresh_token"]})

    reuse = await client.post(
        "/api/auth/refresh", json={"refresh_token": login["refresh_token"]}
    )
    assert reuse.status_code == 401


async def test_reusing_a_rotated_token_kills_every_session(client, user, db_session):
    """
    Reuse means two parties hold the same token and one of them is not the
    owner. There is no way to tell which, so both are signed out.
    """
    first = (
        await client.post(
            "/api/auth/login", json={"email": user.email, "password": TEST_PASSWORD}
        )
    ).json()
    second = (
        await client.post(
            "/api/auth/login", json={"email": user.email, "password": TEST_PASSWORD}
        )
    ).json()

    rotated = (
        await client.post(
            "/api/auth/refresh", json={"refresh_token": first["refresh_token"]}
        )
    ).json()

    # Replay the token that was already spent.
    await client.post("/api/auth/refresh", json={"refresh_token": first["refresh_token"]})

    # Every other live token for this user is now dead too.
    for token in (second["refresh_token"], rotated["refresh_token"]):
        resp = await client.post("/api/auth/refresh", json={"refresh_token": token})
        assert resp.status_code == 401


async def test_an_expired_refresh_token_is_rejected(client, user, db_session):
    login = (
        await client.post(
            "/api/auth/login", json={"email": user.email, "password": TEST_PASSWORD}
        )
    ).json()

    stored = (
        await db_session.execute(
            select(RefreshToken).where(
                RefreshToken.token_hash == hash_refresh_token(login["refresh_token"])
            )
        )
    ).scalar_one()
    stored.expires_at = datetime.now(timezone.utc) - timedelta(days=1)
    await db_session.commit()

    resp = await client.post(
        "/api/auth/refresh", json={"refresh_token": login["refresh_token"]}
    )
    assert resp.status_code == 401


async def test_refresh_tokens_are_only_stored_as_hashes(client, user, db_session):
    login = (
        await client.post(
            "/api/auth/login", json={"email": user.email, "password": TEST_PASSWORD}
        )
    ).json()

    rows = (await db_session.execute(select(RefreshToken))).scalars().all()
    assert rows
    assert all(row.token_hash != login["refresh_token"] for row in rows)


# --- Logout -------------------------------------------------------------

async def test_logout_revokes_only_that_session(client, user):
    laptop = (
        await client.post(
            "/api/auth/login", json={"email": user.email, "password": TEST_PASSWORD}
        )
    ).json()
    phone = (
        await client.post(
            "/api/auth/login", json={"email": user.email, "password": TEST_PASSWORD}
        )
    ).json()

    await client.post("/api/auth/logout", json={"refresh_token": laptop["refresh_token"]})

    dead = await client.post(
        "/api/auth/refresh", json={"refresh_token": laptop["refresh_token"]}
    )
    alive = await client.post(
        "/api/auth/refresh", json={"refresh_token": phone["refresh_token"]}
    )
    assert dead.status_code == 401
    assert alive.status_code == 200


async def test_logout_all_ends_every_session(client, user):
    tokens = [
        (
            await client.post(
                "/api/auth/login", json={"email": user.email, "password": TEST_PASSWORD}
            )
        ).json()["refresh_token"]
        for _ in range(3)
    ]

    resp = await client.post("/api/auth/logout-all", headers=auth_header(user))
    assert resp.status_code == 200

    for token in tokens:
        assert (
            await client.post("/api/auth/refresh", json={"refresh_token": token})
        ).status_code == 401


async def test_logout_with_an_unknown_token_still_succeeds(client):
    """A client discarding a token it can't use shouldn't get an error."""
    resp = await client.post("/api/auth/logout", json={"refresh_token": new_token()})
    assert resp.status_code == 200


# --- Verification -------------------------------------------------------

async def test_verification_signs_the_user_in(client, db_session):
    token = new_token()
    row = User(
        email="verify@example.com",
        password_hash="x",
        display_name="Verify",
        is_verified=False,
        verify_token_hash=hash_token(token),
    )
    db_session.add(row)
    await db_session.commit()

    resp = await client.post("/api/auth/verify-email", json={"token": token})
    assert resp.status_code == 200
    assert resp.json()["user"]["is_verified"] is True


async def test_a_verification_token_works_once(client, db_session):
    token = new_token()
    db_session.add(
        User(
            email="once@example.com",
            password_hash="x",
            is_verified=False,
            verify_token_hash=hash_token(token),
        )
    )
    await db_session.commit()

    assert (await client.post("/api/auth/verify-email", json={"token": token})).status_code == 200
    assert (await client.post("/api/auth/verify-email", json={"token": token})).status_code == 403


async def test_a_bogus_verification_token_is_rejected(client):
    resp = await client.post("/api/auth/verify-email", json={"token": new_token()})
    assert resp.status_code == 403


# --- Password reset -----------------------------------------------------

async def test_forgot_password_answers_the_same_either_way(client, user):
    """Otherwise this endpoint is a free "does this person have an account" oracle."""
    known = await client.post("/api/auth/forgot-password", json={"email": user.email})
    unknown = await client.post(
        "/api/auth/forgot-password", json={"email": "nobody@example.com"}
    )

    assert known.status_code == unknown.status_code == 200
    assert known.json() == unknown.json()


async def test_reset_changes_the_password_and_signs_everyone_out(client, user, db_session):
    live = (
        await client.post(
            "/api/auth/login", json={"email": user.email, "password": TEST_PASSWORD}
        )
    ).json()

    token = new_token()
    user.reset_token_hash = hash_token(token)
    user.reset_token_expires_at = datetime.now(timezone.utc) + timedelta(minutes=30)
    await db_session.commit()

    resp = await client.post(
        "/api/auth/reset-password", json={"token": token, "password": "a-brand-new-password"}
    )
    assert resp.status_code == 200

    # New password works, old one doesn't.
    assert (
        await client.post(
            "/api/auth/login", json={"email": user.email, "password": "a-brand-new-password"}
        )
    ).status_code == 200
    assert (
        await client.post(
            "/api/auth/login", json={"email": user.email, "password": TEST_PASSWORD}
        )
    ).status_code == 401
    # A reset is usually a response to compromise — sessions must not survive it.
    assert (
        await client.post("/api/auth/refresh", json={"refresh_token": live["refresh_token"]})
    ).status_code == 401


async def test_an_expired_reset_token_is_rejected(client, user, db_session):
    token = new_token()
    user.reset_token_hash = hash_token(token)
    user.reset_token_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    await db_session.commit()

    resp = await client.post(
        "/api/auth/reset-password", json={"token": token, "password": "a-brand-new-password"}
    )
    assert resp.status_code == 403


async def test_a_reset_token_works_once(client, user, db_session):
    token = new_token()
    user.reset_token_hash = hash_token(token)
    user.reset_token_expires_at = datetime.now(timezone.utc) + timedelta(minutes=30)
    await db_session.commit()

    assert (
        await client.post(
            "/api/auth/reset-password", json={"token": token, "password": "first-new-password"}
        )
    ).status_code == 200
    assert (
        await client.post(
            "/api/auth/reset-password", json={"token": token, "password": "second-new-password"}
        )
    ).status_code == 403


# --- Change password ----------------------------------------------------

async def test_change_password_requires_the_current_one(client, user):
    resp = await client.post(
        "/api/auth/change-password",
        json={"current_password": "wrong", "new_password": "a-brand-new-password"},
        headers=auth_header(user),
    )
    assert resp.status_code == 403


async def test_change_password_works_and_ends_sessions(client, user):
    live = (
        await client.post(
            "/api/auth/login", json={"email": user.email, "password": TEST_PASSWORD}
        )
    ).json()

    resp = await client.post(
        "/api/auth/change-password",
        json={"current_password": TEST_PASSWORD, "new_password": "a-brand-new-password"},
        headers=auth_header(user),
    )
    assert resp.status_code == 200
    assert (
        await client.post("/api/auth/refresh", json={"refresh_token": live["refresh_token"]})
    ).status_code == 401


# --- Profile and deletion -----------------------------------------------

async def test_display_name_can_be_changed(client, user):
    resp = await client.patch(
        "/api/auth/me", json={"display_name": "New Name"}, headers=auth_header(user)
    )
    assert resp.status_code == 200
    assert resp.json()["display_name"] == "New Name"


async def test_email_cannot_be_changed_through_the_profile(client, user):
    """The address is the account's identity — changing it needs its own flow."""
    await client.patch(
        "/api/auth/me",
        json={"display_name": "Name", "email": "hijack@example.com"},
        headers=auth_header(user),
    )
    resp = await client.get("/api/auth/me", headers=auth_header(user))
    assert resp.json()["email"] == user.email


async def test_deleting_an_account_detaches_letters_rather_than_cancelling_them(
    client, user, db_session, sample_letter
):
    """
    A confirmed letter is a promise to a recipient who is usually someone
    else. Closing your account must not silently cancel their mail.
    """
    sample_letter.user_id = user.id
    await db_session.commit()

    resp = await client.delete("/api/auth/me", headers=auth_header(user))
    assert resp.status_code == 200

    from app.models import Letter, LetterStatus

    letter = (
        await db_session.execute(select(Letter).where(Letter.id == sample_letter.id))
    ).scalar_one()
    assert letter.user_id is None
    assert letter.status == LetterStatus.SCHEDULED


async def test_deleting_an_account_removes_its_journals(
    client, user, db_session, journal, entry
):
    from app.models import Journal, JournalEntry

    await client.delete("/api/auth/me", headers=auth_header(user))

    assert (await db_session.execute(select(Journal))).scalars().all() == []
    assert (await db_session.execute(select(JournalEntry))).scalars().all() == []


async def test_a_logged_out_token_does_not_trigger_the_theft_response(client, user):
    """
    Regression. Logout and rotation both leave `revoked_at` set, so an
    earlier version treated a stale client retrying a logged-out token as
    token theft and signed every device out. Only rotation is evidence of
    theft; an explicit logout is the user's own doing.
    """
    laptop = (
        await client.post(
            "/api/auth/login", json={"email": user.email, "password": TEST_PASSWORD}
        )
    ).json()
    phone = (
        await client.post(
            "/api/auth/login", json={"email": user.email, "password": TEST_PASSWORD}
        )
    ).json()

    await client.post("/api/auth/logout", json={"refresh_token": laptop["refresh_token"]})
    # A stale laptop client retries the token it just gave up.
    await client.post("/api/auth/refresh", json={"refresh_token": laptop["refresh_token"]})

    # The phone must be untouched.
    assert (
        await client.post("/api/auth/refresh", json={"refresh_token": phone["refresh_token"]})
    ).status_code == 200


async def test_revocation_reason_is_recorded(client, user, db_session):
    login = (
        await client.post(
            "/api/auth/login", json={"email": user.email, "password": TEST_PASSWORD}
        )
    ).json()
    await client.post("/api/auth/logout", json={"refresh_token": login["refresh_token"]})

    stored = (
        await db_session.execute(
            select(RefreshToken).where(
                RefreshToken.token_hash == hash_refresh_token(login["refresh_token"])
            )
        )
    ).scalar_one()
    assert stored.revoked_reason == RefreshToken.LOGOUT
