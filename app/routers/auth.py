"""
Account routes — register, verify, sign in, refresh, reset, sign out.

Two rules run through the whole file:

* **Never confirm whether an address has an account.** Registration,
  password reset, and login all answer the same way for a known and an
  unknown address, so none of them can be used to enumerate users.
* **A password change ends every session.** Changing it because you think
  someone else has it is pointless if their session survives.
"""

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.auth import (
    create_access_token,
    hash_password,
    hash_refresh_token,
    issue_refresh_token,
    revoke_all_sessions,
    verify_password,
)
from app.core.auth import get_current_user
from app.core.limiter import limiter
from app.core.security import hash_token, new_token, tokens_match
from app.database import get_db
from app.models import RefreshToken, User, UserRole
from app.schemas import SimpleMessage
from app.schemas_accounts import (
    ChangePasswordRequest,
    ForgotPasswordRequest,
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    ResetPasswordRequest,
    SessionResponse,
    TokenResponse,
    UpdateProfileRequest,
    UserResponse,
    VerifyEmailRequest,
)
from app.services.account_email import (
    send_password_changed_email,
    send_password_reset_email,
    send_verification_email,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])

# Answered to every forgot-password request, found or not.
RESET_SENT_MESSAGE = (
    "If an account exists for that address, a reset link is on its way."
)


def _verify_url(token: str) -> str:
    return f"{get_settings().primary_frontend_url}/verify-email?token={token}"


def _reset_url(token: str) -> str:
    return f"{get_settings().primary_frontend_url}/reset-password?token={token}"


def _mail(fn, *args) -> None:
    """Background mail must never turn a successful request into a 500."""
    try:
        fn(*args)
    except Exception as e:  # noqa: BLE001
        logger.error(f"Account email failed ({fn.__name__}): {e}")


async def _load_user_by_email(db: AsyncSession, email: str) -> User | None:
    result = await db.execute(select(User).where(User.email == email.lower().strip()))
    return result.scalar_one_or_none()


async def _token_response(
    db: AsyncSession, user: User, user_agent: str | None
) -> TokenResponse:
    """Mint a fresh pair and record the sign-in."""
    settings = get_settings()
    refresh = await issue_refresh_token(db, user, user_agent)
    user.last_login_at = datetime.now(timezone.utc)
    await db.flush()
    return TokenResponse(
        access_token=create_access_token(user),
        refresh_token=refresh,
        expires_in=settings.access_token_expire_minutes * 60,
        user=UserResponse.from_user(user),
    )


@router.post(
    "/register",
    response_model=SimpleMessage,
    status_code=status.HTTP_201_CREATED,
    summary="Create an account",
)
@limiter.limit(lambda: get_settings().rate_limit_register)
async def register(
    request: Request,
    response: Response,
    payload: RegisterRequest,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> SimpleMessage:
    """
    Create an account and email a verification link.

    Answers identically whether or not the address is already taken. An
    attacker learns nothing; the real owner of an existing address gets no
    new account and no password change, only mail they can ignore.
    """
    settings = get_settings()
    email = str(payload.email).lower().strip()

    existing = await _load_user_by_email(db, email)
    if existing is not None:
        # Same shape, same status, no new row. If this is the genuine owner
        # having forgotten they signed up, the reset flow is the way back.
        logger.info(f"Registration attempted for existing address {email}")
        return SimpleMessage(
            status="pending",
            message="Check your inbox to finish setting up your account.",
        )

    verify_token = new_token()
    user = User(
        email=email,
        password_hash=hash_password(payload.password),
        display_name=payload.display_name or email.split("@")[0],
        # The very first admin has to come from somewhere; after that,
        # promotion is an admin action.
        role=(
            UserRole.ADMIN
            if settings.bootstrap_admin_email
            and email == settings.bootstrap_admin_email.lower().strip()
            else UserRole.USER
        ),
        is_verified=not settings.require_email_verification,
        verify_token_hash=hash_token(verify_token),
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    if user.role == UserRole.ADMIN:
        logger.warning(f"👑 Bootstrap admin created: {email}")

    background.add_task(
        _mail, send_verification_email, email, user.display_name, _verify_url(verify_token)
    )
    return SimpleMessage(
        status="pending",
        message="Check your inbox to finish setting up your account.",
    )


@router.post("/verify-email", response_model=TokenResponse, summary="Verify an address")
async def verify_email(
    request: Request,
    payload: VerifyEmailRequest,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    """
    Exchange a verification token for a signed-in session.

    Signing them in here is deliberate: they have just proved control of the
    address, and bouncing them to a login form is friction for no security.

    The token arrives in the body rather than the query string, so it stays
    out of browser history, referrer headers, and access logs.
    """
    token = payload.token

    result = await db.execute(
        select(User).where(User.verify_token_hash == hash_token(token))
    )
    user = result.scalar_one_or_none()
    if user is None or not tokens_match(token, user.verify_token_hash):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid or expired link.")
    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This account has been suspended.")

    user.is_verified = True
    user.verify_token_hash = None  # one-time use
    body = await _token_response(db, user, request.headers.get("user-agent"))
    await db.commit()
    return body


@router.post(
    "/resend-verification", response_model=SimpleMessage, summary="Resend the verification email"
)
@limiter.limit(lambda: get_settings().rate_limit_password_reset)
async def resend_verification(
    request: Request,
    response: Response,
    background: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SimpleMessage:
    """Mint a fresh verification link for the signed-in account."""
    if user.is_verified:
        return SimpleMessage(status="ok", message="Your address is already confirmed.")

    token = new_token()
    user.verify_token_hash = hash_token(token)
    await db.commit()

    background.add_task(
        _mail, send_verification_email, user.email, user.display_name, _verify_url(token)
    )
    return SimpleMessage(status="sent", message="A fresh link is on its way.")


@router.post("/login", response_model=TokenResponse, summary="Sign in")
@limiter.limit(lambda: get_settings().rate_limit_login)
async def login(
    request: Request,
    response: Response,
    payload: LoginRequest,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    """
    Exchange an email and password for a token pair.

    A wrong address and a wrong password give the same 401. The password is
    hashed even when no user was found, so the timing doesn't leak which of
    the two it was either.
    """
    user = await _load_user_by_email(db, str(payload.email))

    if user is None:
        # Equalise the work done, so a missing account isn't measurably
        # faster than a wrong password.
        hash_password(payload.password)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect email or password.")

    if not verify_password(payload.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect email or password.")

    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This account has been suspended.")

    body = await _token_response(db, user, request.headers.get("user-agent"))
    await db.commit()
    return body


@router.post("/refresh", response_model=TokenResponse, summary="Exchange a refresh token")
async def refresh(
    request: Request,
    payload: RefreshRequest,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    """
    Rotate the pair.

    The presented token is revoked as the new one is issued. Presenting an
    already-revoked token is treated as theft — every session for that user
    is ended, because either the attacker or the real user is holding a copy
    that must stop working.
    """
    stored = (
        await db.execute(
            select(RefreshToken).where(
                RefreshToken.token_hash == hash_refresh_token(payload.refresh_token)
            )
        )
    ).scalar_one_or_none()

    if stored is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid session. Sign in again.")

    if stored.revoked_at is not None:
        # Only a *rotated* token coming back is evidence of theft: it was
        # handed to exactly one client, which was given a replacement, so a
        # second presentation means someone else has a copy. Kill the chain.
        #
        # A token revoked by an explicit logout carries no such signal — the
        # user ended that session on purpose. Treating it the same way would
        # mean signing out on a laptop also signed out the phone.
        if stored.revoked_reason == RefreshToken.ROTATED:
            logger.warning(
                f"♻️  Refresh token reuse for user {stored.user_id}; revoking all sessions"
            )
            await revoke_all_sessions(db, stored.user_id)
            await db.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid session. Sign in again.")

    if not stored.is_usable():
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired. Sign in again.")

    user = (
        await db.execute(select(User).where(User.id == stored.user_id))
    ).scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid session. Sign in again.")

    stored.revoked_at = datetime.now(timezone.utc)
    stored.revoked_reason = RefreshToken.ROTATED
    body = await _token_response(db, user, request.headers.get("user-agent"))
    await db.commit()
    return body


@router.post("/logout", response_model=SimpleMessage, summary="Sign out of this session")
async def logout(
    payload: RefreshRequest,
    db: AsyncSession = Depends(get_db),
) -> SimpleMessage:
    """
    Revoke one refresh token.

    Deliberately unauthenticated and always 200: a client discarding a token
    it can no longer use should not be told that the logout failed, and
    knowing the token is the only capability being spent.
    """
    stored = (
        await db.execute(
            select(RefreshToken).where(
                RefreshToken.token_hash == hash_refresh_token(payload.refresh_token)
            )
        )
    ).scalar_one_or_none()

    if stored is not None and stored.revoked_at is None:
        stored.revoked_at = datetime.now(timezone.utc)
        stored.revoked_reason = RefreshToken.LOGOUT
        await db.commit()

    return SimpleMessage(status="signed_out", message="Signed out.")


@router.post("/logout-all", response_model=SimpleMessage, summary="Sign out everywhere")
async def logout_all(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SimpleMessage:
    """End every session for this account, on every device."""
    count = await revoke_all_sessions(db, user.id)
    await db.commit()
    return SimpleMessage(
        status="signed_out",
        message=f"Signed out of {count} session{'s' if count != 1 else ''}.",
    )


@router.get("/sessions", response_model=list[SessionResponse], summary="List live sessions")
async def list_sessions(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[SessionResponse]:
    """Where this account is currently signed in."""
    rows = (
        await db.execute(
            select(RefreshToken)
            .where(RefreshToken.user_id == user.id)
            .where(RefreshToken.revoked_at.is_(None))
            .order_by(RefreshToken.created_at.desc())
        )
    ).scalars().all()
    return [
        SessionResponse(
            id=row.id,
            user_agent=row.user_agent,
            created_at=row.created_at,
            expires_at=row.expires_at,
        )
        for row in rows
        if row.is_usable()
    ]


@router.post(
    "/forgot-password", response_model=SimpleMessage, summary="Request a password reset"
)
@limiter.limit(lambda: get_settings().rate_limit_password_reset)
async def forgot_password(
    request: Request,
    response: Response,
    payload: ForgotPasswordRequest,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> SimpleMessage:
    """
    Email a one-time reset link.

    Always answers RESET_SENT_MESSAGE, whether or not the account exists —
    otherwise this endpoint is a free membership oracle.
    """
    settings = get_settings()
    user = await _load_user_by_email(db, str(payload.email))

    if user is not None and user.is_active:
        token = new_token()
        user.reset_token_hash = hash_token(token)
        user.reset_token_expires_at = datetime.now(timezone.utc) + timedelta(
            minutes=settings.password_reset_expire_minutes
        )
        await db.commit()
        background.add_task(
            _mail,
            send_password_reset_email,
            user.email,
            user.display_name,
            _reset_url(token),
            settings.password_reset_expire_minutes,
        )

    return SimpleMessage(status="sent", message=RESET_SENT_MESSAGE)


@router.post("/reset-password", response_model=SimpleMessage, summary="Set a new password")
@limiter.limit(lambda: get_settings().rate_limit_password_reset)
async def reset_password(
    request: Request,
    response: Response,
    payload: ResetPasswordRequest,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> SimpleMessage:
    """
    Spend a reset token on a new password, then sign every device out.

    A reset is usually a response to losing control of the account, so
    leaving other sessions alive would defeat it.
    """
    result = await db.execute(
        select(User).where(User.reset_token_hash == hash_token(payload.token))
    )
    user = result.scalar_one_or_none()

    if user is None or not tokens_match(payload.token, user.reset_token_hash):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid or expired link.")

    expires = user.reset_token_expires_at
    if expires is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid or expired link.")
    if expires.tzinfo is None:  # SQLite hands back naive datetimes
        expires = expires.replace(tzinfo=timezone.utc)
    if expires <= datetime.now(timezone.utc):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "That link has expired.")

    user.password_hash = hash_password(payload.password)
    user.reset_token_hash = None  # one-time use
    user.reset_token_expires_at = None
    # A reset also proves the address works.
    user.is_verified = True
    await revoke_all_sessions(db, user.id)
    await db.commit()

    background.add_task(_mail, send_password_changed_email, user.email, user.display_name)
    return SimpleMessage(
        status="reset",
        message="Password changed. Sign in with your new password.",
    )


@router.post(
    "/change-password", response_model=SimpleMessage, summary="Change your password"
)
async def change_password(
    payload: ChangePasswordRequest,
    background: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SimpleMessage:
    """Change it from inside the account. Proves the old one first."""
    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Current password is incorrect.")

    if verify_password(payload.new_password, user.password_hash):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "That is the password you already have."
        )

    user.password_hash = hash_password(payload.new_password)
    await revoke_all_sessions(db, user.id)
    await db.commit()

    background.add_task(_mail, send_password_changed_email, user.email, user.display_name)
    return SimpleMessage(
        status="changed",
        message="Password changed. Every device has been signed out.",
    )


@router.get("/me", response_model=UserResponse, summary="The signed-in account")
async def me(user: User = Depends(get_current_user)) -> UserResponse:
    return UserResponse.from_user(user)


@router.patch("/me", response_model=UserResponse, summary="Update your profile")
async def update_me(
    payload: UpdateProfileRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UserResponse:
    """
    The display name is the only editable field.

    The email address is not: it is the account's identity and the address
    letters are delivered to, so changing it needs a verification round-trip
    of its own rather than a quiet PATCH.
    """
    user.display_name = payload.display_name or user.email.split("@")[0]
    await db.commit()
    await db.refresh(user)
    return UserResponse.from_user(user)


@router.delete("/me", response_model=SimpleMessage, summary="Delete your account")
async def delete_me(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SimpleMessage:
    """
    Delete the account, its journals, and its entries.

    Letters are *not* deleted — they are detached (user_id → NULL). A letter
    already confirmed is a promise to a recipient who is often someone else,
    and deleting your account should not silently cancel mail they are
    waiting for. Scheduled letters can still be cancelled individually
    beforehand with the manage link.
    """
    from sqlalchemy import update

    from app.models import Journal, JournalEntry, Letter

    await db.execute(
        update(Letter).where(Letter.user_id == user.id).values(user_id=None)
    )
    await db.execute(
        JournalEntry.__table__.delete().where(JournalEntry.user_id == user.id)
    )
    await db.execute(Journal.__table__.delete().where(Journal.user_id == user.id))
    await revoke_all_sessions(db, user.id)
    await db.execute(User.__table__.delete().where(User.id == user.id))
    await db.commit()

    logger.info(f"🗑️  Account deleted: {user.id}")
    return SimpleMessage(
        status="deleted",
        message="Your account and journals are gone. Letters already on their way will still arrive.",
    )
