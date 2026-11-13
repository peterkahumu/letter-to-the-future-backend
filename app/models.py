"""
SQLAlchemy ORM models.
"""

import uuid
from datetime import date, datetime, time, timezone

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Index, Integer, String, Text, Time
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class LetterStatus:
    """
    Lifecycle of a letter. Single source of truth — `is_sent` is derived
    from this, never stored, so the two can't drift.

    pending  → written, waiting for the recipient to confirm (P1)
    scheduled→ confirmed, queued for delivery
    sent     → delivered
    failed   → gave up after max_delivery_attempts (R4)
    cancelled→ withdrawn by the writer before delivery (P2)
    """

    PENDING = "pending"
    SCHEDULED = "scheduled"
    SENT = "sent"
    FAILED = "failed"
    CANCELLED = "cancelled"

    ALL = (PENDING, SCHEDULED, SENT, FAILED, CANCELLED)


class Letter(Base):
    """A letter written to be delivered at a future date."""

    __tablename__ = "letters"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    # Set when a signed-in user wrote the letter, NULL when a guest did.
    # Ownership only decides whether it appears in a dashboard — it never
    # unseals a letter, which stays uneditable either way (E6).
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )

    recipient_email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    sender_name: Mapped[str] = mapped_column(String(150), nullable=False, default="Past You")
    subject: Mapped[str] = mapped_column(String(500), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    media_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    # "image" | "audio" | "video" — decides how the attachment is rendered.
    media_type: Mapped[str | None] = mapped_column(String(20), nullable=True)

    is_public: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_anonymous: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    deliver_at: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    # IANA zone the writer chose the date in. Delivery fires at local
    # midnight in this zone, not UTC midnight (P3).
    timezone_name: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=LetterStatus.PENDING, index=True
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Delivery bookkeeping (R4)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Capability tokens — only SHA-256 hashes are stored, never the token
    # itself, so a database leak does not hand out working links.
    confirm_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    manage_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Moderation of the public feed (P4)
    reported_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_hidden: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    @property
    def is_sent(self) -> bool:
        """Derived for API compatibility — do not store."""
        return self.status == LetterStatus.SENT

    def __repr__(self) -> str:
        return (
            f"<Letter {self.id} to={self.recipient_email} "
            f"deliver={self.deliver_at} status={self.status}>"
        )


class EmailSuppression(Base):
    """
    Addresses that must not be mailed — unsubscribes and hard bounces (P5).
    Checked before a letter is accepted and again before it is delivered.
    """

    __tablename__ = "email_suppressions"

    email: Mapped[str] = mapped_column(String(320), primary_key=True)
    reason: Mapped[str] = mapped_column(String(50), nullable=False, default="unsubscribed")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


class ReportReason:
    """Why a reader flagged a public letter."""

    SPAM = "spam"
    HARASSMENT = "harassment"
    SEXUAL = "sexual"
    VIOLENCE = "violence"
    PERSONAL_INFO = "personal_info"
    OTHER = "other"

    ALL = (SPAM, HARASSMENT, SEXUAL, VIOLENCE, PERSONAL_INFO, OTHER)


class LetterReport(Base):
    """
    One reader's report against a public letter (P4).

    Kept separately from the counter on Letter so moderation can see *why*
    something was flagged, not just how often.
    """

    __tablename__ = "letter_reports"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    letter_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    reason: Mapped[str] = mapped_column(String(30), nullable=False)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


class DeliveryLog(Base):
    """
    Append-only record of every delivery attempt (P7). Support questions
    about a letter that never arrived are unanswerable without this.
    """

    __tablename__ = "delivery_logs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    letter_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    outcome: Mapped[str] = mapped_column(String(20), nullable=False)  # accepted | failed
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


class UserRole:
    """
    Who someone is allowed to be. An admin is also a user (E6) — the role
    widens what they may do, it never gives them a second account or takes
    their own letters and journals away.
    """

    USER = "user"
    ADMIN = "admin"

    ALL = (USER, ADMIN)


class User(Base):
    """
    An account. Optional for writing a letter — guests still write with
    token links (P1) — but required for journals, and for seeing everything
    you've written in one place.
    """

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    # Stored lower-cased. The unique index is what stops two accounts
    # differing only by case from both existing.
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    display_name: Mapped[str] = mapped_column(String(150), nullable=False, default="")

    role: Mapped[str] = mapped_column(String(20), nullable=False, default=UserRole.USER, index=True)
    # Suspension. A suspended account keeps its data but cannot log in, and
    # its existing refresh tokens are revoked at the same time.
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Same discipline as letters: only hashes are stored, so a database
    # leak yields no working verification or reset link.
    verify_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    reset_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    reset_token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    @property
    def is_admin(self) -> bool:
        return self.role == UserRole.ADMIN

    def __repr__(self) -> str:
        return f"<User {self.id} {self.email} role={self.role}>"


class RefreshToken(Base):
    """
    A live session. The access token is a short-lived JWT the API never
    stores; this row is what makes logging out — and suspending an account —
    actually end a session, which a stateless JWT alone cannot do.

    Rotated on every refresh: the old row is revoked as the new one is
    issued, so a stolen token stops working the moment the real user
    refreshes.
    """

    __tablename__ = "refresh_tokens"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Why it was revoked, and the reason this column has to exist: a token
    # revoked by ROTATED being presented again means two parties hold it,
    # which is theft. A token revoked by LOGOUT being presented again just
    # means a stale client retried. Without this the two are the same row
    # and signing out on your laptop would sign out your phone.
    revoked_reason: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # Coarse provenance for the "your sessions" list. Never used for auth.
    user_agent: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    #: Revocation reasons. Only ROTATED implies possible theft on reuse.
    ROTATED = "rotated"
    LOGOUT = "logout"
    REVOKED_BULK = "revoked_bulk"

    def is_usable(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        expires = self.expires_at
        # SQLite hands back naive datetimes; treat them as UTC.
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return self.revoked_at is None and expires > now


class Journal(Base):
    """
    A notebook. Holds diary entries and carries none of its own writing —
    the title and description are how a user tells one from another.

    Unlike a letter, a journal is the user's to change at any time. That is
    the whole distinction: a letter is a promise to the future and is sealed
    once confirmed, a journal is a place to think and stays open.
    """

    __tablename__ = "journals"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    # One of JOURNAL_COLORS — a swatch for the dashboard, not free-form CSS.
    color: Mapped[str] = mapped_column(String(20), nullable=False, default="turquoise")

    is_archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    def __repr__(self) -> str:
        return f"<Journal {self.id} {self.title!r} user={self.user_id}>"


JOURNAL_COLORS = ("turquoise", "gold", "rose", "violet", "amber", "slate")


class JournalEntry(Base):
    """
    One diary entry inside a journal. Dated, editable, and private to its
    author — admins can count these rows but are never served their bodies.
    """

    __tablename__ = "journal_entries"
    __table_args__ = (
        # The diary timeline — "my entries, newest first" — sorts on exactly
        # this pair. Without it that query degrades to sorting the whole
        # table once someone has a few years of entries.
        Index("ix_journal_entries_user_date", "user_id", "entry_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    journal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("journals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Denormalised from the journal so ownership can be checked, and a
    # user's whole diary listed, without a join on every request.
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    title: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    # Sanitized on write, exactly like a letter body (E4).
    body: Mapped[str] = mapped_column(Text, nullable=False)
    # The day being written about, which is not always the day it was
    # written — backfilling yesterday's entry is normal.
    entry_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    # Optional time-of-day — lets the writer note when during the day the
    # entry was about, without breaking date-level indexes and streaks.
    entry_time: Mapped[time | None] = mapped_column(Time, nullable=True, default=None)
    mood: Mapped[str | None] = mapped_column(String(30), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    def __repr__(self) -> str:
        return f"<JournalEntry {self.id} {self.entry_date} journal={self.journal_id}>"


MOODS = (
    "great", "good", "okay", "low", "rough",
    "anxious", "grateful", "tired", "excited", "reflective",
)
