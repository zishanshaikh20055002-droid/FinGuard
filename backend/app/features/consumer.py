"""At-least-once transaction event projection with Redis-before-PostgreSQL recovery."""

import logging
import socket
import time
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.session import get_engine
from app.events.contracts import EventEnvelope, event_fingerprint
from app.features.schemas import FeatureEvent
from app.features.service import feature_store
from app.models import EventInbox, Transaction

logger = logging.getLogger("finguard.features.consumer")


class FeatureProjectionConsumer:
    name = "fraud-features-v1"

    def __init__(self, store):
        self.store = store

    def process_message(self, session_factory, raw: bytes) -> bool:
        event = EventEnvelope.model_validate_json(raw)
        if event.event_type != "transaction.posted":
            return False
        if event.schema_version != 1 or event.aggregate_type != "transaction":
            raise ValueError("unsupported_feature_event_schema")
        transaction_id = UUID(str(event.payload.get("transaction_id", event.aggregate_id)))
        if transaction_id != event.aggregate_id:
            raise ValueError("event_transaction_identity_mismatch")
        with session_factory() as session:
            transaction = session.scalar(
                select(Transaction).where(
                    Transaction.id == transaction_id,
                    Transaction.status == "posted",
                )
            )
            if transaction is None:
                raise ValueError("posted_transaction_not_found")
            amount = event.payload.get("amount_minor")
            currency = event.payload.get("currency")
            if amount is not None and amount != transaction.amount_minor:
                raise ValueError("event_amount_mismatch")
            if currency is not None and currency != transaction.currency:
                raise ValueError("event_currency_mismatch")
            if event.payload.get("status", "posted") != "posted":
                raise ValueError("event_status_mismatch")
            for key, value in (
                ("source_account_id", transaction.source_account_id),
                ("destination_account_id", transaction.destination_account_id),
            ):
                supplied = event.payload.get(key)
                if supplied is not None and UUID(str(supplied)) != value:
                    raise ValueError("event_account_mismatch")
            feature_event = FeatureEvent(
                event_id=event.event_id,
                occurred_at=event.occurred_at,
                source_account_id=transaction.source_account_id,
                destination_account_id=transaction.destination_account_id,
                amount_minor=transaction.amount_minor,
                currency=transaction.currency,
            )
        fingerprint = event_fingerprint(event)
        versions = self.store.write_versions()
        for version in versions:
            try:
                self.store.apply_event(feature_event, fingerprint, version)
            except Exception:
                try:
                    self.store.record_error(version)
                except Exception:
                    pass
                raise
        try:
            with session_factory() as session, session.begin():
                inserted = session.scalar(
                    insert(EventInbox)
                    .values(
                        event_id=event.event_id,
                        consumer_name=self.name,
                        event_type=event.event_type,
                        schema_version=event.schema_version,
                        aggregate_id=event.aggregate_id,
                        processing_status="processed",
                        processed_at=func.now(),
                    )
                    .on_conflict_do_nothing(constraint="uq_event_inbox_consumer_event")
                    .returning(EventInbox.id)
                )
        except Exception:
            for version in versions:
                try:
                    self.store.record_error(version)
                except Exception:
                    pass
            raise
        return inserted is not None


def consume_one(consumer_client, session_factory, consumer: FeatureProjectionConsumer) -> bool:
    message = consumer_client.poll(1.0)
    if message is None:
        return False
    if message.error():
        raise RuntimeError("Kafka feature consumer polling failed")
    while True:
        try:
            consumer.process_message(session_factory, message.value())
            break
        except Exception as error:
            # Retain this exact message and block later offsets until it succeeds.
            logger.error("feature_event_processing_failed error_type=%s", type(error).__name__)
            time.sleep(1)
    consumer_client.commit(message=message, asynchronous=False)
    return True


def run() -> None:
    from confluent_kafka import Consumer

    settings = get_settings()
    if not settings.database_url or not settings.kafka_bootstrap_servers:
        raise RuntimeError("DATABASE_URL and FINGUARD_KAFKA_BOOTSTRAP_SERVERS are required")
    store = feature_store(settings)
    if store is None:
        raise RuntimeError("FINGUARD_REDIS_URL is required for the feature consumer")
    store.ping()
    client = Consumer(
        {
            "bootstrap.servers": settings.kafka_bootstrap_servers,
            "client.id": f"{settings.kafka_client_id}-features",
            "group.id": "finguard-fraud-features-v1",
            "security.protocol": settings.kafka_security_protocol,
            "enable.auto.commit": False,
            "enable.auto.offset.store": False,
            "auto.offset.reset": "earliest",
        }
    )
    client.subscribe([settings.kafka_topic])

    def session_factory():
        return Session(get_engine(settings.database_url), expire_on_commit=False)

    consumer = FeatureProjectionConsumer(store)
    logger.info("feature_consumer_started worker=%s", socket.gethostname())
    try:
        while True:
            consume_one(client, session_factory, consumer)
    finally:
        client.close()
        store.close()


if __name__ == "__main__":
    run()
