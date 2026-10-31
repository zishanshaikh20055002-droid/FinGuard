"""Auditable requests to republish an immutable historical event."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ReplayRequest(Base):
    __tablename__ = "event_replay_requests"
    __table_args__ = (
        Index("ix_event_replay_requests_status_requested", "status", "requested_at"),
        UniqueConstraint(
            "requested_by_admin_id", "idempotency_key", name="uq_event_replay_request_admin_key"
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    original_event_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("outbox_events.id", ondelete="RESTRICT"), nullable=False
    )
    requested_by_admin_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    attempt_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    claim_worker_id: Mapped[str | None] = mapped_column(String(100))
    claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_generation: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(100))
