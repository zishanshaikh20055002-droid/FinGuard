# API contract

Base path: `/api/v1`. Except registration, login, refresh, and health checks, requests require `Authorization: Bearer <access token>`. Amounts are positive integer minor units; supported currencies are USD, EUR, GBP, INR. Error bodies use `{"error":{"code":"...","message":"..."}}`; validation errors use 422. Responses include a correlation ID and hardening headers.

| Method and path | Access and result |
| --- | --- |
| `POST /auth/register` | Public; email/password; 201 safe customer profile; duplicate email 409 |
| `POST /auth/login` | Public; email/password; access token, CSRF token, safe user, HttpOnly refresh cookie |
| `POST /auth/refresh` | Refresh cookie plus `X-CSRF-Token`; rotated cookie and new access token |
| `POST /auth/logout` | Bearer, refresh cookie, CSRF header; revoke session and clear cookie |
| `GET /auth/me` | Authenticated current safe profile |
| `POST /accounts` | Customer only; `{"currency":"INR"}`; creates owned zero-balance wallet |
| `GET /accounts/{id}`, `GET /accounts/{id}/balance` | Owner, analyst, admin; unrelated customer gets 404 |
| `POST /transactions` | Customer only; owned source, eligible destination, required `Idempotency-Key` |
| `GET /transactions/{id}`, `GET /transactions?limit=50&offset=0` | Customer involved in the transaction, analyst, or admin |
| `POST /transactions/{id}/reverse` | Original source owner only, required new `Idempotency-Key` |
| `GET /investigations/audit` | Analyst or admin, recent safe audit metadata |
| `PATCH /admin/users/{id}` | Admin; optional `role` and/or `status`; revokes target sessions |
| `GET /admin/users/{id}/sessions` | Admin; safe session metadata |
| `GET /health/live`, `GET /health/ready` | Public process/database probes |

Transfer JSON: `{"source_account_id":"<uuid>","destination_account_id":"<uuid>","amount_minor":1000,"currency":"INR","memo":"optional"}`. Clients cannot supply owner, role, risk decision, status, score, journal ID, or balance. A successful posting, blocked decision, or challenge returns 201. An insufficient-balance attempt commits a failed transaction and returns 409. The development risk provider challenges at 500000 minor units and blocks at 1000000 by default; it has no challenge approval endpoint.

Idempotency keys are scoped to operation and authenticated customer. Same-key/same-request returns the exact stored status and JSON body with `Idempotency-Replayed: true`; a changed request returns 409. Authorization is checked before any replay. Reversal creates a linked mirrored journal while the original transaction remains posted; retrieval reports `reversed` separately. No API permits editing posted journal rows.

| `GET /admin/events/dead-letter` | Admin; inspect retained outbox dead letters |
| `POST /admin/events/{event_id}/replay` | Admin; requires `reason` and `idempotency_key`; creates an asynchronous transport republication request |
| `GET /features/accounts/{account_id}` | Analyst/admin; currency-specific online features, optional `as_of_timestamp` query |
| `GET /features/accounts/{account_id}/history?limit=100` | Analyst/admin; point-in-time feature rows reconstructed from posted outbox events and journal-backed transactions |
| `GET /features/health` | Analyst/admin; Redis availability, projection freshness, counters, and rebuild state |
| `POST /features/rebuild/{version}` | Admin; asynchronously rebuild an isolated non-active projection namespace |
| `GET /features/rebuild/{version}` | Admin; inspect rebuild status/progress |
| `POST /features/projections/{version}/activate` | Admin; compare candidate values against active projection (or PostgreSQL history after Redis loss), then explicitly activate on parity |

Event replay republishes the unchanged original event envelope. Repeating the same administrator/key/request returns the existing replay request; changing the event or reason with that key returns 409. Processing is asynchronous in the dispatcher. Consumer inbox deduplication means replay does not repeat effects for consumers that already processed the event; use a new consumer identity/version for historical reprocessing.

Feature routes are internal and deny customer access. Redis is optional for API startup; feature retrieval reports unavailable/degraded state when it is not configured or reachable. Online event detail supports as-of timestamps within the latest seven days. Historical retrieval uses the PostgreSQL posted-event/outbox and posted-journal join. Feature values contain behavioral aggregates and do not expose model risk decisions.
