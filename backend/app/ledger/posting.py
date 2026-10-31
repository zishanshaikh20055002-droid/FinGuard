"""Ledger balance queries and journal construction; caller owns the database transaction."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Account, JournalEntry, JournalLine


def wallet_balance_minor(session: Session, account: Account) -> int:
    if account.account_type != "liability":
        raise ValueError("Wallet balance calculation requires a liability account")
    amount = session.scalar(
        select(func.coalesce(func.sum(JournalLine.credit_minor - JournalLine.debit_minor), 0))
        .join(JournalEntry, JournalEntry.id == JournalLine.journal_entry_id)
        .where(JournalLine.account_id == account.id, JournalEntry.status == "posted")
    )
    return int(amount)


def post_transfer(
    session: Session,
    *,
    transaction_id: UUID,
    source_id: UUID,
    destination_id: UUID,
    amount_minor: int,
    currency: str,
) -> JournalEntry:
    entry = JournalEntry(transaction_id=transaction_id, entry_type="standard", status="draft")
    session.add(entry)
    session.flush()
    session.add_all(
        [
            JournalLine(
                journal_entry_id=entry.id,
                account_id=source_id,
                currency=currency,
                debit_minor=amount_minor,
                credit_minor=0,
            ),
            JournalLine(
                journal_entry_id=entry.id,
                account_id=destination_id,
                currency=currency,
                debit_minor=0,
                credit_minor=amount_minor,
            ),
        ]
    )
    session.flush()
    entry.status = "posted"
    entry.posted_at = datetime.now(UTC)
    session.flush()
    return entry


def post_reversal(session: Session, original: JournalEntry) -> JournalEntry:
    lines = session.scalars(
        select(JournalLine)
        .where(JournalLine.journal_entry_id == original.id)
        .order_by(JournalLine.id)
    ).all()
    entry = JournalEntry(
        transaction_id=original.transaction_id,
        entry_type="reversal",
        status="draft",
        reverses_entry_id=original.id,
    )
    session.add(entry)
    session.flush()
    session.add_all(
        JournalLine(
            journal_entry_id=entry.id,
            account_id=line.account_id,
            currency=line.currency,
            debit_minor=line.credit_minor,
            credit_minor=line.debit_minor,
        )
        for line in lines
    )
    session.flush()
    entry.status = "posted"
    entry.posted_at = datetime.now(UTC)
    session.flush()
    return entry
