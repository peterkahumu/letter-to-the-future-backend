"""
SQLAlchemy ORM models.
"""

import uuid
from datetime import date, datetime, timezone

from sqlalchemy import Boolean, Date, DateTime, Integer, String, Text
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
