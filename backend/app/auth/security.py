"""Password, signed access token, and opaque token primitives."""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from argon2.low_level import Type

from app.core.config import Settings
from app.core.errors import DomainError


def password_hasher(settings: Settings) -> PasswordHasher:
    return PasswordHasher(
        time_cost=settings.argon2_time_cost,
        memory_cost=settings.argon2_memory_cost,
        parallelism=settings.argon2_parallelism,
        type=Type.ID,
    )


def hash_password(password: str, settings: Settings) -> str:
    return password_hasher(settings).hash(password)


def verify_password(password: str, stored: str, settings: Settings) -> bool:
    try:
        return password_hasher(settings).verify(stored, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def opaque_token() -> str:
    return secrets.token_urlsafe(48)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def make_access_token(user_id: UUID, session_id: UUID, settings: Settings) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": str(user_id),
            "sid": str(session_id),
            "jti": str(uuid4()),
            "iss": settings.jwt_issuer,
            "aud": settings.jwt_audience,
            "iat": now,
            "exp": now + timedelta(minutes=settings.access_token_minutes),
        },
        settings.jwt_secret,
        algorithm="HS256",
    )


def decode_access_token(token: str, settings: Settings) -> tuple[UUID, UUID]:
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=["HS256"],
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
            options={"require": ["sub", "sid", "jti", "iss", "aud", "iat", "exp"]},
            leeway=0,
        )
        return UUID(claims["sub"]), UUID(claims["sid"])
    except (jwt.PyJWTError, ValueError, KeyError, TypeError) as error:
        raise DomainError("invalid_token", "Authentication required", 401) from error
