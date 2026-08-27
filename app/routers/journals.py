"""
Journal routes — notebooks and the dated entries inside them.

The deliberate contrast with letters: a letter is sealed once confirmed and
has no edit route anywhere in this API, because a message from your past
self is only worth reading if your past self is the one who wrote it. A
journal is the opposite — somewhere to think, revise, and delete freely.

Everything here is private to its author. There is no public journal feed
and no admin route that returns an entry body.
"""

import logging
import uuid
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.security import sanitize_html
from app.database import get_db
from app.models import MOODS, Journal, JournalEntry, User
from app.schemas import SimpleMessage
from app.schemas_accounts import (
    EntryCreate,
    EntryPage,
    EntryResponse,
    EntrySummary,
    EntryUpdate,
    JournalCreate,
    JournalResponse,
    JournalUpdate,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/journals", tags=["journals"])

EXCERPT_CHARS = 200
MAX_JOURNALS_PER_USER = 50


def _excerpt(html: str) -> str:
    """
    Flatten a sanitized body to plain text for the list view.

    A blunt tag strip is enough here: the body has already been through
    nh3, so there is nothing executable left to mishandle.
    """
    import re

    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:EXCERPT_CHARS] + ("…" if len(text) > EXCERPT_CHARS else "")


def _summary(entry: JournalEntry, journal_title: str | None = None) -> EntrySummary:
    return EntrySummary(
        id=entry.id,
        journal_id=entry.journal_id,
        journal_title=journal_title,
        title=entry.title,
        excerpt=_excerpt(entry.body),
        entry_date=entry.entry_date,
        mood=entry.mood,
        updated_at=entry.updated_at,
    )


async def _own_journal(db: AsyncSession, journal_id: uuid.UUID, user: User) -> Journal:
    """
    Load a journal the caller owns.

    404 rather than 403 for someone else's journal — a 403 would confirm
    that the id exists, which is itself a small leak about another user.
    """
    result = await db.execute(
        select(Journal).where(Journal.id == journal_id).where(Journal.user_id == user.id)
    )
    journal = result.scalar_one_or_none()
    if journal is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Journal not found.")
    return journal


async def _own_entry(db: AsyncSession, entry_id: uuid.UUID, user: User) -> JournalEntry:
    result = await db.execute(
        select(JournalEntry)
        .where(JournalEntry.id == entry_id)
        .where(JournalEntry.user_id == user.id)
    )
    entry = result.scalar_one_or_none()
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Entry not found.")
    return entry


def _check_mood(mood: str | None) -> str | None:
    if mood is None:
        return None
    mood = mood.strip().lower()
    if not mood:
        return None
    if mood not in MOODS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"Unknown mood. Choose one of: {', '.join(MOODS)}.",
        )
    return mood


# --- Journals -----------------------------------------------------------

@router.get("", response_model=list[JournalResponse], summary="Your journals")
async def list_journals(
    include_archived: bool = Query(default=False),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[JournalResponse]:
    """
    Every notebook you own, newest activity first.

    Entry counts and the last-written date come from one grouped query
    rather than a count per journal, so the dashboard stays a fixed two
    round trips however many notebooks someone keeps.
    """
    stmt = select(Journal).where(Journal.user_id == user.id)
    if not include_archived:
        stmt = stmt.where(Journal.is_archived.is_(False))
    journals = list((await db.execute(stmt.order_by(Journal.updated_at.desc()))).scalars().all())

    if not journals:
        return []

    stats = (
        await db.execute(
            select(
                JournalEntry.journal_id,
                func.count(JournalEntry.id),
                func.max(JournalEntry.entry_date),
            )
            .where(JournalEntry.journal_id.in_([j.id for j in journals]))
            .group_by(JournalEntry.journal_id)
        )
    ).all()
    by_id = {row[0]: (row[1], row[2]) for row in stats}

    return [
        JournalResponse.from_journal(j, *by_id.get(j.id, (0, None)))
        for j in journals
    ]


@router.post(
    "",
    response_model=JournalResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Start a journal",
)
async def create_journal(
    payload: JournalCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JournalResponse:
    """Create a notebook. Entries go inside it."""
    count = (
        await db.execute(
            select(func.count()).select_from(Journal).where(Journal.user_id == user.id)
        )
    ).scalar() or 0
    if count >= MAX_JOURNALS_PER_USER:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"You've reached the limit of {MAX_JOURNALS_PER_USER} journals. "
            f"Archive or delete one first.",
        )

    journal = Journal(
        user_id=user.id,
        title=payload.title,
        description=payload.description,
        color=payload.color,
    )
    db.add(journal)
    await db.commit()
    await db.refresh(journal)
    return JournalResponse.from_journal(journal)


@router.get("/{journal_id}", response_model=JournalResponse, summary="One journal")
async def get_journal(
    journal_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JournalResponse:
    journal = await _own_journal(db, journal_id, user)
    row = (
        await db.execute(
            select(func.count(JournalEntry.id), func.max(JournalEntry.entry_date))
            .where(JournalEntry.journal_id == journal.id)
        )
    ).one()
    return JournalResponse.from_journal(journal, row[0] or 0, row[1])


@router.patch("/{journal_id}", response_model=JournalResponse, summary="Rename or restyle")
async def update_journal(
    journal_id: uuid.UUID,
    payload: JournalUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JournalResponse:
    """Journals are editable — unlike letters, which have no edit route."""
    journal = await _own_journal(db, journal_id, user)

    # exclude_unset so an omitted field is left alone rather than nulled.
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(journal, field, value)

    await db.commit()
    await db.refresh(journal)
    return JournalResponse.from_journal(journal)


@router.delete("/{journal_id}", response_model=SimpleMessage, summary="Delete a journal")
async def delete_journal(
    journal_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SimpleMessage:
    """
    Delete a journal and everything written in it.

    The entry delete is explicit rather than left to the foreign key's
    ON DELETE CASCADE, because the test suite runs on SQLite, which does
    not enforce foreign keys unless a pragma is set.
    """
    journal = await _own_journal(db, journal_id, user)

    removed = (
        await db.execute(
            select(func.count()).select_from(JournalEntry)
            .where(JournalEntry.journal_id == journal.id)
        )
    ).scalar() or 0

    await db.execute(
        JournalEntry.__table__.delete().where(JournalEntry.journal_id == journal.id)
    )
    await db.execute(Journal.__table__.delete().where(Journal.id == journal.id))
    await db.commit()

    return SimpleMessage(
        status="deleted",
        message=f"Journal deleted, along with {removed} "
                f"{'entry' if removed == 1 else 'entries'}.",
    )


# --- Entries ------------------------------------------------------------

@router.get(
    "/{journal_id}/entries", response_model=EntryPage, summary="Entries in a journal"
)
async def list_entries(
    journal_id: uuid.UUID,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    search: str | None = Query(default=None, max_length=200),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> EntryPage:
    """Entries newest first, with an excerpt rather than the full body."""
    journal = await _own_journal(db, journal_id, user)

    base = select(JournalEntry).where(JournalEntry.journal_id == journal.id)
    if search:
        needle = f"%{search.strip()}%"
        base = base.where(
            JournalEntry.title.ilike(needle) | JournalEntry.body.ilike(needle)
        )

    total = (
        await db.execute(select(func.count()).select_from(base.subquery()))
    ).scalar() or 0

    rows = (
        await db.execute(
            base.order_by(JournalEntry.entry_date.desc(), JournalEntry.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).scalars().all()

    return EntryPage(
        items=[_summary(e, journal.title) for e in rows],
        total=total,
        has_more=offset + len(rows) < total,
    )


@router.post(
    "/{journal_id}/entries",
    response_model=EntryResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Write an entry",
)
async def create_entry(
    journal_id: uuid.UUID,
    payload: EntryCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> EntryResponse:
    """
    Add a dated entry.

    Several entries may share a date on purpose — a day can be worth
    writing about more than once, and a unique constraint would turn the
    second thought of the evening into an error.
    """
    journal = await _own_journal(db, journal_id, user)

    entry = JournalEntry(
        journal_id=journal.id,
        user_id=user.id,
        title=payload.title,
        # Same sanitizer as letter bodies (E4).
        body=sanitize_html(payload.body),
        entry_date=payload.entry_date or datetime.now(timezone.utc).date(),
        mood=_check_mood(payload.mood),
    )
    db.add(entry)
    # Surface the journal so it sorts to the top of the dashboard.
    journal.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(entry)
    return EntryResponse.from_entry(entry)


@router.get(
    "/entries/{entry_id}", response_model=EntryResponse, summary="Read one entry"
)
async def get_entry(
    entry_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> EntryResponse:
    return EntryResponse.from_entry(await _own_entry(db, entry_id, user))


@router.patch(
    "/entries/{entry_id}", response_model=EntryResponse, summary="Edit an entry"
)
async def update_entry(
    entry_id: uuid.UUID,
    payload: EntryUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> EntryResponse:
    """Edit freely. This is the route a letter deliberately does not have."""
    entry = await _own_entry(db, entry_id, user)
    fields = payload.model_dump(exclude_unset=True)

    if "body" in fields and fields["body"] is not None:
        entry.body = sanitize_html(fields["body"])
    if "title" in fields and fields["title"] is not None:
        entry.title = fields["title"].strip()
    if "entry_date" in fields and fields["entry_date"] is not None:
        entry.entry_date = fields["entry_date"]
    if "mood" in fields:
        entry.mood = _check_mood(fields["mood"])

    await db.commit()
    await db.refresh(entry)
    return EntryResponse.from_entry(entry)


@router.delete(
    "/entries/{entry_id}", response_model=SimpleMessage, summary="Delete an entry"
)
async def delete_entry(
    entry_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SimpleMessage:
    entry = await _own_entry(db, entry_id, user)
    await db.execute(JournalEntry.__table__.delete().where(JournalEntry.id == entry.id))
    await db.commit()
    return SimpleMessage(status="deleted", message="Entry deleted.")


@router.get("/entries/all/recent", response_model=EntryPage, summary="Across all journals")
async def recent_entries(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    since: date | None = Query(default=None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> EntryPage:
    """
    One timeline across every notebook — the diary view, as opposed to
    reading a single journal end to end.
    """
    base = select(JournalEntry).where(JournalEntry.user_id == user.id)
    if since:
        base = base.where(JournalEntry.entry_date >= since)

    total = (
        await db.execute(select(func.count()).select_from(base.subquery()))
    ).scalar() or 0

    rows = list(
        (
            await db.execute(
                base.order_by(
                    JournalEntry.entry_date.desc(), JournalEntry.created_at.desc()
                )
                .limit(limit)
                .offset(offset)
            )
        ).scalars().all()
    )

    titles = {
        row[0]: row[1]
        for row in (
            await db.execute(
                select(Journal.id, Journal.title).where(Journal.user_id == user.id)
            )
        ).all()
    }

    return EntryPage(
        items=[_summary(e, titles.get(e.journal_id)) for e in rows],
        total=total,
        has_more=offset + len(rows) < total,
    )
