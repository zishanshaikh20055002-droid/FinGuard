"""Idempotent transaction and reversal endpoints."""

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.api.v1.schemas import TransactionCreate
from app.auth.dependencies import CurrentUser
from app.auth.permissions import require_permission_for, require_transaction_view
from app.core.config import Settings, get_settings
from app.core.errors import DomainError
from app.db.session import get_db_session
from app.models import Account, Transaction
from app.transactions.service import (
    ServiceResult,
    create_transaction,
    get_transaction,
    list_transactions,
    reverse_transaction,
)

router = APIRouter(prefix="/transactions", tags=["transactions"])
logger = logging.getLogger(__name__)
DbSession = Annotated[Session, Depends(get_db_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
IdempotencyHeader = Annotated[str, Header(alias="Idempotency-Key")]


def _response(result: ServiceResult) -> JSONResponse:
    headers = {"Idempotency-Replayed": "true"} if result.replayed else {}
    return JSONResponse(
        status_code=result.response.status_code,
        content=result.response.body,
        headers=headers,
    )


@router.post("")
def create(
    http_request: Request,
    request: TransactionCreate,
    idempotency_key: IdempotencyHeader,
    session: DbSession,
    settings: SettingsDep,
    user: CurrentUser,
) -> JSONResponse:
    require_permission_for(user, "transfer:create")
    result = create_transaction(session, request, idempotency_key, settings, user)
    logger.info(
        "transaction_result correlation_id=%s transaction_id=%s status=%s replayed=%s",
        http_request.state.correlation_id,
        result.response.body.get("transaction_id"),
        result.response.status_code,
        result.replayed,
    )
    return _response(result)


@router.get("")
def list_all(
    session: DbSession,
    user: CurrentUser,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict:
    if user.role == "customer":
        require_permission_for(user, "transaction:own:read")
    else:
        require_permission_for(user, "transaction:investigate")
    return list_transactions(session, limit, offset, user)


@router.get("/{transaction_id}")
def read(transaction_id: UUID, session: DbSession, user: CurrentUser) -> dict:
    transaction = session.get(Transaction, transaction_id)
    if transaction is None:
        raise DomainError("transaction_not_found", "Transaction not found", 404)
    source = session.get(Account, transaction.source_account_id)
    destination = session.get(Account, transaction.destination_account_id)
    require_transaction_view(user, transaction, source, destination)
    return get_transaction(session, transaction_id)


@router.post("/{transaction_id}/reverse")
def reverse(
    http_request: Request,
    transaction_id: UUID,
    idempotency_key: IdempotencyHeader,
    session: DbSession,
    user: CurrentUser,
) -> JSONResponse:
    require_permission_for(user, "transfer:own:reverse")
    result = reverse_transaction(session, transaction_id, idempotency_key, user)
    logger.info(
        "reversal_result correlation_id=%s transaction_id=%s status=%s replayed=%s",
        http_request.state.correlation_id,
        transaction_id,
        result.response.status_code,
        result.replayed,
    )
    return _response(result)
