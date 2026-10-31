"""PostgreSQL inbox coordination and historical projection tests."""

from datetime import UTC, datetime
from urllib.parse import quote, urlsplit, urlunsplit
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, event, func, insert, select, update
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
    compare_projection_to_database,
    historical_account_features,
    rebuild_projection,
)
from app.features.redis_store import RedisFeatureStore
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


@pytest.mark.redis_integration
def test_real_redis_projection_recovers_after_postgres_inbox_commit_failure(
    pg_engine: Engine,
):
    settings = get_settings()
    if not settings.redis_url:
        pytest.skip("FINGUARD_REDIS_URL is not configured; live Redis integration not run")
    store = RedisFeatureStore(
        settings.redis_url, key_prefix=f"finguard:features:consumer-test:{uuid4()}"
    )
    prefix = store.key_prefix
    try:
        assert store.ping()
        event_id, _, _, _, raw = seed_posted_event(pg_engine)
        consumer = FeatureProjectionConsumer(store)
        calls = 0

        def failing_factory():
            nonlocal calls
            calls += 1
            session = Session(pg_engine)
            if calls == 2:

                def fail_commit(_session):
                    raise RuntimeError("injected_inbox_commit_failure")

                event.listen(session, "before_commit", fail_commit)
            return session

        with pytest.raises(RuntimeError, match="injected_inbox_commit_failure"):
            consumer.process_message(failing_factory, raw)
        assert store.get_metrics("v1")["events_processed_total"] == 1
        with Session(pg_engine) as session:
            assert (
                session.scalar(
                    select(EventInbox.id).where(
                        EventInbox.consumer_name == consumer.name,
                        EventInbox.event_id == event_id,
                    )
                )
                is None
            )

        assert consumer.process_message(lambda: Session(pg_engine), raw)
        assert store.get_metrics("v1")["events_processed_total"] == 1
        assert store.get_metrics("v1")["duplicate_events_total"] == 1
        with Session(pg_engine) as session:
            assert session.scalar(
                select(EventInbox.id).where(
                    EventInbox.consumer_name == consumer.name,
                    EventInbox.event_id == event_id,
                )
            )

        class Message:
            @staticmethod
            def error():
                return None

            @staticmethod
            def value():
                return raw

        class Client:
            acknowledged = False

            @staticmethod
            def poll(_timeout):
                return Message()

            def commit(self, message, asynchronous):
                assert message is not None and asynchronous is False
                with Session(pg_engine) as session:
                    assert session.scalar(
                        select(EventInbox.id).where(
                            EventInbox.consumer_name == consumer.name,
                            EventInbox.event_id == event_id,
                        )
                    )
                self.acknowledged = True

        from app.features.consumer import consume_one

        client = Client()
        assert consume_one(client, lambda: Session(pg_engine), consumer)
        assert client.acknowledged
    finally:
        for version in ("v1", "candidate-good", "candidate-bad", "candidate-recovery"):
            for suffix in ("dedup", "metrics", "accounts"):
                store.client.expire(f"{prefix}:{version}:{suffix}", 60)
            store.client.expire(f"{prefix}:rebuild:{version}", 60)
        store.client.expire(f"{prefix}:control:active", 60)
        store.client.expire(f"{prefix}:control:building", 60)
        store.close()


@pytest.mark.redis_integration
def test_real_redis_unavailable_endpoint_does_not_commit_feature_inbox(pg_engine: Engine):
    import socket

    import redis

    from app.features.redis_store import RedisFeatureStore

    settings = get_settings()
    if not settings.redis_url:
        pytest.skip("FINGUARD_REDIS_URL is not configured; live Redis integration not run")
    parts = urlsplit(settings.redis_url)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        unavailable_port = sock.getsockname()[1]
    unavailable_netloc = (
        f"{quote(parts.username or '', safe='')}:{quote(parts.password or '', safe='')}"
        f"@127.0.0.1:{unavailable_port}"
    )
    unavailable_url = urlunsplit(parts._replace(netloc=unavailable_netloc))
    store = RedisFeatureStore(
        unavailable_url, key_prefix=f"finguard:features:unavailable-test:{uuid4()}"
    )
    event_id, transaction_id, _, _, raw = seed_posted_event(pg_engine)
    consumer = FeatureProjectionConsumer(store)
    try:
        with pytest.raises(redis.exceptions.RedisError):
            consumer.process_message(lambda: Session(pg_engine), raw)
        with Session(pg_engine) as session:
            assert session.get(Transaction, transaction_id).status == "posted"
            assert (
                session.scalar(
                    select(EventInbox.id).where(
                        EventInbox.consumer_name == consumer.name,
                        EventInbox.event_id == event_id,
                    )
                )
                is None
            )
    finally:
        store.close()


@pytest.mark.redis_integration
def test_real_redis_rebuild_parity_gate_and_postgresql_loss_recovery(
    pg_engine: Engine, monkeypatch: pytest.MonkeyPatch
):
    settings = get_settings()
    if not settings.redis_url:
        pytest.skip("FINGUARD_REDIS_URL is not configured; live Redis integration not run")
    store = RedisFeatureStore(
        settings.redis_url, key_prefix=f"finguard:features:rebuild-test:{uuid4()}"
    )
    prefix = store.key_prefix
    try:
        assert store.ping()
        store.initialize_active("v1")
        first_id, _, source_id, _, _ = seed_posted_event(pg_engine)
        first = seed_posted_event(pg_engine)
        # Keep the initial committed row projected before candidate construction begins.
        with Session(pg_engine) as session:
            outbox = session.get(OutboxEvent, first_id)
            first_raw = serialize_event(envelope_from_outbox(outbox, outbox.correlation_id))
        consumer = FeatureProjectionConsumer(store)

        def factory():
            return Session(pg_engine)

        assert consumer.process_message(factory, first_raw)

        store.begin_rebuild("candidate-good")
        assert store.get_metrics("candidate-good")["rebuild_status"] == "running"
        assert store.get_events(source_id, "INR", datetime.now(UTC), "v1")

        _, _, second_source_id, _, second_raw = first
        assert consumer.process_message(factory, second_raw)
        assert store.get_events(source_id, "INR", datetime.now(UTC), "v1")
        assert store.get_events(second_source_id, "INR", datetime.now(UTC), "candidate-good")

        history_count = rebuild_projection(factory, store, "candidate-good", batch_size=1)
        assert history_count >= 2
        assert store.get_metrics("candidate-good")["rebuild_status"] == "ready"
        assert store.get_metrics("candidate-good")["rebuild_progress"] == history_count
        assert store.active_version() == "v1"
        with Session(pg_engine) as session:
            expected = compare_projection_to_database(session, store, "candidate-good")
        assert expected["matches"]
        assert expected["comparison_source"] == "postgresql_history"

        assert rebuild_projection(factory, store, "candidate-bad", batch_size=10) == history_count
        from app.features.schemas import FeatureEvent

        extra = FeatureEvent(
            event_id=uuid4(),
            occurred_at=datetime.now(UTC),
            source_account_id=source_id,
            destination_account_id=uuid4(),
            amount_minor=444,
            currency="INR",
        )
        store.apply_event(extra, f"unexpected-{extra.event_id}", "candidate-bad")
        with Session(pg_engine) as session:
            assert not compare_projection_to_database(session, store, "candidate-bad")["matches"]

        admin = Principal(
            uuid4(), "admin@example.test", "admin", "active", uuid4(), datetime.now(UTC)
        )
        api_settings = Settings(
            _env_file=None,
            database_url=str(pg_engine.url),
            redis_url=settings.redis_url,
        )
        monkeypatch.setattr("app.features.routes.feature_store", lambda _settings: store)

        def db_session():
            with Session(pg_engine, expire_on_commit=False) as session:
                yield session

        app.dependency_overrides[get_current_user] = lambda: admin
        app.dependency_overrides[get_db_session] = db_session
        app.dependency_overrides[get_settings] = lambda: api_settings
        try:
            with TestClient(app) as client:
                failed = client.post("/api/v1/features/projections/candidate-bad/activate")
                assert failed.status_code == 409
                assert failed.json()["error"]["code"] == "projection_parity_failed"
                assert store.active_version() == "v1"

                # Simulate the active Redis namespace being lost; candidate recovery is
                # validated against committed PostgreSQL history before activation.
                store.client.set(f"{prefix}:control:active", "lost-v1")
                assert rebuild_projection(factory, store, "candidate-recovery") == history_count
                recovered = client.post("/api/v1/features/projections/candidate-recovery/activate")
                assert recovered.status_code == 200
                assert recovered.json()["active_projection_version"] == "candidate-recovery"
                assert recovered.json()["comparison"]["comparison_source"] == "postgresql_history"
                assert store.active_version() == "candidate-recovery"
        finally:
            app.dependency_overrides.clear()
    finally:
        for version in ("v1", "candidate-good", "candidate-bad", "candidate-recovery"):
            for suffix in ("dedup", "metrics", "accounts"):
                store.client.expire(f"{prefix}:{version}:{suffix}", 60)
            store.client.expire(f"{prefix}:rebuild:{version}", 60)
        store.client.expire(f"{prefix}:control:active", 60)
        store.client.expire(f"{prefix}:control:building", 60)
        store.close()


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
