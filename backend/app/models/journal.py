"""Journal entry headers and monetary debit/credit lines."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    String,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class JournalEntry(Base):
    __tablename__ = "journal_entries"
    __table_args__ = (
        CheckConstraint("entry_type IN ('standard', 'reversal')", name="entry_type"),
        CheckConstraint("status IN ('draft', 'posted')", name="entry_status"),
        CheckConstraint(
            "(status = 'draft' AND posted_at IS NULL) OR "
            "(status = 'posted' AND posted_at IS NOT NULL)",
            name="posted_timestamp",
        ),
        CheckConstraint(
            "(entry_type = 'reversal' AND reverses_entry_id IS NOT NULL) OR "
            "(entry_type = 'standard' AND reverses_entry_id IS NULL)",
            name="reversal_reference",
        ),
        CheckConstraint(
            "reverses_entry_id IS NULL OR reverses_entry_id <> id", name="not_self_reversal"
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    transaction_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("transactions.id", ondelete="RESTRICT"), index=True
    )
    entry_type: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    reverses_entry_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("journal_entries.id", ondelete="RESTRICT"),
        unique=True,
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class JournalLine(Base):
    __tablename__ = "journal_lines"
    __table_args__ = (
        CheckConstraint(
            "length(currency) = 3 AND currency = upper(currency)", name="currency_code"
        ),
        CheckConstraint("currency IN ('USD', 'EUR', 'GBP', 'INR')", name="supported_currency"),
        CheckConstraint("debit_minor >= 0 AND credit_minor >= 0", name="nonnegative_amounts"),
        CheckConstraint(
            "(debit_minor > 0 AND credit_minor = 0) OR (credit_minor > 0 AND debit_minor = 0)",
            name="single_sided_positive_amount",
        ),
        ForeignKeyConstraint(
            ["account_id", "currency"],
            ["accounts.id", "accounts.currency"],
            name="fk_journal_lines_account_currency",
            ondelete="RESTRICT",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    journal_entry_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("journal_entries.id", ondelete="RESTRICT"), index=True
    )
    account_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    debit_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    credit_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
