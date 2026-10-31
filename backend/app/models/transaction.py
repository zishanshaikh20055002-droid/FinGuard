"""Transaction intent and lifecycle; no posting logic lives here."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    String,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Transaction(Base):
    __tablename__ = "transactions"
    __table_args__ = (
        CheckConstraint("amount_minor > 0", name="positive_amount"),
        CheckConstraint("source_account_id <> destination_account_id", name="distinct_accounts"),
        CheckConstraint(
            "length(currency) = 3 AND currency = upper(currency)", name="currency_code"
        ),
        CheckConstraint("currency IN ('USD', 'EUR', 'GBP', 'INR')", name="supported_currency"),
        CheckConstraint(
            "status IN ('received', 'pending_risk', 'challenged', 'approved', "
            "'blocked', 'failed', 'posted')",
            name="transaction_status",
        ),
        ForeignKeyConstraint(
            ["source_account_id", "currency"],
            ["accounts.id", "accounts.currency"],
            name="fk_transactions_source_account_currency",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["destination_account_id", "currency"],
            ["accounts.id", "accounts.currency"],
            name="fk_transactions_destination_account_currency",
            ondelete="RESTRICT",
        ),
        Index("ix_transactions_source_created", "source_account_id", "created_at"),
        Index("ix_transactions_destination_created", "destination_account_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    source_account_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    destination_account_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="received")
    memo: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
