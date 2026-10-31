"""Opt-in tests against a dedicated PostgreSQL 16 FinGuard test database."""

import os
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError


@pytest.fixture(scope="session")
def pg_engine() -> Iterator[Engine]:
    url = os.getenv("FINGUARD_TEST_DATABASE_URL")
    if not url:
        pytest.skip("FINGUARD_TEST_DATABASE_URL is not configured")
    parsed = make_url(url)
    if parsed.drivername != "postgresql+psycopg" or not (parsed.database or "").startswith(
        "finguard_test"
    ):
        pytest.fail("Use a dedicated finguard_test* PostgreSQL database with psycopg")
    engine = create_engine(url, hide_parameters=True, connect_args={"connect_timeout": 3})
    try:
        try:
            with engine.connect() as connection:
                version = int(connection.scalar(text("SHOW server_version_num")))
                if not 160000 <= version < 170000:
                    pytest.fail("Integration tests require PostgreSQL 16")
                revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
                if revision != "be2905957c4e":
                    pytest.fail("Apply Alembic migrations through revision be2905957c4e first")
        except SQLAlchemyError:
            pytest.fail("Cannot connect to a migrated PostgreSQL 16 test database", pytrace=False)
        yield engine
    finally:
        engine.dispose()
