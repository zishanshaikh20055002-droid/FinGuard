"""Admin-only retained dead-letter inspection and replay scheduling."""

import hashlib
import json
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.dependencies import CurrentUser
from app.auth.permissions import require_permission_for
from app.auth.service import audit
from app.core.errors import DomainError
from app.db.session import get_db_session
from app.models import OutboxEvent, ReplayRequest

router = APIRouter(tags=["event operations"])
DbSession = Annotated[Session, Depends(get_db_session)]


class ReplayBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=5, max_length=500)
    idempotency_key: str = Field(min_length=8, max_length=255)


@router.get("/admin/events/dead-letter")
def dead_letters(user: CurrentUser, session: DbSession) -> dict:
    require_permission_for(user, "user:manage")
    rows = session.scalars(
        select(OutboxEvent)
        .where(OutboxEvent.status == "dead_letter")
        .order_by(OutboxEvent.created_at)
        .limit(100)
    ).all()
    return {
        "items": [
            {
                "event_id": str(row.id),
                "event_type": row.event_type,
                "aggregate_id": str(row.aggregate_id),
                "attempt_count": row.attempt_count,
                "last_error": row.last_error,
            }
            for row in rows
        ]
    }


@router.post("/admin/events/{event_id}/replay", status_code=202)
def replay_event(event_id: UUID, body: ReplayBody, user: CurrentUser, session: DbSession) -> dict:
    require_permission_for(user, "user:manage")
    with session.begin():
        row = session.scalar(
            select(OutboxEvent).where(OutboxEvent.id == event_id).with_for_update()
        )
        if row is None:
            raise DomainError("event_not_found", "Event not found", 404)
        existing = session.scalar(
            select(ReplayRequest).where(
                ReplayRequest.requested_by_admin_id == user.id,
                ReplayRequest.idempotency_key == body.idempotency_key,
            )
        )
        if existing:
            digest = hashlib.sha256(
                json.dumps(
                    {"event_id": str(event_id), "reason": body.reason}, sort_keys=True
                ).encode()
            ).hexdigest()
            if existing.original_event_id != event_id or existing.request_fingerprint != digest:
                raise DomainError(
                    "replay_idempotency_conflict", "Replay key belongs to another event", 409
                )
            return {
                "replay_request_id": str(existing.id),
                "event_id": str(event_id),
                "status": existing.status,
            }
        replay = ReplayRequest(
            id=uuid4(),
            original_event_id=event_id,
            requested_by_admin_id=user.id,
            idempotency_key=body.idempotency_key,
            reason=body.reason,
            status="pending",
            request_fingerprint=hashlib.sha256(
                json.dumps(
                    {"event_id": str(event_id), "reason": body.reason}, sort_keys=True
                ).encode()
            ).hexdigest(),
        )
        session.add(replay)
        session.flush()
        audit(session, "event.replay_requested", user_id=user.id, session_id=user.session_id)
        return {
            "replay_request_id": str(replay.id),
            "event_id": str(row.id),
            "status": "pending",
        }
