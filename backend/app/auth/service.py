"""Database-backed authentication and session rotation."""

import hashlib
import hmac
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.schemas import Credentials, LoginRequest
from app.auth.security import (
    hash_password,
    make_access_token,
    opaque_token,
    token_hash,
    verify_password,
)
from app.core.config import Settings
from app.core.errors import DomainError
from app.models import AuthAuditEvent, AuthSession, RefreshToken, User


@dataclass(frozen=True)
class SessionCredentials:
    access_token: str
    refresh_token: str
    csrf_token: str
    user: dict


def safe_user(user: User) -> dict:
    return {
        "user_id": str(user.id),
        "email": user.email,
        "role": user.role,
        "status": user.status,
        "created_at": user.created_at.isoformat(),
    }


def audit(
    session: Session,
    event_type: str,
    *,
    user_id: UUID | None = None,
    session_id: UUID | None = None,
    subject_hash: str | None = None,
) -> None:
    session.add(
        AuthAuditEvent(
            id=uuid4(),
            event_type=event_type,
            user_id=user_id,
            session_id=session_id,
            subject_hash=subject_hash,
        )
    )


def register(session: Session, request: Credentials, settings: Settings) -> dict:
    try:
        with session.begin():
            user = User(
                id=uuid4(),
                email=request.email,
                password_hash=hash_password(request.password, settings),
                role="customer",
                status="active",
            )
            session.add(user)
            session.flush()
            audit(session, "user.registered", user_id=user.id)
        return safe_user(user)
    except IntegrityError as error:
        raise DomainError("email_unavailable", "Email is unavailable", 409) from error


def login(session: Session, request: LoginRequest, settings: Settings) -> SessionCredentials:
    result = None
    invalid = False
    throttled = False
    subject_hash = hashlib.sha256(request.email.encode("utf-8")).hexdigest()
    with session.begin():
        failures = session.scalar(
            select(func.count())
            .select_from(AuthAuditEvent)
            .where(
                AuthAuditEvent.event_type == "login.failed",
                AuthAuditEvent.subject_hash == subject_hash,
                AuthAuditEvent.created_at >= datetime.now(UTC) - timedelta(minutes=15),
            )
        )
        if failures >= 10:
            throttled = True
        user = session.scalar(select(User).where(User.email == request.email))
        if (
            throttled
            or user is None
            or not verify_password(request.password, user.password_hash, settings)
            or user.status != "active"
        ):
            if not throttled:
                audit(
                    session,
                    "login.failed",
                    user_id=None if user is None else user.id,
                    subject_hash=subject_hash,
                )
            invalid = True
        else:
            now = datetime.now(UTC)
            refresh = opaque_token()
            csrf = opaque_token()
            auth_session = AuthSession(
                id=uuid4(),
                user_id=user.id,
                csrf_hash=token_hash(csrf),
                expires_at=now + timedelta(days=settings.refresh_token_days),
            )
            session.add(auth_session)
            session.add(
                RefreshToken(
                    id=uuid4(),
                    session_id=auth_session.id,
                    token_hash=token_hash(refresh),
                    expires_at=auth_session.expires_at,
                )
            )
            user.last_login_at = now
            audit(session, "login.succeeded", user_id=user.id, session_id=auth_session.id)
            session.flush()
            result = SessionCredentials(
                make_access_token(user.id, auth_session.id, settings),
                refresh,
                csrf,
                safe_user(user),
            )
    if invalid:
        raise DomainError(
            "login_unavailable" if throttled else "invalid_credentials",
            "Invalid credentials",
            429 if throttled else 401,
        )
    return result


def _check_csrf(auth_session: AuthSession, csrf: str | None) -> None:
    if not csrf or not hmac.compare_digest(auth_session.csrf_hash, token_hash(csrf)):
        raise DomainError("invalid_csrf", "CSRF validation failed", 403)


def refresh_session(
    session: Session, raw_token: str | None, csrf: str | None, settings: Settings
) -> SessionCredentials:
    if not raw_token:
        raise DomainError("invalid_refresh", "Invalid session", 401)
    outcome = None
    reuse = False
    with session.begin():
        existing = session.scalar(
            select(RefreshToken).where(RefreshToken.token_hash == token_hash(raw_token))
        )
        if existing is None:
            raise DomainError("invalid_refresh", "Invalid session", 401)
        auth_session = session.scalar(
            select(AuthSession).where(AuthSession.id == existing.session_id).with_for_update()
        )
        now = datetime.now(UTC)
        if (
            auth_session is None
            or auth_session.revoked_at is not None
            or auth_session.expires_at <= now
        ):
            raise DomainError("invalid_refresh", "Invalid session", 401)
        _check_csrf(auth_session, csrf)
        # Re-read after the session lock: another refresh may have consumed this token.
        session.refresh(existing, with_for_update=True)
        if existing.consumed_at is not None:
            auth_session.revoked_at = now
            session.execute(
                update(RefreshToken)
                .where(
                    RefreshToken.session_id == auth_session.id, RefreshToken.consumed_at.is_(None)
                )
                .values(consumed_at=now)
            )
            audit(
                session,
                "refresh.reuse_detected",
                user_id=auth_session.user_id,
                session_id=auth_session.id,
            )
            reuse = True
        elif existing.expires_at <= now:
            raise DomainError("invalid_refresh", "Invalid session", 401)
        else:
            user = session.get(User, auth_session.user_id)
            if user is None or user.status != "active":
                raise DomainError("invalid_refresh", "Invalid session", 401)
            existing.consumed_at = now
            new_token = opaque_token()
            session.add(
                RefreshToken(
                    id=uuid4(),
                    session_id=auth_session.id,
                    token_hash=token_hash(new_token),
                    expires_at=auth_session.expires_at,
                )
            )
            audit(session, "refresh.rotated", user_id=user.id, session_id=auth_session.id)
            outcome = SessionCredentials(
                make_access_token(user.id, auth_session.id, settings),
                new_token,
                csrf,
                safe_user(user),
            )
    if reuse:
        raise DomainError("refresh_reuse", "Session revoked", 401)
    return outcome


def logout_session(
    session: Session, session_id: UUID, raw_token: str | None, csrf: str | None
) -> None:
    with session.begin():
        auth_session = session.scalar(
            select(AuthSession).where(AuthSession.id == session_id).with_for_update()
        )
        if auth_session is None or auth_session.revoked_at is not None:
            raise DomainError("invalid_session", "Authentication required", 401)
        _check_csrf(auth_session, csrf)
        if not raw_token:
            raise DomainError("invalid_refresh", "Invalid session", 401)
        token = session.scalar(
            select(RefreshToken).where(RefreshToken.token_hash == token_hash(raw_token))
        )
        if token is None or token.session_id != session_id or token.consumed_at is not None:
            raise DomainError("invalid_refresh", "Invalid session", 401)
        auth_session.revoked_at = datetime.now(UTC)
        session.execute(
            update(RefreshToken)
            .where(RefreshToken.session_id == session_id, RefreshToken.consumed_at.is_(None))
            .values(consumed_at=auth_session.revoked_at)
        )
        audit(session, "logout", user_id=auth_session.user_id, session_id=auth_session.id)
