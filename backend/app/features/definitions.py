"""Single registry for feature names, windows, and semantics."""

from dataclasses import dataclass
from datetime import timedelta


@dataclass(frozen=True)
class Window:
    name: str
    duration: timedelta


WINDOWS = (
    Window("60s", timedelta(seconds=60)),
    Window("15m", timedelta(minutes=15)),
    Window("60m", timedelta(minutes=60)),
    Window("24h", timedelta(hours=24)),
    Window("7d", timedelta(days=7)),
)

FEATURE_SCHEMA_VERSION = "fraud-features-v1"
PROJECTION_VERSION = "v1"
FEATURE_DEFINITIONS = {
    "outgoing_transaction_count_60s": {
        "window": "60s",
        "direction": "outgoing",
        "aggregation": "count",
        "unit": "transactions",
    },
    "outgoing_transaction_count_15m": {
        "window": "15m",
        "direction": "outgoing",
        "aggregation": "count",
        "unit": "transactions",
    },
    "outgoing_transaction_count_60m": {
        "window": "60m",
        "direction": "outgoing",
        "aggregation": "count",
        "unit": "transactions",
    },
    "incoming_transaction_count_60m": {
        "window": "60m",
        "direction": "incoming",
        "aggregation": "count",
        "unit": "transactions",
    },
    "outgoing_amount_minor_15m": {
        "window": "15m",
        "direction": "outgoing",
        "aggregation": "sum",
        "unit": "currency_minor_units",
    },
    "outgoing_amount_minor_60m": {
        "window": "60m",
        "direction": "outgoing",
        "aggregation": "sum",
        "unit": "currency_minor_units",
    },
    "outgoing_amount_minor_24h": {
        "window": "24h",
        "direction": "outgoing",
        "aggregation": "sum",
        "unit": "currency_minor_units",
    },
    "outgoing_amount_minor_7d": {
        "window": "7d",
        "direction": "outgoing",
        "aggregation": "sum",
        "unit": "currency_minor_units",
    },
    "incoming_amount_minor_15m": {
        "window": "15m",
        "direction": "incoming",
        "aggregation": "sum",
        "unit": "currency_minor_units",
    },
    "incoming_amount_minor_60m": {
        "window": "60m",
        "direction": "incoming",
        "aggregation": "sum",
        "unit": "currency_minor_units",
    },
    "incoming_amount_minor_24h": {
        "window": "24h",
        "direction": "incoming",
        "aggregation": "sum",
        "unit": "currency_minor_units",
    },
    "average_outgoing_amount_24h": {
        "window": "24h",
        "direction": "outgoing",
        "aggregation": "mean",
        "unit": "currency_minor_units_float",
    },
    "maximum_outgoing_amount_24h": {
        "window": "24h",
        "direction": "outgoing",
        "aggregation": "max",
        "unit": "currency_minor_units",
    },
    "unique_counterparties_24h": {
        "window": "24h",
        "direction": "incoming_and_outgoing",
        "aggregation": "distinct_counterparty_count",
        "unit": "accounts",
    },
    "transaction_amount_variance_7d": {
        "window": "7d",
        "direction": "incoming_and_outgoing",
        "aggregation": "population_variance",
        "unit": "currency_minor_units_squared_float",
    },
    "total_transaction_count_7d": {
        "window": "7d",
        "direction": "incoming_and_outgoing",
        "aggregation": "count",
        "unit": "transactions",
    },
}
FEATURE_NAMES = tuple(FEATURE_DEFINITIONS)

# Sorted-set retention covers the maximum 7d lookback plus 7d of historical
# scoring. Deduplication IDs are retained for the lifetime of a projection,
# because source outbox events currently have no enforced purge horizon.
EVENT_RETENTION = timedelta(days=14)
DEDUP_RETENTION = "projection_lifetime"
