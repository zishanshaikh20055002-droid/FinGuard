"""Explicit transaction status transitions with an ordered audit trail."""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import DomainError
from app.models import Transaction, TransactionStatusHistory

ALLOWED: dict[str, frozenset[str]] = {
    "received": frozenset({"pending_risk"}),
    "pending_risk": frozenset({"approved", "challenged", "blocked"}),
    "challenged": frozenset({"approved", "blocked"}),
    "approved": frozenset({"posted", "failed"}),
    "blocked": frozenset(),
    "failed": frozenset(),
    "posted": frozenset(),
}


def record_initial(session: Session, transaction: Transaction) -> None:
    session.add(
        TransactionStatusHistory(
            transaction_id=transaction.id,
            sequence=1,
            from_status=None,
            to_status="received",
            reason_code="request_accepted",
        )
    )


def transition(session: Session, transaction: Transaction, target: str, reason_code: str) -> None:
    if target not in ALLOWED.get(transaction.status, frozenset()):
        raise DomainError(
            "invalid_transition", "Transaction state cannot make that transition", 409
        )
    session.flush()
    previous = transaction.status
    next_sequence = session.scalar(
        select(func.coalesce(func.max(TransactionStatusHistory.sequence), 0) + 1).where(
            TransactionStatusHistory.transaction_id == transaction.id
        )
    )
    transaction.status = target
    session.add(
        TransactionStatusHistory(
            transaction_id=transaction.id,
            sequence=next_sequence,
            from_status=previous,
            to_status=target,
            reason_code=reason_code,
        )
    )
