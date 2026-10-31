"""The development seed cannot be invoked under production settings."""

from unittest.mock import MagicMock

import pytest
from backend.scripts.seed_demo import seed

from app.core.config import Settings


def test_production_seed_refuses_before_database_access() -> None:
    settings = Settings(
        _env_file=None,
        app_env="production",
        database_url="postgresql+psycopg://placeholder@localhost/test",
        jwt_secret="6zYp4F8wQ2eL9vX7nC1mR5uT3aB0dHkJ",
        allowed_origins="https://example.test",
    )
    session = MagicMock()
    with pytest.raises(RuntimeError, match="disabled outside development"):
        seed(session, settings)
    session.begin.assert_not_called()
