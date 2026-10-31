"""Persist transaction memo and ordered status history.

Revision ID: 20261031_0003
Revises: 20261031_0002
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261031_0003"
down_revision: str | None = "20261031_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("transactions", sa.Column("memo", sa.String(255), nullable=True))
    op.create_table(
        "transaction_status_history",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("transaction_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("from_status", sa.String(16), nullable=True),
        sa.Column("to_status", sa.String(16), nullable=False),
        sa.Column("reason_code", sa.String(100), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("sequence > 0", name="ck_transaction_status_history_positive_sequence"),
        sa.CheckConstraint(
            "to_status IN ('received', 'pending_risk', 'challenged', 'approved', "
            "'blocked', 'failed', 'posted')",
            name="ck_transaction_status_history_to_status",
        ),
        sa.ForeignKeyConstraint(
            ["transaction_id"],
            ["transactions.id"],
            name="fk_transaction_status_history_transaction_id_transactions",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_transaction_status_history"),
        sa.UniqueConstraint(
            "transaction_id", "sequence", name="uq_transaction_status_history_sequence"
        ),
    )
    op.create_index(
        "ix_transaction_status_history_transaction_created",
        "transaction_status_history",
        ["transaction_id", "created_at"],
    )


def downgrade() -> None:
    op.execute("LOCK TABLE transaction_status_history IN ACCESS EXCLUSIVE MODE")
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM transaction_status_history) THEN
                RAISE EXCEPTION 'FinGuard downgrade blocked: status history is not empty'
                    USING ERRCODE = 'P0001';
            END IF;
        END;
        $$
        """
    )
    op.drop_index(
        "ix_transaction_status_history_transaction_created",
        table_name="transaction_status_history",
    )
    op.drop_table("transaction_status_history")
    op.drop_column("transactions", "memo")
