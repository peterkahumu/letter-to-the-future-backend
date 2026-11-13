"""
Account mail — address verification and password resets.

Kept apart from `email_service`, which is built around a Letter. These are
about the account, they carry no letter content, and they deliberately have
no unsubscribe footer: suppressing your own password reset would lock you
out of your account (P5 covers letters, not transactional account mail).
"""

import logging

from app.config import get_settings
from app.services.email_service import BRAND, SendResult, _dispatch

logger = logging.getLogger(__name__)


def _shell(heading: str, lines: list[str], button_label: str, button_url: str) -> str:
    """One layout for every account email, matching the letter template."""
    body = "".join(
        f'<p style="margin:0 0 14px;color:#CBD5E1;font-size:15px;line-height:1.7;">{line}</p>'
        for line in lines
    )
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
          <tr>
            <td align="center" style="padding:30px 0 20px;">
              <div style="font-size:36px;margin-bottom:8px;">✉️</div>
              <h1 style="margin:0;color:#5EEAD4;font-size:24px;font-weight:700;letter-spacing:-0.5px;">
                {BRAND}
              </h1>
            </td>
          </tr>
          <tr>
            <td style="background:#111827;border:1px solid #1F2937;border-radius:12px;padding:30px;">
              <h2 style="margin:0 0 18px;color:#F8FAFC;font-size:20px;font-weight:600;">{heading}</h2>
              {body}
              <p style="margin:26px 0 8px;">
                <a href="{button_url}"
                   style="display:inline-block;background:#5EEAD4;color:#04201D;
                          text-decoration:none;padding:13px 26px;border-radius:8px;
                          font-weight:600;font-size:15px;">{button_label}</a>
              </p>
              <p style="margin:18px 0 0;color:#64748B;font-size:12px;line-height:1.6;word-break:break-all;">
                If the button doesn't work, paste this into your browser:<br>{button_url}
              </p>
            </td>
          </tr>
          <tr>
            <td style="padding:18px 4px 0;">
              <p style="margin:0;color:#4B5563;font-size:11px;line-height:1.6;">
                {get_settings().mailing_address}
              </p>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


def send_verification_email(to: str, display_name: str, verify_url: str) -> SendResult:
    """Prove the address is theirs before the account can write anything."""
    name = display_name or "there"
    return _dispatch(
        to,
        f"Confirm your {BRAND} account",
        (
            f"Hi {name},\n\n"
            f"Confirm your email address to finish setting up your account:\n\n"
            f"{verify_url}\n\n"
            f"If you didn't sign up, ignore this email — nothing was created "
            f"in your name that can act without this link.\n"
        ),
        _shell(
            "Confirm your email",
            [
                f"Hi {name},",
                "One click and your account is ready — you'll be able to write "
                "letters to the future and keep journals.",
                "If you didn't sign up, ignore this email.",
            ],
            "Confirm my email",
            verify_url,
        ),
    )


def send_password_reset_email(to: str, display_name: str, reset_url: str, minutes: int) -> SendResult:
    """
    Reset link. Always sent to the address that asked, never told to the
    caller — the request endpoint answers identically whether or not the
    account exists, so it can't be used to enumerate who has one.
    """
    name = display_name or "there"
    return _dispatch(
        to,
        f"Reset your {BRAND} password",
        (
            f"Hi {name},\n\n"
            f"Use this link to choose a new password. It expires in {minutes} minutes "
            f"and works once:\n\n{reset_url}\n\n"
            f"If you didn't ask for this, you can ignore it — your password has "
            f"not changed.\n"
        ),
        _shell(
            "Reset your password",
            [
                f"Hi {name},",
                f"Choose a new password with the link below. It expires in "
                f"{minutes} minutes and can only be used once.",
                "If you didn't ask for this, ignore this email — your password "
                "hasn't changed.",
            ],
            "Choose a new password",
            reset_url,
        ),
    )


def send_password_changed_email(to: str, display_name: str) -> SendResult:
    """
    Notification, not a link. If someone else changed the password, this is
    the only warning the real owner gets.
    """
    name = display_name or "there"
    base = get_settings().primary_frontend_url
    return _dispatch(
        to,
        f"Your {BRAND} password was changed",
        (
            f"Hi {name},\n\n"
            f"Your password was just changed, and every signed-in session was "
            f"signed out.\n\n"
            f"If this wasn't you, reset your password immediately:\n"
            f"{base}/forgot-password\n"
        ),
        _shell(
            "Your password was changed",
            [
                f"Hi {name},",
                "Your password was just changed and every device was signed out.",
                "If this wasn't you, reset it immediately — whoever did this "
                "still knows the old password.",
            ],
            "Reset my password",
            f"{base}/forgot-password",
        ),
    )
