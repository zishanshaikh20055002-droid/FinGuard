"""Atomic transaction creation, posting, replay, retrieval, and reversal."""

from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.api.v1.schemas import TransactionCreate
from app.auth.permissions import Principal, require_reversal_source, require_transfer_source
from app.core.config import Settings
from app.core.errors import DomainError
from app.events.outbox import record_event
from app.ledger.posting import post_reversal, post_transfer, wallet_balance_minor
from app.models import Account, JournalEntry, RiskDecision, Transaction, TransactionStatusHistory
from app.risk.provider import get_risk_provider
from app.transactions.idempotency import StoredResponse, claim, fingerprint, store_response
from app.transactions.state import record_initial, transition


@dataclass(frozen=True)
class ServiceResult:
    response: StoredResponse
    replayed: bool = False


def _lock_accounts(session: Session, first_id: UUID, second_id: UUID) -> dict[UUID, Account]:
    accounts = session.scalars(
        select(Account)
        .where(Account.id.in_([first_id, second_id]))
        .order_by(Account.id)
        .with_for_update()
    ).all()
    if len(accounts) != 2:
        raise DomainError("account_not_found", "Account not found", 404)
    return {account.id: account for account in accounts}


def _validate_wallets(source: Account, destination: Account, currency: str) -> None:
    if source.status != "open" or destination.status != "open":
        raise DomainError("inactive_account", "Both accounts must be open", 409)
    if source.account_type != "liability" or destination.account_type != "liability":
        raise DomainError("ineligible_account", "Only customer wallets may transfer", 409)
    if source.currency != currency or destination.currency != currency:
        raise DomainError("currency_mismatch", "Account currency does not match request", 409)


def _transaction_body(transaction: Transaction, *, risk_outcome: str | None = None) -> dict:
    body = {
        "transaction_id": str(transaction.id),
        "source_account_id": str(transaction.source_account_id),
        "destination_account_id": str(transaction.destination_account_id),
        "amount_minor": transaction.amount_minor,
        "currency": transaction.currency,
        "status": transaction.status,
        "memo": transaction.memo,
    }
    if risk_outcome is not None:
        body["risk_outcome"] = risk_outcome
    return body


def create_transaction(
    session: Session, request: TransactionCreate, key: str, settings: Settings, principal: Principal
) -> ServiceResult:
    if request.source_account_id == request.destination_account_id:
        raise DomainError("same_account", "Source and destination accounts must differ", 400)
    provider = get_risk_provider(settings)
    request_data = request.model_dump(mode="json")
    request_fingerprint = fingerprint(request_data)

    with session.begin():
        source_for_auth = session.get(Account, request.source_account_id)
        if source_for_auth is None:
            raise DomainError("account_not_found", "Account not found", 404)
        require_transfer_source(principal, source_for_auth)
        claim_id, replay = claim(
            session,
            operation=f"transaction.create.v1:{principal.id}",
            key=key,
            request_fingerprint=request_fingerprint,
        )
        if replay is not None:
            return ServiceResult(replay, replayed=True)

        locked = _lock_accounts(session, request.source_account_id, request.destination_account_id)
        source = locked[request.source_account_id]
        destination = locked[request.destination_account_id]
        _validate_wallets(source, destination, request.currency)

        transaction = Transaction(
            id=uuid4(),
            source_account_id=source.id,
            destination_account_id=destination.id,
            amount_minor=request.amount_minor,
            currency=request.currency,
            status="received",
            memo=request.memo,
        )
        session.add(transaction)
        session.flush()
        record_initial(session, transaction)
        transition(session, transaction, "pending_risk", "risk_assessment_started")

        assessment = provider.assess(amount_minor=request.amount_minor, currency=request.currency)
        decision_map = {"allow": "approve", "challenge": "review", "block": "block"}
        session.add(
            RiskDecision(
                transaction_id=transaction.id,
                risk_score=assessment.score,
                decision=decision_map[assessment.outcome],
                model_name=assessment.provider_name,
                model_version=assessment.provider_version,
                explanation_metadata={"reason_codes": list(assessment.reason_codes)},
            )
        )

        if assessment.outcome in ("block", "challenge"):
            final_status = "blocked" if assessment.outcome == "block" else "challenged"
            transition(session, transaction, final_status, f"risk_{assessment.outcome}")
            record_event(
                session,
                aggregate_id=transaction.id,
                event_type=f"transaction.{final_status}",
                payload={"transaction_id": str(transaction.id), "status": final_status},
            )
            response = StoredResponse(
                201, _transaction_body(transaction, risk_outcome=assessment.outcome)
            )
        else:
            transition(session, transaction, "approved", "risk_allow")
            if wallet_balance_minor(session, source) < request.amount_minor:
                transition(session, transaction, "failed", "insufficient_funds")
                record_event(
                    session,
                    aggregate_id=transaction.id,
                    event_type="transaction.failed",
                    payload={
                        "transaction_id": str(transaction.id),
                        "status": "failed",
                        "reason_code": "insufficient_funds",
                    },
                )
                response = StoredResponse(
                    409,
                    {
                        "error": {"code": "insufficient_funds", "message": "Insufficient funds"},
                        "transaction_id": str(transaction.id),
                    },
                )
            else:
                post_transfer(
                    session,
                    transaction_id=transaction.id,
                    source_id=source.id,
                    destination_id=destination.id,
                    amount_minor=request.amount_minor,
                    currency=request.currency,
                )
                transition(session, transaction, "posted", "ledger_posted")
                record_event(
                    session,
                    aggregate_id=transaction.id,
                    event_type="transaction.posted",
                    payload={
                        "transaction_id": str(transaction.id),
                        "status": "posted",
                        "amount_minor": transaction.amount_minor,
                        "currency": transaction.currency,
                        "source_account_id": str(transaction.source_account_id),
                        "destination_account_id": str(transaction.destination_account_id),
                    },
                )
                response = StoredResponse(201, _transaction_body(transaction, risk_outcome="allow"))

        store_response(session, record_id=claim_id, resource_id=transaction.id, response=response)
    return ServiceResult(response)


def reverse_transaction(
    session: Session, transaction_id: UUID, key: str, principal: Principal
) -> ServiceResult:
    request_fingerprint = fingerprint({"transaction_id": str(transaction_id)})
    with session.begin():
        transaction = session.scalar(
            select(Transaction).where(Transaction.id == transaction_id).with_for_update()
        )
        if transaction is None:
            raise DomainError("transaction_not_found", "Transaction not found", 404)
        source_for_auth = session.get(Account, transaction.source_account_id)
        require_reversal_source(principal, source_for_auth)
        claim_id, replay = claim(
            session,
            operation=f"transaction.reverse.v1:{principal.id}",
            key=key,
            request_fingerprint=request_fingerprint,
        )
        if replay is not None:
            return ServiceResult(replay, replayed=True)

        if transaction.status != "posted":
            raise DomainError("not_reversible", "Only posted transactions can be reversed", 409)
        original = session.scalar(
            select(JournalEntry)
            .where(
                JournalEntry.transaction_id == transaction.id,
                JournalEntry.entry_type == "standard",
                JournalEntry.status == "posted",
            )
            .with_for_update()
        )
        if original is None:
            raise DomainError("journal_not_found", "Posted journal entry is missing", 409)
        prior = session.scalar(
            select(JournalEntry.id).where(JournalEntry.reverses_entry_id == original.id)
        )
        if prior is not None:
            raise DomainError("already_reversed", "Transaction is already reversed", 409)
        locked = _lock_accounts(
            session, transaction.source_account_id, transaction.destination_account_id
        )
        source = locked[transaction.source_account_id]
        destination = locked[transaction.destination_account_id]
        _validate_wallets(source, destination, transaction.currency)
        if wallet_balance_minor(session, destination) < transaction.amount_minor:
            raise DomainError(
                "insufficient_reversal_funds", "Destination has insufficient funds", 409
            )
        reversal = post_reversal(session, original)
        record_event(
            session,
            aggregate_id=transaction.id,
            event_type="transaction.reversed",
            payload={
                "transaction_id": str(transaction.id),
                "reversal_entry_id": str(reversal.id),
                "status": "reversed",
            },
        )
        response = StoredResponse(
            201,
            {
                "transaction_id": str(transaction.id),
                "reversal_entry_id": str(reversal.id),
                "status": "reversed",
            },
        )
        store_response(session, record_id=claim_id, resource_id=transaction.id, response=response)
    return ServiceResult(response)


def get_transaction(session: Session, transaction_id: UUID) -> dict:
    transaction = session.get(Transaction, transaction_id)
    if transaction is None:
        raise DomainError("transaction_not_found", "Transaction not found", 404)
    risk = session.scalar(
        select(RiskDecision)
        .where(RiskDecision.transaction_id == transaction_id)
        .order_by(RiskDecision.created_at.desc())
    )
    reversed_id = session.scalar(
        select(JournalEntry.id).where(
            JournalEntry.transaction_id == transaction_id,
            JournalEntry.entry_type == "reversal",
            JournalEntry.status == "posted",
        )
    )
    history = session.scalars(
        select(TransactionStatusHistory)
        .where(TransactionStatusHistory.transaction_id == transaction_id)
        .order_by(TransactionStatusHistory.sequence)
    ).all()
    return {
        **_transaction_body(transaction),
        "risk": None
        if risk is None
        else {
            "score": str(risk.risk_score),
            "decision": risk.decision,
            "provider": risk.model_name,
            "version": risk.model_version,
            "reason_codes": risk.explanation_metadata.get("reason_codes", []),
        },
        "reversed": reversed_id is not None,
        "reversal_entry_id": None if reversed_id is None else str(reversed_id),
        "history": [
            {
                "sequence": item.sequence,
                "from_status": item.from_status,
                "to_status": item.to_status,
                "reason_code": item.reason_code,
            }
            for item in history
        ],
        "created_at": transaction.created_at.isoformat(),
        "updated_at": transaction.updated_at.isoformat(),
    }


def list_transactions(session: Session, limit: int, offset: int, principal: Principal) -> dict:
    query = select(Transaction)
    if principal.role == "customer":
        owned = select(Account.id).where(Account.owner_user_id == principal.id)
        query = query.where(
            or_(
                Transaction.source_account_id.in_(owned),
                Transaction.destination_account_id.in_(owned),
            )
        )
    transactions = session.scalars(
        query.order_by(Transaction.created_at.desc(), Transaction.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return {
        "items": [_transaction_body(item) for item in transactions],
        "limit": limit,
        "offset": offset,
    }
