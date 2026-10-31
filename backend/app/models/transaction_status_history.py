"""Ordered audit trail for application-level transaction status transitions."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class TransactionStatusHistory(Base):
    __tablename__ = "transaction_status_history"
    __table_args__ = (
        CheckConstraint("sequence > 0", name="positive_sequence"),
        CheckConstraint(
            "to_status IN ('received', 'pending_risk', 'challenged', 'approved', "
            "'blocked', 'failed', 'posted')",
            name="to_status",
        ),
        UniqueConstraint(
            "transaction_id", "sequence", name="uq_transaction_status_history_sequence"
        ),
        Index("ix_transaction_status_history_transaction_created", "transaction_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    transaction_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("transactions.id", ondelete="RESTRICT"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    to_status: Mapped[str] = mapped_column(String(16), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
