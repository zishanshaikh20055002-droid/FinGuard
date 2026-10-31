"""Point-in-time history, deterministic rebuild, and CSV feature export."""

import csv
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.events.contracts import EventEnvelope, event_fingerprint
from app.features.calculator import calculate_account_features
from app.features.definitions import EVENT_RETENTION, FEATURE_NAMES
from app.features.schemas import FeatureEvent
from app.models import JournalEntry, OutboxEvent, Transaction


def _query_page(session: Session, after, limit: int):
    statement = (
        select(OutboxEvent, Transaction)
        .join(Transaction, Transaction.id == OutboxEvent.aggregate_id)
        .join(
            JournalEntry,
            and_(
                JournalEntry.transaction_id == Transaction.id,
                JournalEntry.entry_type == "standard",
                JournalEntry.status == "posted",
            ),
        )
        .where(OutboxEvent.event_type == "transaction.posted", Transaction.status == "posted")
    )
    if after is not None:
        created_at, event_id = after
        statement = statement.where(
            or_(
                OutboxEvent.created_at > created_at,
                and_(OutboxEvent.created_at == created_at, OutboxEvent.id > event_id),
            )
        )
    return session.execute(
        statement.order_by(OutboxEvent.created_at, OutboxEvent.id).limit(limit)
    ).all()


def _to_feature_event(outbox: OutboxEvent, transaction: Transaction) -> FeatureEvent:
    return FeatureEvent(
        event_id=outbox.id,
        occurred_at=outbox.created_at,
        source_account_id=transaction.source_account_id,
        destination_account_id=transaction.destination_account_id,
        amount_minor=transaction.amount_minor,
        currency=transaction.currency,
    )


def _fingerprint(outbox: OutboxEvent) -> str:
    payload = dict(outbox.payload)
    schema_version = payload.pop("schema_version", 1)
    envelope = EventEnvelope(
        event_id=outbox.id,
        event_type=outbox.event_type,
        schema_version=schema_version,
        aggregate_type=outbox.aggregate_type,
        aggregate_id=outbox.aggregate_id,
        occurred_at=outbox.created_at,
        correlation_id=outbox.correlation_id or outbox.id,
        payload=payload,
    )
    return event_fingerprint(envelope)


def rebuild_projection(session_factory, store, version: str, batch_size: int = 500) -> int:
    """Replaying from the first keyset page makes interrupted rebuilds resumable/idempotent."""
    if version == store.active_version():
        raise ValueError("cannot rebuild the active projection namespace")
    store.begin_rebuild(version)
    cursor = None
    processed = 0
    try:
        while True:
            with session_factory() as session:
                rows = _query_page(session, cursor, batch_size)
                page = [(outbox, transaction) for outbox, transaction in rows]
            if not page:
                break
            for outbox, transaction in page:
                event = _to_feature_event(outbox, transaction)
                store.apply_event(event, _fingerprint(outbox), version)
                processed += 1
                cursor = (outbox.created_at, outbox.id)
            store.update_rebuild(version, processed, cursor[0].isoformat())
        store.complete_rebuild(version)
        return processed
    except Exception as exc:
        store.fail_rebuild(version, type(exc).__name__)
        raise


def compare_projection(store, candidate_version: str, as_of: datetime | None = None) -> dict:
    active_version = store.active_version()
    as_of = (as_of or datetime.now(UTC)).astimezone(UTC)
    active_accounts = set(store.list_accounts(active_version))
    candidate_accounts = set(store.list_accounts(candidate_version))
    if active_accounts != candidate_accounts:
        return {"matches": False, "reason": "account_currency_set_mismatch"}
    for account_id, currency in sorted(active_accounts, key=lambda item: (str(item[0]), item[1])):
        active = calculate_account_features(
            account_id,
            currency,
            as_of,
            store.get_events(account_id, currency, as_of, active_version),
        )
        candidate = calculate_account_features(
            account_id,
            currency,
            as_of,
            store.get_events(account_id, currency, as_of, candidate_version),
        )
        if active != candidate:
            return {"matches": False, "reason": "feature_value_mismatch"}
    return {"matches": True, "accounts_compared": len(active_accounts)}


def compare_projection_to_database(
    session: Session, store, candidate_version: str, as_of: datetime | None = None
) -> dict:
    """Authoritative recovery comparison used when Redis loss invalidated the active projection."""
    as_of = (as_of or datetime.now(UTC)).astimezone(UTC)
    rows = []
    cursor = None
    while True:
        page = _query_page(session, cursor, 1000)
        if not page:
            break
        rows.extend(page)
        last_outbox, _ = page[-1]
        cursor = (last_outbox.created_at, last_outbox.id)
    events = [_to_feature_event(outbox, transaction) for outbox, transaction in rows]
    candidate_accounts = set(store.list_accounts(candidate_version))
    recent_database_accounts = {
        (account_id, event.currency)
        for event in events
        if event.occurred_at >= as_of - EVENT_RETENTION
        for account_id in (event.source_account_id, event.destination_account_id)
    }
    if not recent_database_accounts.issubset(candidate_accounts):
        return {"matches": False, "reason": "candidate_missing_recent_accounts"}
    for account_id, currency in sorted(
        candidate_accounts, key=lambda item: (str(item[0]), item[1])
    ):
        expected = calculate_account_features(account_id, currency, as_of, events)
        candidate = calculate_account_features(
            account_id,
            currency,
            as_of,
            store.get_events(account_id, currency, as_of, candidate_version),
        )
        if expected != candidate:
            return {"matches": False, "reason": "database_feature_value_mismatch"}
    return {
        "matches": True,
        "accounts_compared": len(candidate_accounts),
        "comparison_source": "postgresql_history",
    }


def historical_account_features(
    session: Session, account_id: UUID, currency: str, limit: int = 100
) -> list[dict]:
    rows = []
    cursor = None
    while True:
        page = _query_page(session, cursor, 1000)
        if not page:
            break
        rows.extend(page)
        last_outbox, _ = page[-1]
        cursor = (last_outbox.created_at, last_outbox.id)
    events = [_to_feature_event(outbox, transaction) for outbox, transaction in rows]
    account_events = [
        event
        for event in events
        if event.currency == currency
        and account_id in (event.source_account_id, event.destination_account_id)
    ]
    result = []
    for event in account_events[-limit:]:
        result.append(
            {
                "event_id": str(event.event_id),
                "as_of_timestamp": event.occurred_at,
                "features": calculate_account_features(
                    account_id, currency, event.occurred_at, events
                ),
            }
        )
    return list(reversed(result))


def write_csv(path: str | Path, rows: list[tuple[UUID, str, FeatureEvent, dict]]) -> None:
    fields = ["event_id", "account_id", "currency", "as_of_timestamp", *FEATURE_NAMES]
    with Path(path).open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        for account_id, currency, event, values in rows:
            writer.writerow(
                {
                    "event_id": event.event_id,
                    "account_id": account_id,
                    "currency": currency,
                    "as_of_timestamp": event.occurred_at.isoformat(),
                    **values,
                }
            )


def export_training_csv(session_factory, path: str | Path) -> int:
    cursor = None
    rows_from_db = []
    while True:
        with session_factory() as session:
            page = _query_page(session, cursor, 1000)
            page = [(outbox, transaction) for outbox, transaction in page]
        if not page:
            break
        rows_from_db.extend(page)
        last_outbox, _ = page[-1]
        cursor = (last_outbox.created_at, last_outbox.id)
    events = [_to_feature_event(outbox, transaction) for outbox, transaction in rows_from_db]
    accounts = sorted(
        {
            (account_id, event.currency)
            for event in events
            for account_id in (event.source_account_id, event.destination_account_id)
        },
        key=lambda item: (str(item[0]), item[1]),
    )
    output_rows = []
    for account_id, currency in accounts:
        currency_events = [event for event in events if event.currency == currency]
        for event in currency_events:
            if account_id not in (event.source_account_id, event.destination_account_id):
                continue
            output_rows.append(
                (
                    account_id,
                    currency,
                    event,
                    calculate_account_features(
                        account_id, currency, event.occurred_at, currency_events
                    ),
                )
            )
    write_csv(path, output_rows)
    return len(output_rows)
