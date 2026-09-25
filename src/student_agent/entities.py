from __future__ import annotations

from typing import Any

from .models import CaseFacts

_ID_MAX_LENGTH = 128


def clean_id(value: Any) -> str | None:
    """Normalize an identifier to a schema-safe string or ``None`` when absent/empty.

    The output schema requires ids to be strings with a maximum length; evidence may
    carry integer ids (e.g. ``order_item_id``), so every id is coerced through here.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text if len(text) <= _ID_MAX_LENGTH else None


def _unique(values: list[str | None]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def extract_order_ids(facts: CaseFacts) -> list[str]:
    rows = [
        facts.evidence["order"].data if "order" in facts.evidence else {},
        *facts.items,
        *facts.payments,
        facts.shipment,
    ]
    return _unique(
        [clean_id(row.get("order_id")) for row in rows if isinstance(row, dict)]
    )[:20]


def extract_item_ids(facts: CaseFacts) -> list[str]:
    ids = [clean_id(item.get("order_item_id")) for item in facts.items]
    return sorted(_unique(ids))


def extract_seller_ids(facts: CaseFacts) -> list[str]:
    ids = [clean_id(seller.get("seller_id")) for seller in facts.sellers]
    ids += [clean_id(item.get("seller_id")) for item in facts.items]
    return sorted(_unique(ids))


def extract_payment_references(facts: CaseFacts) -> list[str]:
    """Read canonical payment identifiers only when supplied by MCP."""
    rows = [*facts.payments, *facts.payment_events]
    return _unique(
        [
            clean_id(row.get(key))
            for row in rows
            for key in ("payment_reference", "transaction_id", "capture_id")
        ]
    )[:20]


def extract_shipment_ids(facts: CaseFacts) -> list[str]:
    """Read shipment or tracking identifiers when supplied by MCP."""
    shipment = facts.shipment
    return _unique(
        [clean_id(shipment.get(key)) for key in ("shipment_id", "tracking_id")]
        + [
            clean_id(event.get(key))
            for event in shipment.get("events", [])
            for key in ("shipment_id", "tracking_id")
        ]
    )[:20]
