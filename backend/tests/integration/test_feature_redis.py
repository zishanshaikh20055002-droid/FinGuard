"""Optional tests against a configured real Redis server; never use FakeFeatureStore here."""

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

pytestmark = pytest.mark.redis_integration


@pytest.fixture
def redis_store():
    url = os.getenv("FINGUARD_REDIS_URL")
    if not url:
        pytest.skip("FINGUARD_REDIS_URL is not configured; live Redis integration not run")
    pytest.importorskip("redis")
    from app.features.redis_store import RedisFeatureStore

    prefix = f"finguard:features:test:{uuid4()}"
    store = RedisFeatureStore(url, key_prefix=prefix)
    try:
        store.ping()
    except Exception as exc:
        pytest.skip(f"Configured Redis endpoint is unavailable ({type(exc).__name__})")
    yield store
    for version in ("v1", "v2"):
        for suffix in ("dedup", "metrics", "accounts"):
            store.client.expire(f"{prefix}:{version}:{suffix}", 60)
        store.client.expire(f"{prefix}:rebuild:{version}", 60)
    store.client.expire(f"{prefix}:control:active", 60)
    store.client.expire(f"{prefix}:control:building", 60)
    store.close()


def test_real_redis_atomic_dedup_two_account_update_and_version_isolation(redis_store):
    from app.features.schemas import FeatureEvent

    source, destination = uuid4(), uuid4()
    event = FeatureEvent(
        event_id=uuid4(),
        occurred_at=datetime.now(UTC) - timedelta(seconds=2),
        source_account_id=source,
        destination_account_id=destination,
        amount_minor=777,
        currency="INR",
    )
    assert redis_store.apply_event(event, "fingerprint-1", "v1") == "applied"
    assert redis_store.apply_event(event, "fingerprint-1", "v1") == "duplicate"
    with pytest.raises(ValueError, match="payload_mismatch"):
        redis_store.apply_event(event, "fingerprint-2", "v1")
    assert redis_store.get_events(source, "INR", datetime.now(UTC), "v1") == [event]
    assert redis_store.get_events(destination, "INR", datetime.now(UTC), "v1") == [event]
    assert redis_store.get_events(source, "USD", datetime.now(UTC), "v1") == []
    assert redis_store.get_events(source, "INR", datetime.now(UTC), "v2") == []
    assert redis_store.get_metrics("v1")["events_processed_total"] == 1
    assert redis_store.get_metrics("v1")["duplicate_events_total"] == 1
    from app.features.calculator import calculate_account_features

    as_of = datetime.now(UTC)
    values = calculate_account_features(source, "INR", as_of, [event])
    redis_store.store_features(source, "INR", as_of, values, "v1")
    feature_key = f"{redis_store.key_prefix}:v1:account:{source}:INR:features"
    assert redis_store.client.hget(feature_key, "schema_version") == "fraud-features-v1"
    assert redis_store.client.hget(feature_key, "values")


def test_live_redis_concurrent_projection_and_preflight_failure_are_atomic(redis_store):
    from app.features.schemas import FeatureEvent

    source, destination = uuid4(), uuid4()
    events = [
        FeatureEvent(
            event_id=uuid4(),
            occurred_at=datetime.now(UTC) - timedelta(seconds=20 - index),
            source_account_id=source,
            destination_account_id=destination,
            amount_minor=100 + index,
            currency="INR",
        )
        for index in range(16)
    ]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(
                lambda event: redis_store.apply_event(event, f"fp-{event.event_id}", "v1"),
                events,
            )
        )
    assert results == ["applied"] * len(events)
    source_events = redis_store.get_events(source, "INR", datetime.now(UTC), "v1")
    destination_events = redis_store.get_events(destination, "INR", datetime.now(UTC), "v1")
    assert {event.event_id for event in source_events} == {event.event_id for event in events}
    assert {event.event_id for event in destination_events} == {event.event_id for event in events}

    invalid = FeatureEvent(
        event_id=uuid4(),
        occurred_at=datetime.now(UTC),
        source_account_id=source,
        destination_account_id=destination,
        amount_minor=999,
        currency="INR",
    )
    source_key, _, _ = redis_store._account_keys(source, "INR", "v1")
    redis_store.client.set(source_key, "wrong-type", ex=60)
    with pytest.raises(RuntimeError, match="key_type_mismatch"):
        redis_store.apply_event(invalid, f"fp-{invalid.event_id}", "v1")
    assert (
        redis_store.client.hget(f"{redis_store.key_prefix}:v1:dedup", str(invalid.event_id)) is None
    )
    assert invalid.event_id not in {
        event.event_id
        for event in redis_store.get_events(destination, "INR", datetime.now(UTC), "v1")
    }


def test_live_redis_point_in_time_windows_and_online_offline_parity(redis_store):
    from app.features.calculator import calculate_account_features
    from app.features.schemas import FeatureEvent

    account = uuid4()
    as_of = datetime.now(UTC).replace(microsecond=0)
    boundaries = (
        timedelta(seconds=60),
        timedelta(minutes=15),
        timedelta(minutes=60),
        timedelta(hours=24),
        timedelta(days=7),
    )
    events = []
    for index, boundary in enumerate(boundaries):
        counterparty = uuid4()
        events.append(
            FeatureEvent(
                event_id=uuid4(),
                occurred_at=as_of - boundary,
                source_account_id=account,
                destination_account_id=counterparty,
                amount_minor=100 + index,
                currency="INR",
            )
        )
        events.append(
            FeatureEvent(
                event_id=uuid4(),
                occurred_at=as_of - boundary + timedelta(seconds=1),
                source_account_id=counterparty,
                destination_account_id=account,
                amount_minor=200 + index,
                currency="INR",
            )
        )
    events.extend(
        [
            FeatureEvent(
                event_id=uuid4(),
                occurred_at=as_of,
                source_account_id=account,
                destination_account_id=uuid4(),
                amount_minor=999,
                currency="INR",
            ),
            FeatureEvent(
                event_id=uuid4(),
                occurred_at=as_of - timedelta(days=8),
                source_account_id=account,
                destination_account_id=uuid4(),
                amount_minor=888,
                currency="INR",
            ),
            FeatureEvent(
                event_id=uuid4(),
                occurred_at=as_of - timedelta(seconds=5),
                source_account_id=account,
                destination_account_id=uuid4(),
                amount_minor=777,
                currency="USD",
            ),
        ]
    )
    for event in reversed(events):
        redis_store.apply_event(event, f"fp-{event.event_id}", "v1")

    expected_ids = {
        event.event_id
        for event in events
        if event.currency == "INR" and as_of - timedelta(days=7) <= event.occurred_at < as_of
    }
    projected = redis_store.get_events(account, "INR", as_of, "v1")
    assert {event.event_id for event in projected} == expected_ids
    assert redis_store.get_events(account, "USD", as_of, "v1") == [
        event for event in events if event.currency == "USD"
    ]
    online = calculate_account_features(account, "INR", as_of, projected)
    offline = calculate_account_features(
        account, "INR", as_of, [event for event in events if event.currency == "INR"]
    )
    assert online == offline
    assert online["outgoing_transaction_count_60s"] == 1
    assert online["total_transaction_count_7d"] == len(expected_ids)
    assert online["unique_counterparties_24h"] == 4
    assert online["transaction_amount_variance_7d"] > 0


def test_live_redis_duplicate_fingerprint_survives_client_reconnect(redis_store):
    from app.core.config import get_settings
    from app.features.redis_store import RedisFeatureStore
    from app.features.schemas import FeatureEvent

    event = FeatureEvent(
        event_id=uuid4(),
        occurred_at=datetime.now(UTC),
        source_account_id=uuid4(),
        destination_account_id=uuid4(),
        amount_minor=123,
        currency="INR",
    )
    fingerprint = f"fp-{event.event_id}"
    assert redis_store.apply_event(event, fingerprint, "v1") == "applied"
    reconnected = RedisFeatureStore(get_settings().redis_url, key_prefix=redis_store.key_prefix)
    try:
        assert reconnected.apply_event(event, fingerprint, "v1") == "duplicate"
        with pytest.raises(ValueError, match="payload_mismatch"):
            reconnected.apply_event(event, "changed-content", "v1")
        assert reconnected.get_metrics("v1")["events_processed_total"] == 1
        assert reconnected.get_metrics("v1")["duplicate_events_total"] == 1
    finally:
        reconnected.close()


def test_live_redis_application_acl_rejects_admin_and_unrelated_key_access(redis_store):
    import redis

    with pytest.raises(redis.exceptions.ResponseError):
        redis_store.client.config_get("appendonly")
    with pytest.raises(redis.exceptions.ResponseError):
        redis_store.client.get("unrelated:acceptance:probe")
