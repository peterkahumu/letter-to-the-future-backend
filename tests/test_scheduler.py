"""
Tests for the letter-sending scheduler.
"""

from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from contextlib import asynccontextmanager

import pytest

from app.models import Letter
from app.services.scheduler import check_and_send_letters


@pytest.mark.asyncio
@patch("app.services.scheduler.send_letter_email", return_value=True)
async def test_scheduler_sends_due_letters(mock_send, db_session, due_letter):
    """Scheduler sends letters that are due today."""

    @asynccontextmanager
    async def mock_session():
        yield db_session

    with patch("app.services.scheduler.AsyncSessionLocal", mock_session):
        await check_and_send_letters()

    mock_send.assert_called_once()


@pytest.mark.asyncio
@patch("app.services.scheduler.send_letter_email", return_value=True)
async def test_scheduler_skips_already_sent(mock_send, db_session, due_letter):
    """Scheduler doesn't re-send already-sent letters."""
    # Mark as sent
    due_letter.is_sent = True
    due_letter.sent_at = datetime.now(timezone.utc)
    await db_session.commit()

    @asynccontextmanager
    async def mock_session():
        yield db_session

    with patch("app.services.scheduler.AsyncSessionLocal", mock_session):
        await check_and_send_letters()

    mock_send.assert_not_called()


@pytest.mark.asyncio
@patch("app.services.scheduler.send_letter_email", return_value=False)
async def test_scheduler_handles_send_failure(mock_send, db_session, due_letter):
    """Scheduler handles send failure gracefully without crashing."""

    @asynccontextmanager
    async def mock_session():
        yield db_session

    with patch("app.services.scheduler.AsyncSessionLocal", mock_session):
        await check_and_send_letters()

    mock_send.assert_called_once()
    # Letter should still be unsent
    await db_session.refresh(due_letter)
    assert due_letter.is_sent is False
