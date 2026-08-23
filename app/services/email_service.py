"""
Email service — builds and sends letters.

Transport is pluggable (R8): "smtp" keeps the original Gmail path, "resend"
uses the HTTP API. Gmail caps around 500 recipients/day, gives no bounce
feedback, and suspends accounts that look like bulk senders, so a real
sending platform is the production choice.
"""

import logging
import re
import smtplib
import ssl
import urllib.request
import json
from dataclasses import dataclass
from email.message import EmailMessage

from app.config import get_settings
from app.core.media import resolve_kind
from app.models import Letter

logger = logging.getLogger(__name__)

BRAND = "Letter to the Future"


@dataclass
class SendResult:
    """Outcome of one delivery attempt. `detail` feeds Letter.last_error."""

    ok: bool
    detail: str | None = None

    def __bool__(self) -> bool:
        return self.ok


def _footer_html(unsubscribe_url: str | None) -> str:
    """CAN-SPAM footer: who sent this, where they are, how to stop it."""
    settings = get_settings()
    bits: list[str] = []
    if unsubscribe_url:
        bits.append(
            f'<a href="{unsubscribe_url}" style="color:#5EEAD4;">Stop receiving letters at this address</a>'
        )
    if settings.mailing_address:
        bits.append(settings.mailing_address)
    if not bits:
        return ""
    return (
        '<p style="margin:12px 0 0;color:#4B5563;font-size:11px;line-height:1.6;">'
        + "<br>".join(bits)
        + "</p>"
    )


def _media_block(letter: Letter) -> str:
    """
    Render the attachment for email.

    Images embed. Audio and video do not — no mainstream mail client plays
    them reliably, and a broken <video> reads as a bug — so those become a
    labelled link out to the file instead.
    """
    if not letter.media_url:
        return ""

    kind = resolve_kind(getattr(letter, "media_type", None), letter.media_url)

    if kind == "image":
        inner = (
            f'<img src="{letter.media_url}" alt="Attachment" '
            f'style="max-width:100%;border-radius:8px;">'
        )
    else:
        label = {"audio": "🎧 Listen to the recording",
                 "video": "🎬 Watch the video"}.get(kind, "📎 Open the attachment")
        inner = (
            f'<a href="{letter.media_url}" '
            f'style="display:inline-block;background:rgba(94,234,212,0.12);'
            f'border:1px solid rgba(94,234,212,0.35);color:#5EEAD4;'
            f'text-decoration:none;padding:12px 22px;border-radius:8px;'
            f'font-weight:600;font-size:15px;">{label}</a>'
        )

    return f"""
                <tr>
                  <td style="padding:0 30px 30px;text-align:center;">
                    {inner}
                  </td>
                </tr>"""


def _build_html(letter: Letter, unsubscribe_url: str | None = None) -> str:
    """Build the HTML email for a letter.

    letter.body is already sanitized on write (E4); it is inserted as markup
    on purpose so the writer's formatting survives.
    """
    media_block = _media_block(letter)

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
              <p style="margin:8px 0 0;color:#9CA3AF;font-size:14px;">
                A message from your past self
              </p>
            </td>
          </tr>
          <tr>
            <td style="padding:0 0 30px;">
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
                     style="background-color:#111827;border:1px solid rgba(94,234,212,0.15);border-radius:16px;overflow:hidden;">
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
                <tr>
                  <td style="padding:24px 30px 0;">
                    <h2 style="margin:0;color:#F9FAFB;font-size:20px;font-weight:600;">
                      {letter.subject}
                    </h2>
                  </td>
                </tr>
                <tr>
                  <td style="padding:16px 30px 30px;">
                    <div style="color:#D1D5DB;font-size:16px;line-height:1.7;">
{letter.body}
                    </div>
                  </td>
                </tr>{media_block}
              </table>
            </td>
          </tr>
          <tr>
            <td align="center" style="padding:0 0 30px;">
              <p style="margin:0;color:#4B5563;font-size:12px;line-height:1.6;">
                This letter was scheduled on {letter.created_at.strftime("%B %d, %Y")}
                for delivery today.<br>
                Sent with ❤️ by <span style="color:#5EEAD4;">{BRAND}</span>
              </p>
              {_footer_html(unsubscribe_url)}
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


def _plaintext_media(letter: Letter) -> str:
    if not letter.media_url:
        return ""
    kind = resolve_kind(getattr(letter, "media_type", None), letter.media_url)
    label = {"image": "Image", "audio": "Audio recording",
             "video": "Video"}.get(kind, "Attachment")
    return f"{label}: {letter.media_url}\n\n"


def _build_plaintext(letter: Letter, unsubscribe_url: str | None = None) -> str:
    """Build a plain-text fallback for the letter."""
    tail = ""
    if unsubscribe_url:
        tail += f"\nStop receiving letters at this address: {unsubscribe_url}\n"
    settings = get_settings()
    if settings.mailing_address:
        tail += f"{settings.mailing_address}\n"

    return (
        f"{BRAND}\n"
        f"{'=' * 40}\n\n"
        f"From: {letter.sender_name}\n"
        f"Written on: {letter.created_at.strftime('%B %d, %Y')}\n\n"
        f"Subject: {letter.subject}\n"
        f"{'-' * 40}\n\n"
        f"{letter.body}\n\n"
        f"{_plaintext_media(letter)}"
        f"{'-' * 40}\n"
        f"This letter was scheduled on {letter.created_at.strftime('%B %d, %Y')} "
        f"for delivery today.\n"
        f"Sent with love by {BRAND}.\n"
        f"{tail}"
    )


# --------------------------------------------------------------------------
# Transports
# --------------------------------------------------------------------------

def _send_via_smtp(to: str, subject: str, text: str, html: str) -> SendResult:
    settings = get_settings()
    if not settings.smtp_insecure and (not settings.smtp_user or not settings.smtp_password):
        logger.error("SMTP credentials not configured. Cannot send email.")
        return SendResult(False, "smtp credentials not configured")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{settings.smtp_from_name} <{settings.from_email}>"
    msg["To"] = to
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")

    try:
        if settings.smtp_insecure:
            # Local mail catcher: no TLS, no credentials.
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as server:
                server.send_message(msg)
        else:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(
                settings.smtp_host, settings.smtp_port, context=context
            ) as server:
                server.login(settings.smtp_user, settings.smtp_password)
                server.send_message(msg)
        return SendResult(True)
    except smtplib.SMTPAuthenticationError:
        return SendResult(False, "smtp authentication failed")
    except smtplib.SMTPRecipientsRefused:
        # Hard bounce — the address does not accept mail. Not worth retrying.
        return SendResult(False, "recipient refused")
    except smtplib.SMTPException as e:
        return SendResult(False, f"smtp error: {e}")
    except Exception as e:  # noqa: BLE001 — transport must never raise upward
        return SendResult(False, f"unexpected error: {e}")


def _send_via_resend(to: str, subject: str, text: str, html: str) -> SendResult:
    settings = get_settings()
    if not settings.resend_api_key:
        return SendResult(False, "resend api key not configured")

    payload = json.dumps({
        "from": f"{settings.smtp_from_name} <{settings.from_email}>",
        "to": [to],
        "subject": subject,
        "html": html,
        "text": text,
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=payload,
        headers={
            "Authorization": f"Bearer {settings.resend_api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            if 200 <= resp.status < 300:
                return SendResult(True)
            return SendResult(False, f"resend http {resp.status}")
    except Exception as e:  # noqa: BLE001
        return SendResult(False, f"resend error: {e}")


def _send_via_console(to: str, subject: str, text: str, html: str) -> SendResult:
    """
    Local development transport: print the mail instead of sending it.

    Any links are pulled out and listed on their own, because the whole point
    locally is to click the confirmation link without setting up SMTP.
    """
    pattern = r"""https?://[^\s"'<>)]+"""
    links = re.findall(pattern, f"{text}\n{html}")
    seen: list[str] = []
    for link in links:
        if link not in seen:
            seen.append(link)

    lines = [
        "",
        "─" * 78,
        "  ✉️  EMAIL (console transport — not actually sent)",
        f"  To:      {to}",
        f"  Subject: {subject}",
        "─" * 78,
        text.strip(),
    ]
    if seen:
        lines += ["", "  🔗 Links in this email:"]
        lines += [f"     {link}" for link in seen]
    lines += ["─" * 78, ""]

    logger.info("\n".join(lines))
    return SendResult(True)


def _dispatch(to: str, subject: str, text: str, html: str) -> SendResult:
    """Blocking send. Callers on the event loop must use a worker thread (R3)."""
    settings = get_settings()
    if settings.email_provider == "console":
        return _send_via_console(to, subject, text, html)
    if settings.email_provider == "resend":
        return _send_via_resend(to, subject, text, html)
    return _send_via_smtp(to, subject, text, html)


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def send_letter_email(letter: Letter, unsubscribe_url: str | None = None) -> SendResult:
    """Deliver a letter. Blocking — call from a worker thread."""
    result = _dispatch(
        letter.recipient_email,
        f"📬 {letter.subject}",
        _build_plaintext(letter, unsubscribe_url),
        _build_html(letter, unsubscribe_url),
    )
    if result.ok:
        logger.info(f"✅ Letter {letter.id} sent to {letter.recipient_email}")
    else:
        logger.error(f"❌ Letter {letter.id} failed: {result.detail}")
    return result


def send_confirmation_email(letter: Letter, confirm_url: str, manage_url: str) -> SendResult:
    """
    Ask the recipient to confirm before anything is scheduled (P1).

    Nothing is queued until this link is clicked, which is what stops the
    service being used to mail strangers.
    """
    when = letter.deliver_at.strftime("%B %d, %Y")
    settings = get_settings()

    html = f"""\
<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0"></head>
<body style="margin:0;padding:0;background-color:#0A0F1A;font-family:'Segoe UI',Roboto,Arial,sans-serif;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#0A0F1A;padding:40px 20px;">
<tr><td align="center">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;">
  <tr><td align="center" style="padding:30px 0 20px;">
    <div style="font-size:36px;margin-bottom:8px;">✉️</div>
    <h1 style="margin:0;color:#5EEAD4;font-size:24px;font-weight:700;">{BRAND}</h1>
  </td></tr>
  <tr><td style="background:#111827;border:1px solid rgba(94,234,212,0.15);border-radius:16px;padding:30px;">
    <h2 style="margin:0 0 14px;color:#F9FAFB;font-size:19px;">One click and your letter is scheduled</h2>
    <p style="margin:0 0 18px;color:#D1D5DB;font-size:15px;line-height:1.7;">
      Someone asked us to deliver a letter to this address on <strong style="color:#5EEAD4;">{when}</strong>.
      We won't schedule anything until you confirm.
    </p>
    <p style="margin:0 0 24px;">
      <a href="{confirm_url}" style="display:inline-block;background:#0d9488;color:#ffffff;
         text-decoration:none;padding:12px 22px;border-radius:8px;font-weight:600;font-size:15px;">
        Confirm this letter
      </a>
    </p>
    <p style="margin:0;color:#9CA3AF;font-size:13px;line-height:1.6;">
      Didn't expect this? Ignore this email and nothing will ever be sent.
      You can also <a href="{manage_url}" style="color:#5EEAD4;">cancel it outright</a>.
    </p>
  </td></tr>
  <tr><td align="center" style="padding:20px 0 30px;">
    <p style="margin:0;color:#4B5563;font-size:11px;line-height:1.6;">
      {settings.retention_note}<br>{settings.mailing_address}
    </p>
  </td></tr>
</table></td></tr></table></body></html>"""

    text = (
        f"{BRAND}\n\n"
        f"Someone asked us to deliver a letter to this address on {when}.\n"
        f"Nothing is scheduled until you confirm:\n\n{confirm_url}\n\n"
        f"Didn't expect this? Ignore this email and nothing will be sent.\n"
        f"To cancel it outright: {manage_url}\n\n"
        f"{settings.retention_note}\n{settings.mailing_address}\n"
    )

    return _dispatch(letter.recipient_email, f"Confirm your letter for {when}", text, html)
