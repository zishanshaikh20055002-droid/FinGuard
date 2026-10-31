"""Controlled administrative and investigation APIs."""

from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.auth.dependencies import CurrentUser
from app.auth.permissions import require_permission_for
from app.auth.service import audit, safe_user
from app.core.errors import DomainError
from app.db.session import get_db_session
from app.models import AuthSession, User

router = APIRouter(tags=["security"])
DbSession = Annotated[Session, Depends(get_db_session)]


class UserChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["customer", "analyst", "admin"] | None = None
    status: Literal["active", "disabled"] | None = None


@router.patch("/admin/users/{user_id}")
def change_user(user_id: UUID, change: UserChange, user: CurrentUser, session: DbSession) -> dict:
    require_permission_for(user, "user:manage")
    if change.role is None and change.status is None:
        raise DomainError("invalid_change", "A role or status change is required", 400)
    with session.begin():
        target = session.scalar(select(User).where(User.id == user_id).with_for_update())
        if target is None:
            raise DomainError("user_not_found", "User not found", 404)
        if target.id == user.id and (
            change.status == "disabled" or (change.role and change.role != "admin")
        ):
            raise DomainError("self_change_forbidden", "Cannot remove own administration", 409)
        if change.role is not None and change.role != target.role:
            target.role = change.role
            audit(session, "user.role_changed", user_id=target.id, session_id=user.session_id)
        if change.status is not None and change.status != target.status:
            target.status = change.status
            audit(session, "user.status_changed", user_id=target.id, session_id=user.session_id)
        session.execute(
            update(AuthSession)
            .where(AuthSession.user_id == target.id, AuthSession.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
        )
        audit(session, "sessions.revoked", user_id=target.id, session_id=user.session_id)
        session.flush()
        result = safe_user(target)
    return result


@router.get("/admin/users/{user_id}/sessions")
def inspect_sessions(user_id: UUID, user: CurrentUser, session: DbSession) -> dict:
    require_permission_for(user, "session:inspect")
    if session.get(User, user_id) is None:
        raise DomainError("user_not_found", "User not found", 404)
    rows = session.scalars(
        select(AuthSession)
        .where(AuthSession.user_id == user_id)
        .order_by(AuthSession.created_at.desc())
        .limit(100)
    ).all()
    return {
        "items": [
            {
                "session_id": str(row.id),
                "created_at": row.created_at.isoformat(),
                "expires_at": row.expires_at.isoformat(),
                "revoked": row.revoked_at is not None,
            }
            for row in rows
        ]
    }


@router.get("/investigations/audit")
def investigate_audit(user: CurrentUser, session: DbSession) -> dict:
    require_permission_for(user, "transaction:investigate")
    from app.models import AuthAuditEvent

    rows = session.scalars(
        select(AuthAuditEvent).order_by(AuthAuditEvent.created_at.desc()).limit(100)
    ).all()
    return {
        "items": [
            {
                "event_type": row.event_type,
                "user_id": None if row.user_id is None else str(row.user_id),
                "created_at": row.created_at.isoformat(),
            }
            for row in rows
        ]
    }
