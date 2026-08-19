"""
Unit and integration tests for the letters API.
"""

from datetime import date, datetime, timedelta, timezone

import pytest


@pytest.mark.asyncio
async def test_create_letter_success(client):
    """POST /api/letters creates a letter and returns 201."""
    payload = {
        "recipient_email": "test@example.com",
        "sender_name": "Test User",
        "subject": "Hello Future",
        "body": "This is a test letter to my future self.",
        "deliver_at": (date.today() + timedelta(days=7)).isoformat(),
    }
    response = await client.post("/api/letters", json=payload)
    assert response.status_code == 201

    data = response.json()
    assert data["recipient_email"] == "test@example.com"
    assert data["sender_name"] == "Test User"
    assert data["subject"] == "Hello Future"
    assert data["is_sent"] is False
    assert data["sent_at"] is None
    assert "id" in data
    assert "created_at" in data


@pytest.mark.asyncio
async def test_create_letter_default_sender_name(client):
    """POST /api/letters uses 'Past You' as default sender name."""
    payload = {
        "recipient_email": "test@example.com",
        "subject": "No name provided",
        "body": "Testing default sender name.",
        "deliver_at": (date.today() + timedelta(days=1)).isoformat(),
    }
    response = await client.post("/api/letters", json=payload)
    assert response.status_code == 201
    assert response.json()["sender_name"] == "Past You"


@pytest.mark.asyncio
async def test_create_letter_past_date_rejected(client):
    """POST /api/letters rejects delivery dates in the past."""
    payload = {
        "recipient_email": "test@example.com",
        "subject": "Past date",
        "body": "Should fail.",
        "deliver_at": (date.today() - timedelta(days=1)).isoformat(),
    }
    response = await client.post("/api/letters", json=payload)
    assert response.status_code == 422  # Validation error


@pytest.mark.asyncio
async def test_create_letter_today_date_rejected(client):
    """POST /api/letters rejects today's date in UTC (must be future)."""
    utc_today = datetime.now(timezone.utc).date()
    payload = {
        "recipient_email": "test@example.com",
        "subject": "Today date",
        "body": "Should also fail.",
        "deliver_at": utc_today.isoformat(),
    }
    response = await client.post("/api/letters", json=payload)
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_create_letter_invalid_email(client):
    """POST /api/letters rejects invalid email addresses."""
    payload = {
        "recipient_email": "not-an-email",
        "subject": "Bad email",
        "body": "Should fail.",
        "deliver_at": (date.today() + timedelta(days=1)).isoformat(),
    }
    response = await client.post("/api/letters", json=payload)
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_create_letter_empty_subject_rejected(client):
    """POST /api/letters rejects empty subject."""
    payload = {
        "recipient_email": "test@example.com",
        "subject": "",
        "body": "No subject.",
        "deliver_at": (date.today() + timedelta(days=1)).isoformat(),
    }
    response = await client.post("/api/letters", json=payload)
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_create_letter_empty_body_rejected(client):
    """POST /api/letters rejects empty body."""
    payload = {
        "recipient_email": "test@example.com",
        "subject": "Empty body",
        "body": "",
        "deliver_at": (date.today() + timedelta(days=1)).isoformat(),
    }
    response = await client.post("/api/letters", json=payload)
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_get_letter_success(client, sample_letter):
    """GET /api/letters/{id} returns the letter status."""
    response = await client.get(f"/api/letters/{sample_letter.id}")
    assert response.status_code == 200

    data = response.json()
    assert data["id"] == str(sample_letter.id)
    assert data["subject"] == sample_letter.subject
    assert data["is_sent"] is False


@pytest.mark.asyncio
async def test_get_letter_not_found(client):
    """GET /api/letters/{id} returns 404 for non-existent ID."""
    import uuid
    fake_id = uuid.uuid4()
    response = await client.get(f"/api/letters/{fake_id}")
    assert response.status_code == 404
