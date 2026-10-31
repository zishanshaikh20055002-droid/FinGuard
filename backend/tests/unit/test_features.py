"""Pure calculation, fake projection, and online/offline parity tests."""

import csv
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.features.calculator import calculate_account_features
from app.features.definitions import FEATURE_NAMES
from app.features.fake_store import FakeFeatureStore
from app.features.historical import compare_projection, write_csv
from app.features.schemas import FeatureEvent


def feature_event(at, source, destination, amount, currency="INR", event_id=None):
    return FeatureEvent(
        event_id=event_id or uuid4(),
        occurred_at=at,
        source_account_id=source,
        destination_account_id=destination,
        amount_minor=amount,
        currency=currency,
    )


def test_rolling_features_use_inclusive_lower_exclusive_as_of_boundary():
    account, other = uuid4(), uuid4()
    as_of = datetime(2026, 1, 1, tzinfo=UTC)
    events = [
        feature_event(as_of - timedelta(seconds=60), account, other, 100),
        feature_event(as_of - timedelta(seconds=60, microseconds=1), account, other, 999),
        feature_event(as_of - timedelta(days=7, microseconds=1), account, other, 4000),
        feature_event(as_of, account, other, 500),
    ]
    result = calculate_account_features(account, "INR", as_of, events)
    assert result["outgoing_transaction_count_60s"] == 1
    assert result["outgoing_amount_minor_15m"] == 1099
    assert result["total_transaction_count_7d"] == 2
    assert set(result) == set(FEATURE_NAMES)


def test_incoming_outgoing_currency_counterparties_and_decimal_statistics():
    account, first, second = uuid4(), uuid4(), uuid4()
    as_of = datetime(2026, 1, 1, tzinfo=UTC)
    events = [
        feature_event(as_of - timedelta(hours=1), account, first, 100),
        feature_event(as_of - timedelta(minutes=30), account, second, 300),
        feature_event(as_of - timedelta(minutes=20), first, account, 700),
        feature_event(as_of - timedelta(minutes=10), account, first, 900, currency="USD"),
    ]
    result = calculate_account_features(account, "INR", as_of, events)
    assert result["outgoing_transaction_count_60m"] == 2
    assert result["incoming_transaction_count_60m"] == 1
    assert result["outgoing_amount_minor_24h"] == 400
    assert result["incoming_amount_minor_24h"] == 700
    assert result["average_outgoing_amount_24h"] == 200.0
    assert result["maximum_outgoing_amount_24h"] == 300
    assert result["unique_counterparties_24h"] == 2
    assert result["transaction_amount_variance_7d"] == pytest.approx(62_222.22222222222)
    assert result["total_transaction_count_7d"] == 3


def test_equal_timestamp_order_is_stable_and_utc_is_normalized():
    account, other = uuid4(), uuid4()
    as_of = datetime(2026, 1, 1, tzinfo=UTC)
    instant = as_of - timedelta(seconds=2)
    events = [
        feature_event(instant, account, other, 200, event_id=uuid4()),
        feature_event(instant, other, account, 50, event_id=uuid4()),
    ]
    assert calculate_account_features(account, "INR", as_of, events) == calculate_account_features(
        account, "INR", as_of, list(reversed(events))
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        calculate_account_features(account, "INR", datetime(2026, 1, 1), events)


def test_fake_store_is_idempotent_rejects_payload_mismatch_and_isolates_versions():
    store = FakeFeatureStore()
    account, other = uuid4(), uuid4()
    event = feature_event(datetime.now(UTC) - timedelta(seconds=1), account, other, 123)
    assert store.apply_event(event, "fingerprint-a", "v1") == "applied"
    assert store.apply_event(event, "fingerprint-a", "v1") == "duplicate"
    with pytest.raises(ValueError, match="payload_mismatch"):
        store.apply_event(event, "fingerprint-b", "v1")
    assert store.get_events(account, "INR", datetime.now(UTC), "v1") == [event]
    assert store.get_events(account, "INR", datetime.now(UTC), "v2") == []
    metrics = store.get_metrics("v1")
    assert metrics["events_processed_total"] == 1
    assert metrics["duplicate_events_total"] == 1


def test_online_fake_projection_matches_offline_calculation_numerically():
    store = FakeFeatureStore()
    account, counterparties = uuid4(), [uuid4(), uuid4(), uuid4()]
    as_of = datetime(2026, 2, 1, tzinfo=UTC)
    events = [
        feature_event(as_of - timedelta(minutes=4), account, counterparties[0], 101),
        feature_event(as_of - timedelta(minutes=3), counterparties[1], account, 203),
        feature_event(as_of - timedelta(minutes=2), account, counterparties[2], 307),
    ]
    for event in reversed(events):
        store.apply_event(event, str(event.event_id), "v1")
    store.apply_event(events[0], str(events[0].event_id), "v1")
    online_events = store.get_events(account, "INR", as_of, "v1")
    online = calculate_account_features(account, "INR", as_of, online_events)
    offline = calculate_account_features(account, "INR", as_of, events)
    assert online == offline
    assert online["outgoing_amount_minor_15m"] == 408
    assert online["incoming_amount_minor_15m"] == 203
    assert online["transaction_amount_variance_7d"] == pytest.approx(7072.888888888889)


def test_candidate_projection_dual_write_compare_then_explicit_activation():
    store = FakeFeatureStore()
    account, other = uuid4(), uuid4()
    event = feature_event(datetime.now(UTC) - timedelta(seconds=1), account, other, 500)
    store.apply_event(event, str(event.event_id), "v1")
    store.begin_rebuild("candidate-v2")
    assert store.write_versions() == ["candidate-v2", "v1"]
    store.apply_event(event, str(event.event_id), "candidate-v2")
    store.complete_rebuild("candidate-v2")
    assert compare_projection(store, "candidate-v2")["matches"]
    assert store.activate("candidate-v2")
    assert store.active_version() == "candidate-v2"
    assert store.write_versions() == ["candidate-v2"]


def test_historical_feature_rows_export_to_training_ready_csv(tmp_path):
    account, other = uuid4(), uuid4()
    event = feature_event(datetime(2026, 2, 1, tzinfo=UTC), account, other, 111)
    values = calculate_account_features(account, "INR", event.occurred_at, [event])
    path = tmp_path / "features.csv"
    write_csv(path, [(account, "INR", event, values)])
    with path.open(encoding="utf-8", newline="") as source:
        rows = list(csv.DictReader(source))
    assert len(rows) == 1
    assert rows[0]["account_id"] == str(account)
    assert rows[0]["currency"] == "INR"
    assert rows[0]["outgoing_amount_minor_7d"] == "0"
