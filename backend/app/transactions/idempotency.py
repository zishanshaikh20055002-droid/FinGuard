"""PostgreSQL-backed request deduplication and exact response replay."""

import hashlib
import json
from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.errors import DomainError
from app.models import IdempotencyKey


@dataclass(frozen=True)
class StoredResponse:
    status_code: int
    body: dict


def fingerprint(data: dict) -> str:
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def claim(
    session: Session, *, operation: str, key: str, request_fingerprint: str
) -> tuple[UUID | None, StoredResponse | None]:
    if not key.strip():
        raise DomainError("invalid_idempotency_key", "Idempotency-Key cannot be blank", 400)
    if len(key) > 255:
        raise DomainError("invalid_idempotency_key", "Idempotency-Key is too long", 400)
    record_id = uuid4()
    inserted_id = session.scalar(
        insert(IdempotencyKey)
        .values(
            id=record_id,
            operation=operation,
            key=key,
            request_fingerprint=request_fingerprint,
        )
        .on_conflict_do_nothing(index_elements=["operation", "key"])
        .returning(IdempotencyKey.id)
    )
    if inserted_id is not None:
        return inserted_id, None
    existing = session.scalar(
        select(IdempotencyKey).where(
            IdempotencyKey.operation == operation, IdempotencyKey.key == key
        )
    )
    if existing is None:
        raise RuntimeError("Idempotency record disappeared after conflict")
    if existing.request_fingerprint != request_fingerprint:
        raise DomainError(
            "idempotency_conflict", "Idempotency-Key was used for a different request", 409
        )
    metadata = existing.response_metadata
    if not isinstance(metadata, dict) or "status_code" not in metadata or "body" not in metadata:
        raise DomainError("idempotency_incomplete", "Prior request is not ready for replay", 409)
    return None, StoredResponse(metadata["status_code"], metadata["body"])


def store_response(
    session: Session, *, record_id: UUID, resource_id: UUID, response: StoredResponse
) -> None:
    session.execute(
        update(IdempotencyKey)
        .where(IdempotencyKey.id == record_id)
        .values(
            resource_id=resource_id,
            response_metadata={"status_code": response.status_code, "body": response.body},
        )
    )
