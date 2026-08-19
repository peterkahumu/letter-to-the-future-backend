"""
Letter routes — create and retrieve letters.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Letter
from app.schemas import LetterCreate, LetterResponse, LetterStatusResponse

router = APIRouter(prefix="/api/letters", tags=["letters"])


@router.post(
    "",
    response_model=LetterResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new letter",
)
async def create_letter(
    payload: LetterCreate,
    db: AsyncSession = Depends(get_db),
) -> Letter:
    """
    Create a letter to be delivered at a future date.
    The letter will be emailed on the specified delivery date.
    """
    letter = Letter(
        recipient_email=payload.recipient_email,
        sender_name=payload.sender_name,
        subject=payload.subject,
        body=payload.body,
        deliver_at=payload.deliver_at,
    )
    db.add(letter)
    await db.flush()
    await db.refresh(letter)
    return letter


@router.get(
    "/{letter_id}",
    response_model=LetterStatusResponse,
    summary="Get letter status",
)
async def get_letter(
    letter_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> Letter:
    """Retrieve a letter's delivery status by ID."""
    result = await db.execute(select(Letter).where(Letter.id == letter_id))
    letter = result.scalar_one_or_none()
    if not letter:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Letter not found.",
        )
    return letter

from pydantic import EmailStr
from app.services.email_service import send_letter_email

@router.post(
    "/test-email",
    summary="Instantly trigger a test email",
)
async def trigger_test_email(email: EmailStr) -> dict:
    """Send a test email to the provided address to verify SMTP configuration."""
    from datetime import datetime, timezone
    test_letter = Letter(
        id=uuid.uuid4(),
        recipient_email=email,
        sender_name="Letter to the Future Team",
        subject="Letter to the Future - Production Test",
        body="If you are reading this, your production SMTP configuration is working perfectly on FastAPI Cloud!\n\nEmails are sending successfully.",
        deliver_at=datetime.now(timezone.utc).date(),
        is_sent=False,
        created_at=datetime.now(timezone.utc),
    )
    
    success = send_letter_email(test_letter)
    if success:
        return {"status": "success", "message": f"Test email sent to {email}"}
    else:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to send email. Check production logs."
        )
