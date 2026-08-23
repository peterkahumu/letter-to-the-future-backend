"""
Pydantic schemas for request/response validation.
"""

import uuid
from datetime import date, datetime
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.models import Letter


class LetterCreate(BaseModel):
    """Schema for creating a new letter."""

    recipient_email: EmailStr
    sender_name: str = Field(default="Past You", max_length=150)
    subject: str = Field(..., min_length=1, max_length=500)
    body: str = Field(..., min_length=1, max_length=50000)
    media_url: str | None = Field(default=None, max_length=1000)
    media_type: Literal["image", "audio", "video"] | None = None
    is_public: bool = False
    is_anonymous: bool = True
    deliver_at: date
    # IANA zone the date was chosen in, e.g. "Africa/Nairobi" (P3).
    timezone_name: str = Field(default="UTC", max_length=64)

    @field_validator("timezone_name")
    @classmethod
    def timezone_must_be_real(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Unknown timezone.")
        return v

    @field_validator("media_url")
    @classmethod
    def media_url_must_be_http(cls, v: str | None) -> str | None:
        """Block javascript: and data: URLs from reaching an <img src>."""
        if v and not v.startswith(("http://", "https://")):
            raise ValueError("Media URL must be http(s).")
        return v

    @field_validator("deliver_at")
    @classmethod
    def delivery_must_not_be_past(cls, v: date, info) -> date:
        # Today is allowed: the hourly scheduler picks up anything due
        # on or before the current date, so it goes out this hour.
        tz_name = (info.data or {}).get("timezone_name") or "UTC"
        try:
            today = datetime.now(ZoneInfo(tz_name)).date()
        except (ZoneInfoNotFoundError, ValueError):
            from datetime import timezone as _tz

            today = datetime.now(_tz.utc).date()
        if v < today:
            raise ValueError("Delivery date cannot be in the past.")
        return v


class LetterResponse(BaseModel):
    """
    Full letter view. Only ever returned to someone holding the letter's
    manage token — it carries the recipient address.
    """

    id: uuid.UUID
    recipient_email: str
    sender_name: str
    subject: str
    body: str
    media_url: str | None
    media_type: str | None
    is_public: bool
    is_anonymous: bool
    deliver_at: date
    status: str
    is_sent: bool
    sent_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class LetterStatusResponse(BaseModel):
    """
    Status view for the confirmation page. The recipient address is masked
    so a leaked or guessed letter id does not disclose it (E1).
    """

    id: uuid.UUID
    recipient_email_masked: str
    subject: str
    is_public: bool
    is_anonymous: bool
    deliver_at: date
    status: str
    is_sent: bool
    sent_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}

    @classmethod
    def from_letter(cls, letter: Letter) -> "LetterStatusResponse":
        return cls(
            id=letter.id,
            recipient_email_masked=mask_email(letter.recipient_email),
            subject=letter.subject,
            is_public=letter.is_public,
            is_anonymous=letter.is_anonymous,
            deliver_at=letter.deliver_at,
            status=letter.status,
            is_sent=letter.is_sent,
            sent_at=letter.sent_at,
            created_at=letter.created_at,
        )


class PublicLetterResponse(BaseModel):
    """
    What the public feed is allowed to show (E1, E2).

    Deliberately does NOT include recipient_email, and resolves the sender
    name through is_anonymous rather than trusting the caller to ignore it.
    """

    id: uuid.UUID
    sender_name: str | None
    subject: str
    body: str
    media_url: str | None
    media_type: str | None
    deliver_at: date
    sent_at: datetime | None

    @classmethod
    def from_letter(cls, letter: Letter) -> "PublicLetterResponse":
        from app.core.media import resolve_kind

        return cls(
            id=letter.id,
            sender_name=None if letter.is_anonymous else letter.sender_name,
            subject=letter.subject,
            body=letter.body,
            media_url=letter.media_url,
            media_type=resolve_kind(letter.media_type, letter.media_url),
            deliver_at=letter.deliver_at,
            sent_at=letter.sent_at,
        )


class PublicFeedPage(BaseModel):
    """One page of the public feed (R5)."""

    items: list[PublicLetterResponse]
    next_cursor: str | None = None


class LetterCreatedResponse(BaseModel):
    """
    Returned on create. No letter content comes back — the caller is not
    yet proven to control the address (P1).
    """

    id: uuid.UUID
    status: str
    recipient_email_masked: str
    deliver_at: date
    message: str


class ReportCreate(BaseModel):
    """A reader's report against a public letter (P4)."""

    reason: Literal[
        "spam", "harassment", "sexual", "violence", "personal_info", "other"
    ]
    details: str | None = Field(default=None, max_length=1000)

    @field_validator("details")
    @classmethod
    def blank_is_none(cls, v: str | None) -> str | None:
        if v is None:
            return None
        trimmed = v.strip()
        return trimmed or None


class SimpleMessage(BaseModel):
    status: str
    message: str


class HealthResponse(BaseModel):
    """Health check response."""

    status: str = "ok"
    version: str = "1.0.0"


def mask_email(email: str) -> str:
    """`someone@example.com` -> `s•••••e@example.com`."""
    local, _, domain = email.partition("@")
    if not domain:
        return "•••"
    if len(local) <= 2:
        return f"{local[0]}•••@{domain}"
    return f"{local[0]}{'•' * max(3, len(local) - 2)}{local[-1]}@{domain}"
