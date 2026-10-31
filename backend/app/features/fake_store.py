"""Thread-safe in-memory feature store for deterministic unit tests."""

import threading
from datetime import UTC, datetime
from uuid import UUID

from app.features.schemas import FeatureEvent


class FakeFeatureStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._events: dict[str, dict[UUID, tuple[str, FeatureEvent]]] = {}
        self._metrics: dict[str, dict] = {}
        self._account_updates: dict[tuple[str, UUID, str], datetime] = {}
        self._snapshots: dict[tuple[str, UUID, str], dict] = {}
        self._active = "v1"
        self._building: set[str] = set()
        self._rebuild: dict[str, dict] = {}

    def ping(self) -> bool:
        return True

    def apply_event(self, event: FeatureEvent, fingerprint: str, version: str = "v1") -> str:
        with self._lock:
            events = self._events.setdefault(version, {})
            metrics = self._metrics.setdefault(
                version,
                {
                    "events_processed_total": 0,
                    "duplicate_events_total": 0,
                    "feature_update_errors_total": 0,
                    "last_event_time": None,
                    "last_projection_update": None,
                },
            )
            previous = events.get(event.event_id)
            if previous:
                if previous[0] != fingerprint:
                    raise ValueError("event_id_payload_mismatch")
                metrics["duplicate_events_total"] += 1
                return "duplicate"
            events[event.event_id] = (fingerprint, event)
            metrics["events_processed_total"] += 1
            metrics["last_event_time"] = max(
                metrics["last_event_time"] or event.occurred_at, event.occurred_at
            )
            metrics["last_projection_update"] = datetime.now(UTC)
            for account_id in (event.source_account_id, event.destination_account_id):
                self._account_updates[(version, account_id, event.currency)] = datetime.now(UTC)
            return "applied"

    def get_events(
        self, account_id: UUID, currency: str, as_of: datetime, version: str = "v1"
    ) -> list[FeatureEvent]:
        with self._lock:
            return [
                event
                for _, event in self._events.get(version, {}).values()
                if event.currency == currency
                and account_id in (event.source_account_id, event.destination_account_id)
                and event.occurred_at < as_of
            ]

    def get_metrics(self, version: str = "v1") -> dict:
        with self._lock:
            values = dict(
                self._metrics.get(
                    version,
                    {
                        "events_processed_total": 0,
                        "duplicate_events_total": 0,
                        "feature_update_errors_total": 0,
                        "last_event_time": None,
                        "last_projection_update": None,
                    },
                )
            )
            values["rebuild_status"] = self._rebuild.get(version, {}).get("status")
            values["rebuild_progress"] = self._rebuild.get(version, {}).get("processed", 0)
            return values

    def record_error(self, version: str = "v1") -> None:
        with self._lock:
            self._metrics.setdefault(
                version,
                {
                    "events_processed_total": 0,
                    "duplicate_events_total": 0,
                    "feature_update_errors_total": 0,
                    "last_event_time": None,
                    "last_projection_update": None,
                },
            )["feature_update_errors_total"] += 1

    def active_version(self) -> str:
        return self._active

    def initialize_active(self, version: str) -> None:
        with self._lock:
            if self._active == "v1" and not self._events.get("v1"):
                self._active = version

    def write_versions(self) -> list[str]:
        with self._lock:
            return sorted({self._active, *self._building})

    def list_accounts(self, version: str) -> list[tuple[UUID, str]]:
        with self._lock:
            return sorted(
                {
                    (account, currency)
                    for (v, account, currency) in self._account_updates
                    if v == version
                },
                key=lambda item: (str(item[0]), item[1]),
            )

    def account_last_update(self, account_id: UUID, currency: str, version: str) -> datetime | None:
        with self._lock:
            return self._account_updates.get((version, account_id, currency))

    def store_features(
        self, account_id: UUID, currency: str, as_of: datetime, values: dict, version: str
    ) -> None:
        with self._lock:
            self._snapshots[(version, account_id, currency)] = {
                "as_of_timestamp": as_of,
                "values": dict(values),
            }

    def begin_rebuild(self, version: str) -> None:
        with self._lock:
            self._building.add(version)
            self._rebuild[version] = {"status": "running", "processed": 0}

    def update_rebuild(self, version: str, processed: int, last_event_time: str | None) -> None:
        with self._lock:
            self._rebuild[version] = {
                "status": "running",
                "processed": processed,
                "last_event_time": last_event_time,
            }

    def complete_rebuild(self, version: str) -> None:
        with self._lock:
            self._rebuild.setdefault(version, {})["status"] = "ready"

    def fail_rebuild(self, version: str, error_type: str) -> None:
        with self._lock:
            self._rebuild.setdefault(version, {}).update(
                {"status": "failed", "last_error_type": error_type[:80]}
            )

    def activate(self, version: str) -> bool:
        with self._lock:
            if self._rebuild.get(version, {}).get("status") != "ready":
                return False
            self._active = version
            self._building.discard(version)
            self._rebuild[version]["status"] = "active"
            return True
