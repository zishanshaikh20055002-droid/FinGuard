"""Public request contracts; server-owned fields are forbidden on writes."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

Currency = Literal["USD", "EUR", "GBP", "INR"]


class AccountCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    currency: Currency


class TransactionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_account_id: UUID
    destination_account_id: UUID
    amount_minor: int = Field(gt=0, strict=True)
    currency: Currency
    memo: str | None = Field(default=None, max_length=255)

    @field_validator("memo")
    @classmethod
    def normalize_memo(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None
