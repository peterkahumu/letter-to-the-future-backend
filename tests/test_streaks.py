"""
Streak arithmetic, tested directly.

Pure date maths, so it needs no database and no client — and it is
fiddly enough at the boundaries to be worth pinning on its own.
"""

from datetime import date, timedelta

from app.routers.dashboard import _streak


def test_streak_counts_consecutive_days():
    today = date(2026, 8, 27)
    days = [today, today - timedelta(days=1), today - timedelta(days=2)]
    assert _streak(days, today) == 3


def test_streak_survives_writing_only_yesterday():
    """A day isn't over until it is — don't break a streak at midnight."""
    today = date(2026, 8, 27)
    assert _streak([today - timedelta(days=1)], today) == 1


def test_streak_breaks_after_a_gap():
    today = date(2026, 8, 27)
    assert _streak([today - timedelta(days=3)], today) == 0


def test_streak_ignores_duplicate_days():
    today = date(2026, 8, 27)
    days = [today, today, today - timedelta(days=1)]
    assert _streak(days, today) == 2


def test_streak_of_nothing_is_zero():
    assert _streak([], date(2026, 8, 27)) == 0
