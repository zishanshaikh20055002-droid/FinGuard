"""Atomic standalone-Redis projection adapter (Redis Cluster is unsupported)."""

import json
import re
from datetime import UTC, datetime, timedelta
from uuid import UUID

from app.features.definitions import EVENT_RETENTION, FEATURE_SCHEMA_VERSION
from app.features.schemas import FeatureEvent, ProjectionRecord

APPLY_SCRIPT = r"""
local expected = {'hash','zset','zset','hash','hash','hash','set','hash','hash'}
for i = 1, 9 do
  local actual = redis.call('TYPE', KEYS[i]).ok
  if actual ~= 'none' and actual ~= expected[i] then return -2 end
end
local counters = {
  'events_processed_total',
  'duplicate_events_total',
  'feature_update_errors_total'
}
for _, field in ipairs(counters) do
  local value = redis.call('HGET', KEYS[6], field)
  if value and not tonumber(value) then return -2 end
end
local existing = redis.call('HGET', KEYS[1], ARGV[1])
if existing then
  if existing ~= ARGV[2] then return -1 end
  redis.call('HINCRBY', KEYS[6], 'duplicate_events_total', 1)
  return 0
end
redis.call('ZADD', KEYS[2], ARGV[3], ARGV[1])
redis.call('HSET', KEYS[4], ARGV[1], ARGV[4])
redis.call('ZADD', KEYS[3], ARGV[3], ARGV[1])
redis.call('HSET', KEYS[5], ARGV[1], ARGV[5])
redis.call('SADD', KEYS[7], ARGV[6] .. ':' .. ARGV[7], ARGV[8] .. ':' .. ARGV[7])
local now = redis.call('TIME')
local updated_at = tonumber(now[1]) + tonumber(now[2]) / 1000000
redis.call('HSET', KEYS[8], 'last_projection_update', updated_at)
redis.call('HSET', KEYS[9], 'last_projection_update', updated_at)
redis.call('EXPIRE', KEYS[2], ARGV[10])
redis.call('EXPIRE', KEYS[3], ARGV[10])
redis.call('EXPIRE', KEYS[4], ARGV[10])
redis.call('EXPIRE', KEYS[5], ARGV[10])
redis.call('EXPIRE', KEYS[8], ARGV[10])
redis.call('EXPIRE', KEYS[9], ARGV[10])
local cutoff = tonumber(now[1]) + tonumber(now[2]) / 1000000 - tonumber(ARGV[9])
local old_source = redis.call('ZRANGEBYSCORE', KEYS[2], '-inf', '(' .. cutoff)
for _, id in ipairs(old_source) do
  redis.call('ZREM', KEYS[2], id)
  redis.call('HDEL', KEYS[4], id)
end
local old_dest = redis.call('ZRANGEBYSCORE', KEYS[3], '-inf', '(' .. cutoff)
for _, id in ipairs(old_dest) do
  redis.call('ZREM', KEYS[3], id)
  redis.call('HDEL', KEYS[5], id)
end
local previous = redis.call('HGET', KEYS[6], 'last_event_time')
if not previous or tonumber(ARGV[3]) > tonumber(previous) then
  redis.call('HSET', KEYS[6], 'last_event_time', ARGV[3])
end
redis.call('HSET', KEYS[6], 'last_projection_update', updated_at)
-- The dedup marker is the visibility/commit marker: readers ignore unmarked
-- rows and redelivery safely completes any partial write after a Redis error.
redis.call('HSET', KEYS[1], ARGV[1], ARGV[2])
return 1
"""

READ_EVENTS_SCRIPT = r"""
local ids = redis.call('ZRANGEBYSCORE', KEYS[1], ARGV[1], ARGV[2])
local result = {}
for _, id in ipairs(ids) do
  local marker = redis.call('HGET', KEYS[3], id)
  local row = marker and redis.call('HGET', KEYS[2], id)
  if row then table.insert(result, row) end
end
return result
"""

ACTIVATE_SCRIPT = r"""
if redis.call('HGET', KEYS[1], 'status') ~= 'ready' then return 0 end
redis.call('SET', KEYS[2], ARGV[1])
redis.call('SREM', KEYS[3], ARGV[1])
redis.call('HSET', KEYS[1], 'status', 'active')
return 1
"""


class RedisFeatureStore:
    def __init__(self, url: str, key_prefix: str = "finguard:features") -> None:
        try:
            import redis
        except ImportError as exc:
            raise RuntimeError("Install FinGuard's features extra to use Redis") from exc
        self.client = redis.Redis.from_url(
            url,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
            health_check_interval=30,
        )
        self.key_prefix = key_prefix.rstrip(":")
        self._apply = self.client.register_script(APPLY_SCRIPT)
        self._read_events = self.client.register_script(READ_EVENTS_SCRIPT)
        self._activate = self.client.register_script(ACTIVATE_SCRIPT)

    def _version(self, version: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", version):
            raise ValueError("invalid_projection_version")
        return f"{self.key_prefix}:{version}"

    def _account_keys(self, account_id: UUID, currency: str, version: str) -> tuple[str, str, str]:
        base = f"{self._version(version)}:account:{account_id}:{currency}"
        return f"{base}:events", f"{base}:data", f"{base}:meta"

    def apply_event(self, event: FeatureEvent, fingerprint: str, version: str = "v1") -> str:
        root = self._version(version)
        source_events, source_data, source_meta = self._account_keys(
            event.source_account_id, event.currency, version
        )
        destination_events, destination_data, destination_meta = self._account_keys(
            event.destination_account_id, event.currency, version
        )
        event_time = event.occurred_at.timestamp()
        source_data_json = ProjectionRecord(
            event_id=event.event_id,
            occurred_at=event.occurred_at,
            account_id=event.source_account_id,
            counterparty_account_id=event.destination_account_id,
            amount_minor=event.amount_minor,
            currency=event.currency,
            direction="outgoing",
        ).model_dump_json()
        destination_data_json = ProjectionRecord(
            event_id=event.event_id,
            occurred_at=event.occurred_at,
            account_id=event.destination_account_id,
            counterparty_account_id=event.source_account_id,
            amount_minor=event.amount_minor,
            currency=event.currency,
            direction="incoming",
        ).model_dump_json()
        try:
            result = self._apply(
                keys=[
                    f"{root}:dedup",
                    source_events,
                    destination_events,
                    source_data,
                    destination_data,
                    f"{root}:metrics",
                    f"{root}:accounts",
                    source_meta,
                    destination_meta,
                ],
                args=[
                    str(event.event_id),
                    fingerprint,
                    repr(event_time),
                    source_data_json,
                    destination_data_json,
                    str(event.source_account_id),
                    event.currency,
                    str(event.destination_account_id),
                    str(EVENT_RETENTION.total_seconds()),
                    str(int(EVENT_RETENTION.total_seconds())),
                ],
            )
        except Exception as exc:
            raise RuntimeError("feature_store_update_failed") from exc
        if result == -1:
            raise ValueError("event_id_payload_mismatch")
        if result == -2:
            raise RuntimeError("feature_store_key_type_mismatch")
        return "applied" if result == 1 else "duplicate"

    def get_events(
        self, account_id: UUID, currency: str, as_of: datetime, version: str = "v1"
    ) -> list[FeatureEvent]:
        event_key, data_key, _ = self._account_keys(account_id, currency, version)
        lower = (as_of - timedelta(days=7)).timestamp()
        rows = self._read_events(
            keys=[event_key, data_key, f"{self._version(version)}:dedup"],
            args=[repr(lower), f"({as_of.timestamp()}"],
        )
        records = [ProjectionRecord.model_validate_json(row) for row in rows]
        return [
            FeatureEvent(
                event_id=record.event_id,
                occurred_at=record.occurred_at,
                source_account_id=(
                    record.account_id
                    if record.direction == "outgoing"
                    else record.counterparty_account_id
                ),
                destination_account_id=(
                    record.counterparty_account_id
                    if record.direction == "outgoing"
                    else record.account_id
                ),
                amount_minor=record.amount_minor,
                currency=record.currency,
            )
            for record in records
        ]

    def get_metrics(self, version: str = "v1") -> dict:
        values = self.client.hgetall(f"{self._version(version)}:metrics")
        rebuild = self.client.hgetall(f"{self.key_prefix}:rebuild:{version}")
        return {
            "events_processed_total": int(self.client.hlen(f"{self._version(version)}:dedup")),
            "duplicate_events_total": int(values.get("duplicate_events_total", 0)),
            "feature_update_errors_total": int(values.get("feature_update_errors_total", 0)),
            "last_event_time": (
                datetime.fromtimestamp(float(values["last_event_time"]), tz=UTC)
                if values.get("last_event_time")
                else None
            ),
            "last_projection_update": (
                datetime.fromtimestamp(float(values["last_projection_update"]), tz=UTC)
                if values.get("last_projection_update")
                else None
            ),
            "rebuild_status": self.client.hget(f"{self.key_prefix}:rebuild:{version}", "status"),
            "rebuild_progress": int(rebuild.get("processed", 0)),
            "rebuild_last_event_time": rebuild.get("last_event_time"),
        }

    def store_features(
        self, account_id: UUID, currency: str, as_of: datetime, values: dict, version: str
    ) -> None:
        key = f"{self._version(version)}:account:{account_id}:{currency}:features"
        with self.client.pipeline(transaction=True) as pipe:
            pipe.hset(
                key,
                mapping={
                    "schema_version": FEATURE_SCHEMA_VERSION,
                    "as_of_timestamp": as_of.astimezone(UTC).isoformat(),
                    "values": json.dumps(values, sort_keys=True, separators=(",", ":")),
                    "computed_at": datetime.now(UTC).isoformat(),
                },
            )
            pipe.expire(key, int(EVENT_RETENTION.total_seconds()))
            pipe.execute()

    def ping(self) -> bool:
        return bool(self.client.ping())

    def record_error(self, version: str = "v1") -> None:
        self.client.hincrby(f"{self._version(version)}:metrics", "feature_update_errors_total", 1)

    def account_last_update(self, account_id: UUID, currency: str, version: str) -> datetime | None:
        _, _, meta = self._account_keys(account_id, currency, version)
        value = self.client.hget(meta, "last_projection_update")
        return datetime.fromtimestamp(float(value), tz=UTC) if value else None

    def active_version(self) -> str:
        return self.client.get(f"{self.key_prefix}:control:active") or "v1"

    def initialize_active(self, version: str) -> None:
        self._version(version)
        self.client.set(f"{self.key_prefix}:control:active", version, nx=True)

    def write_versions(self) -> list[str]:
        values = set(self.client.smembers(f"{self.key_prefix}:control:building"))
        values.add(self.active_version())
        return sorted(values)

    def list_accounts(self, version: str) -> list[tuple[UUID, str]]:
        result = []
        for value in self.client.smembers(f"{self._version(version)}:accounts"):
            account, currency = value.rsplit(":", 1)
            account_id = UUID(account)
            event_key, _, _ = self._account_keys(account_id, currency, version)
            if self.client.exists(event_key):
                result.append((account_id, currency))
        return sorted(result, key=lambda item: (str(item[0]), item[1]))

    def begin_rebuild(self, version: str) -> None:
        self._version(version)
        self.client.sadd(f"{self.key_prefix}:control:building", version)
        self.client.hset(
            f"{self.key_prefix}:rebuild:{version}", mapping={"status": "running", "processed": 0}
        )

    def update_rebuild(self, version: str, processed: int, last_event_time: str | None) -> None:
        values = {"status": "running", "processed": processed}
        if last_event_time:
            values["last_event_time"] = last_event_time
        self.client.hset(f"{self.key_prefix}:rebuild:{version}", mapping=values)

    def complete_rebuild(self, version: str) -> None:
        self.client.hset(f"{self.key_prefix}:rebuild:{version}", "status", "ready")

    def fail_rebuild(self, version: str, error_type: str) -> None:
        self.client.hset(
            f"{self.key_prefix}:rebuild:{version}",
            mapping={"status": "failed", "last_error_type": error_type[:80]},
        )

    def activate(self, version: str) -> bool:
        return bool(
            self._activate(
                keys=[
                    f"{self.key_prefix}:rebuild:{version}",
                    f"{self.key_prefix}:control:active",
                    f"{self.key_prefix}:control:building",
                ],
                args=[version],
            )
        )

    def close(self) -> None:
        self.client.close()
