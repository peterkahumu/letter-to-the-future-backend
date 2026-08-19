import asyncio
import uuid
from datetime import datetime, timezone
import os
import sys

# Ensure backend directory is in path
sys.path.insert(0, os.path.dirname(__file__))

from app.services.email_service import send_letter_email
from app.models import Letter
from app.config import get_settings

async def test_email():
    print("Testing email configuration...")
    settings = get_settings()
    
    print(f"SMTP Host: {settings.smtp_host}:{settings.smtp_port}")
    print(f"SMTP User: {settings.smtp_user}")
    print(f"From Name: {settings.smtp_from_name}")
    print("-" * 40)
    
    target_email = input("Enter an email address to send the test letter to: ").strip()
    if not target_email:
        print("Email cannot be empty. Exiting.")
        return

    # Create a dummy letter object
    test_letter = Letter(
        id=uuid.uuid4(),
        recipient_email=target_email,
        sender_name="System Admin",
        subject="Letter to the Future - SMTP Test",
        body="If you are reading this, your SMTP configuration is working perfectly!\n\nKeep building amazing things.",
        deliver_at=datetime.now(timezone.utc).date(),
        is_sent=False,
        created_at=datetime.now(timezone.utc),
    )

    print(f"\nSending test email to {target_email}...")
    try:
        success = send_letter_email(test_letter)
        if success:
            print("✅ Success! Email sent.")
        else:
            print("❌ Failed! The send_letter_email function returned False.")
    except Exception as e:
        print(f"❌ Exception occurred: {e}")

if __name__ == "__main__":
    asyncio.run(test_email())
