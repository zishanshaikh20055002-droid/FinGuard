"""Public customer wallet account operations."""

from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.core.errors import DomainError
from app.ledger.posting import wallet_balance_minor
from app.models import Account


def account_data(account: Account) -> dict:
    return {
        "account_id": str(account.id),
        "reference": account.reference,
        "currency": account.currency,
        "account_type": account.account_type,
        "status": account.status,
    }


def create_customer_account(session: Session, currency: str, owner_user_id: UUID) -> dict:
    account_id = uuid4()
    with session.begin():
        account = Account(
            id=account_id,
            reference=f"CUST-{account_id}",
            currency=currency,
            account_type="liability",
            status="open",
            owner_user_id=owner_user_id,
        )
        session.add(account)
        session.flush()
        result = account_data(account)
    return result


def get_account(session: Session, account_id: UUID) -> Account:
    account = session.get(Account, account_id)
    if account is None:
        raise DomainError("account_not_found", "Account not found", 404)
    return account


def get_account_balance(session: Session, account_id: UUID) -> dict:
    account = get_account(session, account_id)
    if account.account_type != "liability":
        raise DomainError(
            "unsupported_account_type", "Balance is unavailable for this account", 409
        )
    return {**account_data(account), "balance_minor": wallet_balance_minor(session, account)}
