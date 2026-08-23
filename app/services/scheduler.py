"""
Scheduler — claims due letters and delivers them.

Runs hourly via fastapi_utilities repeat_at. Safe to run in more than one
replica: work is claimed with a row lock, and the whole pass is wrapped in a
Postgres advisory lock (R2).
"""

import logging
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import anyio
from sqlalchemy import func, select

from app.config import get_settings
from app.core.security import unsubscribe_token
from sqlalchemy import text

from app.database import AsyncSessionLocal
from app.models import DeliveryLog, EmailSuppression, Letter, LetterStatus
from app.services.email_service import send_letter_email

logger = logging.getLogger(__name__)

# Arbitrary but fixed key identifying this job for pg_try_advisory_lock.
SCHEDULER_LOCK_KEY = 8_413_552_001

# Heartbeat for the readiness probe (P6). If this stops advancing, delivery
# has silently stopped — the one failure this product cannot walk back.
LAST_RUN_AT: datetime | None = None
LAST_RUN_ERROR: str | None = None


def _is_postgres(session) -> bool:
    """Dialect of the session actually in use — not a module-level engine,
    which the test suite legitimately swaps out for SQLite."""
    try:
        return session.get_bind().dialect.name == "postgresql"
    except Exception:  # noqa: BLE001
        return False


def _local_today(tz_name: str) -> date:
    """Current date in the writer's zone, falling back to UTC (P3)."""
    try:
        return datetime.now(ZoneInfo(tz_name)).date()
    except (ZoneInfoNotFoundError, ValueError):
        return datetime.now(timezone.utc).date()


def _is_due(letter: Letter) -> bool:
    """A letter is due once its delivery date has arrived where it was written."""
    return _local_today(letter.timezone_name or "UTC") >= letter.deliver_at


def _unsubscribe_url(email: str) -> str:
    settings = get_settings()
    query = urllib.parse.urlencode({"email": email, "t": unsubscribe_token(email)})
    return f"{settings.primary_frontend_url}/unsubscribe?{query}"


async def _claim_batch(session, limit: int) -> list[Letter]:
    """
    Take a batch of candidate letters, locking them so a concurrent replica
    picks up different rows (R2, R5).

    The date filter is deliberately loose — one day either side of UTC — so
    zones ahead of UTC are not missed. `_is_due` makes the exact call.
    """
    utc_today = datetime.now(timezone.utc).date()
    stmt = (
        select(Letter)
        .where(Letter.status == LetterStatus.SCHEDULED)
        .where(Letter.deliver_at <= utc_today + timedelta(days=1))
        .order_by(Letter.deliver_at)
        .limit(limit)
    )
    if _is_postgres(session):
        stmt = stmt.with_for_update(skip_locked=True)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def _suppressed(session, email: str) -> bool:
    """Has this address asked us to stop, or hard-bounced? (P5)"""
    found = await session.execute(
        select(EmailSuppression.email).where(
            func.lower(EmailSuppression.email) == email.lower()
        )
    )
    return found.scalar_one_or_none() is not None


async def _deliver_one(session, letter: Letter) -> bool:
    """
    Attempt one letter and commit the outcome immediately.

    Committing per letter is what stops a crash mid-batch from re-sending
    everything already delivered in that pass (R1).
    """
    settings = get_settings()

    if await _suppressed(session, letter.recipient_email):
        letter.status = LetterStatus.CANCELLED
        letter.last_error = "recipient unsubscribed"
        session.add(DeliveryLog(
            letter_id=letter.id, outcome="skipped", detail="recipient unsubscribed"
        ))
        await session.commit()
        logger.info(f"🚫 Letter {letter.id} skipped — recipient unsubscribed")
        return False

    letter.attempts += 1

    # smtplib is synchronous; running it inline would stall the event loop
    # for the whole batch and starve the health check (R3).
    result = await anyio.to_thread.run_sync(
        send_letter_email, letter, _unsubscribe_url(letter.recipient_email)
    )

    if result.ok:
        letter.status = LetterStatus.SENT
        letter.sent_at = datetime.now(timezone.utc)
        letter.last_error = None
        session.add(DeliveryLog(letter_id=letter.id, outcome="accepted"))
        await session.commit()
        return True

    letter.last_error = result.detail
    session.add(DeliveryLog(letter_id=letter.id, outcome="failed", detail=result.detail))

    if letter.attempts >= settings.max_delivery_attempts:
        # Stop retrying forever; park it so a human can see it (R4).
        letter.status = LetterStatus.FAILED
        logger.error(
            f"⛔ Letter {letter.id} gave up after {letter.attempts} attempts: {result.detail}"
        )
    await session.commit()
    return False


async def check_and_send_letters() -> None:
    """Deliver every letter whose date has arrived in its own timezone."""
    global LAST_RUN_AT, LAST_RUN_ERROR
    settings = get_settings()
    logger.info("📬 Scheduler running")

    sent_total = 0
    seen_total = 0
    try:
        # One long-lived session holds the advisory lock for the whole pass;
        # the lock is connection-scoped, so it must not be released early.
        async with AsyncSessionLocal() as lock_session:
            locked = False
            if _is_postgres(lock_session):
                got = await lock_session.execute(
                    text(f"SELECT pg_try_advisory_lock({SCHEDULER_LOCK_KEY})")
                )
                locked = bool(got.scalar())
                if not locked:
                    logger.info("🔒 Another replica holds the scheduler lock — skipping.")
                    return

            try:
                processed: set = set()
                while True:
                    async with AsyncSessionLocal() as session:
                        batch = await _claim_batch(session, settings.scheduler_batch_size)
                        fresh = [ltr for ltr in batch if ltr.id not in processed]
                        if not fresh:
                            break

                        due = [ltr for ltr in fresh if _is_due(ltr)]
                        seen_total += len(due)

                        for letter in due:
                            processed.add(letter.id)
                            try:
                                if await _deliver_one(session, letter):
                                    sent_total += 1
                            except Exception as e:  # noqa: BLE001
                                await session.rollback()
                                logger.error(f"❌ Error processing letter {letter.id}: {e}")

                        # Mark the not-yet-due ones seen too, so the next
                        # iteration doesn't re-examine the same rows forever.
                        processed.update(ltr.id for ltr in fresh)

                        if len(batch) < settings.scheduler_batch_size:
                            break
            finally:
                if locked:
                    await lock_session.execute(
                        text(f"SELECT pg_advisory_unlock({SCHEDULER_LOCK_KEY})")
                    )

        LAST_RUN_ERROR = None
    except Exception as e:  # noqa: BLE001
        LAST_RUN_ERROR = str(e)
        logger.exception("❌ Scheduler pass failed")
        raise
    finally:
        LAST_RUN_AT = datetime.now(timezone.utc)

    if seen_total == 0:
        logger.info("📭 No pending letters to send.")
    else:
        logger.info(f"✅ Scheduler complete — sent {sent_total}/{seen_total} letters.")
