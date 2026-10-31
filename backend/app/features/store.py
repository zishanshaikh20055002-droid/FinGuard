"""Feature store protocol shared by Redis and deterministic test adapters."""

from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.features.schemas import FeatureEvent


class FeatureStore(Protocol):
    def apply_event(self, event: FeatureEvent, fingerprint: str, version: str) -> str: ...

    def get_events(
        self, account_id: UUID, currency: str, as_of: datetime, version: str
    ) -> list[FeatureEvent]: ...

    def get_metrics(self, version: str) -> dict: ...

    def store_features(
        self, account_id: UUID, currency: str, as_of: datetime, values: dict, version: str
    ) -> None: ...
