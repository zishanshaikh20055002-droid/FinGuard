"""Fenced PostgreSQL outbox and replay workers with at-least-once delivery."""

import logging
import random
import signal
import socket
import time
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.session import get_engine
from app.events.broker import Publisher, envelope_from_outbox, publisher_from_settings
from app.models import OutboxEvent, ReplayRequest

logger = logging.getLogger("finguard.events.dispatcher")
STOP = False


def backoff_seconds(attempt: int) -> float:
    return min(3600.0, 2.0 ** min(attempt, 10)) * random.uniform(0.8, 1.2)


def claim_batch(
    session: Session, worker_id: str, batch_size: int, lease_seconds: int, event_ids=None
) -> list[OutboxEvent]:
    now = func.clock_timestamp()
    eligible = or_(
        (OutboxEvent.status == "pending")
        & or_(OutboxEvent.next_retry_at.is_(None), OutboxEvent.next_retry_at <= now),
        (OutboxEvent.status == "claimed") & (OutboxEvent.claim_expires_at <= now),
    )
    if event_ids is not None:
        eligible = eligible & OutboxEvent.id.in_(event_ids)
    rows = session.scalars(
        select(OutboxEvent)
        .where(eligible)
        .order_by(OutboxEvent.created_at, OutboxEvent.id)
        .limit(batch_size)
        .with_for_update(skip_locked=True)
    ).all()
    for row in rows:
        row.status = "claimed"
        row.claim_generation += 1
        row.claim_id = worker_id
        row.claim_expires_at = now + timedelta(seconds=lease_seconds)
        row.attempt_count += 1
    session.flush()
    return rows


def fenced_update(session: Session, event_id, worker_id: str, generation: int, values: dict) -> int:
    result = session.execute(
        update(OutboxEvent)
        .where(
            OutboxEvent.id == event_id,
            OutboxEvent.status == "claimed",
            OutboxEvent.claim_id == worker_id,
            OutboxEvent.claim_generation == generation,
            OutboxEvent.claim_expires_at > func.clock_timestamp(),
        )
        .values(**values)
    )
    return result.rowcount


def renew_lease(
    session: Session, event_id, worker_id: str, generation: int, lease_seconds: int
) -> int:
    return fenced_update(
        session,
        event_id,
        worker_id,
        generation,
        {"claim_expires_at": func.clock_timestamp() + timedelta(seconds=lease_seconds)},
    )


def release_claim(session: Session, event_id, worker_id: str, generation: int) -> int:
    return fenced_update(
        session,
        event_id,
        worker_id,
        generation,
        {"status": "pending", "claim_id": None, "claim_expires_at": None},
    )


def _finish_outbox(session_factory, event_id, worker_id, generation, error, settings) -> bool:
    with session_factory() as session, session.begin():
        if error is None:
            return (
                fenced_update(
                    session,
                    event_id,
                    worker_id,
                    generation,
                    {
                        "status": "published",
                        "published_at": func.clock_timestamp(),
                        "claim_id": None,
                        "claim_expires_at": None,
                        "last_error": None,
                    },
                )
                == 1
            )
    with session_factory() as session:
        attempt = (
            session.scalar(select(OutboxEvent.attempt_count).where(OutboxEvent.id == event_id)) or 1
        )
    terminal = attempt >= settings.outbox_max_attempts
    with session_factory() as session, session.begin():
        return (
            fenced_update(
                session,
                event_id,
                worker_id,
                generation,
                {
                    "status": "dead_letter" if terminal else "pending",
                    "last_error": type(error).__name__[:100],
                    "next_retry_at": None
                    if terminal
                    else func.clock_timestamp() + timedelta(seconds=backoff_seconds(attempt)),
                    "claim_id": None,
                    "claim_expires_at": None,
                },
            )
            == 1
        )


def dispatch_once(
    session_factory, publisher: Publisher, settings, worker_id: str | None = None, event_ids=None
) -> int:
    worker_id = worker_id or f"{socket.gethostname()}-{uuid4()}"
    with session_factory() as session, session.begin():
        claims = claim_batch(
            session, worker_id, settings.outbox_batch_size, settings.outbox_lease_seconds, event_ids
        )
        claimed = [(row.id, row.claim_generation) for row in claims]
    delivered = 0
    for event_id, generation in claimed:
        with session_factory() as session:
            row = session.scalar(
                select(OutboxEvent).where(
                    OutboxEvent.id == event_id,
                    OutboxEvent.status == "claimed",
                    OutboxEvent.claim_id == worker_id,
                    OutboxEvent.claim_generation == generation,
                    OutboxEvent.claim_expires_at > func.clock_timestamp(),
                )
            )
            if row is None:
                continue
            envelope = envelope_from_outbox(row, row.correlation_id or row.id)
            key = str(row.aggregate_id)
        failure = None
        try:
            publisher.publish(envelope, key)
        except Exception as error:
            failure = error
        updated = _finish_outbox(
            session_factory, event_id, worker_id, generation, failure, settings
        )
        delivered += int(updated and failure is None)
    return delivered


def claim_replays(
    session: Session,
    worker_id: str,
    batch_size: int,
    lease_seconds: int,
    request_ids=None,
):
    now = func.clock_timestamp()
    eligible = or_(
        (ReplayRequest.status.in_(["pending", "retry_scheduled"]))
        & or_(ReplayRequest.next_retry_at.is_(None), ReplayRequest.next_retry_at <= now),
        (ReplayRequest.status == "claimed") & (ReplayRequest.claim_expires_at <= now),
    )
    if request_ids is not None:
        eligible = eligible & ReplayRequest.id.in_(request_ids)
    rows = session.scalars(
        select(ReplayRequest)
        .where(eligible)
        .order_by(ReplayRequest.requested_at)
        .limit(batch_size)
        .with_for_update(skip_locked=True)
    ).all()
    for row in rows:
        row.status = "claimed"
        row.claim_generation += 1
        row.claim_worker_id = worker_id
        row.claim_expires_at = now + timedelta(seconds=lease_seconds)
        row.attempt_count += 1
    session.flush()
    return rows


def _replay_update(session, request_id, worker_id, generation, values):
    return session.execute(
        update(ReplayRequest)
        .where(
            ReplayRequest.id == request_id,
            ReplayRequest.status == "claimed",
            ReplayRequest.claim_worker_id == worker_id,
            ReplayRequest.claim_generation == generation,
            ReplayRequest.claim_expires_at > func.clock_timestamp(),
        )
        .values(**values)
    ).rowcount


def dispatch_replays_once(
    session_factory, publisher: Publisher, settings, worker_id: str | None = None, request_ids=None
) -> int:
    worker_id = worker_id or f"replay-{socket.gethostname()}-{uuid4()}"
    with session_factory() as session, session.begin():
        rows = claim_replays(
            session,
            worker_id,
            settings.outbox_batch_size,
            settings.outbox_lease_seconds,
            request_ids,
        )
        claims = [(row.id, row.claim_generation) for row in rows]
    completed = 0
    for request_id, generation in claims:
        with session_factory() as session:
            request = session.get(ReplayRequest, request_id)
            event = session.get(OutboxEvent, request.original_event_id)
            envelope = envelope_from_outbox(event, event.correlation_id or event.id)
            key = str(event.aggregate_id)
        error = None
        try:
            publisher.publish(envelope, key)
        except Exception as exc:
            error = exc
        with session_factory() as session:
            attempt = (
                session.scalar(
                    select(ReplayRequest.attempt_count).where(ReplayRequest.id == request_id)
                )
                or 1
            )
        terminal = error is not None and attempt >= settings.outbox_max_attempts
        values = (
            {
                "status": "completed",
                "completed_at": func.clock_timestamp(),
                "claim_worker_id": None,
                "claim_expires_at": None,
                "last_error": None,
            }
            if error is None
            else {
                "status": "dead_letter" if terminal else "retry_scheduled",
                "last_error": type(error).__name__[:100],
                "next_retry_at": None
                if terminal
                else func.clock_timestamp() + timedelta(seconds=backoff_seconds(attempt)),
                "claim_worker_id": None,
                "claim_expires_at": None,
            }
        )
        with session_factory() as session, session.begin():
            changed = _replay_update(session, request_id, worker_id, generation, values)
        completed += int(error is None and changed == 1)
    return completed


def run() -> None:
    global STOP
    settings = get_settings()
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is required for the dispatcher")
    publisher = publisher_from_settings(settings)

    def session_factory():
        return Session(get_engine(settings.database_url), expire_on_commit=False)

    worker_id = f"{socket.gethostname()}-{uuid4()}"

    def stop_handler(signum, frame):
        global STOP
        STOP = True

    signal.signal(signal.SIGINT, stop_handler)
    signal.signal(signal.SIGTERM, stop_handler)
    try:
        while not STOP:
            work = dispatch_once(session_factory, publisher, settings, worker_id)
            work += dispatch_replays_once(session_factory, publisher, settings, worker_id)
            if not work:
                time.sleep(settings.outbox_poll_seconds)
    finally:
        publisher.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
