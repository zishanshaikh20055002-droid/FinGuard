# FinGuard

FinGuard is a simulated financial transaction and security prototype. It does not connect to banks or move real money. The FastAPI modular monolith uses PostgreSQL 16 as its financial source of truth. Customer wallets, risk decisions, balanced immutable journals, idempotent transfers and reversals, and an unpublished transactional outbox are implemented. This phase adds users, sessions, ownership, role permissions, and authentication auditing.

## Local setup

Use Python 3.11+ and the existing project virtual environment, or create one with `python -m venv .venv`. Install the declared package with `.\.venv\Scripts\python.exe -m pip install -e '.[dev,features]'` to include the optional Redis client. Use a dedicated PostgreSQL database and a restricted application role. Keep database credentials in the local PostgreSQL passfile or an ignored environment file. Never commit secrets. See [PostgreSQL setup](docs/postgresql-setup.md).

Set `DATABASE_URL` to the database **owner** URL and apply migrations; then use the **restricted** role URL for the API:

~~~powershell
$env:DATABASE_URL = 'postgresql+psycopg://YOUR_OWNER_ROLE@127.0.0.1:5432/YOUR_DATABASE'
.\.venv\Scripts\python.exe -m alembic upgrade head
$env:DATABASE_URL = 'postgresql+psycopg://YOUR_RESTRICTED_ROLE@127.0.0.1:5432/YOUR_DATABASE'
$env:APP_ENV = 'development'
$env:LOCAL_INSECURE_COOKIE = 'true'
.\.venv\Scripts\python.exe -m uvicorn app.main:app --app-dir backend --reload
~~~

The expected migration head is `be2905957c4e`. `LOCAL_INSECURE_COOKIE=true` is for explicit local HTTP development only. Development uses a random process-local JWT secret if `JWT_SECRET` is absent; configure one through the environment when running multiple processes or preserving sessions across restarts. Production requires a strong `JWT_SECRET`, HTTPS refresh cookies, explicit HTTPS CORS origins, and trusted hosts. Invalid production settings fail application startup.

## Authenticated demo

Use a second PowerShell window with the same restricted `DATABASE_URL` and `APP_ENV=development`. Register two customers and log in. This snippet keeps access and CSRF tokens in PowerShell variables; browser clients should keep access tokens in memory and let the browser manage the HttpOnly refresh cookie.

~~~powershell
$base = 'http://127.0.0.1:8000/api/v1'
$aEmail = 'demo-a@example.test'
$bEmail = 'demo-b@example.test'
$aPassword = Read-Host 'Customer A password'
$bPassword = Read-Host 'Customer B password'
Invoke-RestMethod -Method Post -Uri "$base/auth/register" -ContentType 'application/json' -Body (@{email=$aEmail; password=$aPassword} | ConvertTo-Json)
Invoke-RestMethod -Method Post -Uri "$base/auth/register" -ContentType 'application/json' -Body (@{email=$bEmail; password=$bPassword} | ConvertTo-Json)
$aLogin = Invoke-RestMethod -Method Post -Uri "$base/auth/login" -SessionVariable aWeb -ContentType 'application/json' -Body (@{email=$aEmail; password=$aPassword} | ConvertTo-Json)
$bLogin = Invoke-RestMethod -Method Post -Uri "$base/auth/login" -SessionVariable bWeb -ContentType 'application/json' -Body (@{email=$bEmail; password=$bPassword} | ConvertTo-Json)
~~~

The seed requires two existing active customers for owned wallets. It creates separate, idempotent INR demo accounts for each owner pair and balanced opening journals. Existing unowned demo accounts remain unowned and are inaccessible to customers.

~~~powershell
$seed = .\.venv\Scripts\python.exe backend/scripts/seed_demo.py --confirm-demo-data --customer-a-email $aEmail --customer-b-email $bEmail | ConvertFrom-Json
$aHeaders = @{Authorization="Bearer $($aLogin.access_token)"}
$bHeaders = @{Authorization="Bearer $($bLogin.access_token)"}
Invoke-RestMethod "$base/accounts/$($seed.customer_a_account_id)/balance" -Headers $aHeaders
Invoke-RestMethod "$base/accounts/$($seed.customer_b_account_id)/balance" -Headers $bHeaders
$key = [guid]::NewGuid().ToString()
$transfer = @{source_account_id=$seed.customer_a_account_id; destination_account_id=$seed.customer_b_account_id; amount_minor=1000; currency='INR'; memo='demo transfer'} | ConvertTo-Json
$tx = Invoke-RestMethod -Method Post -Uri "$base/transactions" -Headers ($aHeaders + @{'Idempotency-Key'=$key}) -ContentType 'application/json' -Body $transfer
$tx
Invoke-RestMethod "$base/transactions/$($tx.transaction_id)" -Headers $aHeaders
Invoke-WebRequest -Method Post -Uri "$base/transactions" -Headers ($aHeaders + @{'Idempotency-Key'=$key}) -ContentType 'application/json' -Body $transfer | Select-Object StatusCode, Headers, Content
~~~

The replay has the stored response and `Idempotency-Replayed: true`. For a 409 conflict, send a changed amount with the **same** key. For insufficient funds, send 400000 INR minor units with a **new** key; it is below the default risk challenge threshold but above Customer A's initial balance. PowerShell 7's `Invoke-WebRequest -SkipHttpErrorCheck` displays 409 responses without throwing.

~~~powershell
$changed = @{source_account_id=$seed.customer_a_account_id; destination_account_id=$seed.customer_b_account_id; amount_minor=1001; currency='INR'} | ConvertTo-Json
Invoke-WebRequest -SkipHttpErrorCheck -Method Post -Uri "$base/transactions" -Headers ($aHeaders + @{'Idempotency-Key'=$key}) -ContentType 'application/json' -Body $changed | Select-Object StatusCode, Content
$tooLarge = @{source_account_id=$seed.customer_a_account_id; destination_account_id=$seed.customer_b_account_id; amount_minor=400000; currency='INR'} | ConvertTo-Json
Invoke-WebRequest -SkipHttpErrorCheck -Method Post -Uri "$base/transactions" -Headers ($aHeaders + @{'Idempotency-Key'=[guid]::NewGuid().ToString()}) -ContentType 'application/json' -Body $tooLarge | Select-Object StatusCode, Content
Invoke-RestMethod -Method Post -Uri "$base/transactions/$($tx.transaction_id)/reverse" -Headers ($aHeaders + @{'Idempotency-Key'=[guid]::NewGuid().ToString()})
Invoke-RestMethod "$base/accounts/$($seed.customer_a_account_id)/balance" -Headers $aHeaders
Invoke-RestMethod "$base/accounts/$($seed.customer_b_account_id)/balance" -Headers $bHeaders
~~~

Inspect the outbox in `psql` connected as the restricted role:

~~~sql
SELECT event_type, aggregate_id, payload, published_at, attempt_count
FROM outbox_events ORDER BY created_at DESC LIMIT 20;
~~~

Refresh uses `POST /auth/refresh` with the browser-managed refresh cookie and `X-CSRF-Token` from login. Logout uses `POST /auth/logout` with bearer access token, refresh cookie, and the same CSRF header. Both routes rotate or revoke the server-side session immediately.

## Administrative provisioning and tests

Create the first administrator locally, using a restricted role with user-table write access. Password input is interactive and never accepted as a command argument:

~~~powershell
.\.venv\Scripts\python.exe backend/scripts/create_admin.py --email admin@example.test --confirm-admin
~~~

The script refuses to promote an existing non-admin. For a deliberate password rotation of an existing admin, add `--rotate-existing-password`.

Run tests with `FINGUARD_TEST_DATABASE_URL` set to restricted `fg_test_runner` on `finguard_test`, and `FINGUARD_MIGRATION_TEST_DATABASE_URL` set to owner `fg_migration_owner` on the separate empty `finguard_migration_test`. Tests commit isolated sample data. The migration test downgrades only the migration database.

~~~powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
~~~

See [security](docs/security.md), [API contract](docs/api-contract.md), [architecture](docs/architecture.md), and [financial invariants](docs/invariants.md). This is a security-oriented prototype, not a certified banking security system.

## Event delivery

Committed transaction events are stored in PostgreSQL outbox rows. Run the independent publisher with `python -m app.events.dispatcher` from the backend import environment. It requires `DATABASE_URL`, `FINGUARD_KAFKA_BOOTSTRAP_SERVERS`, `FINGUARD_KAFKA_CLIENT_ID`, and `FINGUARD_KAFKA_SECURITY_PROTOCOL`; the API itself does not connect to Kafka. Consumers commit inbox changes before committing Kafka offsets. Delivery is at least once, with inbox deduplication by consumer name and event ID. Configure a new consumer version to reprocess history without deleting prior inbox rows.

Administrators can request transport republication with `POST /api/v1/admin/events/{event_id}/replay`, JSON `{ "reason": "investigation reference", "idempotency_key": "unique-request-key" }`. The replay worker republishes the original event identity and payload; existing consumers suppress duplicates. Run a single deployment worker process to dispatch both outbox events and replay requests. PostgreSQL verifies lease expiry and generations; FakePublisher tests do not verify broker delivery. Docker is unavailable in this environment. Live Kafka verification: **NOT RUN**.

## Behavioural feature projection

The optional Redis projection consumes committed `transaction.posted` events using the independent `python -m app.features.consumer` worker (run with `backend` on `PYTHONPATH`). The API and financial transaction engine do not require Redis. Set `FINGUARD_REDIS_URL` to a local Redis URL; development should bind Redis to loopback and use a least-privilege ACL user through the environment. Kafka settings use `FINGUARD_KAFKA_BOOTSTRAP_SERVERS`, `FINGUARD_KAFKA_CLIENT_ID`, and `FINGUARD_KAFKA_SECURITY_PROTOCOL`.

Only transfer amount, currency, event time, source account, and destination account are available. No merchant, device, or IP signals exist in the current transaction contract. Features are currency-specific, use `[as_of - window, as_of)` UTC intervals, and keep 14 days of event detail per active account projection. Online scoring timestamps are supported for the latest 7 days. Event-ID fingerprints are retained for the lifetime of each projection because the PostgreSQL outbox has no enforced deletion horizon. Redis Cluster is unsupported because the atomic Lua update touches both account projections and shared deduplication state.

Analysts and administrators can read `GET /api/v1/features/accounts/{account_id}`, `/history`, and `/api/v1/features/health`; customers cannot read internal features. Administrators start an isolated rebuild with `POST /api/v1/features/rebuild/{version}`, inspect it with `GET` on the same path, then activate only after parity comparison using `POST /api/v1/features/projections/{version}/activate`. Rebuilding uses a new namespace and dual-writes live events; after interruption an administrator can request the same version again. Loss of Redis projection data requires a new version and controlled historical rebuild because acknowledged Kafka offsets and PostgreSQL inbox rows do not reconstruct Redis automatically.

Redis is unavailable in this environment, so real Redis integration is **NOT RUN**. The deterministic fake-store and PostgreSQL tests do not prove Redis Lua execution or server behavior. Live Kafka remains **NOT RUN**.
