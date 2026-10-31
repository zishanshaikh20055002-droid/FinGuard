"""PostgreSQL inbox coordination and historical projection tests."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, insert, select, update
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user
from app.auth.permissions import Principal
from app.core.config import Settings, get_settings
from app.db.session import get_db_session
from app.events.broker import envelope_from_outbox
from app.events.contracts import EventEnvelope, serialize_event
from app.features.consumer import FeatureProjectionConsumer
from app.features.fake_store import FakeFeatureStore
from app.features.historical import (
    historical_account_features,
    rebuild_projection,
)
from app.features.service import get_account_features
from app.main import app
from app.models import Account, EventInbox, JournalEntry, JournalLine, OutboxEvent, Transaction

pytestmark = pytest.mark.integration


def seed_posted_event(engine: Engine):
    event_id, transaction_id, source_id, destination_id, entry_id = (uuid4() for _ in range(5))
    with engine.begin() as connection:
        for account_id, account_type in ((source_id, "liability"), (destination_id, "liability")):
            connection.execute(
                insert(Account).values(
                    id=account_id,
                    reference=f"FEATURE-TEST-{account_id}",
                    currency="INR",
                    account_type=account_type,
                    status="open",
                )
            )
        connection.execute(
            insert(Transaction).values(
                id=transaction_id,
                source_account_id=source_id,
                destination_account_id=destination_id,
                amount_minor=1234,
                currency="INR",
                status="posted",
            )
        )
        connection.execute(
            insert(JournalEntry).values(
                id=entry_id,
                transaction_id=transaction_id,
                entry_type="standard",
                status="draft",
            )
        )
        connection.execute(
            insert(JournalLine),
            [
                {
                    "id": uuid4(),
                    "journal_entry_id": entry_id,
                    "account_id": source_id,
                    "currency": "INR",
                    "debit_minor": 1234,
                    "credit_minor": 0,
                },
                {
                    "id": uuid4(),
                    "journal_entry_id": entry_id,
                    "account_id": destination_id,
                    "currency": "INR",
                    "debit_minor": 0,
                    "credit_minor": 1234,
                },
            ],
        )
        connection.execute(
            update(JournalEntry)
            .where(JournalEntry.id == entry_id)
            .values(status="posted", posted_at=func.clock_timestamp())
        )
        connection.execute(
            insert(OutboxEvent).values(
                id=event_id,
                aggregate_type="transaction",
                aggregate_id=transaction_id,
                event_type="transaction.posted",
                payload={
                    "schema_version": 1,
                    "transaction_id": str(transaction_id),
                    "status": "posted",
                    "amount_minor": 1234,
                    "currency": "INR",
                    "source_account_id": str(source_id),
                    "destination_account_id": str(destination_id),
                },
                status="pending",
                attempt_count=0,
                correlation_id=uuid4(),
            )
        )
    with Session(engine) as session:
        row = session.get(OutboxEvent, event_id)
        raw = serialize_event(envelope_from_outbox(row, row.correlation_id))
    return event_id, transaction_id, source_id, destination_id, raw


def test_feature_consumer_updates_both_accounts_and_postgres_inbox(pg_engine: Engine):
    event_id, transaction_id, source_id, destination_id, raw = seed_posted_event(pg_engine)
    store = FakeFeatureStore()
    consumer = FeatureProjectionConsumer(store)

    def factory():
        return Session(pg_engine)

    assert consumer.process_message(factory, raw)
    assert not consumer.process_message(factory, raw)
    with Session(pg_engine) as session:
        inbox = session.scalar(
            select(EventInbox).where(
                EventInbox.consumer_name == consumer.name, EventInbox.event_id == event_id
            )
        )
        assert inbox.processing_status == "processed" and inbox.processed_at is not None
        assert session.get(Transaction, transaction_id).status == "posted"
    as_of = datetime.now(UTC)
    settings = Settings(_env_file=None)
    source = get_account_features(store, source_id, "INR", as_of, settings, "v1")
    destination = get_account_features(store, destination_id, "INR", as_of, settings, "v1")
    assert source.values["outgoing_transaction_count_60s"] == 1
    assert source.values["outgoing_amount_minor_7d"] == 1234
    assert destination.values["incoming_transaction_count_60m"] == 1
    assert destination.values["incoming_amount_minor_24h"] == 1234
    assert store.get_metrics("v1")["events_processed_total"] == 1
    assert store.get_metrics("v1")["duplicate_events_total"] == 1


def test_redis_success_then_postgres_failure_recovers_on_redelivery(pg_engine: Engine):
    event_id, _, _, _, raw = seed_posted_event(pg_engine)
    store = FakeFeatureStore()
    consumer = FeatureProjectionConsumer(store)
    calls = 0

    def failing_factory():
        nonlocal calls
        calls += 1
        if calls == 1:
            return Session(pg_engine)
        raise RuntimeError("simulated inbox database failure")

    with pytest.raises(RuntimeError, match="simulated"):
        consumer.process_message(failing_factory, raw)
    assert store.get_metrics("v1")["feature_update_errors_total"] == 1
    with Session(pg_engine) as session:
        assert (
            session.scalar(
                select(EventInbox.id).where(
                    EventInbox.consumer_name == consumer.name, EventInbox.event_id == event_id
                )
            )
            is None
        )
    assert store.get_metrics("v1")["events_processed_total"] == 1
    assert consumer.process_message(lambda: Session(pg_engine), raw)
    assert store.get_metrics("v1")["events_processed_total"] == 1
    assert store.get_metrics("v1")["duplicate_events_total"] == 1
    assert store.get_metrics("v1")["feature_update_errors_total"] == 1


def test_redis_failure_does_not_commit_inbox_or_change_ledger(pg_engine: Engine):
    event_id, transaction_id, _, _, raw = seed_posted_event(pg_engine)

    class UnavailableStore(FakeFeatureStore):
        def apply_event(self, event, fingerprint, version="v1"):
            raise RuntimeError("redis unavailable")

    consumer = FeatureProjectionConsumer(UnavailableStore())
    with pytest.raises(RuntimeError, match="redis unavailable"):
        consumer.process_message(lambda: Session(pg_engine), raw)
    with Session(pg_engine) as session:
        assert session.get(Transaction, transaction_id).status == "posted"
        assert (
            session.scalar(
                select(EventInbox.id).where(
                    EventInbox.consumer_name == consumer.name, EventInbox.event_id == event_id
                )
            )
            is None
        )


def test_legacy_posted_event_without_account_ids_uses_database_fallback(pg_engine: Engine):
    event_id, _, source_id, destination_id, raw = seed_posted_event(pg_engine)
    envelope = EventEnvelope.model_validate_json(raw)
    legacy_payload = dict(envelope.payload)
    legacy_payload.pop("source_account_id")
    legacy_payload.pop("destination_account_id")
    legacy_raw = serialize_event(envelope.model_copy(update={"payload": legacy_payload}))
    store = FakeFeatureStore()
    consumer = FeatureProjectionConsumer(store)
    assert consumer.process_message(lambda: Session(pg_engine), legacy_raw)
    assert store.get_events(source_id, "INR", datetime.now(UTC), "v1")
    assert store.get_events(destination_id, "INR", datetime.now(UTC), "v1")
    with Session(pg_engine) as session:
        assert session.scalar(
            select(EventInbox.id).where(
                EventInbox.consumer_name == consumer.name, EventInbox.event_id == event_id
            )
        )


def test_history_is_point_in_time_and_rebuild_uses_a_separate_version(pg_engine: Engine):
    event_id, _, source_id, _, _ = seed_posted_event(pg_engine)
    store = FakeFeatureStore()
    consumer = FeatureProjectionConsumer(store)
    with Session(pg_engine) as session:
        outbox = session.get(OutboxEvent, event_id)
        raw = serialize_event(envelope_from_outbox(outbox, outbox.correlation_id))
    consumer.process_message(lambda: Session(pg_engine), raw)
    with Session(pg_engine) as session:
        history = historical_account_features(session, source_id, "INR", limit=10)
    assert len(history) == 1
    assert history[0]["features"]["outgoing_amount_minor_7d"] == 0

    def factory():
        return Session(pg_engine)

    calls = 0

    def interrupted_factory():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated rebuild interruption")
        return Session(pg_engine)

    with pytest.raises(RuntimeError, match="rebuild interruption"):
        rebuild_projection(interrupted_factory, store, "v2", batch_size=1)
    assert store.get_metrics("v2")["rebuild_status"] == "failed"
    rebuilt_count = rebuild_projection(factory, store, "v2", batch_size=1000)
    assert rebuilt_count >= 1
    assert store.active_version() == "v1"
    assert store.get_events(source_id, "INR", datetime.now(UTC), "v2")
    assert store.get_metrics("v2")["rebuild_status"] == "ready"


def test_feature_api_is_internal_and_customer_access_is_denied(
    pg_engine: Engine, monkeypatch: pytest.MonkeyPatch
):
    _, _, source_id, destination_id, _ = seed_posted_event(pg_engine)
    store = FakeFeatureStore()
    from app.features.schemas import FeatureEvent

    event = FeatureEvent(
        event_id=uuid4(),
        occurred_at=datetime.now(UTC),
        source_account_id=source_id,
        destination_account_id=destination_id,
        amount_minor=250,
        currency="INR",
    )
    store.apply_event(event, str(event.event_id), "v1")
    monkeypatch.setattr("app.features.routes.feature_store", lambda settings: store)

    def db_session():
        with Session(pg_engine, expire_on_commit=False) as session:
            yield session

    settings = Settings(_env_file=None, database_url=str(pg_engine.url))
    app.dependency_overrides[get_db_session] = db_session
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        analyst = Principal(
            uuid4(), "analyst@example.test", "analyst", "active", uuid4(), datetime.now(UTC)
        )
        app.dependency_overrides[get_current_user] = lambda: analyst
        with TestClient(app) as client:
            response = client.get(f"/api/v1/features/accounts/{source_id}")
            assert response.status_code == 200
            assert response.json()["values"]["outgoing_amount_minor_7d"] == 250
            assert "risk_score" not in response.json()
            customer = Principal(
                uuid4(), "customer@example.test", "customer", "active", uuid4(), datetime.now(UTC)
            )
            app.dependency_overrides[get_current_user] = lambda: customer
            denied = client.get(f"/api/v1/features/accounts/{source_id}")
            assert denied.status_code == 403
            app.dependency_overrides[get_current_user] = lambda: analyst
            assert client.post("/api/v1/features/rebuild/candidate-v2").status_code == 403
            admin = Principal(
                uuid4(), "admin@example.test", "admin", "active", uuid4(), datetime.now(UTC)
            )
            app.dependency_overrides[get_current_user] = lambda: admin
            rebuild = client.post("/api/v1/features/rebuild/candidate-v2")
            assert rebuild.status_code == 202
            status_response = client.get("/api/v1/features/rebuild/candidate-v2")
            assert status_response.json()["status"] == "ready"
            activation = client.post("/api/v1/features/projections/candidate-v2/activate")
            assert activation.status_code == 200
            assert activation.json()["active_projection_version"] == "candidate-v2"
            assert activation.json()["comparison"]["comparison_source"] == "postgresql_history"
    finally:
        app.dependency_overrides.clear()
