"""Enforce currency consistency and posted journal integrity.

Revision ID: 20261031_0002
Revises: 20261031_0001
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20261031_0002"
down_revision: str | None = "20261031_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint("uq_accounts_id_currency", "accounts", ["id", "currency"])
    for table in ("accounts", "transactions", "journal_lines"):
        op.create_check_constraint(
            f"ck_{table}_supported_currency",
            table,
            "currency IN ('USD', 'EUR', 'GBP', 'INR')",
        )

    op.drop_constraint(
        "fk_transactions_source_account_id_accounts", "transactions", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_transactions_destination_account_id_accounts", "transactions", type_="foreignkey"
    )
    op.create_foreign_key(
        "fk_transactions_source_account_currency",
        "transactions",
        "accounts",
        ["source_account_id", "currency"],
        ["id", "currency"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_transactions_destination_account_currency",
        "transactions",
        "accounts",
        ["destination_account_id", "currency"],
        ["id", "currency"],
        ondelete="RESTRICT",
    )
    op.drop_constraint("fk_journal_lines_account_id_accounts", "journal_lines", type_="foreignkey")
    op.create_foreign_key(
        "fk_journal_lines_account_currency",
        "journal_lines",
        "accounts",
        ["account_id", "currency"],
        ["id", "currency"],
        ondelete="RESTRICT",
    )

    op.drop_constraint("ck_transactions_transaction_status", "transactions", type_="check")
    op.execute("UPDATE transactions SET status = 'pending_risk' WHERE status = 'pending'")
    op.create_check_constraint(
        "ck_transactions_transaction_status",
        "transactions",
        "status IN ('received', 'pending_risk', 'challenged', 'approved', "
        "'blocked', 'failed', 'posted')",
    )
    op.create_check_constraint(
        "ck_journal_entries_not_self_reversal",
        "journal_entries",
        "reverses_entry_id IS NULL OR reverses_entry_id <> id",
    )

    op.execute(
        """
        CREATE FUNCTION finguard_guard_journal_entry() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.status = 'posted' THEN
                RAISE EXCEPTION 'posted journal entries are immutable'
                    USING ERRCODE = '23514';
            END IF;
            IF TG_OP = 'DELETE' THEN
                RETURN OLD;
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER finguard_journal_entry_guard
        BEFORE UPDATE OR DELETE ON journal_entries
        FOR EACH ROW EXECUTE FUNCTION finguard_guard_journal_entry()
        """
    )
    op.execute(
        """
        CREATE FUNCTION finguard_guard_journal_line() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            parent_id uuid;
            parent_status varchar;
        BEGIN
            IF TG_OP = 'UPDATE' THEN
                IF OLD.journal_entry_id <> NEW.journal_entry_id THEN
                    RAISE EXCEPTION 'journal lines cannot move between entries'
                        USING ERRCODE = '23514';
                END IF;
            END IF;
            IF TG_OP = 'DELETE' THEN
                parent_id := OLD.journal_entry_id;
            ELSE
                parent_id := NEW.journal_entry_id;
            END IF;
            SELECT status INTO parent_status
            FROM journal_entries WHERE id = parent_id FOR UPDATE;
            IF parent_status = 'posted' THEN
                RAISE EXCEPTION 'posted journal lines are immutable'
                    USING ERRCODE = '23514';
            END IF;
            IF TG_OP = 'DELETE' THEN
                RETURN OLD;
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER finguard_journal_line_guard
        BEFORE INSERT OR UPDATE OR DELETE ON journal_lines
        FOR EACH ROW EXECUTE FUNCTION finguard_guard_journal_line()
        """
    )
    op.execute(
        """
        CREATE FUNCTION finguard_validate_posted_entry() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            entry_id uuid;
            entry_status varchar;
            entry_type varchar;
            original_id uuid;
            transaction_id uuid;
            transaction_currency varchar;
            line_count bigint;
            original_status varchar;
            original_transaction_id uuid;
        BEGIN
            IF TG_TABLE_NAME = 'journal_entries' THEN
                entry_id := NEW.id;
            ELSIF TG_OP = 'DELETE' THEN
                entry_id := OLD.journal_entry_id;
            ELSE
                entry_id := NEW.journal_entry_id;
            END IF;

            SELECT e.status, e.entry_type, e.reverses_entry_id, e.transaction_id, t.currency
            INTO entry_status, entry_type, original_id, transaction_id, transaction_currency
            FROM journal_entries e JOIN transactions t ON t.id = e.transaction_id
            WHERE e.id = entry_id;
            IF NOT FOUND OR entry_status <> 'posted' THEN
                RETURN NULL;
            END IF;

            SELECT count(*) INTO line_count FROM journal_lines WHERE journal_entry_id = entry_id;
            IF line_count < 2 THEN
                RAISE EXCEPTION 'posted journal entry requires at least two lines'
                    USING ERRCODE = '23514';
            END IF;
            IF EXISTS (
                SELECT 1 FROM journal_lines WHERE journal_entry_id = entry_id
                AND currency <> transaction_currency
            ) THEN
                RAISE EXCEPTION 'journal currency must equal transaction currency'
                    USING ERRCODE = '23514';
            END IF;
            IF EXISTS (
                SELECT 1 FROM journal_lines WHERE journal_entry_id = entry_id
                GROUP BY currency HAVING sum(debit_minor) <> sum(credit_minor)
            ) THEN
                RAISE EXCEPTION 'posted journal entry is unbalanced'
                    USING ERRCODE = '23514';
            END IF;

            IF entry_type = 'reversal' THEN
                SELECT o.status, o.transaction_id INTO original_status, original_transaction_id
                FROM journal_entries o WHERE o.id = original_id;
                IF original_status IS DISTINCT FROM 'posted'
                    OR original_transaction_id IS DISTINCT FROM transaction_id THEN
                    RAISE EXCEPTION 'reversal requires a posted entry in the same transaction'
                        USING ERRCODE = '23514';
                END IF;
                IF EXISTS (
                    WITH original_lines AS (
                        SELECT account_id, currency, sum(debit_minor) AS debit_sum,
                               sum(credit_minor) AS credit_sum
                        FROM journal_lines WHERE journal_entry_id = original_id
                        GROUP BY account_id, currency
                    ), reversal_lines AS (
                        SELECT account_id, currency, sum(debit_minor) AS debit_sum,
                               sum(credit_minor) AS credit_sum
                        FROM journal_lines WHERE journal_entry_id = entry_id
                        GROUP BY account_id, currency
                    )
                    SELECT 1 FROM original_lines o FULL OUTER JOIN reversal_lines r
                      ON o.account_id = r.account_id AND o.currency = r.currency
                    WHERE coalesce(o.debit_sum, 0) <> coalesce(r.credit_sum, 0)
                       OR coalesce(o.credit_sum, 0) <> coalesce(r.debit_sum, 0)
                ) THEN
                    RAISE EXCEPTION 'reversal lines must invert the original entry'
                        USING ERRCODE = '23514';
                END IF;
            END IF;
            RETURN NULL;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER finguard_journal_entry_balance
        AFTER INSERT OR UPDATE ON journal_entries
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finguard_validate_posted_entry()
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER finguard_journal_line_balance
        AFTER INSERT OR UPDATE OR DELETE ON journal_lines
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finguard_validate_posted_entry()
        """
    )


def downgrade() -> None:
    # Lock before inspecting so a concurrent writer cannot introduce a new-only state
    # between the guard and the old status constraint being restored.
    op.execute("LOCK TABLE transactions IN ACCESS EXCLUSIVE MODE")
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM transactions
                WHERE status NOT IN ('pending', 'approved', 'blocked', 'failed', 'posted')
            ) THEN
                RAISE EXCEPTION
                    'FinGuard downgrade blocked: transaction state is absent from revision 0001'
                    USING ERRCODE = 'P0001';
            END IF;
        END;
        $$
        """
    )

    op.execute("DROP TRIGGER finguard_journal_line_balance ON journal_lines")
    op.execute("DROP TRIGGER finguard_journal_entry_balance ON journal_entries")
    op.execute("DROP TRIGGER finguard_journal_line_guard ON journal_lines")
    op.execute("DROP TRIGGER finguard_journal_entry_guard ON journal_entries")
    op.execute("DROP FUNCTION finguard_validate_posted_entry()")
    op.execute("DROP FUNCTION finguard_guard_journal_line()")
    op.execute("DROP FUNCTION finguard_guard_journal_entry()")

    op.drop_constraint("ck_journal_entries_not_self_reversal", "journal_entries", type_="check")
    op.drop_constraint("ck_transactions_transaction_status", "transactions", type_="check")
    op.create_check_constraint(
        "ck_transactions_transaction_status",
        "transactions",
        "status IN ('pending', 'approved', 'blocked', 'failed', 'posted')",
    )

    op.drop_constraint("fk_journal_lines_account_currency", "journal_lines", type_="foreignkey")
    op.create_foreign_key(
        "fk_journal_lines_account_id_accounts",
        "journal_lines",
        "accounts",
        ["account_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.drop_constraint(
        "fk_transactions_destination_account_currency", "transactions", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_transactions_source_account_currency", "transactions", type_="foreignkey"
    )
    op.create_foreign_key(
        "fk_transactions_source_account_id_accounts",
        "transactions",
        "accounts",
        ["source_account_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_transactions_destination_account_id_accounts",
        "transactions",
        "accounts",
        ["destination_account_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    for table in ("journal_lines", "transactions", "accounts"):
        op.drop_constraint(f"ck_{table}_supported_currency", table, type_="check")
    op.drop_constraint("uq_accounts_id_currency", "accounts", type_="unique")
