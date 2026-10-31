"""Create foundational FinGuard schema.

Revision ID: 20261031_0001
Revises:
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "20261031_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "accounts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("reference", sa.String(100), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("account_type", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "length(currency) = 3 AND currency = upper(currency)", name="ck_accounts_currency_code"
        ),
        sa.CheckConstraint(
            "account_type IN ('asset', 'liability', 'equity', 'income', 'expense')",
            name="ck_accounts_account_type",
        ),
        sa.CheckConstraint(
            "status IN ('open', 'frozen', 'closed')", name="ck_accounts_account_status"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_accounts"),
        sa.UniqueConstraint("reference", name="uq_accounts_reference"),
    )
    op.create_table(
        "transactions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_account_id", sa.Uuid(), nullable=False),
        sa.Column("destination_account_id", sa.Uuid(), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("amount_minor > 0", name="ck_transactions_positive_amount"),
        sa.CheckConstraint(
            "source_account_id <> destination_account_id", name="ck_transactions_distinct_accounts"
        ),
        sa.CheckConstraint(
            "length(currency) = 3 AND currency = upper(currency)",
            name="ck_transactions_currency_code",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'blocked', 'failed', 'posted')",
            name="ck_transactions_transaction_status",
        ),
        sa.ForeignKeyConstraint(
            ["source_account_id"],
            ["accounts.id"],
            name="fk_transactions_source_account_id_accounts",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["destination_account_id"],
            ["accounts.id"],
            name="fk_transactions_destination_account_id_accounts",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_transactions"),
    )
    op.create_index(
        "ix_transactions_source_created", "transactions", ["source_account_id", "created_at"]
    )
    op.create_index(
        "ix_transactions_destination_created",
        "transactions",
        ["destination_account_id", "created_at"],
    )
    op.create_table(
        "idempotency_keys",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("operation", sa.String(100), nullable=False),
        sa.Column("key", sa.String(255), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("resource_id", sa.Uuid(), nullable=True),
        sa.Column("response_metadata", JSONB(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_idempotency_keys"),
        sa.UniqueConstraint("operation", "key", name="uq_idempotency_operation_key"),
    )
    op.create_table(
        "risk_decisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("transaction_id", sa.Uuid(), nullable=False),
        sa.Column("risk_score", sa.Numeric(5, 4), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("model_name", sa.String(100), nullable=False),
        sa.Column("model_version", sa.String(100), nullable=False),
        sa.Column("explanation_metadata", JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "risk_score >= 0 AND risk_score <= 1", name="ck_risk_decisions_risk_score_range"
        ),
        sa.CheckConstraint(
            "decision IN ('approve', 'block', 'review')", name="ck_risk_decisions_risk_decision"
        ),
        sa.ForeignKeyConstraint(
            ["transaction_id"],
            ["transactions.id"],
            name="fk_risk_decisions_transaction_id_transactions",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_risk_decisions"),
    )
    op.create_index("ix_risk_decisions_transaction_id", "risk_decisions", ["transaction_id"])
    op.create_table(
        "journal_entries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("transaction_id", sa.Uuid(), nullable=False),
        sa.Column("entry_type", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("reverses_entry_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "entry_type IN ('standard', 'reversal')", name="ck_journal_entries_entry_type"
        ),
        sa.CheckConstraint("status IN ('draft', 'posted')", name="ck_journal_entries_entry_status"),
        sa.CheckConstraint(
            "(status = 'draft' AND posted_at IS NULL) OR "
            "(status = 'posted' AND posted_at IS NOT NULL)",
            name="ck_journal_entries_posted_timestamp",
        ),
        sa.CheckConstraint(
            "(entry_type = 'reversal' AND reverses_entry_id IS NOT NULL) OR "
            "(entry_type = 'standard' AND reverses_entry_id IS NULL)",
            name="ck_journal_entries_reversal_reference",
        ),
        sa.ForeignKeyConstraint(
            ["transaction_id"],
            ["transactions.id"],
            name="fk_journal_entries_transaction_id_transactions",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reverses_entry_id"],
            ["journal_entries.id"],
            name="fk_journal_entries_reverses_entry_id_journal_entries",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_journal_entries"),
        sa.UniqueConstraint("reverses_entry_id", name="uq_journal_entries_reverses_entry_id"),
    )
    op.create_index("ix_journal_entries_transaction_id", "journal_entries", ["transaction_id"])
    op.create_table(
        "journal_lines",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("journal_entry_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("debit_minor", sa.BigInteger(), nullable=False),
        sa.Column("credit_minor", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "length(currency) = 3 AND currency = upper(currency)",
            name="ck_journal_lines_currency_code",
        ),
        sa.CheckConstraint(
            "debit_minor >= 0 AND credit_minor >= 0", name="ck_journal_lines_nonnegative_amounts"
        ),
        sa.CheckConstraint(
            "(debit_minor > 0 AND credit_minor = 0) OR (credit_minor > 0 AND debit_minor = 0)",
            name="ck_journal_lines_single_sided_positive_amount",
        ),
        sa.ForeignKeyConstraint(
            ["journal_entry_id"],
            ["journal_entries.id"],
            name="fk_journal_lines_journal_entry_id_journal_entries",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name="fk_journal_lines_account_id_accounts",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_journal_lines"),
    )
    op.create_index("ix_journal_lines_journal_entry_id", "journal_lines", ["journal_entry_id"])
    op.create_index("ix_journal_lines_account_id", "journal_lines", ["account_id"])
    op.create_table(
        "outbox_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("aggregate_type", sa.String(100), nullable=False),
        sa.Column("aggregate_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.CheckConstraint("attempt_count >= 0", name="ck_outbox_events_nonnegative_attempt_count"),
        sa.PrimaryKeyConstraint("id", name="pk_outbox_events"),
    )
    op.create_index(
        "ix_outbox_events_unpublished_created", "outbox_events", ["published_at", "created_at"]
    )
    op.create_index(
        "ix_outbox_events_aggregate", "outbox_events", ["aggregate_type", "aggregate_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_outbox_events_aggregate", table_name="outbox_events")
    op.drop_index("ix_outbox_events_unpublished_created", table_name="outbox_events")
    op.drop_table("outbox_events")
    op.drop_index("ix_journal_lines_account_id", table_name="journal_lines")
    op.drop_index("ix_journal_lines_journal_entry_id", table_name="journal_lines")
    op.drop_table("journal_lines")
    op.drop_index("ix_journal_entries_transaction_id", table_name="journal_entries")
    op.drop_table("journal_entries")
    op.drop_index("ix_risk_decisions_transaction_id", table_name="risk_decisions")
    op.drop_table("risk_decisions")
    op.drop_table("idempotency_keys")
    op.drop_index("ix_transactions_destination_created", table_name="transactions")
    op.drop_index("ix_transactions_source_created", table_name="transactions")
    op.drop_table("transactions")
    op.drop_table("accounts")
