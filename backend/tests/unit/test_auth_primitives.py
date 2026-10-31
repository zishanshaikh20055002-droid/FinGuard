"""Security primitive and fail-closed configuration checks."""

from uuid import uuid4

import pytest
from fastapi import Response
from pydantic import ValidationError

from app.auth.routes import _set_refresh_cookie
from app.auth.security import decode_access_token, hash_password, make_access_token, verify_password
from app.core.config import Settings
from app.core.errors import DomainError


def test_argon2id_password_hash_and_verification() -> None:
    settings = Settings(_env_file=None)
    password = "Correct Battery Horse 123!"
    stored = hash_password(password, settings)
    assert stored.startswith("$argon2id$")
    assert stored != password
    assert verify_password(password, stored, settings)
    assert not verify_password("wrong", stored, settings)
    assert not verify_password(password, "malformed", settings)


def test_jwt_contains_subject_and_session_and_rejects_tampering() -> None:
    settings = Settings(_env_file=None)
    user_id, session_id = uuid4(), uuid4()
    token = make_access_token(user_id, session_id, settings)
    assert decode_access_token(token, settings) == (user_id, session_id)
    with pytest.raises(DomainError):
        decode_access_token(token + "tamper", settings)


def test_production_cookie_is_secure_and_scoped() -> None:
    settings = Settings(
        _env_file=None,
        app_env="production",
        database_url="postgresql+psycopg://placeholder@localhost/test",
        jwt_secret="6zYp4F8wQ2eL9vX7nC1mR5uT3aB0dHkJ",
        allowed_origins="https://example.test",
    )
    response = Response()
    _set_refresh_cookie(response, "opaque-test-token", settings)
    cookie = response.headers["set-cookie"].lower()
    assert "secure" in cookie and "httponly" in cookie and "samesite=lax" in cookie
    assert "path=/api/v1/auth" in cookie and "domain=" not in cookie


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"jwt_secret": "placeholder-placeholder-placeholder"},
        {"cookie_secure": False},
        {"allowed_origins": "*"},
        {"local_insecure_cookie": True},
    ],
)
def test_production_rejects_insecure_auth_settings(overrides: dict) -> None:
    defaults = {
        "database_url": "postgresql+psycopg://placeholder@localhost/test",
        "jwt_secret": "6zYp4F8wQ2eL9vX7nC1mR5uT3aB0dHkJ",
        "allowed_origins": "https://example.test",
    }
    if not overrides:
        defaults.pop("jwt_secret")
    defaults.update(overrides)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, app_env="production", **defaults)
