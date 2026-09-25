from __future__ import annotations

from collections import Counter
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from .entities import (
    clean_id,
    extract_item_ids,
    extract_order_ids,
    extract_payment_references,
    extract_seller_ids,
    extract_shipment_ids,
)
from .models import CaseFacts, Evidence

TWO_PLACES = Decimal("0.01")

_PAYMENT_TOPICS = {"valid_split_payment", "payment_mismatch", "duplicate_charge"}
_ORDER_PAYMENT_TOPICS = {"canceled_order_paid", "unavailable_order_paid"}
_LATE_TOPICS = {"late_delivery_seller", "late_delivery_logistics", "late_delivery"}
_REFUND_TOPICS = {"refund_pending", "refund_failed", "requested_full_refund"}


def money(value: Any) -> Decimal:
    """Parse a monetary string/number into a :class:`~decimal.Decimal`."""
    return Decimal(str(value))


def _item_amounts(item: dict[str, Any]) -> tuple[Decimal, Decimal] | None:
    try:
        price, freight = money(item["price"]), money(item["freight_value"])
    except (KeyError, InvalidOperation, TypeError, ValueError):
        return None
    return (price, freight) if price.is_finite() and freight.is_finite() else None


# Evidence actually cited for each issue, keyed by the evidence kind stored on
# CaseFacts. Irrelevant domains (e.g. shipment on a pure payment case) are dropped to
# keep evidence precision high.
_ISSUE_EVIDENCE: dict[str, list[str]] = {
    "canceled_order_paid": ["order", "payment_timeline", "policy"],
    "unavailable_order_paid": ["order", "items", "sellers", "payment_timeline", "policy"],
    "late_delivery_seller": ["order", "items", "sellers", "shipment", "policy"],
    "late_delivery_logistics": ["order", "shipment", "policy"],
    "valid_split_payment": ["order", "items", "payments", "payment_timeline", "policy"],
    "payment_mismatch": ["order", "items", "payments", "payment_timeline", "policy"],
    "duplicate_charge": ["order", "payments", "payment_timeline", "policy"],
    "refund_pending": ["order", "refund", "policy"],
    "refund_failed": ["order", "refund", "policy"],
    "unsupported_claim": ["order", "policy"],
    "insufficient_evidence": [],
}


def _captured_total(payment_events: list[dict[str, Any]]) -> Decimal:
    """Sum of authoritative successful (confirmed captured) payments."""
    total = Decimal("0.00")
    for event in payment_events:
        if event.get("event_type") == "captured" and event.get("status") == "confirmed":
            total += money(event.get("amount_brl", "0"))
    return total


def _has_event(events: list[dict[str, Any]], event_type: str, status: str | None) -> bool:
    return any(
        event.get("event_type") == event_type and (status is None or event.get("status") == status)
        for event in events
    )


def _expected_total(facts: CaseFacts) -> Decimal | None:
    if "items" not in facts.evidence or not facts.items:
        return None
    # A repeated order_item_id is the same logical line. If its monetary fields
    # disagree, the obligation cannot be determined from these rows.
    unique_items: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(facts.items):
        item_id = str(item.get("order_item_id") or f"row:{index}")
        previous = unique_items.get(item_id)
        if previous is not None:
            if _item_amounts(previous) != _item_amounts(item):
                return None
            continue
        unique_items[item_id] = item
    amounts = [_item_amounts(item) for item in unique_items.values()]
    if any(value is None for value in amounts):
        return None
    return sum(
        (price + freight for price, freight in amounts if price is not None),
        Decimal("0"),
    )


def _payment_total(facts: CaseFacts) -> Decimal | None:
    if "payments" not in facts.evidence or not facts.payments:
        return None
    return sum((money(row["payment_value"]) for row in facts.payments), Decimal("0"))


def _has_split(payments: list[dict[str, Any]]) -> bool:
    """Two payment records are a split candidate regardless of their amounts."""
    return len(payments) >= 2


def _has_failed_refund(refund_events: list[dict[str, Any]]) -> bool:
    return _has_event(refund_events, "refund_requested", "failed")


def _is_pending_refund(refund_events: list[dict[str, Any]]) -> bool:
    return _has_event(refund_events, "refund_requested", "pending")


def determine_issue(facts: CaseFacts) -> tuple[str, bool]:
    """Evidence-first classification of the primary issue.

    Returns ``(primary_issue, is_ambiguous)``. The customer claim never selects the issue.
    """
    status = facts.order_status
    captured = _captured_total(facts.payment_events)

    # 1. A terminated order that still captured money is a refund situation.
    if status == "canceled":
        if captured > 0:
            return "canceled_order_paid", False
        return "insufficient_evidence", False
    if status == "unavailable":
        if captured > 0:
            return "unavailable_order_paid", False
        return "insufficient_evidence", False

    # 2. Duplicate capture (same payment row charged twice).
    duplicate_expected = _expected_total(facts) if len(facts.payments) >= 4 else None
    if _has_duplicate_payment(facts.payments, facts.payment_events, duplicate_expected):
        return "duplicate_charge", False

    # 3. Late delivery — responsibility comes from the authoritative shipment actor.
    if _is_late(facts.shipment):
        actor = _late_actor(facts.shipment)
        if actor == "seller":
            return "late_delivery_seller", False
        if actor == "logistics_provider":
            return "late_delivery_logistics", False
        return "insufficient_evidence", False

    # 4. Refund lifecycle evidence is authoritative for the refund outcomes.
    if _has_failed_refund(facts.refund_events):
        return "refund_failed", False
    if _is_pending_refund(facts.refund_events):
        return "refund_pending", False

    # 5. A reconciliation mismatch flagged by the payment timeline.
    if _has_event(facts.payment_events, "reconciliation_mismatch", None):
        return "payment_mismatch", False

    # 6. A valid split/installment payment with no error evidence above.
    expected = _expected_total(facts)
    paid = _payment_total(facts)
    if expected is not None and paid is not None:
        if captured > 0 and abs(captured - paid) > TWO_PLACES:
            return "insufficient_evidence", False
        if abs(paid - expected) > TWO_PLACES:
            return "payment_mismatch", False
        if _has_split(facts.payments) and abs(captured - paid) <= TWO_PLACES:
            return "valid_split_payment", False

    return "unsupported_claim", False


def has_required_evidence(issue: str, facts: CaseFacts) -> bool:
    """Whether the critical evidence for ``issue`` has been retrieved from MCP."""
    if "order" not in facts.evidence:
        return False
    payment_scoped = {"duplicate_charge", "payment_mismatch", "valid_split_payment"}
    if issue in payment_scoped and not {"payments", "payment_timeline"} <= facts.evidence.keys():
        return False
    if issue in _ORDER_PAYMENT_TOPICS and "payment_timeline" not in facts.evidence:
        return False
    if issue in {"payment_mismatch", "valid_split_payment"} and "items" not in facts.evidence:
        return False
    if issue in {"unavailable_order_paid", "late_delivery_seller"} and not {
        "items", "sellers"
    } <= facts.evidence.keys():
        return False
    if issue in {"refund_failed", "refund_pending"} and (
        "refund" not in facts.evidence or not facts.refund_events
    ):
        return False
    return not (
        issue in {"late_delivery_seller", "late_delivery_logistics"}
        and "shipment" not in facts.evidence
    )


def _claim_evidence_kinds(topic: str) -> set[str]:
    if topic in _ORDER_PAYMENT_TOPICS:
        return {"order", "payment_timeline"}
    if topic in _LATE_TOPICS:
        return {"order", "shipment"}
    if topic in _PAYMENT_TOPICS:
        return {"order", "payments", "payment_timeline"}
    if topic in {"refund_pending", "refund_failed"}:
        return {"order", "refund"}
    if topic == "requested_full_refund":
        return {"order", "payment_timeline", "policy"}
    return {"order"}


def _claim_has_evidence(topic: str, facts: CaseFacts) -> bool:
    required = _claim_evidence_kinds(topic)
    ordinary = required - {"refund"}
    if topic in _LATE_TOPICS and facts.order_status == "delivered" and (
        _timestamp(facts.shipment.get("delivered_customer_at")) is None
        or _timestamp(facts.shipment.get("estimated_delivery_at")) is None
    ):
        return False
    return ordinary <= facts.evidence.keys() and (
        "refund" not in required
        or facts.refund_lookup_status in {"found", "not_found"}
    )


def has_sufficient_evidence_to_reject_claim(case: dict[str, Any], facts: CaseFacts) -> bool:
    """An unsupported verdict requires successful reads in every claimed domain."""
    claims = case.get("customer_request", {}).get("claims", [])[:5]
    topics = [claim.get("topic", "") for claim in claims] or [facts.topic]
    return all(_claim_has_evidence(topic, facts) for topic in topics)


def policy_rule(facts: CaseFacts, issue: str) -> dict[str, Any] | None:
    """Return the normalized rule for ``issue`` from MCP policy evidence, or None.

    No static fallback amounts are used: if the authoritative policy is missing, the
    caller must downgrade to ``insufficient_evidence`` instead of fabricating a refund.
    """
    rules = facts.policy_rules.get("rules")
    if not isinstance(rules, dict):
        return None
    rule = rules.get(issue)
    if not isinstance(rule, dict):
        return None
    required = {"case_status", "recommended_action", "refund_brl", "responsible_parties"}
    if not required <= rule.keys():
        return None
    try:
        amount = money(rule["refund_brl"])
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not amount.is_finite() or amount < 0:
        return None
    parties = rule["responsible_parties"]
    if not isinstance(parties, list) or not parties or not isinstance(parties[0], dict):
        return None
    party_type = parties[0].get("party_type")
    if party_type not in {
        "seller", "platform", "logistics_provider", "payment_provider", "customer", "unknown"
    }:
        return None
    if rule["case_status"] not in {"action_required", "no_action", "needs_investigation"}:
        return None
    if not isinstance(rule["recommended_action"], str):
        return None
    return {
        "case_status": rule["case_status"],
        "recommended_action": rule["recommended_action"],
        "refund_brl": amount,
        "party_type": party_type,
        "cause_code": rule.get("cause_code") or rule.get("root_cause_code") or issue.upper(),
        "refund_reason_code": rule.get("refund_reason_code") or rule.get("reason_code"),
    }


def _confidence(issue: str, ambiguous: bool, facts: CaseFacts) -> float:
    if issue == "insufficient_evidence" or not has_required_evidence(issue, facts):
        return 0.45
    if ambiguous:
        return 0.88
    if any(kind in _ISSUE_EVIDENCE.get(issue, []) for kind in facts.missing):
        return 0.65
    return 0.95


def select_evidence(issue: str, facts: CaseFacts, case: dict[str, Any]) -> list[Evidence]:
    """Cite evidence used for the issue and the assessed customer claims."""
    kinds = list(_ISSUE_EVIDENCE.get(issue, []))
    if issue == "insufficient_evidence":
        kinds = []
    for claim in case.get("customer_request", {}).get("claims", [])[:5]:
        topic = claim.get("topic", "")
        if topic == "requested_full_refund" and issue not in _ORDER_PAYMENT_TOPICS:
            kinds.append("payment_timeline")
        elif topic != "requested_full_refund":
            kinds.extend(sorted(_claim_evidence_kinds(topic)))
    if issue in _ORDER_PAYMENT_TOPICS and extract_payment_references(facts):
        kinds.append("payments")
    if not kinds:
        kinds = ["order"]
    if any(conflict["field"].startswith("item_amount:") for conflict in _detect_conflicts(facts)):
        kinds.append("items")
    return [facts.evidence[kind] for kind in dict.fromkeys(kinds) if kind in facts.evidence]


def _entity_sets(issue: str, facts: CaseFacts) -> dict[str, list[str]]:
    entities = {
        "order_ids": extract_order_ids(facts),
        "item_ids": [],
        "seller_ids": [],
        "payment_references": [],
        "shipment_ids": [],
    }
    if issue == "unavailable_order_paid":
        entities["item_ids"] = extract_item_ids(facts)
        entities["seller_ids"] = extract_seller_ids(facts)
    elif issue == "late_delivery_seller":
        carrier_at = _timestamp(facts.shipment.get("delivered_carrier_at"))
        affected = [
            item for item in facts.items
            if carrier_at and _timestamp(item.get("shipping_limit_date"))
            and _timestamp(item["shipping_limit_date"]) < carrier_at
        ]
        entities["item_ids"] = sorted(
            {value for item in affected if (value := clean_id(item.get("order_item_id")))}
        )[:20]
        entities["seller_ids"] = sorted(
            {value for item in affected if (value := clean_id(item.get("seller_id")))}
        )[:20]
        if affected and not entities["seller_ids"]:
            known_sellers = extract_seller_ids(facts)
            if len(known_sellers) == 1:
                entities["seller_ids"] = known_sellers
    if issue in _ORDER_PAYMENT_TOPICS | _PAYMENT_TOPICS:
        entities["payment_references"] = extract_payment_references(facts)
    if issue in {"late_delivery_seller", "late_delivery_logistics"}:
        entities["shipment_ids"] = extract_shipment_ids(facts)
    return entities


def _detect_conflicts(facts: CaseFacts) -> list[dict[str, Any]]:
    """Record real conflicts between authoritative sources (never fabricated)."""
    conflicts: list[dict[str, Any]] = []
    order_status = facts.order_status
    shipment_status = facts.shipment.get("order_status")
    statuses_conflict = (
        order_status
        and shipment_status
        and order_status != "unknown"
        and order_status != shipment_status
    )
    if statuses_conflict:
        conflicts.append(
            {
                "field": "order_status",
                "sources": ["order", "shipment"],
                "selected_source": "order",
                "resolution_code": "ORDER_SOURCE_AUTHORITATIVE",
            }
        )
    seen_items: dict[str, dict[str, Any]] = {}
    for item in facts.items:
        item_id = item.get("order_item_id")
        if item_id is None:
            continue
        item_key = str(item_id)
        previous = seen_items.get(item_key)
        if previous is not None and _item_amounts(previous) != _item_amounts(item):
            conflicts.append(
                {
                    "field": f"item_amount:{item_key}"[:100],
                    "sources": ["order_items:first_row", "order_items:repeated_row"],
                    "selected_source": None,
                    "resolution_code": "CONFLICTING_ITEM_ROWS",
                }
            )
            break
        seen_items[item_key] = item
    return conflicts


def _claim_assessments(
    case: dict[str, Any],
    facts: CaseFacts,
    issue: str,
    confidence: float,
    refund: Decimal,
    final_refs: list[str],
) -> list[dict[str, Any]]:
    claims = case.get("customer_request", {}).get("claims", [])
    assessments: list[dict[str, Any]] = []
    for index, claim in enumerate(claims[:5]):
        topic = claim.get("topic", "")
        verdict = assess_claim(topic, issue, facts, refund)
        assessments.append(
            {
                "claim_id": claim.get("claim_id") or f"claim-{index + 1}",
                "verdict": verdict,
                "confidence": 0.45 if verdict == "insufficient_evidence" else confidence,
                "evidence_refs": select_claim_evidence(topic, facts, final_refs),
            }
        )
    return assessments


def assess_claim(topic: str, issue: str, facts: CaseFacts, refund: Decimal) -> str:
    """Assess one topic using its own evidence, not its position in the claim list."""
    if issue == "insufficient_evidence" or not _claim_has_evidence(topic, facts):
        return "insufficient_evidence"
    if issue == "unsupported_claim":
        return "unsupported"
    captured = _captured_total(facts.payment_events)
    if topic == "canceled_order_paid" and facts.order_status == "canceled" and captured > 0:
        return "supported"
    if topic == "unavailable_order_paid" and facts.order_status == "unavailable" and captured > 0:
        return "supported"
    if topic == "refund_failed" and _has_failed_refund(facts.refund_events):
        return "supported"
    if topic == "refund_pending" and _is_pending_refund(facts.refund_events):
        return "supported"
    if topic in _LATE_TOPICS and _is_late(facts.shipment):
        actor = _late_actor(facts.shipment)
        if topic == "late_delivery" or (
            topic == "late_delivery_seller" and actor == "seller"
        ) or (topic == "late_delivery_logistics" and actor == "logistics_provider"):
            return "supported"
    if topic == "duplicate_charge" and _has_duplicate_payment(
        facts.payments, facts.payment_events,
        _expected_total(facts) if len(facts.payments) >= 4 else None,
    ):
        return "supported"
    if topic == "payment_mismatch" and _has_event(
        facts.payment_events, "reconciliation_mismatch", None
    ):
        return "supported"
    if topic == issue:
        return "supported"
    if topic == "requested_full_refund":
        if issue in _ORDER_PAYMENT_TOPICS and refund > 0:
            return (
                "supported"
                if refund >= _captured_total(facts.payment_events)
                else "partially_supported"
            )
        return "unsupported"
    if topic == "late_delivery" and issue in {"late_delivery_seller", "late_delivery_logistics"}:
        return "supported"
    if topic in _REFUND_TOPICS and issue in {"refund_pending", "refund_failed"}:
        return "partially_supported"
    if topic == "valid_split_payment" and issue in {"duplicate_charge", "refund_failed"}:
        return "partially_supported" if _has_split(facts.payments) else "unsupported"
    if topic in _PAYMENT_TOPICS | _ORDER_PAYMENT_TOPICS | _LATE_TOPICS | _REFUND_TOPICS | {
        "unsupported_claim"
    }:
        return "unsupported"
    return "insufficient_evidence"


def select_claim_evidence(topic: str, facts: CaseFacts, final_refs: list[str]) -> list[str]:
    kinds = _claim_evidence_kinds(topic)
    allowed = set(final_refs)
    return [
        item.evidence_ref
        for kind, item in facts.evidence.items()
        if kind in kinds and item.evidence_ref in allowed
    ]


def build_output(case: dict[str, Any], facts: CaseFacts) -> dict[str, Any]:
    case_id = facts.case_id
    issue, ambiguous = determine_issue(facts)

    cannot_reject = issue == "unsupported_claim" and not has_sufficient_evidence_to_reject_claim(
        case, facts
    )
    if issue != "insufficient_evidence" and (
        cannot_reject or not has_required_evidence(issue, facts)
    ):
        issue = "insufficient_evidence"
        ambiguous = False

    rule = None if issue == "insufficient_evidence" else policy_rule(facts, issue)
    if issue != "insufficient_evidence" and rule is None:
        # Authoritative policy is required for a concrete resolution.
        issue = "insufficient_evidence"
        ambiguous = False
        rule = None
    if rule is not None and rule["party_type"] == "seller" and not _entity_sets(
        issue, facts
    )["seller_ids"]:
        issue = "insufficient_evidence"
        ambiguous = False
        rule = None

    if issue == "insufficient_evidence":
        case_status = "needs_investigation"
        refund = Decimal("0.00")
        party_type = "unknown"
        recommended_action = ""
    else:
        assert rule is not None
        case_status = rule["case_status"]
        refund = money(rule["refund_brl"]).quantize(TWO_PLACES)
        if issue in {"canceled_order_paid", "unavailable_order_paid"}:
            refund = _captured_total(facts.payment_events).quantize(TWO_PLACES)
        elif issue == "duplicate_charge":
            duplicates = [
                money(event.get("amount_brl", "0"))
                for event in facts.payment_events
                if event.get("event_type") in {"duplicate_capture", "duplicate_charge"}
                and event.get("status") in {"confirmed", "duplicate"}
            ]
            if duplicates:
                refund = sum(duplicates, Decimal("0")).quantize(TWO_PLACES)
        elif issue == "refund_failed":
            failures = [
                money(event.get("amount_brl", "0"))
                for event in facts.refund_events
                if event.get("event_type") == "refund_requested"
                and event.get("status") == "failed"
            ]
            if failures:
                refund = failures[-1].quantize(TWO_PLACES)
        elif issue == "late_delivery_seller" and rule["recommended_action"] == "refund_freight":
            carrier_at = _timestamp(facts.shipment.get("delivered_carrier_at"))
            affected = [
                item
                for item in facts.items
                if carrier_at and _timestamp(item.get("shipping_limit_date"))
                and _timestamp(item["shipping_limit_date"]) < carrier_at
            ]
            if affected:
                refund = sum(
                    (money(item["freight_value"]) for item in affected), Decimal("0")
                ).quantize(TWO_PLACES)
        elif issue == "payment_mismatch":
            expected = _expected_total(facts)
            paid = _payment_total(facts)
            if expected is not None and paid is not None:
                refund = max(paid - expected, Decimal("0")).quantize(TWO_PLACES)
            else:
                mismatch_events = [
                    event for event in facts.payment_events
                    if event.get("event_type") == "reconciliation_mismatch"
                    and event.get("amount_brl") is not None
                ]
                if mismatch_events:
                    refund = money(mismatch_events[-1]["amount_brl"]).quantize(TWO_PLACES)
        party_type = rule["party_type"]
        recommended_action = rule["recommended_action"]

    confidence = _confidence(issue, ambiguous, facts)

    evidence = select_evidence(issue, facts, case)
    evidence_refs = [item.evidence_ref for item in evidence]

    refund_lines: list[dict[str, Any]] = []
    if refund > 0:
        refund_lines.append(
            {
                "reason_code": rule["refund_reason_code"] or rule["cause_code"],
                "amount_brl": float(refund),
                "entity_id": facts.order_id,
            }
        )

    entities = _entity_sets(issue, facts)
    party_id: str | None = None
    if party_type == "seller":
        party_id = entities["seller_ids"][0]
    if issue == "insufficient_evidence":
        party_id = None

    cause_code = "INSUFFICIENT_EVIDENCE" if rule is None else rule["cause_code"]
    ranked_causes = [{"cause_code": cause_code, "rank": 1}]
    actions = [recommended_action] if recommended_action else []

    return {
        "schema_version": "day09-l3a-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": issue,
            "case_status": case_status,
            "confidence": confidence,
        },
        "affected_entities": entities,
        "claim_assessments": _claim_assessments(
            case,
            facts,
            issue,
            confidence,
            refund,
            evidence_refs,
        ),
        "root_cause_analysis": {
            "ranked_causes": ranked_causes,
            "responsible_parties": [{"party_type": party_type, "party_id": party_id}],
        },
        "evidence_refs": evidence_refs,
        "data_conflicts": _detect_conflicts(facts),
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": float(refund),
            "refund_lines": refund_lines,
        },
        "resolution_actions": actions,
    }


def _has_duplicate_payment(
    payments: list[dict[str, Any]],
    payment_events: list[dict[str, Any]] | None = None,
    expected_total: Decimal | None = None,
) -> bool:
    """Require a marker, repeated capture identifier, or corroborated payment batch.

    The MCP payment rows can repeat the same amount, type and sequence for separate
    transactions, so their plain row signature cannot establish a duplicate charge.
    """
    events = payment_events or []
    if any(
        event.get("event_type") in {"duplicate_capture", "duplicate_charge"}
        and event.get("status") in {"confirmed", "duplicate"}
        for event in events
    ):
        return True
    for key in ("capture_id", "transaction_id", "payment_reference"):
        identifiers = [event.get(key) for event in events if event.get("event_type") == "captured"]
        identifiers = [identifier for identifier in identifiers if identifier]
        if len(identifiers) > len(set(identifiers)):
            return True
    if any(row.get("duplicate_capture") is True for row in payments):
        return True
    signatures = Counter(
        (
            row.get("payment_sequential"),
            row.get("payment_type"),
            row.get("payment_value"),
        )
        for row in payments
    )
    if expected_total is None or len(signatures) < 2 or not all(
        count == 2 for count in signatures.values()
    ):
        return False
    confirmed_captures = [
        event
        for event in events
        if event.get("event_type") == "captured" and event.get("status") == "confirmed"
    ]
    payment_total = sum((money(row["payment_value"]) for row in payments), Decimal("0"))
    captured_total = _captured_total(confirmed_captures)
    return (
        payment_total > expected_total + TWO_PLACES
        and abs(captured_total - payment_total) <= TWO_PLACES
        and len(confirmed_captures) == len(payments)
        and Counter(money(event["amount_brl"]) for event in confirmed_captures)
        == Counter(money(row["payment_value"]) for row in payments)
    )


def _is_late(shipment: dict[str, Any]) -> bool:
    customer = _timestamp(shipment.get("delivered_customer_at"))
    estimated = _timestamp(shipment.get("estimated_delivery_at"))
    return bool(customer and estimated and customer > estimated)


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _late_actor(shipment: dict[str, Any]) -> str | None:
    for event in shipment.get("events", []):
        if event.get("event_type") == "delivered_late" and event.get("status") == "confirmed":
            actor = event.get("actor")
            if actor in {"seller", "logistics_provider"}:
                return actor
    return None
