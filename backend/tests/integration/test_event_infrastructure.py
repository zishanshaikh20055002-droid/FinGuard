"""PostgreSQL verification for leases, retries, inbox deduplication and recovery."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine, func, insert, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.events.broker import FakePublisher, envelope_from_outbox
from app.events.consumer import TransactionAuditConsumer, process_message
from app.events.contracts import EventEnvelope, serialize_event
from app.events.dispatcher import (
    _replay_update,
    claim_batch,
    claim_replays,
    dispatch_once,
    dispatch_replays_once,
)
from app.models import EventInbox, OutboxEvent, ReplayRequest, User

pytestmark = pytest.mark.integration


def seed_event(engine: Engine, *, status: str = "pending", attempt_count: int = 0) -> uuid4:
    event_id, aggregate_id = uuid4(), uuid4()
    with engine.begin() as connection:
        connection.execute(
            insert(OutboxEvent).values(
                id=event_id,
                aggregate_type="transaction",
                aggregate_id=aggregate_id,
                event_type="transaction.posted",
                payload={
                    "schema_version": 1,
                    "transaction_id": str(aggregate_id),
                    "amount_minor": 10,
                    "currency": "INR",
                },
                attempt_count=attempt_count,
                status=status,
                correlation_id=uuid4(),
            )
        )
    return event_id


def seed_replay(engine: Engine) -> tuple[uuid4, uuid4, uuid4, dict, object]:
    from app.auth.security import hash_password

    event_id = seed_event(engine)
    admin_id, request_id = uuid4(), uuid4()
    with pg_engine_session(engine) as session:
        original = session.get(OutboxEvent, event_id)
        original_payload = dict(original.payload)
        original_created_at = original.created_at
        original_aggregate_id = original.aggregate_id
    with engine.begin() as connection:
        connection.execute(
            insert(User).values(
                id=admin_id,
                email=f"replay-{admin_id}@example.test",
                password_hash=hash_password("Valid Replay Password 123!", Settings(_env_file=None)),
                role="admin",
                status="active",
            )
        )
        connection.execute(
            insert(ReplayRequest).values(
                id=request_id,
                original_event_id=event_id,
                requested_by_admin_id=admin_id,
                idempotency_key=f"replay-{request_id}",
                reason="reliability integration test",
                request_fingerprint="b" * 64,
                status="pending",
                attempt_count=0,
                claim_generation=0,
            )
        )
    return request_id, event_id, original_aggregate_id, original_payload, original_created_at


def pg_engine_session(engine: Engine) -> Session:
    return Session(engine)


def expire_replay_lease(engine: Engine, request_id) -> None:
    with engine.begin() as connection:
        connection.execute(
            ReplayRequest.__table__.update()
            .where(ReplayRequest.id == request_id)
            .values(claim_expires_at=func.clock_timestamp() - timedelta(seconds=1))
        )


def test_claim_lease_expiry_and_late_owner_fencing(pg_engine: Engine) -> None:
    event_id = seed_event(pg_engine)
    with Session(pg_engine) as session, session.begin():
        first = claim_batch(session, "worker-a", 1, 5, [event_id])[0]
        token_a = first.claim_id
        generation_a = first.claim_generation
    with Session(pg_engine) as session, session.begin():
        assert claim_batch(session, "worker-b", 1, 5, [event_id]) == []
    with pg_engine.begin() as connection:
        connection.execute(
            OutboxEvent.__table__.update()
            .where(OutboxEvent.id == event_id)
            .values(claim_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    with Session(pg_engine) as session, session.begin():
        second = claim_batch(session, "worker-b", 1, 30, [event_id])[0]
        token_b = second.claim_id
        assert second.claim_generation > generation_a
        assert token_a != token_b
    with Session(pg_engine) as session, session.begin():
        late = session.scalar(
            select(OutboxEvent).where(OutboxEvent.id == event_id, OutboxEvent.claim_id == token_a)
        )
        assert late is None
    with Session(pg_engine) as session:
        current = session.get(OutboxEvent, event_id)
        assert current.claim_id == token_b and current.status == "claimed"


def test_concurrent_dispatchers_skip_locked_and_do_not_double_claim(pg_engine: Engine) -> None:
    ids = [seed_event(pg_engine) for _ in range(8)]

    def claim(worker: str) -> list[str]:
        with Session(pg_engine) as session, session.begin():
            return [str(row.id) for row in claim_batch(session, worker, 8, 60, ids)]

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, ("worker-a", "worker-b")))
    flattened = results[0] + results[1]
    assert len(flattened) == len(set(flattened)) == len(ids)


def test_dispatch_ack_failure_retry_and_dead_letter(pg_engine: Engine) -> None:
    event_id = seed_event(pg_engine)
    settings = Settings(_env_file=None, outbox_max_attempts=2, outbox_batch_size=10)

    class BrokenPublisher(FakePublisher):
        def publish(self, event, key):
            raise TimeoutError("broker timeout")

    assert (
        dispatch_once(
            lambda: Session(pg_engine), BrokenPublisher(), settings, "fail-worker", [event_id]
        )
        == 0
    )
    with Session(pg_engine) as session:
        row = session.get(OutboxEvent, event_id)
        assert row.status == "pending" and row.attempt_count == 1
        assert row.next_retry_at is not None and row.last_error == "TimeoutError"
    with pg_engine.begin() as connection:
        connection.execute(
            OutboxEvent.__table__.update()
            .where(OutboxEvent.id == event_id)
            .values(next_retry_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    assert (
        dispatch_once(
            lambda: Session(pg_engine), BrokenPublisher(), settings, "fail-worker", [event_id]
        )
        == 0
    )
    with Session(pg_engine) as session:
        row = session.get(OutboxEvent, event_id)
        assert row.status == "dead_letter" and row.attempt_count == 2


def test_dispatch_marks_published_only_after_ack_and_can_redeliver(pg_engine: Engine) -> None:
    event_id = seed_event(pg_engine)
    settings = Settings(_env_file=None)
    fake = FakePublisher()
    assert dispatch_once(lambda: Session(pg_engine), fake, settings, "publisher", [event_id]) == 1
    assert len(fake.messages) == 1 and fake.messages[0][0] == str(
        Session(pg_engine).get(OutboxEvent, event_id).aggregate_id
    )
    envelope = EventEnvelope.model_validate_json(fake.messages[0][1])
    assert envelope.event_id == event_id and envelope.schema_version == 1
    with Session(pg_engine) as session:
        assert session.get(OutboxEvent, event_id).status == "published"


def test_replay_worker_republishes_immutable_event_and_completes(pg_engine: Engine) -> None:
    request_id, event_id, aggregate_id, original_payload, original_created_at = seed_replay(
        pg_engine
    )
    fake = FakePublisher()
    settings = Settings(_env_file=None, outbox_batch_size=10, outbox_lease_seconds=30)
    assert dispatch_replays_once(lambda: Session(pg_engine), fake, settings, "replay-worker") == 1
    envelope = EventEnvelope.model_validate_json(fake.messages[0][1])
    assert envelope.event_id == event_id and envelope.aggregate_id == aggregate_id
    with Session(pg_engine) as session:
        request = session.get(ReplayRequest, request_id)
        original = session.get(OutboxEvent, event_id)
        assert request.status == "completed" and request.claim_generation == 1
        assert original.payload == original_payload and original.created_at == original_created_at


def test_replay_transient_failure_retries_same_request(pg_engine: Engine) -> None:
    request_id, event_id, _, payload, created_at = seed_replay(pg_engine)
    settings = Settings(_env_file=None, outbox_max_attempts=3)

    class FailOncePublisher(FakePublisher):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def publish(self, event, key):
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError("temporary broker error")
            super().publish(event, key)

    publisher = FailOncePublisher()

    def factory():
        return Session(pg_engine)

    assert dispatch_replays_once(factory, publisher, settings, "retry-worker", [request_id]) == 0
    with Session(pg_engine) as session:
        failed = session.get(ReplayRequest, request_id)
        original = session.get(OutboxEvent, event_id)
        assert failed.status == "retry_scheduled"
        assert failed.attempt_count == 1 and failed.next_retry_at is not None
        assert failed.last_error == "TimeoutError"
        assert original.payload == payload and original.created_at == created_at
    with pg_engine.begin() as connection:
        connection.execute(
            ReplayRequest.__table__.update()
            .where(ReplayRequest.id == request_id)
            .values(next_retry_at=func.clock_timestamp() - timedelta(seconds=1))
        )
    assert dispatch_replays_once(factory, publisher, settings, "retry-worker-2", [request_id]) == 1
    assert publisher.calls == 2 and len(publisher.messages) == 1
    with Session(pg_engine) as session:
        completed = session.get(ReplayRequest, request_id)
        assert completed.status == "completed" and completed.attempt_count == 2
        assert session.get(OutboxEvent, event_id).payload == payload


def test_replay_failure_exhaustion_retains_dead_letter(pg_engine: Engine) -> None:
    request_id, event_id, _, payload, created_at = seed_replay(pg_engine)
    settings = Settings(_env_file=None, outbox_max_attempts=2)

    class BrokenPublisher(FakePublisher):
        def publish(self, event, key):
            raise ConnectionError("broker unavailable")

    def factory():
        return Session(pg_engine)

    publisher = BrokenPublisher()
    for attempt in range(2):
        assert (
            dispatch_replays_once(
                factory, publisher, settings, f"dead-worker-{attempt}", [request_id]
            )
            == 0
        )
        if attempt == 0:
            with pg_engine.begin() as connection:
                connection.execute(
                    ReplayRequest.__table__.update()
                    .where(ReplayRequest.id == request_id)
                    .values(next_retry_at=func.clock_timestamp() - timedelta(seconds=1))
                )
    with Session(pg_engine) as session:
        request = session.get(ReplayRequest, request_id)
        original = session.get(OutboxEvent, event_id)
        assert request.status == "dead_letter" and request.attempt_count == 2
        assert request.last_error == "ConnectionError" and request.next_retry_at is None
        assert request.id == request_id
        assert original.payload == payload and original.created_at == created_at


def test_replay_worker_crash_before_publish_recovers_after_lease(pg_engine: Engine) -> None:
    request_id, _, _, _, _ = seed_replay(pg_engine)
    with Session(pg_engine) as session, session.begin():
        claimed = claim_replays(session, "crashed-before-publish", 1, 30, [request_id])[0]
        assert claimed.claim_generation == 1
    expire_replay_lease(pg_engine, request_id)
    publisher = FakePublisher()
    assert (
        dispatch_replays_once(
            lambda: Session(pg_engine),
            publisher,
            Settings(_env_file=None),
            "recovery-worker",
            [request_id],
        )
        == 1
    )
    with Session(pg_engine) as session:
        request = session.get(ReplayRequest, request_id)
        assert request.status == "completed" and request.claim_generation == 2
    assert len(publisher.messages) == 1


def test_replay_worker_crash_after_ack_can_republish_and_complete(pg_engine: Engine) -> None:
    request_id, event_id, aggregate_id, payload, created_at = seed_replay(pg_engine)
    with Session(pg_engine) as session, session.begin():
        claimed = claim_replays(session, "crashed-after-ack", 1, 30, [request_id])[0]
        generation = claimed.claim_generation
    publisher = FakePublisher()
    with Session(pg_engine) as session:
        event = session.get(OutboxEvent, event_id)
        publisher.publish(
            envelope_from_outbox(event, event.correlation_id or event.id), str(aggregate_id)
        )
    expire_replay_lease(pg_engine, request_id)
    assert (
        dispatch_replays_once(
            lambda: Session(pg_engine),
            publisher,
            Settings(_env_file=None),
            "ack-recovery",
            [request_id],
        )
        == 1
    )
    assert len(publisher.messages) == 2
    envelopes = [EventEnvelope.model_validate_json(message[1]) for message in publisher.messages]
    assert envelopes[0] == envelopes[1]
    with Session(pg_engine) as session:
        request = session.get(ReplayRequest, request_id)
        original = session.get(OutboxEvent, event_id)
        assert request.status == "completed" and request.claim_generation == generation + 1
        assert original.payload == payload and original.created_at == created_at


def test_stale_replay_worker_cannot_mutate_or_release_new_claim(pg_engine: Engine) -> None:
    request_id, _, _, _, _ = seed_replay(pg_engine)
    with Session(pg_engine) as session, session.begin():
        claim_a = claim_replays(session, "worker-a", 1, 30, [request_id])[0]
        generation_a = claim_a.claim_generation
    expire_replay_lease(pg_engine, request_id)
    with Session(pg_engine) as session, session.begin():
        claim_b = claim_replays(session, "worker-b", 1, 30, [request_id])[0]
        generation_b = claim_b.claim_generation
        assert generation_b == generation_a + 1
    stale_values = [
        {"status": "completed", "completed_at": func.clock_timestamp()},
        {"status": "retry_scheduled", "next_retry_at": func.clock_timestamp()},
        {"status": "dead_letter", "last_error": "stale"},
        {"claim_expires_at": func.clock_timestamp() + timedelta(seconds=60)},
        {"status": "pending", "claim_worker_id": None, "claim_expires_at": None},
    ]
    for values in stale_values:
        with Session(pg_engine) as session, session.begin():
            assert _replay_update(session, request_id, "worker-a", generation_a, values) == 0
    with Session(pg_engine) as session:
        current = session.get(ReplayRequest, request_id)
        assert current.status == "claimed"
        assert current.claim_worker_id == "worker-b" and current.claim_generation == generation_b
    with Session(pg_engine) as session, session.begin():
        assert (
            _replay_update(
                session,
                request_id,
                "worker-b",
                generation_b,
                {
                    "status": "completed",
                    "completed_at": func.clock_timestamp(),
                    "claim_worker_id": None,
                    "claim_expires_at": None,
                },
            )
            == 1
        )
    with Session(pg_engine) as session:
        assert session.get(ReplayRequest, request_id).status == "completed"


def test_consumer_inbox_deduplicates_per_consumer_and_validates(pg_engine: Engine) -> None:
    event = EventEnvelope(
        event_id=uuid4(),
        event_type="transaction.posted",
        schema_version=1,
        aggregate_type="transaction",
        aggregate_id=uuid4(),
        occurred_at=datetime.now(UTC),
        correlation_id=uuid4(),
        payload={"amount_minor": 7, "currency": "INR"},
    )

    def factory():
        return Session(pg_engine)

    consumer = TransactionAuditConsumer()
    raw = serialize_event(event)
    assert process_message(factory, consumer, raw)
    assert not process_message(factory, consumer, raw)
    newer = TransactionAuditConsumer(name="transaction-audit-v2")
    with Session(pg_engine) as session, session.begin():
        assert newer.process(session, event)
    with Session(pg_engine) as session:
        assert session.scalar(
            select(EventInbox.id).where(
                EventInbox.event_id == event.event_id, EventInbox.consumer_name == consumer.name
            )
        )
        assert session.scalar(
            select(EventInbox.id).where(
                EventInbox.event_id == event.event_id,
                EventInbox.consumer_name == newer.name,
            )
        )
    with pytest.raises(ValidationError):
        EventEnvelope.model_validate_json(b'{"event_id":"bad"}')
