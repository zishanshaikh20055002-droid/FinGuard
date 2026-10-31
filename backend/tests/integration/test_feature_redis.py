"""Optional tests against a configured real Redis server; never use FakeFeatureStore here."""

import os
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

    prefix = f"finguard:test-features:{uuid4()}"
    store = RedisFeatureStore(url, key_prefix=prefix)
    try:
        store.ping()
    except Exception as exc:
        pytest.skip(f"Configured Redis endpoint is unavailable ({type(exc).__name__})")
    yield store
    keys = list(store.client.scan_iter(match=f"{prefix}:*"))
    if keys:
        store.client.delete(*keys)
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
