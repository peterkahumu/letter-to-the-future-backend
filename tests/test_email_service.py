"""
Unit tests for the email service.
"""

from datetime import date, datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.models import Letter
from app.services.email_service import (
    SendResult,
    _build_html,
    _build_plaintext,
    send_letter_email,
)


@pytest.fixture
def mock_letter():
    """Create a mock Letter object for email tests."""
    letter = MagicMock(spec=Letter)
    letter.id = "test-uuid-1234"
    letter.recipient_email = "future@example.com"
    letter.sender_name = "Past Me"
    letter.subject = "Hello Future"
    letter.body = "Remember to be kind."
    letter.media_url = None
    letter.deliver_at = date(2025, 12, 25)
    letter.sent_at = None
    letter.created_at = datetime(2024, 6, 15, 10, 0, 0, tzinfo=timezone.utc)
    return letter


def test_build_html_contains_key_elements(mock_letter):
    """HTML email contains the letter's content."""
    html = _build_html(mock_letter)
    assert "Hello Future" in html
    assert "Remember to be kind." in html
    assert "Past Me" in html
    assert "Letter to the Future" in html
    assert "June 15, 2024" in html


def test_build_plaintext_contains_key_elements(mock_letter):
    """Plain-text fallback contains the letter's content."""
    text = _build_plaintext(mock_letter)
    assert "Hello Future" in text
    assert "Remember to be kind." in text
    assert "Past Me" in text


def test_unsubscribe_link_is_included_when_given(mock_letter):
    """CAN-SPAM requires a working opt-out in every delivery (P5)."""
    url = "https://example.com/unsubscribe?email=a%40b.com&t=abc"
    assert url in _build_html(mock_letter, url)
    assert url in _build_plaintext(mock_letter, url)


def test_media_is_embedded_when_present(mock_letter):
    mock_letter.media_url = "https://cdn.example.com/pic.jpg"
    assert "https://cdn.example.com/pic.jpg" in _build_html(mock_letter)


@patch("app.services.email_service.get_settings")
def test_send_letter_no_credentials(mock_settings, mock_letter):
    """send_letter_email fails cleanly when SMTP credentials are missing."""
    settings = MagicMock()
    settings.email_provider = "smtp"
    settings.smtp_insecure = False
    settings.smtp_user = ""
    settings.smtp_password = ""
    settings.mailing_address = ""
    mock_settings.return_value = settings

    result = send_letter_email(mock_letter)
    assert result.ok is False
    assert "credentials" in result.detail


@patch("app.services.email_service.smtplib.SMTP_SSL")
@patch("app.services.email_service.get_settings")
def test_send_letter_success(mock_settings, mock_smtp_class, mock_letter):
    """send_letter_email reports success and logs in once."""
    settings = MagicMock()
    settings.email_provider = "smtp"
    settings.smtp_insecure = False
    settings.smtp_user = "test@gmail.com"
    settings.smtp_password = "app-password"
    settings.smtp_host = "smtp.gmail.com"
    settings.smtp_port = 465
    settings.smtp_from_name = "Letter to the Future"
    settings.from_email = "test@gmail.com"
    settings.mailing_address = ""
    mock_settings.return_value = settings

    mock_server = MagicMock()
    mock_smtp_class.return_value.__enter__ = MagicMock(return_value=mock_server)
    mock_smtp_class.return_value.__exit__ = MagicMock(return_value=False)

    result = send_letter_email(mock_letter)
    assert result.ok is True
    mock_server.login.assert_called_once_with("test@gmail.com", "app-password")
    mock_server.send_message.assert_called_once()


@patch("app.services.email_service.smtplib.SMTP_SSL")
@patch("app.services.email_service.get_settings")
def test_send_letter_reports_smtp_error_detail(mock_settings, mock_smtp_class, mock_letter):
    """The failure reason is captured so it can land in Letter.last_error (R4)."""
    import smtplib

    settings = MagicMock()
    settings.email_provider = "smtp"
    settings.smtp_insecure = False
    settings.smtp_user = "u"
    settings.smtp_password = "p"
    settings.smtp_host = "h"
    settings.smtp_port = 465
    settings.smtp_from_name = "n"
    settings.from_email = "u"
    settings.mailing_address = ""
    mock_settings.return_value = settings

    mock_smtp_class.side_effect = smtplib.SMTPAuthenticationError(535, b"bad creds")

    result = send_letter_email(mock_letter)
    assert result.ok is False
    assert "authentication" in result.detail


@patch("app.services.email_service.urllib.request.urlopen")
@patch("app.services.email_service.get_settings")
def test_resend_transport(mock_settings, mock_urlopen, mock_letter):
    """The Resend transport is selected by configuration alone (R8)."""
    settings = MagicMock()
    settings.email_provider = "resend"
    settings.resend_api_key = "re_test"
    settings.smtp_from_name = "Letter to the Future"
    settings.from_email = "hello@example.com"
    settings.mailing_address = ""
    mock_settings.return_value = settings

    response = MagicMock()
    response.status = 200
    mock_urlopen.return_value.__enter__ = MagicMock(return_value=response)
    mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)

    result = send_letter_email(mock_letter)
    assert result.ok is True
    mock_urlopen.assert_called_once()


def test_send_result_is_truthy():
    assert bool(SendResult(True)) is True
    assert bool(SendResult(False, "nope")) is False
