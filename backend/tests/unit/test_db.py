"""Metadata and lazy database probe tests."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy.exc import OperationalError

import app.db.session as session_module
import app.models  # noqa: F401
from app.db.base import Base


def test_all_foundational_tables_are_registered() -> None:
    assert set(Base.metadata.tables) == {
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
    assert "balance" not in Base.metadata.tables["accounts"].columns


def test_database_probe_without_url_does_not_create_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session_module, "get_settings", lambda: SimpleNamespace(database_url=None))
    engine = MagicMock(side_effect=AssertionError("engine should remain lazy"))
    monkeypatch.setattr(session_module, "get_engine", engine)
    assert session_module.check_database_connection() is False
    engine.assert_not_called()


def test_database_probe_executes_select_one(monkeypatch: pytest.MonkeyPatch) -> None:
    url = "postgresql+psycopg://local:local@localhost/test"
    monkeypatch.setattr(session_module, "get_settings", lambda: SimpleNamespace(database_url=url))
    engine = MagicMock()
    monkeypatch.setattr(session_module, "get_engine", lambda _: engine)
    assert session_module.check_database_connection() is True
    engine.connect.return_value.__enter__.return_value.execute.assert_called_once()


def test_database_probe_handles_connection_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    url = "postgresql+psycopg://local:local@localhost/test"
    monkeypatch.setattr(session_module, "get_settings", lambda: SimpleNamespace(database_url=url))
    engine = MagicMock()
    engine.connect.side_effect = OperationalError("SELECT 1", {}, Exception("unavailable"))
    monkeypatch.setattr(session_module, "get_engine", lambda _: engine)
    assert session_module.check_database_connection() is False
