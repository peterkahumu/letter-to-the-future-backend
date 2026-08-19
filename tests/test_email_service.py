"""
Unit tests for the email service.
"""

from datetime import date, datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.models import Letter
from app.services.email_service import _build_html, _build_plaintext, send_letter_email


@pytest.fixture
def mock_letter():
    """Create a mock Letter object for email tests."""
    letter = MagicMock(spec=Letter)
    letter.id = "test-uuid-1234"
    letter.recipient_email = "future@example.com"
    letter.sender_name = "Past Me"
    letter.subject = "Hello Future"
    letter.body = "Remember to be kind."
    letter.deliver_at = date(2025, 12, 25)
    letter.is_sent = False
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


@patch("app.services.email_service.get_settings")
def test_send_letter_no_credentials(mock_settings, mock_letter):
    """send_letter_email returns False when SMTP credentials are missing."""
    settings = MagicMock()
    settings.smtp_user = ""
    settings.smtp_password = ""
    mock_settings.return_value = settings

    result = send_letter_email(mock_letter)
    assert result is False


@patch("app.services.email_service.smtplib.SMTP_SSL")
@patch("app.services.email_service.get_settings")
def test_send_letter_success(mock_settings, mock_smtp_class, mock_letter):
    """send_letter_email returns True on successful send."""
    settings = MagicMock()
    settings.smtp_user = "test@gmail.com"
    settings.smtp_password = "app-password"
    settings.smtp_host = "smtp.gmail.com"
    settings.smtp_port = 465
    settings.smtp_from_name = "Letter to the Future"
    mock_settings.return_value = settings

    mock_server = MagicMock()
    mock_smtp_class.return_value.__enter__ = MagicMock(return_value=mock_server)
    mock_smtp_class.return_value.__exit__ = MagicMock(return_value=False)

    result = send_letter_email(mock_letter)
    assert result is True
    mock_server.login.assert_called_once_with("test@gmail.com", "app-password")
    mock_server.send_message.assert_called_once()
