"""
The signed-in home screen.

One request that answers "what have I written, and what's coming" — counts,
the next delivery, and a short recent list of each kind. Assembled here
rather than in the browser so the dashboard is one round trip instead of
five.
"""

import logging
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.database import get_db
from app.models import Journal, JournalEntry, Letter, LetterStatus, User
from app.schemas import mask_email
from app.schemas_accounts import (
    DashboardResponse,
    LetterSummary,
    UserResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])

RECENT_LIMIT = 5


def _letter_summary(letter: Letter) -> LetterSummary:
    return LetterSummary(
        id=letter.id,
        # Masked even for the owner. Someone reading over a shoulder in a
        # café learns no more than the owner already knows (E1).
        recipient_email_masked=mask_email(letter.recipient_email),
        subject=letter.subject,
        status=letter.status,
        deliver_at=letter.deliver_at,
        sent_at=letter.sent_at,
        is_public=letter.is_public,
        created_at=letter.created_at,
    )


def _streak(dates: list[date], today: date) -> int:
    """
    Consecutive days written, counting back from today.

    Yesterday still counts as a live streak — a day isn't over until it is,
    and breaking someone's streak at midnight for not having written yet
    would be both wrong and discouraging.
    """
    if not dates:
        return 0
    unique = sorted(set(dates), reverse=True)
    if (today - unique[0]).days > 1:
        return 0

    streak = 1
    for earlier, later in zip(unique[1:], unique):
        if (later - earlier).days == 1:
            streak += 1
        else:
            break
    return streak


@router.get("", response_model=DashboardResponse, summary="Your dashboard")
async def get_dashboard(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DashboardResponse:
    """Everything the signed-in home screen shows."""
    today = datetime.now(timezone.utc).date()
    month_start = today.replace(day=1)

    # Letters, grouped once rather than counted per status.
    by_status = {
        row[0]: row[1]
        for row in (
            await db.execute(
                select(Letter.status, func.count(Letter.id))
                .where(Letter.user_id == user.id)
                .group_by(Letter.status)
            )
        ).all()
    }

    next_delivery = (
        await db.execute(
            select(func.min(Letter.deliver_at))
            .where(Letter.user_id == user.id)
            .where(Letter.status == LetterStatus.SCHEDULED)
            .where(Letter.deliver_at >= today)
        )
    ).scalar()

    journals_total = (
        await db.execute(
            select(func.count()).select_from(Journal).where(Journal.user_id == user.id)
        )
    ).scalar() or 0

    entries_total = (
        await db.execute(
            select(func.count())
            .select_from(JournalEntry)
            .where(JournalEntry.user_id == user.id)
        )
    ).scalar() or 0

    entries_this_month = (
        await db.execute(
            select(func.count())
            .select_from(JournalEntry)
            .where(JournalEntry.user_id == user.id)
            .where(JournalEntry.entry_date >= month_start)
        )
    ).scalar() or 0

    # Only the recent window is loaded for the streak — a 60-day lookback
    # bounds the query, and a streak longer than that is rare enough to be
    # worth capping rather than paying for on every dashboard load.
    recent_dates = list(
        (
            await db.execute(
                select(JournalEntry.entry_date)
                .where(JournalEntry.user_id == user.id)
                .where(JournalEntry.entry_date >= today - timedelta(days=60))
                .order_by(JournalEntry.entry_date.desc())
            )
        ).scalars().all()
    )

    recent_letters = list(
        (
            await db.execute(
                select(Letter)
                .where(Letter.user_id == user.id)
                .order_by(Letter.created_at.desc())
                .limit(RECENT_LIMIT)
            )
        ).scalars().all()
    )

    recent_entries = list(
        (
            await db.execute(
                select(JournalEntry)
                .where(JournalEntry.user_id == user.id)
                .order_by(JournalEntry.updated_at.desc())
                .limit(RECENT_LIMIT)
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

    from app.routers.journals import _summary

    return DashboardResponse(
        user=UserResponse.from_user(user),
        letters_total=sum(by_status.values()),
        letters_scheduled=by_status.get(LetterStatus.SCHEDULED, 0),
        letters_sent=by_status.get(LetterStatus.SENT, 0),
        next_delivery=next_delivery,
        journals_total=journals_total,
        entries_total=entries_total,
        entries_this_month=entries_this_month,
        current_streak=_streak(recent_dates, today),
        recent_letters=[_letter_summary(letter) for letter in recent_letters],
        recent_entries=[
            _summary(entry, titles.get(entry.journal_id)) for entry in recent_entries
        ],
    )


@router.get("/letters", response_model=list[LetterSummary], summary="Your letters")
async def my_letters(
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[LetterSummary]:
    """
    Letters this account wrote, newest first.

    Read-only by design. There is no edit route here or anywhere else: a
    letter you can revise on the day it arrives is not a letter from your
    past self. Cancelling one before delivery is still possible from the
    letters API, which is a different thing from rewriting it.
    """
    stmt = select(Letter).where(Letter.user_id == user.id)

    if status_filter:
        if status_filter not in LetterStatus.ALL:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"Unknown status. Choose one of: {', '.join(LetterStatus.ALL)}.",
            )
        stmt = stmt.where(Letter.status == status_filter)

    rows = (
        await db.execute(stmt.order_by(Letter.created_at.desc()).limit(limit).offset(offset))
    ).scalars().all()
    return [_letter_summary(letter) for letter in rows]
