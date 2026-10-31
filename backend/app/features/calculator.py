"""Deterministic point-in-time feature calculation shared by online/offline paths."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from app.features.definitions import FEATURE_DEFINITIONS, WINDOWS
from app.features.schemas import FeatureEvent


def normalize_time(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("feature timestamps must be timezone-aware")
    return value.astimezone(UTC)


def calculate_account_features(
    account_id: UUID,
    currency: str,
    as_of: datetime,
    events: list[FeatureEvent],
) -> dict[str, int | float]:
    """Calculate windows with inclusive lower and exclusive scoring-time boundaries."""
    as_of = normalize_time(as_of)
    relevant = sorted(
        (
            event
            for event in events
            if event.currency == currency
            and account_id in (event.source_account_id, event.destination_account_id)
            and normalize_time(event.occurred_at) < as_of
            and normalize_time(event.occurred_at) >= as_of - WINDOWS[-1].duration
        ),
        key=lambda event: (normalize_time(event.occurred_at), str(event.event_id)),
    )

    def direction(event: FeatureEvent) -> str:
        return "outgoing" if event.source_account_id == account_id else "incoming"

    def window_events(duration) -> list[FeatureEvent]:
        lower = as_of - duration
        return [event for event in relevant if lower <= normalize_time(event.occurred_at) < as_of]

    result: dict[str, int | float] = {}
    for window in WINDOWS:
        rows = window_events(window.duration)
        for flow in ("outgoing", "incoming"):
            selected = [event for event in rows if direction(event) == flow]
            if flow == "outgoing" and window.name in ("60s", "15m", "60m"):
                result[f"outgoing_transaction_count_{window.name}"] = len(selected)
            if flow == "incoming" and window.name == "60m":
                result["incoming_transaction_count_60m"] = len(selected)
            if flow == "outgoing" and window.name in ("15m", "60m", "24h", "7d"):
                result[f"outgoing_amount_minor_{window.name}"] = sum(
                    event.amount_minor for event in selected
                )
            if flow == "incoming" and window.name in ("15m", "60m", "24h"):
                result[f"{flow}_amount_minor_{window.name}"] = sum(
                    event.amount_minor for event in selected
                )

    out_24h = [
        event.amount_minor
        for event in window_events(WINDOWS[3].duration)
        if direction(event) == "outgoing"
    ]
    amounts_7d = [event.amount_minor for event in relevant]
    all_24h = window_events(WINDOWS[3].duration)
    result["average_outgoing_amount_24h"] = (
        float(Decimal(sum(out_24h)) / Decimal(len(out_24h))) if out_24h else 0.0
    )
    result["maximum_outgoing_amount_24h"] = max(out_24h, default=0)
    result["unique_counterparties_24h"] = len(
        {
            event.destination_account_id
            if direction(event) == "outgoing"
            else event.source_account_id
            for event in all_24h
        }
    )
    if amounts_7d:
        mean = Decimal(sum(amounts_7d)) / Decimal(len(amounts_7d))
        variance = sum((Decimal(amount) - mean) ** 2 for amount in amounts_7d) / Decimal(
            len(amounts_7d)
        )
        result["transaction_amount_variance_7d"] = float(variance)
    else:
        result["transaction_amount_variance_7d"] = 0.0
    result["total_transaction_count_7d"] = len(relevant)
    if result.keys() != FEATURE_DEFINITIONS.keys():
        raise RuntimeError("feature_calculator_registry_mismatch")
    return result
