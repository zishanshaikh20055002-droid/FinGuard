"""Versioned, validated event envelope contracts."""

import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class EventEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: UUID
    event_type: str = Field(min_length=3, max_length=100)
    schema_version: int = Field(ge=1)
    aggregate_type: str = Field(min_length=1, max_length=100)
    aggregate_id: UUID
    occurred_at: datetime
    correlation_id: UUID
    payload: dict

    @field_validator("occurred_at")
    @classmethod
    def timezone_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must include a timezone")
        return value


def serialize_event(event: EventEnvelope) -> bytes:
    return event.model_dump_json().encode("utf-8")


def event_fingerprint(event: EventEnvelope) -> str:
    """Canonical content fingerprint; JSON field ordering and timestamp offsets do not matter."""
    content = event.model_dump(mode="json")
    content["occurred_at"] = event.occurred_at.astimezone(UTC).isoformat()
    encoded = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
