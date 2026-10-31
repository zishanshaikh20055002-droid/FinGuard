"""Replaceable risk assessment contract and deterministic development rules."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, Protocol

from app.core.config import Settings
from app.core.errors import DomainError

RiskOutcome = Literal["allow", "challenge", "block"]


@dataclass(frozen=True)
class Assessment:
    outcome: RiskOutcome
    score: Decimal
    reason_codes: tuple[str, ...]
    provider_name: str
    provider_version: str


class RiskProvider(Protocol):
    def assess(self, *, amount_minor: int, currency: str) -> Assessment: ...


class DevelopmentRulesRiskProvider:
    def __init__(self, challenge_amount_minor: int, block_amount_minor: int) -> None:
        self.challenge_amount_minor = challenge_amount_minor
        self.block_amount_minor = block_amount_minor

    def assess(self, *, amount_minor: int, currency: str) -> Assessment:
        if amount_minor >= self.block_amount_minor:
            return Assessment(
                "block", Decimal("0.9500"), ("amount_block_threshold",), "dev_rules", "1"
            )
        if amount_minor >= self.challenge_amount_minor:
            return Assessment(
                "challenge", Decimal("0.7000"), ("amount_challenge_threshold",), "dev_rules", "1"
            )
        return Assessment(
            "allow", Decimal("0.1000"), ("amount_within_demo_limit",), "dev_rules", "1"
        )


def get_risk_provider(settings: Settings) -> RiskProvider:
    if settings.app_env != "development":
        raise DomainError("risk_provider_unavailable", "Risk assessment is unavailable", 503)
    return DevelopmentRulesRiskProvider(
        settings.demo_challenge_amount_minor,
        settings.demo_block_amount_minor,
    )
