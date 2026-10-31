"""Validated inputs and API-facing feature values."""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Currency = Literal["USD", "EUR", "GBP", "INR"]


class FeatureEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: UUID
    occurred_at: datetime
    source_account_id: UUID
    destination_account_id: UUID
    amount_minor: int = Field(gt=0)
    currency: Currency

    @field_validator("occurred_at")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def require_distinct_accounts(self) -> "FeatureEvent":
        if self.source_account_id == self.destination_account_id:
            raise ValueError("source and destination accounts must differ")
        return self


class ProjectionRecord(BaseModel):
    """Account-local event detail persisted in the Redis projection hash."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: UUID
    occurred_at: datetime
    account_id: UUID
    counterparty_account_id: UUID
    amount_minor: int = Field(gt=0)
    currency: Currency
    direction: Literal["incoming", "outgoing"]


class AccountFeatures(BaseModel):
    account_id: UUID
    currency: Currency
    feature_schema_version: str
    as_of_timestamp: datetime
    values: dict[str, int | float]
    last_projection_update: datetime | None
    freshness_seconds: float | None
    freshness: Literal["fresh", "stale", "unknown"]
