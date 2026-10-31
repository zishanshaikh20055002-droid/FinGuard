"""Destructive migration cycle only on an empty, dedicated PostgreSQL 16 test database."""

import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from app.core.config import get_settings

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[3]
EXPECTED_TABLES = {
    "accounts",
    "transactions",
    "idempotency_keys",
    "risk_decisions",
    "journal_entries",
    "journal_lines",
    "outbox_events",
    "transaction_status_history",
    "users",
    "auth_sessions",
    "auth_refresh_tokens",
    "auth_audit_events",
    "event_inbox",
    "event_replay_requests",
}


def _alembic_config() -> Config:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "backend" / "migrations"))
    return config


def _revision(engine) -> str | None:
    with engine.connect() as connection:
        return connection.scalar(text("SELECT version_num FROM alembic_version"))


def _tables(engine) -> set[str]:
    with engine.connect() as connection:
        return set(
            connection.scalars(
                text("SELECT tablename FROM pg_tables WHERE schemaname = current_schema()")
            )
        )


def _run_migration(config: Config, direction: str, target: str) -> None:
    get_settings.cache_clear()
    if direction == "upgrade":
        command.upgrade(config, target)
    else:
        command.downgrade(config, target)


def test_downgrade_guard_preserves_new_states_and_allows_clean_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = os.getenv("FINGUARD_MIGRATION_TEST_DATABASE_URL")
    if not url:
        pytest.skip("FINGUARD_MIGRATION_TEST_DATABASE_URL is not configured")
    parsed = make_url(url)
    if parsed.drivername != "postgresql+psycopg" or parsed.database != "finguard_migration_test":
        pytest.fail("Use only the dedicated finguard_migration_test PostgreSQL database")
    monkeypatch.setenv("DATABASE_URL", url)
    engine = create_engine(url, hide_parameters=True, connect_args={"connect_timeout": 3})
    config = _alembic_config()
    try:
        with engine.connect() as connection:
            version = int(connection.scalar(text("SHOW server_version_num")))
            if not 160000 <= version < 170000:
                pytest.fail("Migration test requires PostgreSQL 16")
            owner, current_user, superuser = connection.execute(
                text(
                    "SELECT pg_get_userbyid(d.datdba), current_user, "
                    "(SELECT rolsuper FROM pg_roles WHERE rolname = current_user) "
                    "FROM pg_database d WHERE d.datname = current_database()"
                )
            ).one()
            if owner != current_user or superuser:
                pytest.fail("Migration test role must own the database and must not be a superuser")

        existing = _tables(engine)
        if existing - (EXPECTED_TABLES | {"alembic_version"}):
            pytest.fail("Migration test database contains unrelated tables")
        for table in existing & EXPECTED_TABLES:
            with engine.connect() as connection:
                if connection.scalar(text(f'SELECT count(*) FROM "{table}"')):
                    pytest.fail("Migration test database must have no application rows")

        engine.dispose()
        _run_migration(config, "upgrade", "head")
        assert _revision(engine) == "be2905957c4e"
        assert EXPECTED_TABLES <= _tables(engine)

        auth_user_id = uuid4()
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO users (id, email, password_hash, role, status) "
                    "VALUES (CAST(:id AS uuid), :email, "
                    "'migration-test-hash', 'customer', 'active')"
                ),
                {"id": str(auth_user_id), "email": f"migration-{auth_user_id}@example.test"},
            )
        try:
            with pytest.raises(DBAPIError) as auth_error:
                _run_migration(config, "downgrade", "20261031_0003")
            assert auth_error.value.orig.sqlstate == "P0001"
            assert _revision(engine) == "be2905957c4e"
        finally:
            with engine.begin() as connection:
                connection.execute(
                    text("DELETE FROM users WHERE id = CAST(:id AS uuid)"),
                    {"id": str(auth_user_id)},
                )

        source, destination, transaction_id = uuid4(), uuid4(), uuid4()
        with engine.begin() as connection:
            for account_id in (source, destination):
                connection.execute(
                    text(
                        "INSERT INTO accounts (id, reference, currency, account_type, status) "
                        "VALUES (CAST(:id AS uuid), :reference, 'USD', 'asset', 'open')"
                    ),
                    {"id": str(account_id), "reference": f"migration-test-{account_id}"},
                )
            connection.execute(
                text(
                    "INSERT INTO transactions "
                    "(id, source_account_id, destination_account_id, "
                    "amount_minor, currency, status) "
                    "VALUES (CAST(:id AS uuid), CAST(:source AS uuid), "
                    "CAST(:destination AS uuid), 100, 'USD', 'received')"
                ),
                {"id": str(transaction_id), "source": str(source), "destination": str(destination)},
            )

        try:
            engine.dispose()
            with pytest.raises(DBAPIError) as error:
                _run_migration(config, "downgrade", "20261031_0001")
            assert error.value.orig.sqlstate == "P0001"
            assert _revision(engine) == "be2905957c4e"
            with engine.connect() as connection:
                assert (
                    connection.scalar(
                        text("SELECT status FROM transactions WHERE id = CAST(:id AS uuid)"),
                        {"id": str(transaction_id)},
                    )
                    == "received"
                )
        finally:
            with engine.begin() as connection:
                connection.execute(
                    text("DELETE FROM transactions WHERE id = CAST(:id AS uuid)"),
                    {"id": str(transaction_id)},
                )
                for account_id in (source, destination):
                    connection.execute(
                        text("DELETE FROM accounts WHERE id = CAST(:id AS uuid)"),
                        {"id": str(account_id)},
                    )

        compatible_source, compatible_destination, compatible_id = uuid4(), uuid4(), uuid4()
        with engine.begin() as connection:
            for account_id in (compatible_source, compatible_destination):
                connection.execute(
                    text(
                        "INSERT INTO accounts (id, reference, currency, account_type, status) "
                        "VALUES (CAST(:id AS uuid), :reference, 'USD', 'asset', 'open')"
                    ),
                    {"id": str(account_id), "reference": f"migration-test-{account_id}"},
                )
            connection.execute(
                text(
                    "INSERT INTO transactions "
                    "(id, source_account_id, destination_account_id, "
                    "amount_minor, currency, status) "
                    "VALUES (CAST(:id AS uuid), CAST(:source AS uuid), "
                    "CAST(:destination AS uuid), 100, 'USD', 'approved')"
                ),
                {
                    "id": str(compatible_id),
                    "source": str(compatible_source),
                    "destination": str(compatible_destination),
                },
            )
        engine.dispose()
        _run_migration(config, "downgrade", "20261031_0001")
        assert _revision(engine) == "20261031_0001"
        with engine.begin() as connection:
            assert (
                connection.scalar(
                    text("SELECT status FROM transactions WHERE id = CAST(:id AS uuid)"),
                    {"id": str(compatible_id)},
                )
                == "approved"
            )
            connection.execute(
                text("DELETE FROM transactions WHERE id = CAST(:id AS uuid)"),
                {"id": str(compatible_id)},
            )
            for account_id in (compatible_source, compatible_destination):
                connection.execute(
                    text("DELETE FROM accounts WHERE id = CAST(:id AS uuid)"),
                    {"id": str(account_id)},
                )

        engine.dispose()
        _run_migration(config, "downgrade", "base")
        assert not (EXPECTED_TABLES & _tables(engine))
        engine.dispose()
        _run_migration(config, "upgrade", "head")
        assert _revision(engine) == "be2905957c4e"
    finally:
        engine.dispose()
        get_settings.cache_clear()
