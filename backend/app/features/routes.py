"""Protected internal feature retrieval, health, and projection operations."""

import re
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Query, status
from sqlalchemy.orm import Session

from app.auth.dependencies import CurrentUser
from app.auth.permissions import require_permission_for
from app.core.config import Settings, get_settings
from app.core.errors import DomainError
from app.db.session import get_db_session, get_engine
from app.features.historical import (
    compare_projection,
    compare_projection_to_database,
    historical_account_features,
    rebuild_projection,
)
from app.features.service import (
    FeatureUnavailable,
    feature_health,
    feature_store,
    get_account_features,
)
from app.models import Account

router = APIRouter(prefix="/features", tags=["behavioural features"])
DbSession = Annotated[Session, Depends(get_db_session)]
AppSettings = Annotated[Settings, Depends(get_settings)]


def _authorized(user: CurrentUser) -> None:
    require_permission_for(user, "features:read")


def _require_store(settings: Settings):
    try:
        store = feature_store(settings)
    except Exception as exc:
        raise DomainError("feature_store_unavailable", "Feature store is unavailable", 503) from exc
    if store is None:
        raise DomainError("feature_store_unavailable", "Feature store is not configured", 503)
    return store


def _background_rebuild(settings: Settings, version: str) -> None:
    try:
        store = feature_store(settings)
        if store is None or not settings.database_url:
            return

        def session_factory():
            return Session(get_engine(settings.database_url), expire_on_commit=False)

        rebuild_projection(session_factory, store, version)
    except Exception:
        # Rebuild status is retained in Redis without exposing event data or credentials.
        return


@router.get("/accounts/{account_id}")
def read_account_features(
    account_id: UUID,
    user: CurrentUser,
    session: DbSession,
    settings: AppSettings,
    as_of_timestamp: Annotated[datetime | None, Query()] = None,
) -> dict:
    _authorized(user)
    account = session.get(Account, account_id)
    if account is None:
        raise DomainError("account_not_found", "Account not found", 404)
    store = _require_store(settings)
    try:
        result = get_account_features(
            store,
            account.id,
            account.currency,
            as_of_timestamp or datetime.now(UTC),
            settings,
        )
    except FeatureUnavailable as exc:
        raise DomainError("feature_data_unavailable", str(exc), 409) from exc
    except ValueError as exc:
        raise DomainError("invalid_as_of_timestamp", str(exc), 422) from exc
    except Exception as exc:
        raise DomainError("feature_store_unavailable", "Feature store is unavailable", 503) from exc
    return result.model_dump(mode="json")


@router.get("/accounts/{account_id}/history")
def read_account_feature_history(
    account_id: UUID,
    user: CurrentUser,
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict:
    _authorized(user)
    account = session.get(Account, account_id)
    if account is None:
        raise DomainError("account_not_found", "Account not found", 404)
    return {
        "account_id": str(account.id),
        "currency": account.currency,
        "source": "posted_journals_and_transaction_outbox",
        "items": historical_account_features(session, account.id, account.currency, limit),
    }


@router.get("/health")
def read_feature_health(user: CurrentUser, settings: AppSettings) -> dict:
    _authorized(user)
    try:
        store = feature_store(settings)
    except Exception:
        store = None
    return feature_health(store, settings)


@router.post("/rebuild/{version}", status_code=status.HTTP_202_ACCEPTED)
def request_projection_rebuild(
    version: str,
    background_tasks: BackgroundTasks,
    user: CurrentUser,
    settings: AppSettings,
) -> dict:
    if user.role != "admin":
        raise DomainError("forbidden", "Permission denied", 403)
    store = _require_store(settings)
    if not settings.database_url:
        raise DomainError("database_unavailable", "Database is not configured", 503)
    if version == store.active_version():
        raise DomainError("active_projection_rebuild", "Cannot rebuild the active projection", 409)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", version):
        raise DomainError("invalid_projection_version", "Invalid projection version", 422)
    try:
        store.ping()
    except Exception as exc:
        raise DomainError("feature_store_unavailable", "Feature store is unavailable", 503) from exc
    background_tasks.add_task(_background_rebuild, settings, version)
    return {"projection_version": version, "status": "accepted"}


@router.get("/rebuild/{version}")
def read_rebuild_status(version: str, user: CurrentUser, settings: AppSettings) -> dict:
    if user.role != "admin":
        raise DomainError("forbidden", "Permission denied", 403)
    store = _require_store(settings)
    try:
        metrics = store.get_metrics(version)
    except Exception as exc:
        raise DomainError("feature_store_unavailable", "Feature store is unavailable", 503) from exc
    return {
        "projection_version": version,
        "status": metrics.get("rebuild_status"),
        "processed_events": metrics.get("rebuild_progress", 0),
        "last_event_time": metrics.get("rebuild_last_event_time"),
    }


@router.post("/projections/{version}/activate")
def activate_projection(
    version: str, user: CurrentUser, settings: AppSettings, session: DbSession
) -> dict:
    if user.role != "admin":
        raise DomainError("forbidden", "Permission denied", 403)
    store = _require_store(settings)
    try:
        status_info = store.get_metrics(version)
    except Exception as exc:
        raise DomainError("feature_store_unavailable", "Feature store is unavailable", 503) from exc
    if status_info.get("rebuild_status") != "ready":
        raise DomainError("projection_not_ready", "Projection rebuild is not complete", 409)
    try:
        comparison = compare_projection(store, version)
        if not comparison["matches"]:
            # If Redis data was lost, the old active namespace may be empty or stale.
            # Validate the rebuilt candidate against committed PostgreSQL history.
            comparison = compare_projection_to_database(session, store, version)
    except Exception as exc:
        raise DomainError("feature_store_unavailable", "Feature store is unavailable", 503) from exc
    if not comparison["matches"]:
        raise DomainError("projection_parity_failed", "Projection comparison failed", 409)
    if not store.activate(version):
        raise DomainError("projection_activation_failed", "Projection could not be activated", 409)
    return {"active_projection_version": version, "comparison": comparison}
