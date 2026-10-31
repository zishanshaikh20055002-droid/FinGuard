"""Real PostgreSQL authentication, ownership, and authorization tests."""

import hashlib
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import jwt
import pytest
from backend.scripts import create_admin
from backend.scripts.seed_demo import seed
from fastapi.testclient import TestClient
from sqlalchemy import Engine, insert, select, update
from sqlalchemy.orm import Session

from app.auth.dependencies import get_auth_db_session
from app.auth.security import hash_password
from app.core.config import Settings, get_settings
from app.db.session import get_db_session
from app.ledger.posting import post_transfer
from app.main import app
from app.models import Account, AuthAuditEvent, RefreshToken, Transaction, User

pytestmark = pytest.mark.integration
PASSWORD = "Correct Battery Horse 123!"


@pytest.fixture
def security_client(pg_engine: Engine):
    settings = Settings(
        _env_file=None,
        app_env="development",
        database_url="postgresql+psycopg://unused@localhost/test",
        jwt_secret="test-only-secret-" + str(uuid4()) + str(uuid4()),
        local_insecure_cookie=True,
    )

    def db_session():
        with Session(pg_engine, expire_on_commit=False) as session:
            yield session

    app.dependency_overrides[get_db_session] = db_session
    app.dependency_overrides[get_auth_db_session] = db_session
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        with TestClient(app) as client:
            yield client, settings
    finally:
        app.dependency_overrides.clear()


def register(client: TestClient, email: str) -> dict:
    response = client.post("/api/v1/auth/register", json={"email": email, "password": PASSWORD})
    assert response.status_code == 201, response.text
    return response.json()


def login(client: TestClient, email: str, password: str = PASSWORD) -> dict:
    response = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return response.json()


def headers(auth: dict) -> dict:
    return {"Authorization": f"Bearer {auth['access_token']}"}


def user_with_role(engine: Engine, role: str) -> tuple[str, UUID]:
    user_id = uuid4()
    email = f"{role}-{user_id}@example.test"
    settings = Settings(_env_file=None)
    with engine.begin() as connection:
        connection.execute(
            insert(User).values(
                id=user_id,
                email=email,
                password_hash=hash_password(PASSWORD, settings),
                role=role,
                status="active",
            )
        )
    return email, user_id


def test_registration_hash_login_and_generic_failures(security_client, pg_engine: Engine) -> None:
    client, _ = security_client
    email = f"  PERSON-{uuid4()}@Example.Test  "
    user = register(client, email)
    assert user["role"] == "customer" and user["status"] == "active"
    assert "password_hash" not in user
    assert user["email"] == email.strip().casefold()
    assert (
        client.post(
            "/api/v1/auth/register", json={"email": email.strip().lower(), "password": PASSWORD}
        ).status_code
        == 409
    )
    assert (
        client.post(
            "/api/v1/auth/register",
            json={"email": f"other-{uuid4()}@example.test", "password": PASSWORD, "role": "admin"},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/v1/auth/register",
            json={"email": f"other-{uuid4()}@example.test", "password": "short"},
        ).status_code
        == 422
    )
    with pg_engine.connect() as connection:
        stored = connection.scalar(
            select(User.password_hash).where(User.id == UUID(user["user_id"]))
        )
    assert stored.startswith("$argon2id$") and PASSWORD not in stored
    wrong = client.post("/api/v1/auth/login", json={"email": user["email"], "password": "wrong"})
    missing = client.post(
        "/api/v1/auth/login", json={"email": f"missing-{uuid4()}@example.test", "password": "wrong"}
    )
    assert wrong.status_code == missing.status_code == 401
    assert wrong.json() == missing.json()
    auth = login(client, user["email"])
    assert client.get("/api/v1/auth/me", headers=headers(auth)).json()["user_id"] == user["user_id"]
    assert "fg_refresh" not in auth
    assert client.cookies.get("fg_refresh")
    assert (
        "HttpOnly"
        in client.post(
            "/api/v1/auth/login", json={"email": user["email"], "password": PASSWORD}
        ).headers["set-cookie"]
    )


def test_access_token_validation_and_disabled_user(security_client, pg_engine: Engine) -> None:
    client, settings = security_client
    user = register(client, f"jwt-{uuid4()}@example.test")
    auth = login(client, user["email"])
    base = jwt.decode(
        auth["access_token"],
        settings.jwt_secret,
        algorithms=["HS256"],
        audience=settings.jwt_audience,
        issuer=settings.jwt_issuer,
    )
    for changes in (
        {"exp": datetime.now(UTC) - timedelta(seconds=1)},
        {"iss": "wrong"},
        {"aud": "wrong"},
    ):
        token = jwt.encode({**base, **changes}, settings.jwt_secret, algorithm="HS256")
        assert (
            client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code
            == 401
        )
    bad = jwt.encode(base, "different-signing-key-0123456789abcdef", algorithm="HS256")
    assert (
        client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {bad}"}).status_code == 401
    )
    with pg_engine.begin() as connection:
        connection.execute(
            update(User).where(User.id == UUID(user["user_id"])).values(status="disabled")
        )
    assert client.get("/api/v1/auth/me", headers=headers(auth)).status_code == 401
    assert (
        client.post(
            "/api/v1/auth/login", json={"email": user["email"], "password": PASSWORD}
        ).status_code
        == 401
    )


def test_refresh_rotation_reuse_revocation_and_csrf(security_client, pg_engine: Engine) -> None:
    client, _ = security_client
    user = register(client, f"rotate-{uuid4()}@example.test")
    auth = login(client, user["email"])
    old_cookie = client.cookies.get("fg_refresh")
    assert client.post("/api/v1/auth/refresh", headers={"X-CSRF-Token": "wrong"}).status_code == 403
    response = client.post("/api/v1/auth/refresh", headers={"X-CSRF-Token": auth["csrf_token"]})
    assert response.status_code == 200
    rotated = response.json()
    new_cookie = client.cookies.get("fg_refresh")
    assert new_cookie != old_cookie
    with pg_engine.connect() as connection:
        rows = connection.scalars(
            select(RefreshToken.token_hash).where(
                RefreshToken.session_id
                == UUID(
                    jwt.decode(auth["access_token"], options={"verify_signature": False})["sid"]
                )
            )
        ).all()
    assert old_cookie not in rows and new_cookie not in rows and len(rows) == 2
    assert client.get("/api/v1/auth/me", headers=headers(rotated)).status_code == 200
    client.cookies.set("fg_refresh", old_cookie, path="/api/v1/auth")
    reused = client.post("/api/v1/auth/refresh", headers={"X-CSRF-Token": auth["csrf_token"]})
    assert reused.status_code == 401 and reused.json()["error"]["code"] == "refresh_reuse"
    assert client.get("/api/v1/auth/me", headers=headers(rotated)).status_code == 401
    with pg_engine.connect() as connection:
        assert (
            connection.scalar(
                select(AuthAuditEvent.id).where(
                    AuthAuditEvent.event_type == "refresh.reuse_detected",
                    AuthAuditEvent.user_id == UUID(user["user_id"]),
                )
            )
            is not None
        )


def test_concurrent_refresh_reuse_revokes_session(security_client) -> None:
    client, _ = security_client
    user = register(client, f"parallel-refresh-{uuid4()}@example.test")
    auth = login(client, user["email"])
    cookie = client.cookies.get("fg_refresh")
    request_headers = {"X-CSRF-Token": auth["csrf_token"], "Cookie": f"fg_refresh={cookie}"}
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(client.post, "/api/v1/auth/refresh", headers=request_headers)
            for _ in range(2)
        ]
        responses = [future.result(timeout=10) for future in futures]
    assert sorted(response.status_code for response in responses) == [200, 401]
    assert any(
        response.json().get("error", {}).get("code") == "refresh_reuse" for response in responses
    )
    issued = next(response.json() for response in responses if response.status_code == 200)
    assert client.get("/api/v1/auth/me", headers=headers(issued)).status_code == 401


def test_logout_and_cookie_attributes(security_client) -> None:
    client, _ = security_client
    user = register(client, f"logout-{uuid4()}@example.test")
    response = client.post(
        "/api/v1/auth/login", json={"email": user["email"], "password": PASSWORD}
    )
    auth = response.json()
    cookie = response.headers["set-cookie"]
    assert "httponly" in cookie.lower() and "samesite=lax" in cookie.lower()
    assert "path=/api/v1/auth" in cookie.lower() and "secure" not in cookie.lower()
    assert (
        client.post(
            "/api/v1/auth/logout", headers={**headers(auth), "X-CSRF-Token": "bad"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/v1/auth/logout", headers={**headers(auth), "X-CSRF-Token": auth["csrf_token"]}
        ).status_code
        == 200
    )
    assert client.get("/api/v1/auth/me", headers=headers(auth)).status_code == 401


def test_database_backed_login_throttle_is_bounded(security_client, pg_engine: Engine) -> None:
    client, _ = security_client
    email = f"throttle-{uuid4()}@example.test"
    register(client, email)
    digest = hashlib.sha256(email.encode()).hexdigest()
    with pg_engine.begin() as connection:
        connection.execute(
            insert(AuthAuditEvent),
            [
                {"id": uuid4(), "event_type": "login.failed", "subject_hash": digest}
                for _ in range(10)
            ],
        )
    response = client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 429
    assert response.json()["error"]["message"] == "Invalid credentials"


def test_ownership_roles_and_replay_isolation(security_client, pg_engine: Engine) -> None:
    client, _ = security_client
    first = register(client, f"first-{uuid4()}@example.test")
    second = register(client, f"second-{uuid4()}@example.test")
    first_auth = login(client, first["email"])
    second_auth = login(client, second["email"])
    assert client.get("/api/v1/auth/me").status_code == 401
    assert client.get("/api/v1/accounts/" + str(uuid4())).status_code == 401
    account_a = client.post(
        "/api/v1/accounts", json={"currency": "INR"}, headers=headers(first_auth)
    ).json()
    account_b = client.post(
        "/api/v1/accounts", json={"currency": "INR"}, headers=headers(second_auth)
    ).json()
    assert (
        client.post(
            "/api/v1/accounts",
            json={"currency": "INR", "owner_user_id": second["user_id"]},
            headers=headers(first_auth),
        ).status_code
        == 422
    )
    assert (
        client.get(
            f"/api/v1/accounts/{account_a['account_id']}", headers=headers(second_auth)
        ).status_code
        == 404
    )
    assert (
        client.get(
            f"/api/v1/accounts/{account_a['account_id']}/balance", headers=headers(second_auth)
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/api/v1/transactions",
            headers={**headers(second_auth), "Idempotency-Key": str(uuid4())},
            json={
                "source_account_id": account_a["account_id"],
                "destination_account_id": account_b["account_id"],
                "amount_minor": 100,
                "currency": "INR",
            },
        ).status_code
        == 404
    )
    analyst_email, _ = user_with_role(pg_engine, "analyst")
    admin_email, admin_id = user_with_role(pg_engine, "admin")
    analyst = login(client, analyst_email)
    admin = login(client, admin_email)
    assert (
        client.get(
            f"/api/v1/accounts/{account_a['account_id']}", headers=headers(analyst)
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/v1/accounts", json={"currency": "INR"}, headers=headers(analyst)
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/v1/transactions",
            headers={**headers(analyst), "Idempotency-Key": str(uuid4())},
            json={
                "source_account_id": account_a["account_id"],
                "destination_account_id": account_b["account_id"],
                "amount_minor": 100,
                "currency": "INR",
            },
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/v1/transactions",
            headers={**headers(admin), "Idempotency-Key": str(uuid4())},
            json={
                "source_account_id": account_a["account_id"],
                "destination_account_id": account_b["account_id"],
                "amount_minor": 100,
                "currency": "INR",
            },
        ).status_code
        == 403
    )
    assert client.get("/api/v1/investigations/audit", headers=headers(analyst)).status_code == 200
    assert (
        client.get(
            f"/api/v1/admin/users/{admin_id}/sessions", headers=headers(first_auth)
        ).status_code
        == 403
    )
    assert (
        client.get(f"/api/v1/admin/users/{admin_id}/sessions", headers=headers(admin)).status_code
        == 200
    )
    assert (
        client.patch(
            f"/api/v1/admin/users/{first['user_id']}",
            headers=headers(admin),
            json={"status": "disabled"},
        ).status_code
        == 200
    )
    assert client.get("/api/v1/auth/me", headers=headers(first_auth)).status_code == 401


def test_authorized_transfer_replay_and_reversal_hide_from_other_customer(
    security_client, pg_engine: Engine
) -> None:
    client, _ = security_client
    alice = register(client, f"alice-{uuid4()}@example.test")
    bob = register(client, f"bob-{uuid4()}@example.test")
    eve = register(client, f"eve-{uuid4()}@example.test")
    alice_auth, bob_auth, eve_auth = (login(client, user["email"]) for user in (alice, bob, eve))
    source = client.post(
        "/api/v1/accounts", json={"currency": "INR"}, headers=headers(alice_auth)
    ).json()["account_id"]
    destination = client.post(
        "/api/v1/accounts", json={"currency": "INR"}, headers=headers(bob_auth)
    ).json()["account_id"]
    settlement_id, opening_id = uuid4(), uuid4()
    with Session(pg_engine, expire_on_commit=False) as session, session.begin():
        session.add(
            Account(
                id=settlement_id,
                reference=f"SECURITY-SETTLEMENT-{settlement_id}",
                currency="INR",
                account_type="asset",
                status="open",
            )
        )
        session.flush()
        session.add(
            Transaction(
                id=opening_id,
                source_account_id=settlement_id,
                destination_account_id=UUID(source),
                amount_minor=2000,
                currency="INR",
                status="posted",
                memo="security test opening",
            )
        )
        session.flush()
        post_transfer(
            session,
            transaction_id=opening_id,
            source_id=settlement_id,
            destination_id=UUID(source),
            amount_minor=2000,
            currency="INR",
        )
    key = str(uuid4())
    payload = {
        "source_account_id": source,
        "destination_account_id": destination,
        "amount_minor": 100,
        "currency": "INR",
    }
    first = client.post(
        "/api/v1/transactions",
        headers={**headers(alice_auth), "Idempotency-Key": key},
        json=payload,
    )
    assert first.status_code == 201 and first.json()["status"] == "posted"
    replay = client.post(
        "/api/v1/transactions",
        headers={**headers(alice_auth), "Idempotency-Key": key},
        json=payload,
    )
    assert replay.status_code == 201 and replay.json() == first.json()
    assert replay.headers["Idempotency-Replayed"] == "true"
    alien = client.post(
        "/api/v1/transactions", headers={**headers(eve_auth), "Idempotency-Key": key}, json=payload
    )
    assert alien.status_code == 404 and "transaction_id" not in alien.json()
    transaction_id = first.json()["transaction_id"]
    assert (
        client.get(f"/api/v1/transactions/{transaction_id}", headers=headers(eve_auth)).status_code
        == 404
    )
    assert (
        client.get(f"/api/v1/transactions/{transaction_id}", headers=headers(bob_auth)).status_code
        == 200
    )
    assert all(
        item["transaction_id"] != transaction_id
        for item in client.get("/api/v1/transactions", headers=headers(eve_auth)).json()["items"]
    )
    assert (
        client.post(
            f"/api/v1/transactions/{transaction_id}/reverse",
            headers={**headers(bob_auth), "Idempotency-Key": str(uuid4())},
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/api/v1/transactions/{transaction_id}/reverse",
            headers={**headers(alice_auth), "Idempotency-Key": str(uuid4())},
        ).status_code
        == 201
    )


def test_owned_demo_seed_is_repeatable(security_client, pg_engine: Engine) -> None:
    client, settings = security_client
    alice = register(client, f"demo-a-{uuid4()}@example.test")
    bob = register(client, f"demo-b-{uuid4()}@example.test")
    owners = (alice["email"], bob["email"])
    with Session(pg_engine, expire_on_commit=False) as session:
        first = seed(session, settings, owners)
    with Session(pg_engine, expire_on_commit=False) as session:
        replay = seed(session, settings, owners)
    assert first["replayed"] is False and replay["replayed"] is True
    assert first["customer_a_account_id"] == replay["customer_a_account_id"]
    alice_auth = login(client, alice["email"])
    bob_auth = login(client, bob["email"])
    assert (
        client.get(
            f"/api/v1/accounts/{first['customer_a_account_id']}/balance",
            headers=headers(alice_auth),
        ).json()["balance_minor"]
        == 200000
    )
    assert (
        client.get(
            f"/api/v1/accounts/{first['customer_b_account_id']}/balance", headers=headers(bob_auth)
        ).json()["balance_minor"]
        == 100000
    )
    assert (
        client.get(
            f"/api/v1/accounts/{first['customer_b_account_id']}/balance",
            headers=headers(alice_auth),
        ).status_code
        == 404
    )


def test_admin_provisioning_requires_explicit_rotation(
    security_client, pg_engine: Engine, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _, settings = security_client
    email = f"provision-{uuid4()}@example.test"
    monkeypatch.setattr(create_admin, "get_settings", lambda: settings)
    monkeypatch.setattr(create_admin, "get_engine", lambda url: pg_engine)
    monkeypatch.setattr(create_admin.getpass, "getpass", lambda prompt: PASSWORD)
    monkeypatch.setattr(
        sys,
        "argv",
        ["create_admin.py", "--email", email, "--confirm-admin"],
    )
    create_admin.main()
    with pg_engine.connect() as connection:
        assert connection.scalar(select(User.role).where(User.email == email)) == "admin"
    with pytest.raises(SystemExit):
        create_admin.main()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "create_admin.py",
            "--email",
            email,
            "--confirm-admin",
            "--rotate-existing-password",
        ],
    )
    create_admin.main()
    assert PASSWORD not in capsys.readouterr().out
