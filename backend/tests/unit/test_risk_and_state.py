"""Risk thresholds, production guard, and terminal-state validation."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.core.config import Settings
from app.core.errors import DomainError
from app.risk.provider import DevelopmentRulesRiskProvider, get_risk_provider
from app.transactions.state import transition


def test_rules_provider_returns_all_three_decisions() -> None:
    provider = DevelopmentRulesRiskProvider(500, 1000)
    assert provider.assess(amount_minor=499, currency="INR").outcome == "allow"
    assert provider.assess(amount_minor=500, currency="INR").outcome == "challenge"
    assert provider.assess(amount_minor=1000, currency="INR").outcome == "block"


def test_production_has_no_demo_risk_provider() -> None:
    settings = Settings(
        _env_file=None,
        app_env="production",
        database_url="postgresql+psycopg://placeholder@localhost/test",
        jwt_secret="6zYp4F8wQ2eL9vX7nC1mR5uT3aB0dHkJ",
        allowed_origins="https://example.test",
    )
    with pytest.raises(DomainError, match="Risk assessment is unavailable"):
        get_risk_provider(settings)


@pytest.mark.parametrize("terminal", ["blocked", "failed", "posted"])
def test_terminal_transaction_cannot_transition(terminal: str) -> None:
    session = MagicMock()
    transaction = SimpleNamespace(status=terminal)
    with pytest.raises(DomainError, match="Transaction state cannot make that transition"):
        transition(session, transaction, "posted", "invalid")
    session.flush.assert_not_called()
