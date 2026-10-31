"""Database invariant tests. These require a migrated PostgreSQL 16 test database."""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from time import sleep
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, insert, select, text, update
from sqlalchemy.exc import IntegrityError

import app.models  # noqa: F401
from app.db.base import Base

accounts = Base.metadata.tables["accounts"]
transactions = Base.metadata.tables["transactions"]
journal_entries = Base.metadata.tables["journal_entries"]
journal_lines = Base.metadata.tables["journal_lines"]
idempotency_keys = Base.metadata.tables["idempotency_keys"]

pytestmark = pytest.mark.integration


@contextmanager
def expect_sqlstate(expected: str) -> Iterator[None]:
    with pytest.raises(IntegrityError) as error:
        yield
    assert error.value.orig.sqlstate == expected


def add_account(connection, currency: str = "USD") -> UUID:
    account_id = uuid4()
    connection.execute(
        insert(accounts).values(
            id=account_id,
            reference=f"test-{account_id}",
            currency=currency,
            account_type="asset",
            status="open",
        )
    )
    return account_id


def add_transaction(connection, source: UUID, destination: UUID, currency: str = "USD") -> UUID:
    transaction_id = uuid4()
    connection.execute(
        insert(transactions).values(
            id=transaction_id,
            source_account_id=source,
            destination_account_id=destination,
            amount_minor=100,
            currency=currency,
            status="approved",
        )
    )
    return transaction_id


def seed_transaction(engine: Engine) -> tuple[UUID, UUID, UUID]:
    with engine.begin() as connection:
        source = add_account(connection)
        destination = add_account(connection)
        transaction_id = add_transaction(connection, source, destination)
    return source, destination, transaction_id


def add_entry(connection, transaction_id: UUID, *, reversal_of: UUID | None = None) -> UUID:
    entry_id = uuid4()
    connection.execute(
        insert(journal_entries).values(
            id=entry_id,
            transaction_id=transaction_id,
            entry_type="reversal" if reversal_of else "standard",
            status="draft",
            reverses_entry_id=reversal_of,
        )
    )
    return entry_id


def add_line(
    connection,
    entry_id: UUID,
    account_id: UUID,
    *,
    currency: str = "USD",
    debit: int = 0,
    credit: int = 0,
) -> UUID:
    line_id = uuid4()
    connection.execute(
        insert(journal_lines).values(
            id=line_id,
            journal_entry_id=entry_id,
            account_id=account_id,
            currency=currency,
            debit_minor=debit,
            credit_minor=credit,
        )
    )
    return line_id


def mark_posted(connection, entry_id: UUID) -> None:
    connection.execute(
        update(journal_entries)
        .where(journal_entries.c.id == entry_id)
        .values(status="posted", posted_at=text("now()"))
    )


def post_standard_entry(engine: Engine) -> tuple[UUID, UUID, UUID, UUID, UUID, UUID]:
    source, destination, transaction_id = seed_transaction(engine)
    with engine.begin() as connection:
        entry_id = add_entry(connection, transaction_id)
        debit_line = add_line(connection, entry_id, source, debit=100)
        credit_line = add_line(connection, entry_id, destination, credit=100)
        mark_posted(connection, entry_id)
    return source, destination, transaction_id, entry_id, debit_line, credit_line


def test_valid_account_and_balanced_entry(pg_engine: Engine) -> None:
    source, destination, _, entry_id, _, _ = post_standard_entry(pg_engine)
    with pg_engine.connect() as connection:
        assert (
            connection.scalar(select(accounts.c.currency).where(accounts.c.id == source)) == "USD"
        )
        assert (
            connection.scalar(select(accounts.c.currency).where(accounts.c.id == destination))
            == "USD"
        )
        assert (
            connection.scalar(
                select(journal_entries.c.status).where(journal_entries.c.id == entry_id)
            )
            == "posted"
        )
        assert (
            len(
                connection.execute(
                    select(journal_lines).where(journal_lines.c.journal_entry_id == entry_id)
                ).all()
            )
            == 2
        )


@pytest.mark.parametrize("currency", ["XYZ", "US1", "usd"])
def test_unsupported_account_currency_rejected(pg_engine: Engine, currency: str) -> None:
    with expect_sqlstate("23514"), pg_engine.begin() as connection:
        add_account(connection, currency)


@pytest.mark.parametrize("mismatched_side", ["source", "destination"])
def test_transaction_currency_must_match_both_accounts(
    pg_engine: Engine, mismatched_side: str
) -> None:
    with pg_engine.begin() as connection:
        source = add_account(connection, "EUR" if mismatched_side == "source" else "USD")
        destination = add_account(connection, "EUR" if mismatched_side == "destination" else "USD")
    with expect_sqlstate("23503"), pg_engine.begin() as connection:
        add_transaction(connection, source, destination)


def test_transaction_accounts_must_differ_and_amount_positive(pg_engine: Engine) -> None:
    with pg_engine.begin() as connection:
        account_id = add_account(connection)
    with expect_sqlstate("23514"), pg_engine.begin() as connection:
        add_transaction(connection, account_id, account_id)
    with pg_engine.begin() as connection:
        other = add_account(connection)
    with expect_sqlstate("23514"), pg_engine.begin() as connection:
        connection.execute(
            insert(transactions).values(
                id=uuid4(),
                source_account_id=account_id,
                destination_account_id=other,
                amount_minor=0,
                currency="USD",
                status="received",
            )
        )


def test_line_currency_must_match_account(pg_engine: Engine) -> None:
    source, _, transaction_id = seed_transaction(pg_engine)
    with pg_engine.begin() as connection:
        entry_id = add_entry(connection, transaction_id)
    with expect_sqlstate("23503"), pg_engine.begin() as connection:
        add_line(connection, entry_id, source, currency="EUR", debit=100)


def test_nonexistent_account_rejected(pg_engine: Engine) -> None:
    _, _, transaction_id = seed_transaction(pg_engine)
    with pg_engine.begin() as connection:
        entry_id = add_entry(connection, transaction_id)
    with expect_sqlstate("23503"), pg_engine.begin() as connection:
        add_line(connection, entry_id, uuid4(), debit=100)


@pytest.mark.parametrize("debit,credit", [(-1, 0), (0, -1), (1, 1), (0, 0)])
def test_line_amount_checks(pg_engine: Engine, debit: int, credit: int) -> None:
    source, _, transaction_id = seed_transaction(pg_engine)
    with pg_engine.begin() as connection:
        entry_id = add_entry(connection, transaction_id)
    with expect_sqlstate("23514"), pg_engine.begin() as connection:
        add_line(connection, entry_id, source, debit=debit, credit=credit)


def test_empty_posted_entry_fails_at_commit_without_partial_entry(pg_engine: Engine) -> None:
    _, _, transaction_id = seed_transaction(pg_engine)
    entry_id = uuid4()
    with pg_engine.connect() as connection:
        transaction = connection.begin()
        connection.execute(
            insert(journal_entries).values(
                id=entry_id,
                transaction_id=transaction_id,
                entry_type="standard",
                status="posted",
                posted_at=text("now()"),
            )
        )
        with expect_sqlstate("23514"):
            transaction.commit()
    with pg_engine.connect() as connection:
        assert (
            connection.scalar(select(journal_entries.c.id).where(journal_entries.c.id == entry_id))
            is None
        )


def test_unbalanced_entry_fails_at_commit_without_partial_lines(pg_engine: Engine) -> None:
    source, destination, transaction_id = seed_transaction(pg_engine)
    entry_id = uuid4()
    with pg_engine.connect() as connection:
        transaction = connection.begin()
        connection.execute(
            insert(journal_entries).values(
                id=entry_id,
                transaction_id=transaction_id,
                entry_type="standard",
                status="draft",
            )
        )
        add_line(connection, entry_id, source, debit=100)
        add_line(connection, entry_id, destination, credit=99)
        mark_posted(connection, entry_id)  # Deferred trigger has not rejected it yet.
        with expect_sqlstate("23514"):
            transaction.commit()
    with pg_engine.connect() as connection:
        assert (
            connection.scalar(select(journal_entries.c.id).where(journal_entries.c.id == entry_id))
            is None
        )
        assert (
            connection.scalar(
                select(journal_lines.c.id).where(journal_lines.c.journal_entry_id == entry_id)
            )
            is None
        )


def test_posted_lines_must_use_transaction_currency(pg_engine: Engine) -> None:
    _, _, transaction_id = seed_transaction(pg_engine)
    with pg_engine.begin() as connection:
        euro_source = add_account(connection, "EUR")
        euro_destination = add_account(connection, "EUR")
    with pg_engine.connect() as connection:
        transaction = connection.begin()
        entry_id = add_entry(connection, transaction_id)
        add_line(connection, entry_id, euro_source, currency="EUR", debit=100)
        add_line(connection, entry_id, euro_destination, currency="EUR", credit=100)
        mark_posted(connection, entry_id)
        with expect_sqlstate("23514"):
            transaction.commit()


def test_mixed_currency_posting_is_rejected(pg_engine: Engine) -> None:
    source, destination, transaction_id = seed_transaction(pg_engine)
    with pg_engine.begin() as connection:
        euro_source = add_account(connection, "EUR")
        euro_destination = add_account(connection, "EUR")
    with pg_engine.connect() as connection:
        transaction = connection.begin()
        entry_id = add_entry(connection, transaction_id)
        add_line(connection, entry_id, source, debit=100)
        add_line(connection, entry_id, destination, credit=100)
        add_line(connection, entry_id, euro_source, currency="EUR", debit=50)
        add_line(connection, entry_id, euro_destination, currency="EUR", credit=50)
        mark_posted(connection, entry_id)
        with expect_sqlstate("23514"):
            transaction.commit()


def test_failed_posting_rolls_back_transaction_and_journal(pg_engine: Engine) -> None:
    with pg_engine.begin() as connection:
        source = add_account(connection)
        destination = add_account(connection)
    transaction_id = uuid4()
    entry_id = uuid4()
    with pg_engine.connect() as connection:
        transaction = connection.begin()
        connection.execute(
            insert(transactions).values(
                id=transaction_id,
                source_account_id=source,
                destination_account_id=destination,
                amount_minor=100,
                currency="USD",
                status="approved",
            )
        )
        connection.execute(
            insert(journal_entries).values(
                id=entry_id,
                transaction_id=transaction_id,
                entry_type="standard",
                status="draft",
            )
        )
        add_line(connection, entry_id, source, debit=100)
        add_line(connection, entry_id, destination, credit=99)
        mark_posted(connection, entry_id)
        with expect_sqlstate("23514"):
            transaction.commit()
    with pg_engine.connect() as connection:
        assert (
            connection.scalar(select(transactions.c.id).where(transactions.c.id == transaction_id))
            is None
        )
        assert (
            connection.scalar(select(journal_entries.c.id).where(journal_entries.c.id == entry_id))
            is None
        )
        assert (
            connection.scalar(
                select(journal_lines.c.id).where(journal_lines.c.journal_entry_id == entry_id)
            )
            is None
        )


@pytest.mark.parametrize(
    "target,operation",
    [("entry", "update"), ("entry", "delete"), ("line", "update"), ("line", "delete")],
)
def test_posted_record_is_immutable(pg_engine: Engine, target: str, operation: str) -> None:
    _, _, _, entry_id, line_id, _ = post_standard_entry(pg_engine)
    if target == "entry" and operation == "update":
        sql = "UPDATE journal_entries SET entry_type = 'reversal' WHERE id = CAST(:id AS uuid)"
        record_id = entry_id
    elif target == "entry":
        sql = "DELETE FROM journal_entries WHERE id = CAST(:id AS uuid)"
        record_id = entry_id
    elif operation == "update":
        sql = "UPDATE journal_lines SET debit_minor = 101 WHERE id = CAST(:id AS uuid)"
        record_id = line_id
    else:
        sql = "DELETE FROM journal_lines WHERE id = CAST(:id AS uuid)"
        record_id = line_id
    with expect_sqlstate("23514"), pg_engine.begin() as connection:
        connection.execute(text(sql), {"id": str(record_id)})


def test_posted_entry_rejects_new_line(pg_engine: Engine) -> None:
    source, _, _, entry_id, _, _ = post_standard_entry(pg_engine)
    with expect_sqlstate("23514"), pg_engine.begin() as connection:
        add_line(connection, entry_id, source, debit=1)


def test_reversal_entry_mirrors_original(pg_engine: Engine) -> None:
    source, destination, transaction_id, original_id, _, _ = post_standard_entry(pg_engine)
    with pg_engine.begin() as connection:
        reversal_id = add_entry(connection, transaction_id, reversal_of=original_id)
        add_line(connection, reversal_id, source, credit=100)
        add_line(connection, reversal_id, destination, debit=100)
        mark_posted(connection, reversal_id)
    with pg_engine.connect() as connection:
        assert (
            connection.scalar(
                select(journal_entries.c.status).where(journal_entries.c.id == reversal_id)
            )
            == "posted"
        )
        assert (
            connection.scalar(
                select(journal_entries.c.status).where(journal_entries.c.id == original_id)
            )
            == "posted"
        )


def test_false_reversal_fails_at_commit(pg_engine: Engine) -> None:
    source, destination, transaction_id, original_id, _, _ = post_standard_entry(pg_engine)
    with pg_engine.connect() as connection:
        transaction = connection.begin()
        reversal_id = add_entry(connection, transaction_id, reversal_of=original_id)
        add_line(connection, reversal_id, source, debit=100)
        add_line(connection, reversal_id, destination, credit=100)
        mark_posted(connection, reversal_id)
        with expect_sqlstate("23514"):
            transaction.commit()


def test_idempotency_key_is_unique_within_operation(pg_engine: Engine) -> None:
    key = f"test-{uuid4()}"
    with pg_engine.begin() as connection:
        connection.execute(
            insert(idempotency_keys).values(
                id=uuid4(), operation="submit_transaction", key=key, request_fingerprint="a" * 64
            )
        )
    with expect_sqlstate("23505"), pg_engine.begin() as connection:
        connection.execute(
            insert(idempotency_keys).values(
                id=uuid4(), operation="submit_transaction", key=key, request_fingerprint="b" * 64
            )
        )


def test_concurrent_idempotency_insert_has_one_winner(pg_engine: Engine) -> None:
    key = f"test-{uuid4()}"
    first = dict(id=uuid4(), operation="submit_transaction", key=key, request_fingerprint="a" * 64)
    second = dict(id=uuid4(), operation="submit_transaction", key=key, request_fingerprint="a" * 64)
    with pg_engine.connect() as first_connection, ThreadPoolExecutor(max_workers=1) as pool:
        transaction = first_connection.begin()
        first_connection.execute(insert(idempotency_keys).values(**first))

        def insert_second() -> bool:
            try:
                with pg_engine.begin() as connection:
                    connection.execute(text("SET LOCAL lock_timeout = '5s'"))
                    connection.execute(insert(idempotency_keys).values(**second))
            except IntegrityError as error:
                assert error.orig.sqlstate == "23505"
                return False
            return True

        result = pool.submit(insert_second)
        sleep(0.2)
        transaction.commit()
        assert result.result(timeout=6) is False
    with pg_engine.connect() as connection:
        assert (
            connection.scalar(
                select(text("count(*)"))
                .select_from(idempotency_keys)
                .where(idempotency_keys.c.key == key)
            )
            == 1
        )
