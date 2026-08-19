"""
Shared test fixtures for the backend test suite.
Uses an in-memory SQLite database to avoid needing PostgreSQL for tests.
"""

import asyncio
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base, get_db
from app.main import app
from app.models import Letter


# In-memory SQLite for tests
TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

test_engine = create_async_engine(TEST_DATABASE_URL, echo=False)
TestSessionLocal = async_sessionmaker(
    bind=test_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


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

    app.dependency_overrides[get_db] = _override_get_db

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def sample_letter(db_session: AsyncSession) -> Letter:
    """Insert a sample letter into the test database."""
    letter = Letter(
        id=uuid.uuid4(),
        recipient_email="future@example.com",
        sender_name="Past Me",
        subject="Hello Future Me",
        body="Remember to be kind to yourself.",
        deliver_at=date.today() + timedelta(days=30),
        is_sent=False,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(letter)
    await db_session.commit()
    await db_session.refresh(letter)
    return letter


@pytest_asyncio.fixture
async def due_letter(db_session: AsyncSession) -> Letter:
    """Insert a letter that is due today in UTC (for scheduler tests)."""
    utc_today = datetime.now(timezone.utc).date()
    letter = Letter(
        id=uuid.uuid4(),
        recipient_email="today@example.com",
        sender_name="Yesterday You",
        subject="Time has come",
        body="Today is the day!",
        deliver_at=utc_today,
        is_sent=False,
        created_at=datetime.now(timezone.utc) - timedelta(days=7),
    )
    db_session.add(letter)
    await db_session.commit()
    await db_session.refresh(letter)
    return letter
