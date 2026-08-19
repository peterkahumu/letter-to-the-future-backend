"""
Scheduler — checks for letters due today and sends them.
Runs every hour via fastapi_utilities repeat_at.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models import Letter
from app.services.email_service import send_letter_email

logger = logging.getLogger(__name__)


async def check_and_send_letters() -> None:
    """
    Query for all unsent letters whose delivery date is today or earlier,
    then send each one. Failures are logged but don't block other letters.
    """
    today = datetime.now(timezone.utc).date()
    logger.info(f"📬 Scheduler running — checking for letters due on or before {today}")

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Letter)
            .where(Letter.deliver_at <= today)
            .where(Letter.is_sent == False)  # noqa: E712
        )
        letters = result.scalars().all()

        if not letters:
            logger.info("📭 No pending letters to send.")
            return

        logger.info(f"📨 Found {len(letters)} letter(s) to send.")

        sent_count = 0
        for letter in letters:
            try:
                success = send_letter_email(letter)
                if success:
                    letter.is_sent = True
                    letter.sent_at = datetime.now(timezone.utc)
                    sent_count += 1
                else:
                    logger.warning(f"⚠️  Failed to send letter {letter.id}, will retry next hour.")
            except Exception as e:
                logger.error(f"❌ Error processing letter {letter.id}: {e}")

        await session.commit()
        logger.info(f"✅ Scheduler complete — sent {sent_count}/{len(letters)} letters.")
