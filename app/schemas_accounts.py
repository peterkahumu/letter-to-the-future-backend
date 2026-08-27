"""
Schemas for accounts, journals, and the admin surface.

Split out of `schemas.py` to keep the letter contract — the part that has
been deployed and has clients — readable on its own.
"""

import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.config import get_settings
from app.models import JOURNAL_COLORS, Journal, JournalEntry, User


# --- Passwords ----------------------------------------------------------

def _validate_password(v: str) -> str:
    """
    Length is the requirement, not a character-class puzzle.

    Composition rules push people toward `Passw0rd!`; length is what
    actually costs an attacker. The upper bound is bcrypt's — it hashes only
    the first 72 bytes, so anything longer would be silently truncated.
    """
    settings = get_settings()
    if len(v) < settings.min_password_length:
        raise ValueError(
            f"Password must be at least {settings.min_password_length} characters."
        )
    if len(v.encode("utf-8")) > 72:
        raise ValueError("Password is too long (72 bytes maximum).")
    if not v.strip():
        raise ValueError("Password cannot be only whitespace.")
    return v


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str
    display_name: str = Field(default="", max_length=150)

    _check_password = field_validator("password")(_validate_password)

    @field_validator("display_name")
    @classmethod
    def trim_name(cls, v: str) -> str:
        return v.strip()


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str = Field(..., min_length=16)


class TokenResponse(BaseModel):
    """
    What a successful sign-in returns.

    The access token is short-lived; `refresh_token` is what the client
    exchanges for the next one, and is revoked on logout.
    """

    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int  # seconds until the access token expires
    user: "UserResponse"


class VerifyEmailRequest(BaseModel):
    """Verification token, posted in the body so it stays out of logs."""

    token: str = Field(..., min_length=16)


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str = Field(..., min_length=16)
    password: str

    _check_password = field_validator("password")(_validate_password)


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str

    _check_password = field_validator("new_password")(_validate_password)


class UserResponse(BaseModel):
    """The account as its owner sees it. Never carries the password hash."""

    id: uuid.UUID
    email: EmailStr
    display_name: str
    role: str
    is_active: bool
    is_verified: bool
    created_at: datetime

    model_config = {"from_attributes": True}

    @classmethod
    def from_user(cls, user: User) -> "UserResponse":
        return cls(
            id=user.id,
            email=user.email,
            display_name=user.display_name,
            role=user.role,
            is_active=user.is_active,
            is_verified=user.is_verified,
            created_at=user.created_at,
        )


class UpdateProfileRequest(BaseModel):
    display_name: str = Field(..., max_length=150)

    @field_validator("display_name")
    @classmethod
    def trim_name(cls, v: str) -> str:
        return v.strip()


class SessionResponse(BaseModel):
    """One live sign-in, for the "where am I signed in" list."""

    id: uuid.UUID
    user_agent: str | None
    created_at: datetime
    expires_at: datetime
    is_current: bool = False


# --- Journals -----------------------------------------------------------

class JournalCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1000)
    color: str = "turquoise"

    @field_validator("color")
    @classmethod
    def known_color(cls, v: str) -> str:
        if v not in JOURNAL_COLORS:
            raise ValueError(f"Colour must be one of: {', '.join(JOURNAL_COLORS)}.")
        return v

    @field_validator("title")
    @classmethod
    def trim_title(cls, v: str) -> str:
        trimmed = v.strip()
        if not trimmed:
            raise ValueError("Give the journal a title.")
        return trimmed


class JournalUpdate(BaseModel):
    """Every field optional — a PATCH only touches what it names."""

    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1000)
    color: str | None = None
    is_archived: bool | None = None

    @field_validator("color")
    @classmethod
    def known_color(cls, v: str | None) -> str | None:
        if v is not None and v not in JOURNAL_COLORS:
            raise ValueError(f"Colour must be one of: {', '.join(JOURNAL_COLORS)}.")
        return v

    @field_validator("title")
    @classmethod
    def trim_title(cls, v: str | None) -> str | None:
        if v is None:
            return None
        trimmed = v.strip()
        if not trimmed:
            raise ValueError("Give the journal a title.")
        return trimmed


class JournalResponse(BaseModel):
    id: uuid.UUID
    title: str
    description: str | None
    color: str
    is_archived: bool
    entry_count: int = 0
    last_entry_at: date | None = None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_journal(
        cls,
        journal: Journal,
        entry_count: int = 0,
        last_entry_at: date | None = None,
    ) -> "JournalResponse":
        return cls(
            id=journal.id,
            title=journal.title,
            description=journal.description,
            color=journal.color,
            is_archived=journal.is_archived,
            entry_count=entry_count,
            last_entry_at=last_entry_at,
            created_at=journal.created_at,
            updated_at=journal.updated_at,
        )


class EntryCreate(BaseModel):
    title: str = Field(default="", max_length=300)
    body: str = Field(..., min_length=1, max_length=100000)
    # Defaults to today on the server when omitted.
    entry_date: date | None = None
    mood: str | None = Field(default=None, max_length=30)

    @field_validator("title")
    @classmethod
    def trim_title(cls, v: str) -> str:
        return v.strip()


class EntryUpdate(BaseModel):
    """A journal entry is editable — that is the point of it (E6)."""

    title: str | None = Field(default=None, max_length=300)
    body: str | None = Field(default=None, min_length=1, max_length=100000)
    entry_date: date | None = None
    mood: str | None = Field(default=None, max_length=30)


class EntryResponse(BaseModel):
    id: uuid.UUID
    journal_id: uuid.UUID
    title: str
    body: str
    entry_date: date
    mood: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_entry(cls, entry: JournalEntry) -> "EntryResponse":
        return cls(
            id=entry.id,
            journal_id=entry.journal_id,
            title=entry.title,
            body=entry.body,
            entry_date=entry.entry_date,
            mood=entry.mood,
            created_at=entry.created_at,
            updated_at=entry.updated_at,
        )


class EntrySummary(BaseModel):
    """
    List view. Carries an excerpt rather than the body, so scrolling a year
    of entries doesn't ship a year of prose.
    """

    id: uuid.UUID
    journal_id: uuid.UUID
    journal_title: str | None = None
    title: str
    excerpt: str
    entry_date: date
    mood: str | None
    updated_at: datetime


class EntryPage(BaseModel):
    items: list[EntrySummary]
    total: int
    has_more: bool


# --- Dashboards ---------------------------------------------------------

class LetterSummary(BaseModel):
    """A letter as its author sees it in their own dashboard."""

    id: uuid.UUID
    recipient_email_masked: str
    subject: str
    status: str
    deliver_at: date
    sent_at: datetime | None
    is_public: bool
    created_at: datetime


class DashboardResponse(BaseModel):
    """Everything the signed-in home screen needs, in one request."""

    user: UserResponse
    letters_total: int
    letters_scheduled: int
    letters_sent: int
    next_delivery: date | None
    journals_total: int
    entries_total: int
    entries_this_month: int
    current_streak: int
    recent_letters: list[LetterSummary]
    recent_entries: list[EntrySummary]


# --- Admin --------------------------------------------------------------

class AdminUserResponse(BaseModel):
    """
    A user as an admin sees them: identity, role, and *counts* of what they
    have written. Never titles, never bodies — administering the service
    does not extend to reading people's diaries (E6).
    """

    id: uuid.UUID
    email: EmailStr
    display_name: str
    role: str
    is_active: bool
    is_verified: bool
    letters_count: int = 0
    journals_count: int = 0
    entries_count: int = 0
    last_login_at: datetime | None
    created_at: datetime


class AdminUserPage(BaseModel):
    items: list[AdminUserResponse]
    total: int
    page: int
    pages: int


class AdminUserUpdate(BaseModel):
    """Role and suspension are the only fields an admin may set."""

    role: Literal["user", "admin"] | None = None
    is_active: bool | None = None
    is_verified: bool | None = None


class AdminStats(BaseModel):
    """Aggregates for the admin home. Counts only, no content."""

    users_total: int
    users_active: int
    users_verified: int
    admins_total: int
    new_users_this_week: int

    letters_total: int
    letters_pending: int
    letters_scheduled: int
    letters_sent: int
    letters_failed: int
    letters_cancelled: int
    letters_guest: int
    due_next_7_days: int

    journals_total: int
    entries_total: int

    reports_open: int
    letters_hidden: int
    suppressions_total: int


class AdminReportItem(BaseModel):
    """A flagged public letter, with why it was flagged."""

    letter_id: uuid.UUID
    subject: str
    body: str
    sender_name: str | None
    reported_count: int
    is_hidden: bool
    status: str
    sent_at: datetime | None
    reasons: list[str]
    latest_report_at: datetime | None


class AdminDeliveryLogItem(BaseModel):
    id: uuid.UUID
    letter_id: uuid.UUID
    outcome: str
    detail: str | None
    attempted_at: datetime
    subject: str | None = None
    recipient_email_masked: str | None = None


class AdminSuppressionItem(BaseModel):
    email: str
    reason: str
    created_at: datetime


class ModerationAction(BaseModel):
    hidden: bool
    note: str | None = Field(default=None, max_length=500)
