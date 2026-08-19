"""
SQLAlchemy ORM models.
"""

import uuid
from datetime import date, datetime, timezone

from sqlalchemy import Boolean, Date, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Letter(Base):
    """A letter written to be delivered at a future date."""

    __tablename__ = "letters"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
    )
    recipient_email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    sender_name: Mapped[str] = mapped_column(String(150), nullable=False, default="Past You")
    subject: Mapped[str] = mapped_column(String(500), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    deliver_at: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    is_sent: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    def __repr__(self) -> str:
        return f"<Letter {self.id} to={self.recipient_email} deliver={self.deliver_at} sent={self.is_sent}>"
