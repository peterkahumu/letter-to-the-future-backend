"""
Pydantic schemas for request/response validation.
"""

import uuid
from datetime import date, datetime

from pydantic import BaseModel, EmailStr, Field, field_validator


class LetterCreate(BaseModel):
    """Schema for creating a new letter."""

    recipient_email: EmailStr
    sender_name: str = Field(default="Past You", max_length=150)
    subject: str = Field(..., min_length=1, max_length=500)
    body: str = Field(..., min_length=1, max_length=50000)
    media_url: str | None = Field(default=None, max_length=1000)
    is_public: bool = False
    is_anonymous: bool = True
    deliver_at: date

    @field_validator("deliver_at")
    @classmethod
    def delivery_must_not_be_past(cls, v: date) -> date:
        # Today is allowed: the hourly scheduler picks up anything due
        # on or before the current UTC date, so it goes out this hour.
        from datetime import timezone, datetime as dt
        if v < dt.now(timezone.utc).date():
            raise ValueError("Delivery date cannot be in the past.")
        return v


class LetterResponse(BaseModel):
    """Schema for letter responses."""

    id: uuid.UUID
    recipient_email: str
    sender_name: str
    subject: str
    body: str
    media_url: str | None
    is_public: bool
    is_anonymous: bool
    deliver_at: date
    is_sent: bool
    sent_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class LetterStatusResponse(BaseModel):
    """Minimal schema for checking letter delivery status."""

    id: uuid.UUID
    recipient_email: str
    subject: str
    is_public: bool
    is_anonymous: bool
    deliver_at: date
    is_sent: bool
    sent_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class HealthResponse(BaseModel):
    """Health check response."""

    status: str = "ok"
    version: str = "1.0.0"
