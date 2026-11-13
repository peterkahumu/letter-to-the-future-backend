"""
Journal tests.

Two things carry the weight here: that journals are editable where letters
are not, and that one user's writing is completely unreachable to another.
"""

import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app.models import Journal, JournalEntry
from tests.conftest import auth_header

pytestmark = pytest.mark.asyncio


# --- Access -------------------------------------------------------------

async def test_journals_require_signing_in(client):
    assert (await client.get("/api/journals")).status_code == 401
    assert (await client.post("/api/journals", json={"title": "x"})).status_code == 401


async def test_create_and_list(client, user):
    created = await client.post(
        "/api/journals",
        json={"title": "Travel 2026", "description": "Trips", "color": "gold"},
        headers=auth_header(user),
    )
    assert created.status_code == 201
    assert created.json()["title"] == "Travel 2026"
    assert created.json()["entry_count"] == 0

    listed = await client.get("/api/journals", headers=auth_header(user))
    assert [j["title"] for j in listed.json()] == ["Travel 2026"]


async def test_an_unknown_colour_is_rejected(client, user):
    resp = await client.post(
        "/api/journals",
        json={"title": "Journal", "color": "chartreuse"},
        headers=auth_header(user),
    )
    assert resp.status_code == 422


async def test_a_blank_title_is_rejected(client, user):
    resp = await client.post(
        "/api/journals", json={"title": "   "}, headers=auth_header(user)
    )
    assert resp.status_code == 422


# --- The point of the feature -------------------------------------------

async def test_a_journal_entry_can_be_edited(client, user, entry):
    """
    The distinction the whole feature rests on. A letter has no edit route
    anywhere in the API; an entry does, and this is it.
    """
    resp = await client.patch(
        f"/api/journals/entries/{entry.id}",
        json={"body": "<p>Actually, it smelled like dust.</p>"},
        headers=auth_header(user),
    )
    assert resp.status_code == 200
    assert "dust" in resp.json()["body"]


async def test_letters_have_no_edit_route(client, user):
    """
    The counterpart. If a PUT or PATCH ever appears on a letter, a message
    from your past self stops being one — so assert on the route table.
    """
    from app.main import app

    letter_paths = {
        path: set(spec.keys())
        for path, spec in app.openapi()["paths"].items()
        if path.startswith("/api/letters")
    }
    for path, methods in letter_paths.items():
        assert "put" not in methods, f"{path} gained a PUT"
        assert "patch" not in methods, f"{path} gained a PATCH"


async def test_editing_only_touches_the_fields_it_names(client, user, entry):
    resp = await client.patch(
        f"/api/journals/entries/{entry.id}",
        json={"title": "A better title"},
        headers=auth_header(user),
    )
    assert resp.status_code == 200
    assert resp.json()["title"] == "A better title"
    # The body was not named, so it must be untouched.
    assert "hot tarmac" in resp.json()["body"]


async def test_a_journal_can_be_renamed_and_archived(client, user, journal):
    resp = await client.patch(
        f"/api/journals/{journal.id}",
        json={"title": "Travels", "is_archived": True},
        headers=auth_header(user),
    )
    assert resp.status_code == 200
    assert resp.json()["title"] == "Travels"
    assert resp.json()["is_archived"] is True

    # Archived journals drop out of the default listing.
    default = await client.get("/api/journals", headers=auth_header(user))
    assert default.json() == []

    included = await client.get(
        "/api/journals?include_archived=true", headers=auth_header(user)
    )
    assert len(included.json()) == 1


# --- Entries ------------------------------------------------------------

async def test_writing_an_entry(client, user, journal):
    resp = await client.post(
        f"/api/journals/{journal.id}/entries",
        json={"title": "Market day", "body": "<p>Bought too many mangoes.</p>", "mood": "good"},
        headers=auth_header(user),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["mood"] == "good"
    # Defaults to today when no date is given.
    assert body["entry_date"] == date.today().isoformat()


async def test_an_entry_body_is_sanitized(client, user, journal):
    """Same sanitizer as letters — the API is reachable without the browser."""
    resp = await client.post(
        f"/api/journals/{journal.id}/entries",
        json={"body": "<p>Fine</p><script>alert('xss')</script>"},
        headers=auth_header(user),
    )
    assert resp.status_code == 201
    assert "<script>" not in resp.json()["body"]
    assert "Fine" in resp.json()["body"]


async def test_an_edited_body_is_sanitized_too(client, user, entry):
    resp = await client.patch(
        f"/api/journals/entries/{entry.id}",
        json={"body": "<p>Hi</p><img src=x onerror=alert(1)>"},
        headers=auth_header(user),
    )
    assert resp.status_code == 200
    assert "onerror" not in resp.json()["body"]


async def test_an_unknown_mood_is_rejected(client, user, journal):
    resp = await client.post(
        f"/api/journals/{journal.id}/entries",
        json={"body": "<p>Text</p>", "mood": "supercalifragilistic"},
        headers=auth_header(user),
    )
    assert resp.status_code == 422


async def test_several_entries_may_share_a_date(client, user, journal):
    """A day can be worth writing about twice; that must not be an error."""
    for text in ("<p>Morning.</p>", "<p>Evening.</p>"):
        resp = await client.post(
            f"/api/journals/{journal.id}/entries",
            json={"body": text, "entry_date": date.today().isoformat()},
            headers=auth_header(user),
        )
        assert resp.status_code == 201


async def test_the_list_view_sends_excerpts_not_bodies(client, user, journal):
    await client.post(
        f"/api/journals/{journal.id}/entries",
        json={"title": "Long one", "body": "<p>" + ("word " * 500) + "</p>"},
        headers=auth_header(user),
    )
    resp = await client.get(
        f"/api/journals/{journal.id}/entries", headers=auth_header(user)
    )
    item = resp.json()["items"][0]
    assert "excerpt" in item and "body" not in item
    assert len(item["excerpt"]) < 250


async def test_entries_can_be_searched(client, user, journal):
    for title, body in [("Mangoes", "<p>Fruit market.</p>"), ("Rain", "<p>Downpour.</p>")]:
        await client.post(
            f"/api/journals/{journal.id}/entries",
            json={"title": title, "body": body},
            headers=auth_header(user),
        )

    resp = await client.get(
        f"/api/journals/{journal.id}/entries?search=mango", headers=auth_header(user)
    )
    assert [i["title"] for i in resp.json()["items"]] == ["Mangoes"]


async def test_deleting_a_journal_takes_its_entries(client, user, journal, entry, db_session):
    resp = await client.delete(f"/api/journals/{journal.id}", headers=auth_header(user))
    assert resp.status_code == 200

    assert (await db_session.execute(select(Journal))).scalars().all() == []
    assert (await db_session.execute(select(JournalEntry))).scalars().all() == []


async def test_the_diary_timeline_spans_journals(client, user, db_session, journal):
    second = Journal(user_id=user.id, title="Work", color="slate")
    db_session.add(second)
    await db_session.commit()
    await db_session.refresh(second)

    for jid, day in [(journal.id, date.today()), (second.id, date.today() - timedelta(days=1))]:
        await client.post(
            f"/api/journals/{jid}/entries",
            json={"body": "<p>Something.</p>", "entry_date": day.isoformat()},
            headers=auth_header(user),
        )

    resp = await client.get("/api/journals/entries/all/recent", headers=auth_header(user))
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 2
    # Newest first, each labelled with the notebook it came from.
    assert {i["journal_title"] for i in items} == {"Travel 2026", "Work"}
    assert items[0]["entry_date"] > items[1]["entry_date"]


# --- Isolation ----------------------------------------------------------

async def test_another_users_journal_is_invisible(client, other_user, journal):
    """404, not 403 — a 403 would confirm the id exists."""
    resp = await client.get(f"/api/journals/{journal.id}", headers=auth_header(other_user))
    assert resp.status_code == 404


async def test_another_users_journal_cannot_be_edited(client, other_user, journal):
    resp = await client.patch(
        f"/api/journals/{journal.id}",
        json={"title": "Mine now"},
        headers=auth_header(other_user),
    )
    assert resp.status_code == 404


async def test_another_users_journal_cannot_be_deleted(client, other_user, journal, db_session):
    resp = await client.delete(f"/api/journals/{journal.id}", headers=auth_header(other_user))
    assert resp.status_code == 404
    assert (await db_session.execute(select(Journal))).scalars().first() is not None


async def test_another_users_entry_cannot_be_read(client, other_user, entry):
    resp = await client.get(
        f"/api/journals/entries/{entry.id}", headers=auth_header(other_user)
    )
    assert resp.status_code == 404


async def test_another_users_entry_cannot_be_edited(client, other_user, entry, db_session):
    resp = await client.patch(
        f"/api/journals/entries/{entry.id}",
        json={"body": "<p>Rewritten by someone else.</p>"},
        headers=auth_header(other_user),
    )
    assert resp.status_code == 404

    await db_session.refresh(entry)
    assert "hot tarmac" in entry.body


async def test_another_users_entry_cannot_be_deleted(client, other_user, entry, db_session):
    resp = await client.delete(
        f"/api/journals/entries/{entry.id}", headers=auth_header(other_user)
    )
    assert resp.status_code == 404
    assert (await db_session.execute(select(JournalEntry))).scalars().first() is not None


async def test_you_cannot_write_into_another_users_journal(client, other_user, journal):
    resp = await client.post(
        f"/api/journals/{journal.id}/entries",
        json={"body": "<p>Planted.</p>"},
        headers=auth_header(other_user),
    )
    assert resp.status_code == 404


async def test_listing_only_returns_your_own(client, user, other_user, journal):
    mine = await client.get("/api/journals", headers=auth_header(user))
    theirs = await client.get("/api/journals", headers=auth_header(other_user))

    assert len(mine.json()) == 1
    assert theirs.json() == []


async def test_the_diary_timeline_only_returns_your_own(client, other_user, entry):
    resp = await client.get(
        "/api/journals/entries/all/recent", headers=auth_header(other_user)
    )
    assert resp.json()["items"] == []
    assert resp.json()["total"] == 0


async def test_a_missing_journal_is_a_404(client, user):
    resp = await client.get(f"/api/journals/{uuid.uuid4()}", headers=auth_header(user))
    assert resp.status_code == 404
