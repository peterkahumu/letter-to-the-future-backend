"""
Passwords, access tokens, and the dependencies that guard a route.

Two credentials, deliberately different in kind:

* the **access token** is a short-lived signed JWT the server never stores.
  It is fast to check — no database round trip — but for the same reason it
  cannot be taken back before it expires.
* the **refresh token** is an opaque random string whose SHA-256 hash is a
  row in `refresh_tokens`. It is what makes logging out, and suspending an
  account, actually end a session.

Access tokens are therefore kept short, and anything irreversible re-reads
the user row rather than trusting the claims alone.
"""

import hashlib
import logging
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.models import RefreshToken, User, UserRole

logger = logging.getLogger(__name__)

ALGORITHM = "HS256"
TOKEN_TYPE_ACCESS = "access"

# bcrypt hashes at most 72 bytes and silently ignores the rest, which would
# make a long passphrase no stronger than its first 72 bytes. Reject those
# outright rather than quietly truncating.
MAX_PASSWORD_BYTES = 72

# auto_error=False so an anonymous request reaches the route and gets our
# own 401 shape, not HTTPBearer's bare {"detail": "Not authenticated"}.
_bearer = HTTPBearer(auto_error=False)


# --- Passwords ----------------------------------------------------------

def hash_password(password: str) -> str:
    """bcrypt with a per-password salt, at the library's default cost."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """Constant-time check. Never raises on a malformed stored hash."""
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# --- Access tokens ------------------------------------------------------

def create_access_token(user: User) -> str:
    """
    Sign a JWT for this user.

    `role` rides along so ordinary reads can authorise without a second
    query, but every state-changing admin route re-checks the database —
    a token minted before a demotion stays valid until it expires.
    """
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user.id),
        "email": user.email,
        "role": user.role,
        "type": TOKEN_TYPE_ACCESS,
        "iat": now,
        "exp": now + timedelta(minutes=settings.access_token_expire_minutes),
    }
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict | None:
    """Verified claims, or None for anything we would not act on."""
    try:
        claims = jwt.decode(
            token,
            get_settings().secret_key,
            algorithms=[ALGORITHM],
        )
    except jwt.PyJWTError:
        return None
    # A refresh token must never be accepted where an access token is meant.
    if claims.get("type") != TOKEN_TYPE_ACCESS:
        return None
    return claims


# --- Refresh tokens -----------------------------------------------------

def hash_refresh_token(token: str) -> str:
    """Only this ever reaches the database."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def issue_refresh_token(
    db: AsyncSession, user: User, user_agent: str | None = None
) -> str:
    """Mint a refresh token and record its hash. Returns the plaintext once."""
    from app.core.security import new_token

    settings = get_settings()
    token = new_token()
    db.add(
        RefreshToken(
            user_id=user.id,
            token_hash=hash_refresh_token(token),
            expires_at=datetime.now(timezone.utc)
            + timedelta(days=settings.refresh_token_expire_days),
            user_agent=(user_agent or "")[:300] or None,
        )
    )
    return token


async def revoke_all_sessions(
    db: AsyncSession, user_id: uuid.UUID, reason: str = RefreshToken.REVOKED_BULK
) -> int:
    """
    End every session for a user — logout-everywhere, suspension, password
    change. Returns how many were live.

    The reason is recorded so that presenting one of these again is read as
    a stale client, not as token theft.
    """
    rows = (
        await db.execute(
            select(RefreshToken)
            .where(RefreshToken.user_id == user_id)
            .where(RefreshToken.revoked_at.is_(None))
        )
    ).scalars().all()
    now = datetime.now(timezone.utc)
    for row in rows:
        row.revoked_at = now
        row.revoked_reason = reason
    return len(rows)


# --- Dependencies -------------------------------------------------------

def _unauthorized(detail: str = "Not signed in.") -> HTTPException:
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: AsyncSession = Depends(get_db),
) -> User:
    """
    The signed-in user, or 401.

    The user row is loaded on every request rather than trusted from the
    claims, so a deleted or suspended account loses access immediately
    instead of when its token happens to expire.
    """
    if credentials is None:
        raise _unauthorized()

    claims = decode_access_token(credentials.credentials)
    if not claims:
        raise _unauthorized("Session expired. Sign in again.")

    try:
        user_id = uuid.UUID(claims["sub"])
    except (KeyError, ValueError):
        raise _unauthorized("Malformed token.")

    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise _unauthorized("Account no longer exists.")
    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This account has been suspended.")
    return user


async def get_optional_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> User | None:
    """
    The signed-in user if there is one, otherwise None — never raises.

    This is what lets a guest and a signed-in writer post to the same
    endpoint, with the letter attached to an owner only in the second case.
    """
    header = request.headers.get("authorization") or ""
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None

    claims = decode_access_token(token)
    if not claims:
        return None
    try:
        user_id = uuid.UUID(claims["sub"])
    except (KeyError, ValueError):
        return None

    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None or not user.is_active:
        return None
    return user


async def require_admin(user: User = Depends(get_current_user)) -> User:
    """
    Admin-only routes. The role comes from the freshly loaded row, so a
    demotion takes effect at once.

    404, not 403 — an admin surface should not confirm its own existence to
    someone who may be probing for it.
    """
    if user.role != UserRole.ADMIN:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found.")
    return user
