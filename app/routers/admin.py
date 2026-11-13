"""
Admin routes — stats, users, moderation, and delivery health.

Every route here depends on `require_admin`, which re-reads the user row, so
a demotion takes effect on the next request rather than when a token happens
to expire.

**The line this file does not cross.** An admin can see that a user has 40
journal entries. An admin cannot see a title, an excerpt, or a body of any
of them, and no route below selects `JournalEntry.body` or
`JournalEntry.title`. Moderation covers the *public* feed — writing someone
chose to publish — and nothing else. Private writing has no admin read path
by construction, not by policy, because a policy is only as good as the next
person to add an endpoint.
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import revoke_all_sessions, require_admin
from app.database import get_db
from app.models import (
    DeliveryLog,
    EmailSuppression,
    Journal,
    JournalEntry,
    Letter,
    LetterReport,
    LetterStatus,
    User,
    UserRole,
)
from app.schemas import SimpleMessage, mask_email
from app.schemas_accounts import (
    AdminDeliveryLogItem,
    AdminReportItem,
    AdminStats,
    AdminSuppressionItem,
    AdminUserPage,
    AdminUserResponse,
    AdminUserUpdate,
    ModerationAction,
)

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/api/admin",
    tags=["admin"],
    dependencies=[Depends(require_admin)],
)


async def _count(db: AsyncSession, model, *conditions) -> int:
    stmt = select(func.count()).select_from(model)
    for condition in conditions:
        stmt = stmt.where(condition)
    return (await db.execute(stmt)).scalar() or 0


# --- Stats --------------------------------------------------------------

@router.get("/stats", response_model=AdminStats, summary="Site statistics")
async def stats(db: AsyncSession = Depends(get_db)) -> AdminStats:
    """Aggregate counts for the admin home. No content, only numbers."""
    today = datetime.now(timezone.utc).date()
    week_ago = datetime.now(timezone.utc) - timedelta(days=7)

    letters_by_status = {
        row[0]: row[1]
        for row in (
            await db.execute(
                select(Letter.status, func.count(Letter.id)).group_by(Letter.status)
            )
        ).all()
    }

    return AdminStats(
        users_total=await _count(db, User),
        users_active=await _count(db, User, User.is_active.is_(True)),
        users_verified=await _count(db, User, User.is_verified.is_(True)),
        admins_total=await _count(db, User, User.role == UserRole.ADMIN),
        new_users_this_week=await _count(db, User, User.created_at >= week_ago),
        letters_total=sum(letters_by_status.values()),
        letters_pending=letters_by_status.get(LetterStatus.PENDING, 0),
        letters_scheduled=letters_by_status.get(LetterStatus.SCHEDULED, 0),
        letters_sent=letters_by_status.get(LetterStatus.SENT, 0),
        letters_failed=letters_by_status.get(LetterStatus.FAILED, 0),
        letters_cancelled=letters_by_status.get(LetterStatus.CANCELLED, 0),
        letters_guest=await _count(db, Letter, Letter.user_id.is_(None)),
        due_next_7_days=await _count(
            db,
            Letter,
            Letter.status == LetterStatus.SCHEDULED,
            Letter.deliver_at <= today + timedelta(days=7),
        ),
        journals_total=await _count(db, Journal),
        entries_total=await _count(db, JournalEntry),
        reports_open=await _count(db, Letter, Letter.reported_count > 0, Letter.is_hidden.is_(False)),
        letters_hidden=await _count(db, Letter, Letter.is_hidden.is_(True)),
        suppressions_total=await _count(db, EmailSuppression),
    )


# --- Users --------------------------------------------------------------

@router.get("/users", response_model=AdminUserPage, summary="List users")
async def list_users(
    q: str | None = Query(default=None, max_length=200),
    role: str | None = Query(default=None),
    is_active: bool | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=25, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> AdminUserPage:
    """
    Users, with counts of what each has written.

    Counts come from three grouped queries over the page's ids, not a
    per-user count, so the cost doesn't grow with the page size.
    """
    stmt = select(User)
    if q:
        needle = f"%{q.strip()}%"
        stmt = stmt.where(or_(User.email.ilike(needle), User.display_name.ilike(needle)))
    if role:
        if role not in UserRole.ALL:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Unknown role.")
        stmt = stmt.where(User.role == role)
    if is_active is not None:
        stmt = stmt.where(User.is_active.is_(is_active))

    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar() or 0

    rows = list(
        (
            await db.execute(
                stmt.order_by(User.created_at.desc())
                .limit(per_page)
                .offset((page - 1) * per_page)
            )
        ).scalars().all()
    )
    ids = [u.id for u in rows]

    async def _grouped(model, column) -> dict[uuid.UUID, int]:
        if not ids:
            return {}
        result = await db.execute(
            select(column, func.count()).where(column.in_(ids)).group_by(column)
        )
        return {row[0]: row[1] for row in result.all()}

    letters = await _grouped(Letter, Letter.user_id)
    journals = await _grouped(Journal, Journal.user_id)
    entries = await _grouped(JournalEntry, JournalEntry.user_id)

    return AdminUserPage(
        items=[
            AdminUserResponse(
                id=u.id,
                email=u.email,
                display_name=u.display_name,
                role=u.role,
                is_active=u.is_active,
                is_verified=u.is_verified,
                letters_count=letters.get(u.id, 0),
                journals_count=journals.get(u.id, 0),
                entries_count=entries.get(u.id, 0),
                last_login_at=u.last_login_at,
                created_at=u.created_at,
            )
            for u in rows
        ],
        total=total,
        page=page,
        pages=max(1, -(-total // per_page)),
    )


@router.get("/users/{user_id}", response_model=AdminUserResponse, summary="One user")
async def get_user(
    user_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> AdminUserResponse:
    """
    One user's account and activity counts.

    Counts, again — not their journals. There is no admin route that
    returns an entry body anywhere in this file.
    """
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found.")

    return AdminUserResponse(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        role=user.role,
        is_active=user.is_active,
        is_verified=user.is_verified,
        letters_count=await _count(db, Letter, Letter.user_id == user.id),
        journals_count=await _count(db, Journal, Journal.user_id == user.id),
        entries_count=await _count(db, JournalEntry, JournalEntry.user_id == user.id),
        last_login_at=user.last_login_at,
        created_at=user.created_at,
    )


@router.patch(
    "/users/{user_id}", response_model=AdminUserResponse, summary="Change role or suspend"
)
async def update_user(
    user_id: uuid.UUID,
    payload: AdminUserUpdate,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> AdminUserResponse:
    """
    Promote, demote, suspend, or reinstate.

    Two guards, both about not locking the service out of itself:
    an admin cannot change their own role or suspend themselves, and the
    last remaining admin cannot be demoted or suspended by anyone.
    """
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found.")

    fields = payload.model_dump(exclude_unset=True)

    if user.id == admin.id:
        if "role" in fields and fields["role"] != user.role:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "You can't change your own role. Ask another admin.",
            )
        if fields.get("is_active") is False:
            raise HTTPException(status.HTTP_409_CONFLICT, "You can't suspend yourself.")

    demoting = user.role == UserRole.ADMIN and (
        fields.get("role") == UserRole.USER or fields.get("is_active") is False
    )
    if demoting:
        remaining = await _count(
            db,
            User,
            User.role == UserRole.ADMIN,
            User.is_active.is_(True),
            User.id != user.id,
        )
        if remaining == 0:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "This is the only active admin. Promote someone else first.",
            )

    for field, value in fields.items():
        setattr(user, field, value)

    # A suspended account must stop working now, not when its access token
    # expires — that is exactly what the refresh-token table is for.
    if fields.get("is_active") is False:
        await revoke_all_sessions(db, user.id)

    await db.commit()
    await db.refresh(user)
    logger.warning(f"👤 Admin {admin.email} updated user {user.email}: {fields}")

    return await get_user(user.id, db)


@router.delete("/users/{user_id}", response_model=SimpleMessage, summary="Delete a user")
async def delete_user(
    user_id: uuid.UUID,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> SimpleMessage:
    """
    Remove an account and its journals.

    Letters are detached, not deleted — the same rule as self-deletion. A
    confirmed letter is a promise to a recipient who is usually a third
    party, and deleting the writer's account should not cancel mail someone
    else is waiting for.
    """
    from sqlalchemy import update

    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found.")
    if user.id == admin.id:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Delete your own account from your settings, not from here.",
        )
    if user.role == UserRole.ADMIN:
        remaining = await _count(
            db, User, User.role == UserRole.ADMIN, User.id != user.id
        )
        if remaining == 0:
            raise HTTPException(status.HTTP_409_CONFLICT, "That is the only admin.")

    await db.execute(update(Letter).where(Letter.user_id == user.id).values(user_id=None))
    await db.execute(JournalEntry.__table__.delete().where(JournalEntry.user_id == user.id))
    await db.execute(Journal.__table__.delete().where(Journal.user_id == user.id))
    await revoke_all_sessions(db, user.id)
    await db.execute(User.__table__.delete().where(User.id == user.id))
    await db.commit()

    logger.warning(f"🗑️  Admin {admin.email} deleted user {user.email}")
    return SimpleMessage(status="deleted", message=f"{user.email} has been deleted.")


# --- Moderation ---------------------------------------------------------

@router.get("/reports", response_model=list[AdminReportItem], summary="Reported letters")
async def list_reports(
    include_resolved: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> list[AdminReportItem]:
    """
    The moderation queue: public letters readers have flagged.

    The body is included here — unlike a journal entry — because a public
    letter was published deliberately, and a moderator cannot judge a report
    without seeing what was reported.
    """
    stmt = select(Letter).where(Letter.reported_count > 0)
    if not include_resolved:
        stmt = stmt.where(Letter.is_hidden.is_(False))

    letters = list(
        (await db.execute(stmt.order_by(Letter.reported_count.desc()).limit(limit)))
        .scalars().all()
    )
    if not letters:
        return []

    reports = (
        await db.execute(
            select(LetterReport)
            .where(LetterReport.letter_id.in_([letter.id for letter in letters]))
            .order_by(LetterReport.created_at.desc())
        )
    ).scalars().all()

    grouped: dict[uuid.UUID, list[LetterReport]] = {}
    for report in reports:
        grouped.setdefault(report.letter_id, []).append(report)

    return [
        AdminReportItem(
            letter_id=letter.id,
            subject=letter.subject,
            body=letter.body,
            sender_name=None if letter.is_anonymous else letter.sender_name,
            reported_count=letter.reported_count,
            is_hidden=letter.is_hidden,
            status=letter.status,
            sent_at=letter.sent_at,
            reasons=[r.reason for r in grouped.get(letter.id, [])],
            latest_report_at=(
                grouped[letter.id][0].created_at if grouped.get(letter.id) else None
            ),
        )
        for letter in letters
    ]


@router.post(
    "/letters/{letter_id}/moderate",
    response_model=SimpleMessage,
    summary="Hide or restore a public letter",
)
async def moderate_letter(
    letter_id: uuid.UUID,
    payload: ModerationAction,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> SimpleMessage:
    """
    Take a letter out of the public feed, or put it back.

    Hiding affects the feed only. The letter is still delivered to its
    recipient: publishing was optional, delivery is the promise, and a
    moderator declining to feature something is not grounds to intercept
    someone's mail.
    """
    letter = (
        await db.execute(select(Letter).where(Letter.id == letter_id))
    ).scalar_one_or_none()
    if letter is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Letter not found.")

    letter.is_hidden = payload.hidden
    if not payload.hidden:
        # Restoring clears the counter, so the auto-hide threshold doesn't
        # trip again on the same reports a moderator has already dismissed.
        letter.reported_count = 0
    await db.commit()

    logger.warning(
        f"🛡️  Admin {admin.email} {'hid' if payload.hidden else 'restored'} "
        f"letter {letter_id}"
        + (f" — {payload.note}" if payload.note else "")
    )
    return SimpleMessage(
        status="hidden" if payload.hidden else "restored",
        message="Letter hidden from the public board."
        if payload.hidden
        else "Letter is back on the public board.",
    )


# --- Delivery health ----------------------------------------------------

@router.get(
    "/delivery/logs", response_model=list[AdminDeliveryLogItem], summary="Delivery attempts"
)
async def delivery_logs(
    outcome: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> list[AdminDeliveryLogItem]:
    """
    Recent delivery attempts, newest first.

    Subjects and masked addresses are joined in so "did this letter go out"
    is answerable without a second lookup — this is the table support
    questions are answered from.
    """
    stmt = select(DeliveryLog)
    if outcome:
        stmt = stmt.where(DeliveryLog.outcome == outcome)

    logs = list(
        (await db.execute(stmt.order_by(DeliveryLog.attempted_at.desc()).limit(limit)))
        .scalars().all()
    )
    if not logs:
        return []

    letters = {
        letter.id: letter
        for letter in (
            await db.execute(
                select(Letter).where(Letter.id.in_([log.letter_id for log in logs]))
            )
        ).scalars().all()
    }

    return [
        AdminDeliveryLogItem(
            id=log.id,
            letter_id=log.letter_id,
            outcome=log.outcome,
            detail=log.detail,
            attempted_at=log.attempted_at,
            subject=letters[log.letter_id].subject if log.letter_id in letters else None,
            recipient_email_masked=(
                mask_email(letters[log.letter_id].recipient_email)
                if log.letter_id in letters
                else None
            ),
        )
        for log in logs
    ]


@router.get(
    "/delivery/failed", response_model=list[AdminDeliveryLogItem], summary="Failed letters"
)
async def failed_letters(
    limit: int = Query(default=50, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> list[AdminDeliveryLogItem]:
    """Letters that exhausted their retries and were given up on (R4)."""
    letters = (
        await db.execute(
            select(Letter)
            .where(Letter.status == LetterStatus.FAILED)
            .order_by(Letter.deliver_at.desc())
            .limit(limit)
        )
    ).scalars().all()

    return [
        AdminDeliveryLogItem(
            id=letter.id,
            letter_id=letter.id,
            outcome="failed",
            detail=letter.last_error,
            attempted_at=letter.created_at,
            subject=letter.subject,
            recipient_email_masked=mask_email(letter.recipient_email),
        )
        for letter in letters
    ]


@router.post(
    "/delivery/{letter_id}/retry", response_model=SimpleMessage, summary="Retry a failed letter"
)
async def retry_letter(
    letter_id: uuid.UUID,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> SimpleMessage:
    """
    Put a failed letter back in the queue.

    Resets the attempt counter and returns it to `scheduled`; the next
    hourly pass picks it up like any other due letter. Nothing is sent from
    this request — a retry that mails inline would bypass the suppression
    check the scheduler does.
    """
    letter = (
        await db.execute(select(Letter).where(Letter.id == letter_id))
    ).scalar_one_or_none()
    if letter is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Letter not found.")
    if letter.status != LetterStatus.FAILED:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Only failed letters can be retried; this one is {letter.status}.",
        )

    letter.status = LetterStatus.SCHEDULED
    letter.attempts = 0
    letter.last_error = None
    await db.commit()

    logger.warning(f"🔁 Admin {admin.email} requeued letter {letter_id}")
    return SimpleMessage(
        status="requeued", message="Queued — it goes out on the next delivery pass."
    )


@router.get(
    "/suppressions", response_model=list[AdminSuppressionItem], summary="Suppressed addresses"
)
async def suppressions(
    limit: int = Query(default=100, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> list[AdminSuppressionItem]:
    """Addresses that have unsubscribed or hard-bounced (P5)."""
    rows = (
        await db.execute(
            select(EmailSuppression)
            .order_by(EmailSuppression.created_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    return [
        AdminSuppressionItem(email=r.email, reason=r.reason, created_at=r.created_at)
        for r in rows
    ]


@router.delete(
    "/suppressions/{email}", response_model=SimpleMessage, summary="Un-suppress an address"
)
async def remove_suppression(
    email: str,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> SimpleMessage:
    """
    Lift a suppression — for a bounce that turned out to be a mail server
    having a bad day, not for an unsubscribe. Removing one of those puts an
    address back on a list it explicitly asked to leave.
    """
    row = (
        await db.execute(
            select(EmailSuppression).where(
                func.lower(EmailSuppression.email) == email.lower()
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Address is not suppressed.")

    await db.execute(
        EmailSuppression.__table__.delete().where(EmailSuppression.email == row.email)
    )
    await db.commit()
    logger.warning(f"📬 Admin {admin.email} un-suppressed {email} (was: {row.reason})")
    return SimpleMessage(status="removed", message=f"{email} can receive letters again.")
