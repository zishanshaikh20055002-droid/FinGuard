"""Publisher adapter; importing the API never requires a Kafka broker."""

from typing import Protocol
from uuid import UUID

from app.core.config import Settings
from app.events.contracts import EventEnvelope, serialize_event


class Publisher(Protocol):
    def publish(self, event: EventEnvelope, key: str) -> None: ...
    def close(self) -> None: ...


class FakePublisher:
    def __init__(self) -> None:
        self.messages: list[tuple[str, bytes]] = []
        self.fail_with: Exception | None = None

    def publish(self, event: EventEnvelope, key: str) -> None:
        if self.fail_with:
            raise self.fail_with
        self.messages.append((key, serialize_event(event)))

    def close(self) -> None:
        return None


class KafkaPublisher:
    def __init__(self, settings: Settings) -> None:
        if not settings.kafka_bootstrap_servers:
            raise RuntimeError("FINGUARD_KAFKA_BOOTSTRAP_SERVERS is required")
        from confluent_kafka import Producer

        self.topic = settings.kafka_topic
        self.producer = Producer(
            {
                "bootstrap.servers": settings.kafka_bootstrap_servers,
                "client.id": settings.kafka_client_id,
                "security.protocol": settings.kafka_security_protocol,
                "enable.idempotence": True,
                "acks": "all",
                "message.timeout.ms": 10000,
                "retries": 5,
            }
        )

    def publish(self, event: EventEnvelope, key: str) -> None:
        import threading

        delivered = threading.Event()
        result: list[Exception | None] = [None]

        def callback(error, message) -> None:
            result[0] = RuntimeError(str(error)) if error else None
            delivered.set()

        self.producer.produce(
            self.topic, key=key.encode(), value=serialize_event(event), on_delivery=callback
        )
        self.producer.poll(0)
        if not delivered.wait(12):
            raise TimeoutError("Kafka delivery acknowledgement timed out")
        if result[0]:
            raise result[0]

    def close(self) -> None:
        self.producer.flush(12)


def envelope_from_outbox(row, correlation_id: UUID) -> EventEnvelope:
    payload = dict(row.payload)
    version = payload.pop("schema_version", 1)
    return EventEnvelope(
        event_id=row.id,
        event_type=row.event_type,
        schema_version=version,
        aggregate_type=row.aggregate_type,
        aggregate_id=row.aggregate_id,
        occurred_at=row.created_at,
        correlation_id=correlation_id,
        payload=payload,
    )


def publisher_from_settings(settings: Settings) -> Publisher:
    if settings.app_env == "production" and not settings.kafka_bootstrap_servers:
        raise RuntimeError("Kafka configuration is required in production")
    if not settings.kafka_bootstrap_servers:
        raise RuntimeError("Kafka is not configured; worker will not simulate publication")
    return KafkaPublisher(settings)
