"""Settings should stay safe without a local environment file."""

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_development_settings_do_not_require_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    settings = Settings(_env_file=None)
    assert settings.app_env == "development"
    assert settings.database_url is None


def test_production_requires_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValidationError, match="DATABASE_URL is required"):
        Settings(_env_file=None, app_env="production")


def test_database_url_requires_psycopg_driver() -> None:
    with pytest.raises(ValidationError, match=r"postgresql\+psycopg"):
        Settings(_env_file=None, database_url="sqlite:///bad.db")


def test_event_and_redis_settings_use_documented_environment_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FINGUARD_KAFKA_BOOTSTRAP_SERVERS", "broker.example.test:9092")
    monkeypatch.setenv("FINGUARD_KAFKA_CLIENT_ID", "finguard-test")
    monkeypatch.setenv("FINGUARD_REDIS_URL", "redis://127.0.0.1:6379/0")
    monkeypatch.setenv("FINGUARD_FEATURE_FRESHNESS_SECONDS", "45")
    settings = Settings(_env_file=None)
    assert settings.kafka_bootstrap_servers == "broker.example.test:9092"
    assert settings.kafka_client_id == "finguard-test"
    assert settings.redis_url == "redis://127.0.0.1:6379/0"
    assert settings.feature_freshness_seconds == 45
