# Event architecture

## Envelope and event types

Every published message is JSON validated as an envelope with `event_id`, `event_type`, `schema_version`, `aggregate_type`, `aggregate_id`, timezone-aware `occurred_at`, `correlation_id`, and a payload. The event UUID is the stable outbox UUID. Payloads must not contain authentication credentials or unnecessary personal data. Current transaction event types are posted, blocked, challenged, failed, and reversed; the application also supports account creation and risk decision event names as contracts. Schema version currently starts at 1.

Kafka message keys are aggregate UUIDs. Ordering is preserved by Kafka only within a partition for a given key; the deployment must use a stable partitioning strategy. This is not a global ordering guarantee.

## Transactional outbox and delivery

Financial services insert their outbox records in the same PostgreSQL transaction as financial state. Kafka is never contacted in that transaction. The dispatcher claims eligible rows with `FOR UPDATE SKIP LOCKED`, increments a per-event generation, stores a worker claim and finite lease, and commits before publishing. After broker acknowledgement, a conditional update marks the row published. The condition checks event ID, claimed status, worker, generation, and an unexpired lease using PostgreSQL time. Failure schedules exponential backoff with jitter or retains the row as dead letter after the configured attempt limit.

If a worker crashes before publication, the lease expires and another worker can reclaim it. If Kafka acknowledges and the worker crashes before PostgreSQL records publication, it may publish again. This is at-least-once delivery, not exactly-once processing. A fencing token prevents stale workers from changing database state; it cannot retract a message already sent to Kafka.

## Consumer inbox and offsets

The transaction audit consumer validates the envelope, inserts a unique `(consumer_name, event_id)` inbox row, and commits PostgreSQL before the Kafka adapter commits its offset. Redelivery is recognized by the unique inbox identity. Consumer names include versions; a new consumer identity such as `transaction-analytics-v2` may independently process historical events while the v1 inbox history remains intact.

## Administrative transport replay

An ADMIN may request republication with a reason and an idempotency key. The request is stored separately and attributed to the administrator. Transport republication sends the original event ID and unchanged payload/timestamp. Existing consumers suppress duplicates using their inbox row. Historical reprocessing is a separate consumer-version change; inbox rows are never deleted to force reprocessing.

Replay requests are processed asynchronously by the dispatcher and retain request status and failure details. Publication acknowledgement followed by a worker crash can produce another publication, consistent with at-least-once delivery.

## Configuration and verification status

The API starts without Kafka. Configure `FINGUARD_KAFKA_BOOTSTRAP_SERVERS`, `FINGUARD_KAFKA_CLIENT_ID`, and `FINGUARD_KAFKA_SECURITY_PROTOCOL` to run the Kafka publisher. The dispatcher command is `python -m app.events.dispatcher` with `backend` on the Python import path. The consumer adapter is designed to commit offsets only after `process_message` succeeds.

Docker Desktop is available and hosts the local Redis OSS service used by the feature projection. No Kafka broker was provisioned. PostgreSQL tests exercise claim, lease, database state, and inbox behavior; FakePublisher tests exercise adapter flow only. **Live Kafka verification: NOT RUN.** The publisher requires the configured Confluent Kafka client and a separately provisioned broker to verify delivery.
