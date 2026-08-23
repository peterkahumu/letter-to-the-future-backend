"""
Tests for the letter-sending scheduler.
"""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import select

from app.models import DeliveryLog, EmailSuppression, LetterStatus
from app.services.email_service import SendResult
from app.services.scheduler import check_and_send_letters
from tests.conftest import _letter


@asynccontextmanager
async def _session_factory(db_session):
    yield db_session


def _patched_session(db_session):
    return patch(
        "app.services.scheduler.AsyncSessionLocal",
        lambda: _session_factory(db_session),
    )


@pytest.mark.asyncio
@patch("app.services.scheduler.send_letter_email", return_value=SendResult(True))
async def test_scheduler_sends_due_letters(mock_send, db_session, due_letter):
    """Scheduler sends letters that are due today."""
    with _patched_session(db_session):
        await check_and_send_letters()

    mock_send.assert_called_once()
    await db_session.refresh(due_letter)
    assert due_letter.status == LetterStatus.SENT
    assert due_letter.sent_at is not None


@pytest.mark.asyncio
@patch("app.services.scheduler.send_letter_email", return_value=SendResult(True))
async def test_scheduler_skips_already_sent(mock_send, db_session, due_letter):
    """Scheduler doesn't re-send already-sent letters."""
    due_letter.status = LetterStatus.SENT
    due_letter.sent_at = datetime.now(timezone.utc)
    await db_session.commit()

    with _patched_session(db_session):
        await check_and_send_letters()

    mock_send.assert_not_called()


@pytest.mark.asyncio
@patch("app.services.scheduler.send_letter_email", return_value=SendResult(True))
async def test_scheduler_skips_unconfirmed_letters(mock_send, db_session):
    """A letter nobody confirmed is never delivered (P1)."""
    db_session.add(_letter(
        status=LetterStatus.PENDING, deliver_at=datetime.now(timezone.utc).date()
    ))
    await db_session.commit()

    with _patched_session(db_session):
        await check_and_send_letters()

    mock_send.assert_not_called()


@pytest.mark.asyncio
@patch("app.services.scheduler.send_letter_email", return_value=SendResult(True))
async def test_scheduler_skips_cancelled_letters(mock_send, db_session):
    """A cancelled letter stays cancelled (P2)."""
    db_session.add(_letter(
        status=LetterStatus.CANCELLED, deliver_at=datetime.now(timezone.utc).date()
    ))
    await db_session.commit()

    with _patched_session(db_session):
        await check_and_send_letters()

    mock_send.assert_not_called()


@pytest.mark.asyncio
@patch("app.services.scheduler.send_letter_email",
       return_value=SendResult(False, "smtp error: boom"))
async def test_scheduler_records_failure_and_retries(mock_send, db_session, due_letter):
    """A failed send is recorded, counted, and left for the next pass (R4)."""
    with _patched_session(db_session):
        await check_and_send_letters()

    mock_send.assert_called_once()
    await db_session.refresh(due_letter)
    assert due_letter.status == LetterStatus.SCHEDULED
    assert due_letter.attempts == 1
    assert due_letter.last_error == "smtp error: boom"


@pytest.mark.asyncio
@patch("app.services.scheduler.send_letter_email",
       return_value=SendResult(False, "recipient refused"))
async def test_scheduler_gives_up_after_max_attempts(mock_send, db_session, due_letter):
    """Permanently failing letters stop retrying forever (R4)."""
    from app.config import get_settings

    due_letter.attempts = get_settings().max_delivery_attempts - 1
    await db_session.commit()

    with _patched_session(db_session):
        await check_and_send_letters()

    await db_session.refresh(due_letter)
    assert due_letter.status == LetterStatus.FAILED


@pytest.mark.asyncio
@patch("app.services.scheduler.send_letter_email", return_value=SendResult(True))
async def test_scheduler_respects_suppression_list(mock_send, db_session, due_letter):
    """An unsubscribed address is never mailed, even if already queued (P5)."""
    db_session.add(EmailSuppression(email=due_letter.recipient_email))
    await db_session.commit()

    with _patched_session(db_session):
        await check_and_send_letters()

    mock_send.assert_not_called()
    await db_session.refresh(due_letter)
    assert due_letter.status == LetterStatus.CANCELLED


@pytest.mark.asyncio
@patch("app.services.scheduler.send_letter_email", return_value=SendResult(True))
async def test_scheduler_writes_delivery_log(mock_send, db_session, due_letter):
    """Every attempt leaves an audit trail (P7)."""
    with _patched_session(db_session):
        await check_and_send_letters()

    logs = (await db_session.execute(select(DeliveryLog))).scalars().all()
    assert [log.outcome for log in logs] == ["accepted"]
    assert logs[0].letter_id == due_letter.id


@pytest.mark.asyncio
@patch("app.services.scheduler.send_letter_email", return_value=SendResult(True))
async def test_scheduler_updates_heartbeat(mock_send, db_session, due_letter):
    """The readiness probe depends on this advancing (P6)."""
    from app.services import scheduler as sched

    sched.LAST_RUN_AT = None
    with _patched_session(db_session):
        await check_and_send_letters()

    assert sched.LAST_RUN_AT is not None


# --------------------------------------------------------------------------
# Timezone-correct delivery (P3)
# --------------------------------------------------------------------------

@pytest.mark.asyncio
@patch("app.services.scheduler.send_letter_email", return_value=SendResult(True))
async def test_scheduler_waits_for_local_date(mock_send, db_session):
    """
    A letter dated tomorrow everywhere is not sent, even though the loose
    UTC prefilter pulls it into the candidate batch.
    """
    db_session.add(_letter(
        deliver_at=datetime.now(timezone.utc).date() + timedelta(days=1),
        timezone_name="Pacific/Kiritimati",  # UTC+14, the earliest zone
    ))
    await db_session.commit()

    with _patched_session(db_session):
        await check_and_send_letters()

    # Kiritimati may already be on tomorrow's date; assert against the zone
    # rather than hard-coding an expectation.
    from zoneinfo import ZoneInfo

    local_today = datetime.now(ZoneInfo("Pacific/Kiritimati")).date()
    expected = local_today >= datetime.now(timezone.utc).date() + timedelta(days=1)
    assert mock_send.called is expected


@pytest.mark.asyncio
@patch("app.services.scheduler.send_letter_email", return_value=SendResult(True))
async def test_scheduler_sends_when_local_date_arrives(mock_send, db_session):
    """A zone already past midnight gets its letter (P3)."""
    db_session.add(_letter(
        deliver_at=datetime.now(timezone.utc).date(),
        timezone_name="Pacific/Kiritimati",
    ))
    await db_session.commit()

    with _patched_session(db_session):
        await check_and_send_letters()

    mock_send.assert_called_once()
