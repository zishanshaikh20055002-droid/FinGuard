"""Online feature retrieval and health diagnostics."""

from datetime import UTC, datetime, timedelta
from functools import lru_cache
from uuid import UUID

from app.core.config import Settings
from app.features.calculator import calculate_account_features
from app.features.definitions import EVENT_RETENTION, FEATURE_SCHEMA_VERSION
from app.features.redis_store import RedisFeatureStore
from app.features.schemas import AccountFeatures


class FeatureUnavailable(RuntimeError):
    pass


@lru_cache(maxsize=4)
def _redis_store(url: str) -> RedisFeatureStore:
    return RedisFeatureStore(url)


def feature_store(settings: Settings) -> RedisFeatureStore | None:
    if not settings.redis_url:
        return None
    store = _redis_store(settings.redis_url)
    store.initialize_active(settings.feature_projection_version)
    return store


def get_account_features(
    store,
    account_id: UUID,
    currency: str,
    as_of: datetime,
    settings: Settings,
    version: str | None = None,
) -> AccountFeatures:
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of_timestamp must include a timezone")
    as_of = as_of.astimezone(UTC)
    now = datetime.now(UTC)
    if as_of > now:
        raise ValueError("as_of_timestamp cannot be in the future")
    if as_of < now - (EVENT_RETENTION - timedelta(days=7)):
        raise FeatureUnavailable("requested timestamp is outside online feature retention")
    version = version or store.active_version()
    events = store.get_events(account_id, currency, as_of, version)
    values = calculate_account_features(account_id, currency, as_of, events)
    store.store_features(account_id, currency, as_of, values, version)
    account_update = store.account_last_update(account_id, currency, version)
    metrics = store.get_metrics(version)
    last_projection_update = metrics.get("last_projection_update")
    age = (
        None
        if last_projection_update is None
        else max(0.0, (now - last_projection_update).total_seconds())
    )
    account_age = (
        None if account_update is None else max(0.0, (now - account_update).total_seconds())
    )
    freshness_age = max((value for value in (age, account_age) if value is not None), default=None)
    freshness = (
        "unknown"
        if freshness_age is None
        else ("fresh" if freshness_age <= settings.feature_freshness_seconds else "stale")
    )
    return AccountFeatures(
        account_id=account_id,
        currency=currency,
        feature_schema_version=FEATURE_SCHEMA_VERSION,
        as_of_timestamp=as_of,
        values=values,
        last_projection_update=account_update or last_projection_update,
        freshness_seconds=freshness_age,
        freshness=freshness,
    )


def feature_health(store, settings: Settings) -> dict:
    if store is None:
        return {"status": "degraded", "redis": "not_configured", "projection": "paused"}
    try:
        store.ping()
        version = store.active_version()
        metrics = store.get_metrics(version)
    except Exception:
        return {"status": "degraded", "redis": "unavailable", "projection": "paused"}
    last_update = metrics.get("last_projection_update")
    update_age = (
        None if last_update is None else max(0.0, (datetime.now(UTC) - last_update).total_seconds())
    )
    event_time = metrics.get("last_event_time")
    lag = None if event_time is None else max(0.0, (datetime.now(UTC) - event_time).total_seconds())
    status = (
        "degraded"
        if update_age is not None and update_age > settings.feature_freshness_seconds
        else "ready"
    )
    if last_update is None:
        status = "unknown"
    return {
        "status": status,
        "redis": "available",
        "projection_version": version,
        "events_processed_total": metrics["events_processed_total"],
        "duplicate_events_suppressed_total": metrics["duplicate_events_total"],
        "feature_update_errors_total": metrics["feature_update_errors_total"],
        "projection_lag_seconds": lag,
        "last_projection_update_age_seconds": update_age,
        "last_successful_event_timestamp": metrics["last_event_time"],
        "last_projection_update": last_update,
        "rebuild_status": metrics.get("rebuild_status"),
        "rebuild_progress": metrics.get("rebuild_progress"),
    }
