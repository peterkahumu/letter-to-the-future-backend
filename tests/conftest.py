"""
Shared test fixtures for the backend test suite.
Uses an in-memory SQLite database to avoid needing PostgreSQL for tests.
"""

import os
import uuid
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

# Pin configuration BEFORE app modules are imported. Environment variables
# outrank .env and .env.local in pydantic-settings, so this keeps the suite
# independent of whatever a developer has configured locally.
os.environ.update({
    "DATABASE_URL": "postgresql+asyncpg://test:test@localhost:1/test",
    "SECRET_KEY": "test-secret-key",
    "EMAIL_PROVIDER": "console",
    "DEBUG": "false",
    "RUN_MIGRATIONS_ON_STARTUP": "false",
    "RATE_LIMIT_CREATE": "5/hour",
    "RATE_LIMIT_UPLOAD": "10/hour",
    "RATE_LIMIT_REPORT": "20/hour",
    "MAX_PENDING_PER_RECIPIENT": "20",
    "MAX_DELIVERY_ATTEMPTS": "5",
    "SCHEDULER_BATCH_SIZE": "100",
    "FRONTEND_URL": "http://localhost:3000",
    "SMTP_INSECURE": "false",
    # Accounts: verification off so fixtures can sign in directly, and a
    # short password floor so test passwords stay readable.
    "REQUIRE_EMAIL_VERIFICATION": "false",
    "MIN_PASSWORD_LENGTH": "8",
    "BOOTSTRAP_ADMIN_EMAIL": "",
    "RATE_LIMIT_LOGIN": "100/hour",
    "RATE_LIMIT_REGISTER": "100/hour",
    "RATE_LIMIT_PASSWORD_RESET": "100/hour",
})

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.limiter import limiter
from app.core.auth import create_access_token, hash_password
from app.core.security import hash_token
from app.database import Base, get_db
from app.main import app
from app.models import Journal, JournalEntry, Letter, LetterStatus, User, UserRole
from app.services.email_service import SendResult

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

test_engine = create_async_engine(TEST_DATABASE_URL, echo=False)
TestSessionLocal = async_sessionmaker(
    bind=test_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

CONFIRM_TOKEN = "test-confirm-token-0123456789abcdef"
MANAGE_TOKEN = "test-manage-token-0123456789abcdef"


@pytest.fixture(autouse=True)
def _no_rate_limits():
    """Rate limits are per-IP and every test shares one — disable them here
    and assert on them explicitly in the tests that care."""
    limiter.enabled = False
    yield
    limiter.enabled = True


@pytest.fixture(autouse=True)
def _no_outbound_mail():
    """Never touch SMTP from the suite."""
    with patch(
        "app.routers.letters.send_confirmation_email", return_value=SendResult(True)
    ) as mock:
        yield mock


@pytest_asyncio.fixture
async def db_session():
    """Create tables and yield a test database session."""
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with TestSessionLocal() as session:
        yield session

    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture
async def client(db_session: AsyncSession):
    """Async test client with overridden DB dependency."""

    async def _override_get_db():
        yield db_session
        await db_session.commit()

    app.dependency_overrides[get_db] = _override_get_db

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


def _letter(**overrides) -> Letter:
    base = dict(
        id=uuid.uuid4(),
        recipient_email="future@example.com",
        sender_name="Past Me",
        subject="Hello Future Me",
        body="<p>Remember to be kind to yourself.</p>",
        deliver_at=date.today() + timedelta(days=30),
        timezone_name="UTC",
        status=LetterStatus.SCHEDULED,
        confirm_token_hash=hash_token(CONFIRM_TOKEN),
        manage_token_hash=hash_token(MANAGE_TOKEN),
        created_at=datetime.now(timezone.utc),
    )
    base.update(overrides)
    return Letter(**base)


@pytest_asyncio.fixture
async def sample_letter(db_session: AsyncSession) -> Letter:
    """A confirmed letter scheduled well into the future."""
    letter = _letter()
    db_session.add(letter)
    await db_session.commit()
    await db_session.refresh(letter)
    return letter


@pytest_asyncio.fixture
async def pending_letter(db_session: AsyncSession) -> Letter:
    """A letter awaiting recipient confirmation."""
    letter = _letter(status=LetterStatus.PENDING)
    db_session.add(letter)
    await db_session.commit()
    await db_session.refresh(letter)
    return letter


@pytest_asyncio.fixture
async def due_letter(db_session: AsyncSession) -> Letter:
    """A confirmed letter due today in UTC (for scheduler tests)."""
    letter = _letter(
        recipient_email="today@example.com",
        sender_name="Yesterday You",
        subject="Time has come",
        body="Today is the day!",
        deliver_at=datetime.now(timezone.utc).date(),
        created_at=datetime.now(timezone.utc) - timedelta(days=7),
    )
    db_session.add(letter)
    await db_session.commit()
    await db_session.refresh(letter)
    return letter


# --- Accounts -----------------------------------------------------------

TEST_PASSWORD = "correct-horse-battery"


def _user(**overrides) -> User:
    base = dict(
        id=uuid.uuid4(),
        email="writer@example.com",
        password_hash=hash_password(TEST_PASSWORD),
        display_name="Writer",
        role=UserRole.USER,
        is_active=True,
        is_verified=True,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    base.update(overrides)
    return User(**base)


@pytest_asyncio.fixture
async def user(db_session: AsyncSession) -> User:
    """An ordinary verified account."""
    row = _user()
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    return row


@pytest_asyncio.fixture
async def other_user(db_session: AsyncSession) -> User:
    """A second account — the one that must never see the first's writing."""
    row = _user(email="someone.else@example.com", display_name="Someone Else")
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    return row


@pytest_asyncio.fixture
async def admin_user(db_session: AsyncSession) -> User:
    """An admin. Also a user — same table, same rights, wider role."""
    row = _user(email="admin@example.com", display_name="Admin", role=UserRole.ADMIN)
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    return row


def auth_header(user: User) -> dict[str, str]:
    """Bearer header for a user, minted the same way login does."""
    return {"Authorization": f"Bearer {create_access_token(user)}"}


@pytest_asyncio.fixture
async def journal(db_session: AsyncSession, user: User) -> Journal:
    row = Journal(
        user_id=user.id,
        title="Travel 2026",
        description="Where I went and what it was like.",
        color="turquoise",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    return row


@pytest_asyncio.fixture
async def entry(db_session: AsyncSession, journal: Journal) -> JournalEntry:
    row = JournalEntry(
        journal_id=journal.id,
        user_id=journal.user_id,
        title="Landed in Nairobi",
        body="<p>The air smelled like rain on hot tarmac.</p>",
        entry_date=date.today(),
        mood="excited",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    return row
