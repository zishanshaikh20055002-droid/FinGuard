"""Inbox-based idempotent consumer primitives."""

from datetime import UTC, datetime

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.events.contracts import EventEnvelope
from app.models import EventInbox


class PermanentEventError(ValueError):
    pass


class TransactionAuditConsumer:
    name = "transaction-audit-v1"
    supported = {"transaction.posted", "transaction.blocked", "transaction.reversed"}

    def __init__(self, name: str | None = None) -> None:
        self.name = name or type(self).name

    def process(self, session: Session, event: EventEnvelope) -> bool:
        if event.event_type not in self.supported:
            raise PermanentEventError("unsupported_event_type")
        inserted = session.scalar(
            insert(EventInbox)
            .values(
                event_id=event.event_id,
                consumer_name=self.name,
                event_type=event.event_type,
                schema_version=event.schema_version,
                aggregate_id=event.aggregate_id,
                processing_status="processed",
                processed_at=datetime.now(UTC),
            )
            .on_conflict_do_nothing(constraint="uq_event_inbox_consumer_event")
            .returning(EventInbox.id)
        )
        return inserted is not None


def process_message(session_factory, consumer: TransactionAuditConsumer, raw: bytes) -> bool:
    event = EventEnvelope.model_validate_json(raw)
    with session_factory() as session, session.begin():
        return consumer.process(session, event)


def consume_one(consumer_client, session_factory, consumer: TransactionAuditConsumer) -> bool:
    """Commit inbox transaction before acknowledging Kafka offset."""
    message = consumer_client.poll(1.0)
    if message is None:
        return False
    if message.error():
        raise RuntimeError("Kafka consumer polling failed")
    process_message(session_factory, consumer, message.value())
    consumer_client.commit(message=message, asynchronous=False)
    return True
