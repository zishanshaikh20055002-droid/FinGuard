"""Central role permissions and ownership checks."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.core.errors import DomainError
from app.models import Account, Transaction

PERMISSIONS = {
    "customer": {
        "wallet:create",
        "wallet:own:read",
        "transaction:own:read",
        "transfer:create",
        "transfer:own:reverse",
    },
    "analyst": {"wallet:investigate", "transaction:investigate", "features:read"},
    "admin": {
        "wallet:investigate",
        "transaction:investigate",
        "features:read",
        "user:manage",
        "session:inspect",
    },
}


@dataclass(frozen=True)
class Principal:
    id: UUID
    email: str
    role: str
    status: str
    session_id: UUID
    created_at: datetime


def require_permission_for(principal: Principal, permission: str) -> None:
    if permission not in PERMISSIONS[principal.role]:
        raise DomainError("forbidden", "Permission denied", 403)


def can_view_account(principal: Principal, account: Account) -> bool:
    return "wallet:investigate" in PERMISSIONS[principal.role] or (
        account.owner_user_id == principal.id and "wallet:own:read" in PERMISSIONS[principal.role]
    )


def require_account_view(principal: Principal, account: Account) -> None:
    if not can_view_account(principal, account):
        raise DomainError("account_not_found", "Account not found", 404)


def require_transaction_view(
    principal: Principal, transaction: Transaction, source: Account, destination: Account
) -> None:
    if "transaction:investigate" in PERMISSIONS[principal.role]:
        return
    if "transaction:own:read" in PERMISSIONS[principal.role] and (
        source.owner_user_id == principal.id or destination.owner_user_id == principal.id
    ):
        return
    raise DomainError("transaction_not_found", "Transaction not found", 404)


def require_transfer_source(principal: Principal, source: Account) -> None:
    require_permission_for(principal, "transfer:create")
    if source.owner_user_id != principal.id:
        raise DomainError("account_not_found", "Account not found", 404)


def require_reversal_source(principal: Principal, source: Account) -> None:
    require_permission_for(principal, "transfer:own:reverse")
    if source.owner_user_id != principal.id:
        raise DomainError("transaction_not_found", "Transaction not found", 404)
