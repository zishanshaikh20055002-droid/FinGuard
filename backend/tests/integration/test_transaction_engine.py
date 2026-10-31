"""End-to-end API and service behavior against PostgreSQL 16."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from backend.scripts.seed_demo import seed
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, insert, select, update
from sqlalchemy.orm import Session

import app.transactions.service as transaction_service
from app.auth.dependencies import get_current_user
from app.auth.permissions import Principal
from app.core.config import Settings, get_settings
from app.db.session import get_db_session
from app.main import app
from app.models import (
    Account,
    IdempotencyKey,
    JournalEntry,
    JournalLine,
    OutboxEvent,
    RiskDecision,
    Transaction,
    User,
)
from app.risk.provider import Assessment

pytestmark = pytest.mark.integration
_test_principal: Principal | None = None


@pytest.fixture
def client(pg_engine: Engine):
    global _test_principal
    user_id = uuid4()
    with pg_engine.begin() as connection:
        connection.execute(
            insert(User).values(
                id=user_id,
                email=f"engine-{user_id}@example.test",
                password_hash="test-fixture-only",
                role="customer",
                status="active",
            )
        )
    _test_principal = Principal(
        user_id, f"engine-{user_id}@example.test", "customer", "active", uuid4(), datetime.now(UTC)
    )

    def db_session():
        with Session(pg_engine, expire_on_commit=False) as session:
            yield session

    app.dependency_overrides[get_db_session] = db_session
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None,
        app_env="development",
        database_url="postgresql+psycopg://unused@localhost/test",
    )
    app.dependency_overrides[get_current_user] = lambda: _test_principal
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        _test_principal = None


def funded_wallets(
    engine: Engine, *, source_minor: int = 2_000_000, destination_minor: int = 0
) -> tuple[UUID, UUID, UUID]:
    assert _test_principal is not None
    system_id, source_id, destination_id = uuid4(), uuid4(), uuid4()
    with engine.begin() as connection:
        for account_id, account_type in (
            (system_id, "asset"),
            (source_id, "liability"),
            (destination_id, "liability"),
        ):
            connection.execute(
                insert(Account).values(
                    id=account_id,
                    reference=f"ENGINE-TEST-{account_id}",
                    currency="INR",
                    account_type=account_type,
                    status="open",
                    owner_user_id=None if account_type == "asset" else _test_principal.id,
                )
            )
        for wallet_id, amount in ((source_id, source_minor), (destination_id, destination_minor)):
            if amount == 0:
                continue
            transaction_id, entry_id = uuid4(), uuid4()
            connection.execute(
                insert(Transaction).values(
                    id=transaction_id,
                    source_account_id=system_id,
                    destination_account_id=wallet_id,
                    amount_minor=amount,
                    currency="INR",
                    status="posted",
                    memo="test opening funding",
                )
            )
            connection.execute(
                insert(JournalEntry).values(
                    id=entry_id,
                    transaction_id=transaction_id,
                    entry_type="standard",
                    status="draft",
                )
            )
            connection.execute(
                insert(JournalLine).values(
                    id=uuid4(),
                    journal_entry_id=entry_id,
                    account_id=system_id,
                    currency="INR",
                    debit_minor=amount,
                    credit_minor=0,
                )
            )
            connection.execute(
                insert(JournalLine).values(
                    id=uuid4(),
                    journal_entry_id=entry_id,
                    account_id=wallet_id,
                    currency="INR",
                    debit_minor=0,
                    credit_minor=amount,
                )
            )
            connection.execute(
                update(JournalEntry)
                .where(JournalEntry.id == entry_id)
                .values(status="posted", posted_at=func.now())
            )
    return system_id, source_id, destination_id


def payload(source: UUID, destination: UUID, amount: int = 100, memo: str | None = None) -> dict:
    return {
        "source_account_id": str(source),
        "destination_account_id": str(destination),
        "amount_minor": amount,
        "currency": "INR",
        "memo": memo,
    }


def submit(client: TestClient, request: dict, key: str | None = None):
    return client.post(
        "/api/v1/transactions",
        json=request,
        headers={"Idempotency-Key": key or f"test-{uuid4()}"},
    )


def balance(client: TestClient, account_id: UUID) -> int:
    response = client.get(f"/api/v1/accounts/{account_id}/balance")
    assert response.status_code == 200
    return response.json()["balance_minor"]


def test_customer_account_creation_retrieval_and_zero_balance(client: TestClient) -> None:
    response = client.post("/api/v1/accounts", json={"currency": "INR"})
    assert response.status_code == 201
    account = response.json()
    assert account["account_type"] == "liability"
    assert account["status"] == "open"
    assert client.get(f"/api/v1/accounts/{account['account_id']}").json() == account
    assert balance(client, UUID(account["account_id"])) == 0
    assert client.post("/api/v1/accounts", json={"currency": "XYZ"}).status_code == 422
    assert (
        client.post(
            "/api/v1/accounts", json={"currency": "INR", "account_type": "asset"}
        ).status_code
        == 422
    )


def test_posted_transfer_balance_history_risk_and_outbox(
    client: TestClient, pg_engine: Engine
) -> None:
    _, source, destination = funded_wallets(pg_engine)
    response = submit(client, payload(source, destination, 12_345, "test transfer"))
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "posted"
    assert body["risk_outcome"] == "allow"
    assert balance(client, source) == 2_000_000 - 12_345
    assert balance(client, destination) == 12_345
    transaction_id = UUID(body["transaction_id"])
    detail = client.get(f"/api/v1/transactions/{transaction_id}")
    assert detail.status_code == 200
    assert [row["to_status"] for row in detail.json()["history"]] == [
        "received",
        "pending_risk",
        "approved",
        "posted",
    ]
    assert detail.json()["risk"]["provider"] == "dev_rules"
    assert any(
        item["transaction_id"] == str(transaction_id)
        for item in client.get("/api/v1/transactions?limit=100").json()["items"]
    )
    with pg_engine.connect() as connection:
        entries = connection.scalars(
            select(JournalEntry.id).where(JournalEntry.transaction_id == transaction_id)
        ).all()
        assert len(entries) == 1
        lines = connection.execute(
            select(JournalLine.debit_minor, JournalLine.credit_minor).where(
                JournalLine.journal_entry_id == entries[0]
            )
        ).all()
        assert sum(row[0] for row in lines) == sum(row[1] for row in lines) == 12_345
        assert (
            connection.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(
                    OutboxEvent.aggregate_id == transaction_id,
                    OutboxEvent.event_type == "transaction.posted",
                )
            )
            == 1
        )
        event = connection.execute(
            select(
                OutboxEvent.published_at,
                OutboxEvent.attempt_count,
                OutboxEvent.payload,
            ).where(
                OutboxEvent.aggregate_id == transaction_id,
                OutboxEvent.event_type == "transaction.posted",
            )
        ).one()
        assert event.published_at is None
        assert event.attempt_count == 0
        assert event.payload["schema_version"] == 1


def test_idempotent_replay_and_conflict(client: TestClient, pg_engine: Engine) -> None:
    _, source, destination = funded_wallets(pg_engine)
    request = payload(source, destination, 250)
    key = f"replay-{uuid4()}"
    first = submit(client, request, key)
    replay = submit(client, request, key)
    assert first.status_code == replay.status_code == 201
    assert replay.json() == first.json()
    assert replay.headers["Idempotency-Replayed"] == "true"
    conflict = submit(client, payload(source, destination, 251), key)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"
    transaction_id = UUID(first.json()["transaction_id"])
    with pg_engine.connect() as connection:
        assert (
            connection.scalar(
                select(func.count())
                .select_from(JournalEntry)
                .where(JournalEntry.transaction_id == transaction_id)
            )
            == 1
        )


def test_invalid_accounts_amount_and_currency(client: TestClient, pg_engine: Engine) -> None:
    system, source, destination = funded_wallets(pg_engine)
    assert submit(client, payload(uuid4(), destination)).status_code == 404
    assert submit(client, payload(source, source)).status_code == 400
    assert submit(client, payload(source, destination, 0)).status_code == 422
    assert submit(client, payload(source, destination, -1)).status_code == 422
    assert submit(client, {**payload(source, destination), "currency": "USD"}).status_code == 409
    assert submit(client, payload(system, destination)).status_code == 404
    with pg_engine.begin() as connection:
        connection.execute(update(Account).where(Account.id == source).values(status="frozen"))
    assert submit(client, payload(source, destination)).status_code == 409


@pytest.mark.parametrize("amount,expected", [(500_000, "challenged"), (1_000_000, "blocked")])
def test_risk_challenge_and_block_do_not_post(
    client: TestClient, pg_engine: Engine, amount: int, expected: str
) -> None:
    _, source, destination = funded_wallets(pg_engine)
    response = submit(client, payload(source, destination, amount))
    assert response.status_code == 201
    transaction_id = UUID(response.json()["transaction_id"])
    assert response.json()["status"] == expected
    assert balance(client, source) == 2_000_000
    assert balance(client, destination) == 0
    with pg_engine.connect() as connection:
        assert (
            connection.scalar(
                select(func.count())
                .select_from(JournalEntry)
                .where(JournalEntry.transaction_id == transaction_id)
            )
            == 0
        )
        assert connection.scalar(
            select(RiskDecision.decision).where(RiskDecision.transaction_id == transaction_id)
        ) == ("review" if expected == "challenged" else "block")
        assert (
            connection.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(OutboxEvent.aggregate_id == transaction_id)
            )
            == 1
        )
    assert (
        client.post(
            f"/api/v1/transactions/{transaction_id}/reverse",
            headers={"Idempotency-Key": f"reverse-{uuid4()}"},
        ).status_code
        == 409
    )


def test_insufficient_funds_is_failed_without_posting(
    client: TestClient, pg_engine: Engine
) -> None:
    _, source, destination = funded_wallets(pg_engine, source_minor=100)
    key = f"insufficient-{uuid4()}"
    response = submit(client, payload(source, destination, 800), key)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "insufficient_funds"
    replay = submit(client, payload(source, destination, 800), key)
    assert replay.status_code == 409 and replay.json() == response.json()
    assert replay.headers["Idempotency-Replayed"] == "true"
    transaction_id = UUID(response.json()["transaction_id"])
    assert client.get(f"/api/v1/transactions/{transaction_id}").json()["status"] == "failed"
    assert balance(client, source) == 100
    with pg_engine.connect() as connection:
        assert (
            connection.scalar(
                select(func.count())
                .select_from(JournalEntry)
                .where(JournalEntry.transaction_id == transaction_id)
            )
            == 0
        )
        assert (
            connection.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(
                    OutboxEvent.aggregate_id == transaction_id,
                    OutboxEvent.event_type == "transaction.failed",
                )
            )
            == 1
        )


def test_reversal_preserves_original_and_restores_balances(
    client: TestClient, pg_engine: Engine
) -> None:
    _, source, destination = funded_wallets(pg_engine)
    posted = submit(client, payload(source, destination, 5_000))
    assert posted.status_code == 201
    transaction_id = posted.json()["transaction_id"]
    key = f"reverse-{uuid4()}"
    reversed_response = client.post(
        f"/api/v1/transactions/{transaction_id}/reverse", headers={"Idempotency-Key": key}
    )
    assert reversed_response.status_code == 201
    assert reversed_response.json()["status"] == "reversed"
    replay = client.post(
        f"/api/v1/transactions/{transaction_id}/reverse", headers={"Idempotency-Key": key}
    )
    assert replay.status_code == 201 and replay.json() == reversed_response.json()
    assert replay.headers["Idempotency-Replayed"] == "true"
    duplicate = client.post(
        f"/api/v1/transactions/{transaction_id}/reverse",
        headers={"Idempotency-Key": f"reverse-{uuid4()}"},
    )
    assert duplicate.status_code == 409
    assert balance(client, source) == 2_000_000
    assert balance(client, destination) == 0
    detail = client.get(f"/api/v1/transactions/{transaction_id}").json()
    assert detail["reversed"] is True
    assert detail["status"] == "posted"
    with pg_engine.connect() as connection:
        entries = connection.execute(
            select(JournalEntry.entry_type, JournalEntry.status).where(
                JournalEntry.transaction_id == UUID(transaction_id)
            )
        ).all()
        assert sorted(row[0] for row in entries) == ["reversal", "standard"]
        assert all(row[1] == "posted" for row in entries)
        assert (
            connection.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(
                    OutboxEvent.aggregate_id == UUID(transaction_id),
                    OutboxEvent.event_type == "transaction.reversed",
                )
            )
            == 1
        )


def test_reversal_refuses_to_overdraw_recipient(client: TestClient, pg_engine: Engine) -> None:
    _, source, destination = funded_wallets(pg_engine)
    transaction_id = submit(client, payload(source, destination, 1_000)).json()["transaction_id"]
    other_wallet = uuid4()
    with pg_engine.begin() as connection:
        connection.execute(
            insert(Account).values(
                id=other_wallet,
                reference=f"ENGINE-TEST-{other_wallet}",
                currency="INR",
                account_type="liability",
                status="open",
                owner_user_id=_test_principal.id,
            )
        )
    assert submit(client, payload(destination, other_wallet, 900)).status_code == 201
    response = client.post(
        f"/api/v1/transactions/{transaction_id}/reverse",
        headers={"Idempotency-Key": f"reverse-{uuid4()}"},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "insufficient_reversal_funds"
    assert balance(client, destination) == 100


def test_concurrent_same_key_posts_once(client: TestClient, pg_engine: Engine) -> None:
    _, source, destination = funded_wallets(pg_engine)
    request = payload(source, destination, 300)
    key = f"parallel-{uuid4()}"
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(submit, client, request, key) for _ in range(2)]
        responses = [future.result(timeout=10) for future in futures]
    assert [response.status_code for response in responses] == [201, 201]
    assert responses[0].json() == responses[1].json()
    assert (
        sum(response.headers.get("Idempotency-Replayed") == "true" for response in responses) == 1
    )
    transaction_id = UUID(responses[0].json()["transaction_id"])
    with pg_engine.connect() as connection:
        assert (
            connection.scalar(
                select(func.count())
                .select_from(JournalEntry)
                .where(JournalEntry.transaction_id == transaction_id)
            )
            == 1
        )
        assert (
            connection.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(
                    OutboxEvent.aggregate_id == transaction_id,
                    OutboxEvent.event_type == "transaction.posted",
                )
            )
            == 1
        )


def test_concurrent_transfers_cannot_overdraw(client: TestClient, pg_engine: Engine) -> None:
    _, source, destination_a = funded_wallets(pg_engine, source_minor=1_000)
    destination_b = uuid4()
    with pg_engine.begin() as connection:
        connection.execute(
            insert(Account).values(
                id=destination_b,
                reference=f"ENGINE-TEST-{destination_b}",
                currency="INR",
                account_type="liability",
                status="open",
                owner_user_id=_test_principal.id,
            )
        )
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(submit, client, payload(source, dest, 800))
            for dest in (destination_a, destination_b)
        ]
        responses = [future.result(timeout=10) for future in futures]
    assert sorted(response.status_code for response in responses) == [201, 409]
    assert balance(client, source) == 200
    assert balance(client, destination_a) + balance(client, destination_b) == 800
    ids = [UUID(response.json()["transaction_id"]) for response in responses]
    with pg_engine.connect() as connection:
        assert (
            connection.scalar(
                select(func.count())
                .select_from(JournalEntry)
                .where(JournalEntry.transaction_id.in_(ids))
            )
            == 1
        )


def test_concurrent_duplicate_reversal_posts_once(client: TestClient, pg_engine: Engine) -> None:
    _, source, destination = funded_wallets(pg_engine)
    transaction_id = submit(client, payload(source, destination, 500)).json()["transaction_id"]
    key = f"concurrent-reversal-{uuid4()}"
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                client.post,
                f"/api/v1/transactions/{transaction_id}/reverse",
                headers={"Idempotency-Key": key},
            )
            for _ in range(2)
        ]
        responses = [future.result(timeout=10) for future in futures]
    assert [response.status_code for response in responses] == [201, 201]
    assert responses[0].json() == responses[1].json()
    assert (
        sum(response.headers.get("Idempotency-Replayed") == "true" for response in responses) == 1
    )
    with pg_engine.connect() as connection:
        assert (
            connection.scalar(
                select(func.count())
                .select_from(JournalEntry)
                .where(
                    JournalEntry.transaction_id == UUID(transaction_id),
                    JournalEntry.entry_type == "reversal",
                )
            )
            == 1
        )


def test_concurrent_distinct_reversal_keys_allow_only_one(
    client: TestClient, pg_engine: Engine
) -> None:
    _, source, destination = funded_wallets(pg_engine)
    transaction_id = submit(client, payload(source, destination, 500)).json()["transaction_id"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                client.post,
                f"/api/v1/transactions/{transaction_id}/reverse",
                headers={"Idempotency-Key": f"distinct-reversal-{uuid4()}"},
            )
            for _ in range(2)
        ]
        responses = [future.result(timeout=10) for future in futures]
    assert sorted(response.status_code for response in responses) == [201, 409]
    with pg_engine.connect() as connection:
        assert (
            connection.scalar(
                select(func.count())
                .select_from(JournalEntry)
                .where(
                    JournalEntry.transaction_id == UUID(transaction_id),
                    JournalEntry.entry_type == "reversal",
                )
            )
            == 1
        )


def test_database_failure_rolls_back_claim_transaction_and_outbox(
    client: TestClient, pg_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, source, destination = funded_wallets(pg_engine)
    key = f"db-failure-{uuid4()}"
    memo = f"failure-{uuid4()}"
    request = payload(source, destination, 100, memo)
    with pg_engine.connect() as connection:
        before_events = connection.scalar(select(func.count()).select_from(OutboxEvent))

    class InvalidRiskProvider:
        def assess(self, *, amount_minor: int, currency: str) -> Assessment:
            return Assessment("allow", Decimal("1.5000"), ("invalid_test_score",), "test", "1")

    original = transaction_service.get_risk_provider
    monkeypatch.setattr(
        transaction_service, "get_risk_provider", lambda settings: InvalidRiskProvider()
    )
    failure = submit(client, request, key)
    assert failure.status_code == 500
    monkeypatch.setattr(transaction_service, "get_risk_provider", original)
    with pg_engine.connect() as connection:
        assert (
            connection.scalar(
                select(func.count())
                .select_from(IdempotencyKey)
                .where(
                    IdempotencyKey.operation == f"transaction.create.v1:{_test_principal.id}",
                    IdempotencyKey.key == key,
                )
            )
            == 0
        )
        assert (
            connection.scalar(
                select(func.count()).select_from(Transaction).where(Transaction.memo == memo)
            )
            == 0
        )
        assert connection.scalar(select(func.count()).select_from(OutboxEvent)) == before_events
    retry = submit(client, request, key)
    assert retry.status_code == 201 and retry.json()["status"] == "posted"


def test_demo_seed_is_idempotent_and_uses_balanced_opening_entries(pg_engine: Engine) -> None:
    settings = Settings(
        _env_file=None,
        app_env="development",
        database_url="postgresql+psycopg://unused@localhost/test",
    )
    with Session(pg_engine, expire_on_commit=False) as session:
        first = seed(session, settings)
    with Session(pg_engine, expire_on_commit=False) as session:
        second = seed(session, settings)
    assert second["replayed"] is True
    assert {key: value for key, value in first.items() if key != "replayed"} == {
        key: value for key, value in second.items() if key != "replayed"
    }
    with pg_engine.connect() as connection:
        assert (
            connection.scalar(
                select(func.count())
                .select_from(Transaction)
                .where(
                    Transaction.memo == "demo opening funding",
                    Transaction.destination_account_id.in_(
                        [
                            UUID(first["customer_a_account_id"]),
                            UUID(first["customer_b_account_id"]),
                        ]
                    ),
                )
            )
            == 2
        )
