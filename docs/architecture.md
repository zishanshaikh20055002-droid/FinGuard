# Architecture

FinGuard is a modular monolith: one FastAPI application and one PostgreSQL transactional store. API routes validate transport data; account and transaction services coordinate work; the risk provider returns a versioned decision; ledger code writes balanced journals; the outbox module writes durable event rows. SQLAlchemy sessions define transaction boundaries. PostgreSQL remains authoritative.

The `auth` package owns password hashing, JWT validation, refresh rotation, sessions, audit, permissions, and protected routes. Authentication dependencies use an independent read session so application services can begin their own explicit write transaction. Protected requests validate a current database user and session, then route and service permissions apply. The account owner foreign key is nullable solely to preserve legacy and system accounts; public wallet creation always sets it. See [security](security.md).

## Transfer path

1. Validate the request and claim its operation-scoped idempotency key with PostgreSQL `INSERT ... ON CONFLICT DO NOTHING`. An identical committed request returns the stored status and body.
   The source-account ownership check occurs **before** the claim or any replay, and the operation scope includes the authenticated user ID.
2. Lock both account rows in UUID order with `SELECT FOR UPDATE`, then verify eligibility, status, and currency.
3. Insert the transaction and ordered audit transitions; record the development risk decision and reason codes.
4. For `allow`, calculate source balance from posted journal lines while holding the account locks. On sufficient funds, insert a draft entry and two lines, then mark the entry and transaction posted. On insufficient funds, commit a failed transaction with no journal.
5. Insert the corresponding outbox event and final idempotency response in the same PostgreSQL commit. The existing deferred triggers validate posted journal balance and reversal integrity at commit.

Blocked and challenged decisions commit their decision, audit, and outbox event without a journal. The demo seed is an internal opening-funding flow using a settlement asset and skips the public risk lifecycle; it still posts balanced journals and outbox events atomically.

Reversal locks the original transaction and entry, then both wallets in UUID order, checks that the recipient can cover the reversal, inserts a new mirrored journal, and records an outbox event and idempotent response. The original transaction and journal remain unchanged. A transaction's `posted` status is historical; retrieval derives `reversed` from the reversal entry.

The application assumes PostgreSQL `READ COMMITTED` isolation. Account-row locks serialize balance checks for transfers between these wallet accounts. Deadlock or serialization errors roll back the whole request, including its idempotency claim; clients should retry the **same** key with bounded backoff. There is no automatic retry in the HTTP process yet. Journal triggers provide a final database integrity guard. Direct SQL writers bypassing the service could still violate the wallet overdraft policy.

Outbox publication, reconciliation, model serving, graph analysis, and monitoring are future stages. Outbox consumers must tolerate replay and deduplicate by event ID. The current risk provider is deterministic development logic, not a trained model; production transaction intake is disabled until a production provider exists.

## Event delivery

The outbox worker is separate from financial request handling. It claims rows with PostgreSQL `SKIP LOCKED`, increments each event's generation, commits its lease, and only then publishes. All claim-dependent state updates require the same worker and generation and a live lease checked using PostgreSQL clock time. Stale updates affect zero rows. Publication is at least once. The transaction audit consumer stores a `(consumer_name, event_id)` inbox record before offset acknowledgement; consumer versions have separate inbox scopes.

ADMIN replay requests are durable, attributed, reasoned, and idempotent. The replay worker republishes the original immutable event envelope. Existing consumers deduplicate it; intentional historical processing requires a new consumer identity/version. See [event architecture](event-architecture.md). Kafka broker delivery remains unverified in this Docker-free environment.

## Behavioural features

Feature projection is an independent consumer and analytical read path. It validates posted events against the posted PostgreSQL transaction, applies the event to Redis first, then commits the `fraud-features-v1` PostgreSQL inbox row before Kafka acknowledgement. If Redis succeeds and PostgreSQL fails, replay is safe because Redis stores an event-ID/content fingerprint. If Redis is lost after offsets and inbox rows commit, build a new namespace from retained PostgreSQL posted outbox events joined to posted journal-backed transactions; inbox rows are not used as the rebuild source.

The calculator uses the centralized versioned feature registry, UTC event time, and `[t - window, t)` boundaries. Amounts remain integer minor units and are isolated by account currency; average outgoing amount and incoming-plus-outgoing population variance are analytics-only floating-point values. Redis holds account/currency sorted sets with direction-explicit metadata hashes for a bounded 14-day scoring history, versioned hash snapshots for calculated features, and a projection-lifetime dedup hash. Outbox rows currently have no enforced purge horizon, so dedup IDs cannot safely expire while replay remains supported. Redis Cluster is unsupported because one standalone Lua operation updates both account keys plus shared dedup and metrics keys.

Rebuild requests and activation are ADMIN-only. A building namespace receives live events while historical events are keyset-replayed in deterministic event-time/ID order. Interrupted rebuilds can be restarted by requesting the same version; event dedup makes replay idempotent. Activation compares all retained account/currency feature vectors against the active version before atomically changing the active-version pointer. If Redis loss left the active namespace empty or stale, activation validates the candidate against committed PostgreSQL history instead. No Redis credentials, merchant IDs, device fingerprints, or IP addresses are stored in feature keys.

The current transaction contract has no merchant, device, or IP signals. Redis is optional to financial transaction processing; an unavailable feature consumer retries the same Kafka message and holds its offset, leaving committed outbox rows available for recovery.
