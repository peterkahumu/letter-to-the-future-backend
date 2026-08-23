"""
Media kinds for letter attachments.

Cloudinary reports audio as resource_type "video", so the uploaded file's own
MIME type is the reliable signal. The URL-extension fallback exists for rows
written before this column did.
"""

from urllib.parse import urlparse

IMAGE = "image"
AUDIO = "audio"
VIDEO = "video"
KINDS = (IMAGE, AUDIO, VIDEO)

_EXTENSIONS: dict[str, str] = {
    **{e: IMAGE for e in ("jpg", "jpeg", "png", "gif", "webp", "avif", "bmp", "svg")},
    **{e: AUDIO for e in ("mp3", "m4a", "aac", "ogg", "oga", "wav", "flac", "opus")},
    **{e: VIDEO for e in ("mp4", "webm", "mov", "m4v", "avi", "mkv")},
}


def kind_from_content_type(content_type: str | None) -> str | None:
    """`audio/mpeg` -> `audio`. The authoritative path, used at upload time."""
    if not content_type:
        return None
    prefix = content_type.split("/", 1)[0].lower()
    return prefix if prefix in KINDS else None


def kind_from_url(url: str | None) -> str | None:
    """Best-effort guess for legacy attachments with no recorded kind."""
    if not url:
        return None
    ext = urlparse(url).path.rsplit(".", 1)
    if len(ext) != 2:
        return None
    return _EXTENSIONS.get(ext[1].lower())


def resolve_kind(media_type: str | None, url: str | None) -> str | None:
    """Recorded kind wins; fall back to the extension; otherwise unknown."""
    if media_type in KINDS:
        return media_type
    return kind_from_url(url)
