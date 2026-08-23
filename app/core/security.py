"""
Capability tokens and HTML sanitizing.

Letters have no user accounts, so links are the credential: a random token
is emailed, and only its SHA-256 hash is stored. Anyone holding the link can
act on that one letter; a database leak yields hashes, not working links.
"""

import hashlib
import hmac
import secrets

import nh3

TOKEN_BYTES = 32

# Rich-text output from the compose editor. Deliberately narrow — no script,
# no style, no iframe, no event handlers, and no javascript: URLs.
ALLOWED_TAGS: set[str] = {
    "p", "br", "strong", "b", "em", "i", "u", "s", "strike",
    "blockquote", "code", "pre",
    "h1", "h2", "h3", "h4",
    "ul", "ol", "li",
    "a", "span",
}
ALLOWED_ATTRS: dict[str, set[str]] = {
    # nh3 manages "rel" itself via link_rel below.
    "a": {"href", "title"},
}


def new_token() -> str:
    """A fresh URL-safe capability token."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> str:
    """Stable hash of a token, for storage and lookup."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def tokens_match(token: str, stored_hash: str | None) -> bool:
    """Constant-time comparison so token lookup can't be timed."""
    if not stored_hash:
        return False
    return hmac.compare_digest(hash_token(token), stored_hash)


def sanitize_html(raw: str) -> str:
    """
    Strip anything executable out of a letter body (E4).

    Applied on write, server-side. Sanitizing only in the browser is
    pointless here because the API that accepts the body is public.
    """
    return nh3.clean(
        raw,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRS,
        url_schemes={"http", "https", "mailto"},
        link_rel="noopener noreferrer nofollow",
    )


def unsubscribe_token(email: str) -> str:
    """
    Stable signature over an address, so an unsubscribe link can be built
    without storing another token. Verified with a constant-time compare.
    """
    from app.config import get_settings

    key = get_settings().secret_key.encode("utf-8")
    return hmac.new(key, email.lower().encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def verify_unsubscribe_token(email: str, token: str) -> bool:
    return hmac.compare_digest(unsubscribe_token(email), token)
