"""Request authentication and reusable permission dependencies."""

from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.auth.permissions import Principal, require_permission_for
from app.auth.security import decode_access_token
from app.core.config import Settings, get_settings
from app.core.errors import DomainError
from app.db.session import get_db_session
from app.models import AuthSession, User

bearer = HTTPBearer(auto_error=False)


def get_auth_db_session():
    yield from get_db_session()


def get_current_user(
    credential: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    session: Annotated[Session, Depends(get_auth_db_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Principal:
    if credential is None or credential.scheme.lower() != "bearer":
        raise DomainError("authentication_required", "Authentication required", 401)
    user_id, session_id = decode_access_token(credential.credentials, settings)
    auth_session = session.get(AuthSession, session_id)
    if (
        auth_session is None
        or auth_session.user_id != user_id
        or auth_session.revoked_at is not None
    ):
        raise DomainError("invalid_session", "Authentication required", 401)
    from datetime import UTC, datetime

    if auth_session.expires_at <= datetime.now(UTC):
        raise DomainError("invalid_session", "Authentication required", 401)
    user = session.get(User, user_id)
    if user is None or user.status != "active":
        raise DomainError("invalid_session", "Authentication required", 401)
    return Principal(user.id, user.email, user.role, user.status, auth_session.id, user.created_at)


CurrentUser = Annotated[Principal, Depends(get_current_user)]


def require_permission(permission: str):
    def dependency(user: CurrentUser) -> Principal:
        require_permission_for(user, permission)
        return user

    return dependency


def require_role(*roles: str):
    def dependency(user: CurrentUser) -> Principal:
        if user.role not in roles:
            raise DomainError("forbidden", "Permission denied", 403)
        return user

    return dependency
