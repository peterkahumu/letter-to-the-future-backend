"""
Attachments must render as what they are — image, audio, or video.
"""

from datetime import date, datetime, timezone
from unittest.mock import MagicMock

import pytest

from app.core.media import kind_from_content_type, kind_from_url, resolve_kind
from app.models import Letter
from app.services.email_service import _build_html, _build_plaintext, _send_via_console


@pytest.mark.parametrize("content_type,expected", [
    ("image/png", "image"),
    ("audio/mpeg", "audio"),
    ("video/quicktime", "video"),
    ("text/html", None),
    (None, None),
])
def test_kind_from_content_type(content_type, expected):
    assert kind_from_content_type(content_type) == expected


@pytest.mark.parametrize("url,expected", [
    ("https://cdn/x/track.mp3", "audio"),
    ("https://cdn/x/CLIP.MOV", "video"),
    ("https://cdn/x/photo.webp", "image"),
    ("https://cdn/x/thing.xyz", None),
    ("https://cdn/x/noext", None),
])
def test_kind_from_url(url, expected):
    """Legacy attachments have no recorded kind; the extension is the fallback."""
    assert kind_from_url(url) == expected


def test_recorded_kind_beats_extension():
    assert resolve_kind("image", "https://cdn/x/weird.mp3") == "image"


def _letter_with(url, media_type):
    letter = MagicMock(spec=Letter)
    letter.sender_name = "S"
    letter.subject = "Subject"
    letter.body = "<p>body</p>"
    letter.created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    letter.deliver_at = date(2026, 1, 2)
    letter.media_url = url
    letter.media_type = media_type
    return letter


def test_image_is_embedded_in_email():
    html = _build_html(_letter_with("https://cdn/p.png", "image"))
    assert '<img src="https://cdn/p.png"' in html


@pytest.mark.parametrize("media_type,url,label", [
    ("audio", "https://cdn/a.mp3", "Listen to the recording"),
    ("video", "https://cdn/v.mp4", "Watch the video"),
])
def test_audio_and_video_become_links_in_email(media_type, url, label):
    """
    No mainstream mail client plays <audio> or <video>; a dead player reads
    as a bug, so email links out instead.
    """
    html = _build_html(_letter_with(url, media_type))
    assert label in html
    assert "<video" not in html
    assert "<audio" not in html
    assert url in html


def test_plaintext_names_the_media_kind():
    text = _build_plaintext(_letter_with("https://cdn/a.mp3", "audio"))
    assert "Audio recording: https://cdn/a.mp3" in text


def test_no_media_block_when_absent():
    html = _build_html(_letter_with(None, None))
    assert "<img" not in html


@pytest.mark.asyncio
async def test_create_letter_records_media_type(client, db_session):
    from sqlalchemy import select

    payload = {
        "recipient_email": "m@example.com",
        "sender_name": "S",
        "subject": "With audio",
        "body": "listen",
        "deliver_at": date.today().isoformat(),
        "media_url": "https://cdn/x/voice.m4a",
        "media_type": "audio",
        "is_public": False,
        "is_anonymous": True,
        "timezone_name": "UTC",
    }
    assert (await client.post("/api/letters", json=payload)).status_code == 201

    letter = (await db_session.execute(select(Letter))).scalars().one()
    assert letter.media_type == "audio"


@pytest.mark.asyncio
async def test_media_type_backfilled_from_url(client, db_session):
    """A client that omits the kind still gets it inferred from the URL."""
    from sqlalchemy import select

    payload = {
        "recipient_email": "m2@example.com",
        "sender_name": "S",
        "subject": "No type given",
        "body": "watch",
        "deliver_at": date.today().isoformat(),
        "media_url": "https://cdn/x/clip.webm",
        "is_public": False,
        "is_anonymous": True,
        "timezone_name": "UTC",
    }
    assert (await client.post("/api/letters", json=payload)).status_code == 201

    letter = (await db_session.execute(select(Letter))).scalars().one()
    assert letter.media_type == "video"


def test_console_transport_surfaces_links(caplog):
    """Local development depends on the confirmation link being visible."""
    import logging

    with caplog.at_level(logging.INFO):
        result = _send_via_console(
            "you@example.com",
            "Confirm your letter",
            "Click http://localhost:3000/confirm/abc?token=xyz to confirm.",
            "<a href='http://localhost:3000/confirm/abc?token=xyz'>confirm</a>",
        )

    assert result.ok
    assert "http://localhost:3000/confirm/abc?token=xyz" in caplog.text
    assert "Links in this email" in caplog.text
