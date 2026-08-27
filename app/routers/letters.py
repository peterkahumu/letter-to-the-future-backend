"""
Letter routes — write, confirm, read, cancel, and report letters.
"""

import logging
import uuid
from datetime import datetime, timezone

import cloudinary
import cloudinary.uploader
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.auth import get_optional_user
from app.core.limiter import limiter
from app.core.media import kind_from_content_type, resolve_kind
from app.core.security import (
    hash_token,
    new_token,
    sanitize_html,
    tokens_match,
    verify_unsubscribe_token,
)
from app.database import get_db
from app.models import EmailSuppression, Letter, LetterReport, LetterStatus, User
from app.schemas import (
    LetterCreate,
    LetterCreatedResponse,
    LetterStatusResponse,
    PublicFeedPage,
    PublicLetterResponse,
    ReportCreate,
    SimpleMessage,
    mask_email,
)
from app.services.email_service import send_confirmation_email

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/letters", tags=["letters"])

ALLOWED_UPLOAD_TYPES = {
    "image/jpeg", "image/png", "image/gif", "image/webp",
    "audio/mpeg", "audio/mp4", "audio/ogg", "audio/wav",
    "video/mp4", "video/webm", "video/quicktime",
}
# Letters older than this drop out of the feed cursor; keeps the query bounded.
FEED_PAGE_MAX = 50


def _confirm_url(letter_id: uuid.UUID, token: str) -> str:
    base = get_settings().primary_frontend_url
    return f"{base}/confirm/{letter_id}?token={token}"


def _manage_url(letter_id: uuid.UUID, token: str) -> str:
    base = get_settings().primary_frontend_url
    return f"{base}/confirmation/{letter_id}?token={token}"


async def _load_letter(db: AsyncSession, letter_id: uuid.UUID) -> Letter:
    result = await db.execute(select(Letter).where(Letter.id == letter_id))
    letter = result.scalar_one_or_none()
    if not letter:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Letter not found.")
    return letter


def _send_confirmation(letter: Letter, confirm_url: str, manage_url: str) -> None:
    """Background task — never let a mail failure break the create request."""
    try:
        send_confirmation_email(letter, confirm_url, manage_url)
    except Exception as e:  # noqa: BLE001
        logger.error(f"Confirmation email failed for {letter.id}: {e}")


@router.post(
    "",
    response_model=LetterCreatedResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Write a letter (pending confirmation)",
)
@limiter.limit(lambda: get_settings().rate_limit_create)
async def create_letter(
    request: Request,
    response: Response,
    payload: LetterCreate,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    author: User | None = Depends(get_optional_user),
) -> LetterCreatedResponse:
    """
    Record a letter and email the recipient a confirmation link.

    Nothing is scheduled until that link is clicked (P1) — this is what stops
    the service being used to mail people who never asked for it.

    Signing in is optional here on purpose. A signed-in writer gets the
    letter attached to their account so it shows up in their dashboard; a
    guest gets exactly the flow that existed before accounts did. Either
    way the letter is sealed once confirmed — ownership decides who can
    *see* it listed, never who can rewrite it (E6).
    """
    settings = get_settings()
    email = str(payload.recipient_email)

    # Respect unsubscribes before anything else (P5).
    suppressed = await db.execute(
        select(EmailSuppression.email).where(
            func.lower(EmailSuppression.email) == email.lower()
        )
    )
    if suppressed.scalar_one_or_none():
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "This address has asked not to receive letters.",
        )

    # Cap how many unconfirmed letters can pile up against one address, so
    # the confirmation mail itself can't be used to spam someone (E5).
    pending = await db.execute(
        select(func.count())
        .select_from(Letter)
        .where(func.lower(Letter.recipient_email) == email.lower())
        .where(Letter.status == LetterStatus.PENDING)
    )
    if (pending.scalar() or 0) >= settings.max_pending_per_recipient:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Too many unconfirmed letters for this address.",
        )

    confirm_token = new_token()
    manage_token = new_token()

    letter = Letter(
        user_id=author.id if author else None,
        recipient_email=email,
        sender_name=payload.sender_name,
        subject=payload.subject,
        # Sanitized on write — the API is public, so browser-side cleaning
        # would be trivially bypassed (E4).
        body=sanitize_html(payload.body),
        media_url=payload.media_url,
        media_type=resolve_kind(payload.media_type, payload.media_url),
        is_public=payload.is_public,
        is_anonymous=payload.is_anonymous,
        deliver_at=payload.deliver_at,
        timezone_name=payload.timezone_name,
        status=LetterStatus.PENDING,
        confirm_token_hash=hash_token(confirm_token),
        manage_token_hash=hash_token(manage_token),
    )
    db.add(letter)
    await db.commit()
    await db.refresh(letter)

    background.add_task(
        _send_confirmation,
        letter,
        _confirm_url(letter.id, confirm_token),
        _manage_url(letter.id, manage_token),
    )

    return LetterCreatedResponse(
        id=letter.id,
        status=letter.status,
        recipient_email_masked=mask_email(email),
        deliver_at=letter.deliver_at,
        message="Check your inbox — the letter is scheduled once you confirm.",
    )


@router.post(
    "/{letter_id}/confirm",
    response_model=LetterStatusResponse,
    summary="Confirm a letter and schedule it",
)
async def confirm_letter(
    letter_id: uuid.UUID,
    token: str = Query(..., min_length=16),
    db: AsyncSession = Depends(get_db),
) -> LetterStatusResponse:
    """Exchange a confirmation token for a scheduled letter (P1)."""
    letter = await _load_letter(db, letter_id)

    if not tokens_match(token, letter.confirm_token_hash):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid or expired link.")

    if letter.status == LetterStatus.PENDING:
        letter.status = LetterStatus.SCHEDULED
        letter.confirmed_at = datetime.now(timezone.utc)
        # One-time use.
        letter.confirm_token_hash = None
        await db.commit()
        await db.refresh(letter)

    return LetterStatusResponse.from_letter(letter)


@router.get(
    "/{letter_id}",
    response_model=LetterStatusResponse,
    summary="Get letter status",
)
async def get_letter(
    letter_id: uuid.UUID,
    token: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> LetterStatusResponse:
    """
    Status of a single letter. The address is masked either way; the manage
    token is what proves the caller may cancel it.
    """
    letter = await _load_letter(db, letter_id)
    if token and not tokens_match(token, letter.manage_token_hash):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid link.")
    return LetterStatusResponse.from_letter(letter)


@router.delete(
    "/{letter_id}",
    response_model=SimpleMessage,
    summary="Cancel a letter before delivery",
)
async def cancel_letter(
    letter_id: uuid.UUID,
    token: str = Query(..., min_length=16),
    db: AsyncSession = Depends(get_db),
) -> SimpleMessage:
    """
    Withdraw a letter that hasn't been delivered yet (P2).

    Requires the manage token from the confirmation email — there are no
    accounts, so the link is the credential.
    """
    letter = await _load_letter(db, letter_id)

    if not tokens_match(token, letter.manage_token_hash):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid link.")

    if letter.status == LetterStatus.SENT:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This letter has already been delivered and can't be recalled.",
        )

    letter.status = LetterStatus.CANCELLED
    await db.commit()
    return SimpleMessage(status="cancelled", message="The letter will not be sent.")


@router.get(
    "/feed/public",
    response_model=PublicFeedPage,
    summary="Get delivered public letters",
)
async def get_public_letters(
    limit: int = Query(default=20, ge=1, le=FEED_PAGE_MAX),
    cursor: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> PublicFeedPage:
    """
    Public feed — delivered letters their writers chose to share.

    Returns PublicLetterResponse, which carries no recipient address and
    resolves the sender name through is_anonymous (E1, E2). Paginated so the
    query stays bounded (R5).
    """
    stmt = (
        select(Letter)
        .where(Letter.is_public.is_(True))
        .where(Letter.status == LetterStatus.SENT)
        .where(Letter.is_hidden.is_(False))
        .order_by(Letter.sent_at.desc())
        .limit(limit + 1)
    )
    if cursor:
        try:
            stmt = stmt.where(Letter.sent_at < datetime.fromisoformat(cursor))
        except ValueError:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid cursor.")

    rows = list((await db.execute(stmt)).scalars().all())
    has_more = len(rows) > limit
    rows = rows[:limit]

    next_cursor = None
    if has_more and rows and rows[-1].sent_at:
        next_cursor = rows[-1].sent_at.isoformat()

    return PublicFeedPage(
        items=[PublicLetterResponse.from_letter(letter) for letter in rows],
        next_cursor=next_cursor,
    )


@router.post(
    "/{letter_id}/report",
    response_model=SimpleMessage,
    summary="Report a public letter",
)
@limiter.limit(lambda: get_settings().rate_limit_report)
async def report_letter(
    request: Request,
    response: Response,
    letter_id: uuid.UUID,
    payload: ReportCreate,
    db: AsyncSession = Depends(get_db),
) -> SimpleMessage:
    """
    Flag a public letter for review (P4).

    The reason is recorded alongside the count so moderation can see what is
    actually being reported. Auto-hides once several people agree, so abuse
    comes down without waiting for a human.
    """
    letter = await _load_letter(db, letter_id)

    db.add(LetterReport(
        letter_id=letter.id, reason=payload.reason, details=payload.details
    ))
    letter.reported_count += 1
    if letter.reported_count >= 3:
        letter.is_hidden = True
        logger.warning(
            f"🚩 Letter {letter.id} auto-hidden after {letter.reported_count} reports "
            f"(latest reason: {payload.reason})"
        )
    await db.flush()
    return SimpleMessage(
        status="received",
        message="Thanks — a moderator will review this letter.",
    )


@router.post(
    "/unsubscribe",
    response_model=SimpleMessage,
    summary="Stop all letters to an address",
)
async def unsubscribe(
    email: str = Query(...),
    t: str = Query(...),
    db: AsyncSession = Depends(get_db),
) -> SimpleMessage:
    """
    Honour an unsubscribe link (P5). Suppresses the address and cancels
    everything still queued for it.
    """
    if not verify_unsubscribe_token(email, t):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid unsubscribe link.")

    exists = await db.execute(
        select(EmailSuppression).where(func.lower(EmailSuppression.email) == email.lower())
    )
    if exists.scalar_one_or_none() is None:
        db.add(EmailSuppression(email=email.lower(), reason="unsubscribed"))

    queued = await db.execute(
        select(Letter)
        .where(func.lower(Letter.recipient_email) == email.lower())
        .where(Letter.status.in_([LetterStatus.PENDING, LetterStatus.SCHEDULED]))
    )
    for letter in queued.scalars().all():
        letter.status = LetterStatus.CANCELLED
        letter.last_error = "recipient unsubscribed"

    await db.flush()
    return SimpleMessage(
        status="unsubscribed",
        message="You won't receive any more letters at this address.",
    )


@router.post(
    "/upload",
    summary="Upload media to Cloudinary",
)
@limiter.limit(lambda: get_settings().rate_limit_upload)
async def upload_media(
    request: Request,
    response: Response,
    file: UploadFile = File(...),
) -> dict:
    """Upload an attachment. Size and type are capped so the endpoint can't
    be used as free file hosting on your Cloudinary quota (E5)."""
    settings = get_settings()
    if not settings.cloudinary_url:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Cloudinary is not configured.")

    if file.content_type not in ALLOWED_UPLOAD_TYPES:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            f"Unsupported file type: {file.content_type}",
        )

    # Read with a ceiling rather than trusting the declared length.
    data = await file.read(settings.max_upload_bytes + 1)
    if len(data) > settings.max_upload_bytes:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"File exceeds {settings.max_upload_bytes // (1024 * 1024)}MB.",
        )

    # Cloudinary reports audio as resource_type "video", so the browser's
    # declared MIME type is what decides how this renders later.
    kind = kind_from_content_type(file.content_type)

    try:
        import anyio

        result = await anyio.to_thread.run_sync(
            lambda: cloudinary.uploader.upload(
                data, folder="letters_to_future", resource_type="auto"
            )
        )
        url = result.get("secure_url")
        return {"url": url, "media_type": resolve_kind(kind, url)}
    except Exception as e:
        logger.error(f"Upload failed: {e}")
        # Don't echo provider internals back to the caller.
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Upload failed.")
