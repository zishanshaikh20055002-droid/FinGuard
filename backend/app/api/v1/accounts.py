"""Customer wallet account endpoints."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.accounts.service import (
    account_data,
    create_customer_account,
    get_account,
    get_account_balance,
)
from app.api.v1.schemas import AccountCreate
from app.auth.dependencies import CurrentUser
from app.auth.permissions import require_account_view, require_permission_for
from app.db.session import get_db_session

router = APIRouter(prefix="/accounts", tags=["accounts"])
DbSession = Annotated[Session, Depends(get_db_session)]


@router.post("", status_code=status.HTTP_201_CREATED)
def create_account(request: AccountCreate, session: DbSession, user: CurrentUser) -> dict:
    require_permission_for(user, "wallet:create")
    return create_customer_account(session, request.currency, user.id)


@router.get("/{account_id}")
def read_account(account_id: UUID, session: DbSession, user: CurrentUser) -> dict:
    account = get_account(session, account_id)
    require_account_view(user, account)
    return account_data(account)


@router.get("/{account_id}/balance")
def read_balance(account_id: UUID, session: DbSession, user: CurrentUser) -> dict:
    require_account_view(user, get_account(session, account_id))
    return get_account_balance(session, account_id)
