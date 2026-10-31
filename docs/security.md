# Security architecture

FinGuard is a security-oriented prototype, not a certified banking security system. The application is a modular monolith using PostgreSQL for users, sessions, token hashes, audit rows, account ownership, and financial records.

## Credentials and sessions

Registration creates an active `customer` only. Email is stripped and case-folded, then protected by a database unique constraint. Passwords require 12–128 characters and are hashed with Argon2id through argon2-cffi. Time, memory, and parallelism costs are configured with `ARGON2_TIME_COST`, `ARGON2_MEMORY_COST`, and `ARGON2_PARALLELISM`. Hashes and raw credentials are never returned or intentionally logged. Login failure responses are generic. A database-backed per-email-hash limit returns 429 after ten failures in fifteen minutes; it is a bounded abuse control, not a production WAF.

Each login creates a revocable PostgreSQL session. A signed HS256 JWT access token has `sub`, `sid`, `jti`, `iss`, `aud`, `iat`, and `exp`; default life is ten minutes. Every protected request validates the signature and claims, then reads the current session and user from PostgreSQL. Current database role and status govern permissions; JWT role claims are not used. Logout, account disablement, and role changes revoke sessions immediately.

The refresh token is a cryptographically random opaque value. Only its SHA-256 hash is stored in `auth_refresh_tokens`. The raw token exists in an HttpOnly, SameSite=Lax cookie scoped to `/api/v1/auth`; it is Secure by default and may be insecure only with `LOCAL_INSECURE_COOKIE=true` in development. It is never included in JSON or localStorage. Refresh rotates tokens in a database transaction under a session lock. Use of a consumed token revokes the entire session, including newer access tokens. Expired sessions and disabled users cannot refresh.

Login returns a separate random CSRF token in JSON; only its SHA-256 hash is stored with the session. The client sends it in `X-CSRF-Token` on refresh and logout. The header is compared with the session hash in constant time. A browser frontend must keep the access and CSRF tokens in memory, send bearer access tokens on protected requests, include credentials only for the auth cookie routes, replace access tokens after refresh, and clear client state after logout or refresh failure. CORS origins must be explicitly configured; wildcard origins are rejected. Trusted hosts and response hardening headers are configured. Never cache auth responses.

## Permissions

| Role | Permitted actions |
| --- | --- |
| Customer | Create owned wallets; read owned wallets and balances; transfer from owned source wallets; read transactions involving owned wallets; reverse own posted transfers |
| Analyst | Read platform accounts, balances, transactions, risk details, and investigative audit data; no financial writes |
| Admin | Analyst reads, plus user role/status changes and session inspection; no financial writes |

Read attempts against unrelated customer accounts or transactions return 404. Transfer and reversal ownership checks run before idempotency replay. The idempotency operation includes the authenticated user ID. Existing unowned wallets, including earlier demo wallets, remain unowned; customers cannot adopt them. The owned demo seed creates new wallets for explicitly named registered customers. Settlement assets remain system-controlled.

Administrative provisioning uses `backend/scripts/create_admin.py` with interactive getpass input and an explicit flag. It refuses to promote an existing non-admin and requires a separate flag to rotate an existing admin password. Public registration cannot set a role. Admin changes revoke target sessions and write audit events atomically.

## Audit and configuration

`auth_audit_events` records registration, login success/failure, refresh rotation/reuse, logout, administrative changes, and session revocation without credentials or tokens. Audit writes are part of the same transaction as successful security operations. Login failures commit their audit rows before the generic error is returned. The login throttle stores a SHA-256 normalized-email digest, not the email itself.

Production startup rejects absent or weak placeholder-like JWT secrets, insecure cookies, wildcard/non-HTTPS CORS origins, and wildcard trusted hosts. Supply secrets through environment or a secret manager, never Git. Development generates an ephemeral signing secret if none is supplied, so tokens do not survive process restarts and multiple workers require a shared configured secret.

Not yet implemented: MFA, email verification, password reset, external identity providers, production WAF, distributed edge rate limiting, formal penetration testing, or credential breach screening. The database-backed login throttle is per normalized email and does not replace IP-aware defenses. The demo risk provider is not an ML fraud model. Outbox dispatch is not implemented.
