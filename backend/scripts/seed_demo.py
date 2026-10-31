"""Explicit, idempotent development-only simulated opening balances."""

import argparse
import json
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.session import get_engine
from app.events.outbox import record_event
from app.ledger.posting import post_transfer
from app.models import Account, Transaction, TransactionStatusHistory, User
from app.transactions.idempotency import StoredResponse, claim, fingerprint, store_response

DEMO_KEY = "demo-opening-inr-v1"
REFERENCES = ("DEMO-SETTLEMENT-INR", "DEMO-CUSTOMER-A-INR", "DEMO-CUSTOMER-B-INR")
OPENING_AMOUNTS = (200_000, 100_000)


def seed(
    session: Session, settings: Settings, customer_emails: tuple[str, str] | None = None
) -> dict:
    if settings.app_env != "development":
        raise RuntimeError("demo seeding is disabled outside development")
    with session.begin():
        references = REFERENCES
        demo_key = DEMO_KEY
        owner_ids = (None, None)
        if customer_emails is not None:
            users = [
                session.scalar(select(User).where(User.email == email.strip().casefold()))
                for email in customer_emails
            ]
            if any(
                user is None or user.role != "customer" or user.status != "active" for user in users
            ):
                raise RuntimeError("Both demo owners must be active registered customers")
            if users[0].id == users[1].id:
                raise RuntimeError("Demo customers must be different users")
            owner_ids = (users[0].id, users[1].id)
            suffix = f"{owner_ids[0].hex}-{owner_ids[1].hex}"
            references = tuple(
                f"DEMO-OWNED-{kind}-INR-{suffix}"
                for kind in ("SETTLEMENT", "CUSTOMER-A", "CUSTOMER-B")
            )
            demo_key = f"demo-owned-opening-inr-v1:{suffix}"
        seed_request = {"references": references, "amounts": OPENING_AMOUNTS, "currency": "INR"}
        if customer_emails is not None:
            seed_request["owners"] = [str(owner) for owner in owner_ids]
        claim_id, replay = claim(
            session,
            operation="demo.seed.v1",
            key=demo_key,
            request_fingerprint=fingerprint(seed_request),
        )
        if replay is not None:
            return {**replay.body, "replayed": True}
        existing = session.scalars(
            select(Account).where(Account.reference.in_(references)).with_for_update()
        ).all()
        if existing:
            raise RuntimeError("A designated demo reference already exists without its seed record")

        accounts = [
            Account(
                id=uuid4(),
                reference=references[0],
                currency="INR",
                account_type="asset",
                status="open",
            ),
            Account(
                id=uuid4(),
                reference=references[1],
                currency="INR",
                account_type="liability",
                status="open",
                owner_user_id=owner_ids[0],
            ),
            Account(
                id=uuid4(),
                reference=references[2],
                currency="INR",
                account_type="liability",
                status="open",
                owner_user_id=owner_ids[1],
            ),
        ]
        session.add_all(accounts)
        session.flush()
        for wallet, amount in zip(accounts[1:], OPENING_AMOUNTS, strict=True):
            transaction = Transaction(
                id=uuid4(),
                source_account_id=accounts[0].id,
                destination_account_id=wallet.id,
                amount_minor=amount,
                currency="INR",
                status="posted",
                memo="demo opening funding",
            )
            session.add(transaction)
            session.flush()
            session.add(
                TransactionStatusHistory(
                    transaction_id=transaction.id,
                    sequence=1,
                    from_status=None,
                    to_status="posted",
                    reason_code="demo_opening_funding",
                )
            )
            post_transfer(
                session,
                transaction_id=transaction.id,
                source_id=accounts[0].id,
                destination_id=wallet.id,
                amount_minor=amount,
                currency="INR",
            )
            record_event(
                session,
                aggregate_id=transaction.id,
                event_type="transaction.posted",
                payload={
                    "transaction_id": str(transaction.id),
                    "status": "posted",
                    "amount_minor": amount,
                    "currency": "INR",
                    "origin": "demo_seed",
                },
            )
        body = {
            "currency": "INR",
            "settlement_account_id": str(accounts[0].id),
            "customer_a_account_id": str(accounts[1].id),
            "customer_b_account_id": str(accounts[2].id),
            "customer_a_opening_minor": OPENING_AMOUNTS[0],
            "customer_b_opening_minor": OPENING_AMOUNTS[1],
        }
        store_response(
            session,
            record_id=claim_id,
            resource_id=accounts[0].id,
            response=StoredResponse(200, body),
        )
    return {**body, "replayed": False}


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed simulated FinGuard INR accounts")
    parser.add_argument("--confirm-demo-data", action="store_true")
    parser.add_argument("--customer-a-email")
    parser.add_argument("--customer-b-email")
    arguments = parser.parse_args()
    if not arguments.confirm_demo_data:
        parser.error("--confirm-demo-data is required")
    settings = get_settings()
    if settings.app_env != "development":
        parser.error("demo seeding is disabled outside development")
    if not settings.database_url:
        parser.error("DATABASE_URL is required")
    if bool(arguments.customer_a_email) != bool(arguments.customer_b_email):
        parser.error("Both customer email options must be provided together")
    emails = (
        (arguments.customer_a_email, arguments.customer_b_email)
        if arguments.customer_a_email
        else None
    )
    with Session(get_engine(settings.database_url), expire_on_commit=False) as session:
        result = seed(session, settings, emails)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
