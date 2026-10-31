# Local Redis feature store

FinGuard uses Redis only for the online behavioural feature projection. PostgreSQL remains the financial source of truth; Redis does not authorize or post a financial transaction. Kafka is not provisioned by this setup.

## Version and network boundary

The development service uses the official `redis:8.6.8-alpine3.23` image. Redis 8.6.8 supports the adapter's ACL, scripting, sorted-set, hash, set, and persistence commands, and includes the upstream AOF transaction replay ACL fix. The corresponding backport is not yet available in the Redis 7.4 branch, so 8.6.8 is selected for correct AOF recovery with a disabled default user. The tag is pinned; do not replace it with `latest`. See the [Redis 8.6.8 release](https://github.com/redis/redis/releases/tag/8.6.8), [upstream AOF ACL fix](https://github.com/redis/redis/pull/14545), and [official Redis image tags](https://hub.docker.com/_/redis/tags).

The Compose service is named `redis`. It publishes only `127.0.0.1:6379`, uses a dedicated Compose bridge network, mounts the ACL file read-only, and stores AOF data in the named `infra_redis_data` volume. AOF is enabled with `appendfsync everysec`. Redis Cluster is unsupported: the projection Lua scripts update multiple account keys and shared deduplication keys atomically on one standalone Redis server.

## Generate local credentials

From the repository root, generate a URL-safe random password and a SHA-256 ACL hash without printing either value:

~~~powershell
.\infra\scripts\provision-redis-secrets.ps1
~~~

The script writes `FINGUARD_REDIS_URL` to the ignored root `.env` and writes `infra/secrets/redis-users.acl`. The ACL file contains only a Redis SHA-256 password hash. Both files and their containing secret directory are restricted to the current Windows account, SYSTEM, and local administrators. Git ignores both. The script refuses to overwrite existing credentials; use `-Rotate` only when deliberately rotating the URL and ACL together, then run `docker restart infra-redis-1` immediately.

The default Redis user is disabled. `finguard_app` is restricted to `~finguard:features:*` and to the exact commands used by the adapter: `PING`, `CLIENT SETINFO`, `TYPE`, `HGET`, `HINCRBY`, `ZADD`, `HSET`, `SADD`, `TIME`, `EXPIRE`, `ZRANGEBYSCORE`, `ZREM`, `HDEL`, `GET`, `SET`, `SREM`, `SMEMBERS`, `EXISTS`, `HGETALL`, `HLEN`, `MULTI`, `EXEC`, `EVALSHA`, and `SCRIPT LOAD`. Administrative commands, `SCAN`, and `DEL` are not granted. Redis integration tests use short-lived unique namespaces and expire their test indexes; account projection rows use the adapter's existing 14-day retention.

The host application reads the existing `FINGUARD_REDIS_URL` setting from `.env`; no new application setting is required. Do not copy its value into a terminal command, issue, log, or tracked file.

## Start and stop Redis

If Docker Desktop's CLI is not on the current PowerShell `PATH`, add its per-user binary directory for this shell only:

~~~powershell
$dockerBin = Join-Path $env:LOCALAPPDATA 'Programs\DockerDesktop\resources\bin'
$env:PATH = "$dockerBin;$env:PATH"
~~~

The existing Compose file requires PostgreSQL interpolation values even when only Redis is selected. When those values are not already in the local environment, use temporary non-secret values for the Redis-only command below. `up redis` starts only Redis; it does not start the PostgreSQL container or alter the native PostgreSQL service.

~~~powershell
$env:POSTGRES_DB = 'unused-redis-only'
$env:POSTGRES_USER = 'unused-redis-only'
$env:POSTGRES_PASSWORD = 'unused-redis-only'
docker compose -f infra/compose.yaml up -d redis
Remove-Item Env:POSTGRES_DB, Env:POSTGRES_USER, Env:POSTGRES_PASSWORD
~~~

Verify the host binding and server build without displaying credentials:

~~~powershell
docker ps --filter 'name=infra-redis-1'
docker port infra-redis-1
docker exec infra-redis-1 redis-server --version
~~~

The expected host mapping is `127.0.0.1:6379`. For application-level authentication and ACL verification, run the real Redis integration tests below. They read the URL from the local environment; expose it to the test process without echoing it:

~~~powershell
$redisSetting = Get-Content .env | Where-Object { $_ -match '^FINGUARD_REDIS_URL=' }
$env:FINGUARD_REDIS_URL = $redisSetting.Substring('FINGUARD_REDIS_URL='.Length)
$env:PYTHONPATH = 'backend'
.\.venv\Scripts\python.exe -m pytest -q backend/tests/integration/test_feature_redis.py
Remove-Item Env:FINGUARD_REDIS_URL
~~~

Stop or restart only the Redis service and retain its volume:

~~~powershell
docker stop infra-redis-1
docker start infra-redis-1
docker restart infra-redis-1
~~~

Do not use `docker compose down -v` or remove `infra_redis_data`. Restart acceptance was verified against the actual container and named volume: an applied event, its two account projections, and its dedup fingerprint survived container restart; redelivery was suppressed.

## Failure, replay, and rebuild

The API can run without Redis; feature health reports a degraded/unavailable feature store. Financial transaction processing and PostgreSQL ledger posting remain independent. The feature worker requires Redis at startup. During runtime processing, it applies to Redis before inserting the `fraud-features-v1` PostgreSQL inbox row and commits only after processing succeeds. An inbox commit failure is recoverable by redelivery because the Redis event ID and content fingerprint are idempotent. Kafka offsets are committed only after the consumer returns successfully; no live Kafka broker or offset behavior was tested in this task.

For Redis projection loss, keep PostgreSQL history intact and build a new candidate namespace using `POST /api/v1/features/rebuild/{version}` as an ADMIN. New events are dual-written to the active and building namespaces. Check progress with `GET` on the rebuild path, then activate through `POST /api/v1/features/projections/{version}/activate`; activation compares features and falls back to committed PostgreSQL history if the active Redis namespace is missing or stale. A failed parity check blocks activation. Do not rebuild into the active namespace.

Redis AOF with `appendfsync everysec` provides local container-restart persistence; a host crash or storage failure may still lose roughly the latest second of writes. The restart check covers a clean container restart, not host or power-loss durability. Redis is a rebuildable projection, and committed PostgreSQL history remains authoritative. The current standalone configuration has no Redis Cluster support, cross-host failover, TLS, or managed-secret integration; it is for a loopback-only development prototype.
