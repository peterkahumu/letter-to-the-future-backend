"""
Email service — sends beautifully formatted HTML letters via SMTP (Gmail).
Uses only Python standard library: smtplib + email.message.
"""

import logging
import smtplib
import ssl
from email.message import EmailMessage

from app.config import get_settings
from app.models import Letter

logger = logging.getLogger(__name__)


def _build_html(letter: Letter) -> str:
    """Build a beautiful HTML email for the letter."""
    return f"""\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
</head>
<body style="margin:0;padding:0;background-color:#0A0F1A;font-family:'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#0A0F1A;padding:40px 20px;">
    <tr>
      <td align="center">
        <table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;">
          <!-- Header -->
          <tr>
            <td align="center" style="padding:30px 0 20px;">
              <div style="font-size:36px;margin-bottom:8px;">✉️</div>
              <h1 style="margin:0;color:#5EEAD4;font-size:24px;font-weight:700;letter-spacing:-0.5px;">
                Letter to the Future
              </h1>
              <p style="margin:8px 0 0;color:#9CA3AF;font-size:14px;">
                A message from your past self
              </p>
            </td>
          </tr>

          <!-- Card -->
          <tr>
            <td style="padding:0 0 30px;">
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
                     style="background-color:#111827;border:1px solid rgba(94,234,212,0.15);border-radius:16px;overflow:hidden;">
                <!-- From / Date bar -->
                <tr>
                  <td style="padding:20px 30px;border-bottom:1px solid rgba(94,234,212,0.1);">
                    <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
                      <tr>
                        <td style="color:#5EEAD4;font-size:13px;font-weight:600;text-transform:uppercase;letter-spacing:1px;">
                          From: {letter.sender_name}
                        </td>
                        <td align="right" style="color:#6B7280;font-size:13px;">
                          Written on {letter.created_at.strftime("%B %d, %Y")}
                        </td>
                      </tr>
                    </table>
                  </td>
                </tr>

                <!-- Subject -->
                <tr>
                  <td style="padding:24px 30px 0;">
                    <h2 style="margin:0;color:#F9FAFB;font-size:20px;font-weight:600;">
                      {letter.subject}
                    </h2>
                  </td>
                </tr>

                <!-- Body -->
                <tr>
                  <td style="padding:16px 30px 30px;">
                    <div style="color:#D1D5DB;font-size:16px;line-height:1.7;white-space:pre-wrap;">
{letter.body}
                    </div>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <!-- Footer -->
          <tr>
            <td align="center" style="padding:0 0 30px;">
              <p style="margin:0;color:#4B5563;font-size:12px;line-height:1.6;">
                This letter was scheduled on {letter.created_at.strftime("%B %d, %Y")}
                for delivery today.<br>
                Sent with ❤️ by <span style="color:#5EEAD4;">Letter to the Future</span>
              </p>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


def _build_plaintext(letter: Letter) -> str:
    """Build a plain-text fallback for the letter."""
    return (
        f"Letter to the Future\n"
        f"{'=' * 40}\n\n"
        f"From: {letter.sender_name}\n"
        f"Written on: {letter.created_at.strftime('%B %d, %Y')}\n\n"
        f"Subject: {letter.subject}\n"
        f"{'-' * 40}\n\n"
        f"{letter.body}\n\n"
        f"{'-' * 40}\n"
        f"This letter was scheduled on {letter.created_at.strftime('%B %d, %Y')} "
        f"for delivery today.\n"
        f"Sent with love by Letter to the Future."
    )


def send_letter_email(letter: Letter) -> bool:
    """
    Send a letter as a beautifully formatted HTML email.

    Returns True if sent successfully, False otherwise.
    """
    settings = get_settings()

    if not settings.smtp_user or not settings.smtp_password:
        logger.error("SMTP credentials not configured. Cannot send email.")
        return False

    msg = EmailMessage()
    msg["Subject"] = f"📬 {letter.subject}"
    msg["From"] = f"{settings.smtp_from_name} <{settings.smtp_user}>"
    msg["To"] = letter.recipient_email

    # Plain-text fallback first
    msg.set_content(_build_plaintext(letter))

    # HTML version as alternative
    msg.add_alternative(_build_html(letter), subtype="html")

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, context=context) as server:
            server.login(settings.smtp_user, settings.smtp_password)
            server.send_message(msg)
        logger.info(f"✅ Letter {letter.id} sent to {letter.recipient_email}")
        return True
    except smtplib.SMTPAuthenticationError:
        logger.error(f"❌ SMTP auth failed for letter {letter.id}. Check credentials.")
        return False
    except smtplib.SMTPException as e:
        logger.error(f"❌ SMTP error sending letter {letter.id}: {e}")
        return False
    except Exception as e:
        logger.error(f"❌ Unexpected error sending letter {letter.id}: {e}")
        return False
