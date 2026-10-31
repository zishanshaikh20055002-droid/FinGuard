"""Persist events atomically with application state; no external publishing yet."""

from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.models import OutboxEvent


def record_event(
    session: Session,
    *,
    aggregate_id: UUID,
    event_type: str,
    payload: dict,
    correlation_id: UUID | None = None,
) -> OutboxEvent:
    event = OutboxEvent(
        aggregate_type="transaction",
        aggregate_id=aggregate_id,
        event_type=event_type,
        payload={"schema_version": 1, **payload},
        attempt_count=0,
        status="pending",
        correlation_id=correlation_id or uuid4(),
    )
    session.add(event)
    return event
