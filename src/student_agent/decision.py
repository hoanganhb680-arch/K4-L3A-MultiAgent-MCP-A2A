from __future__ import annotations

from collections import Counter
from decimal import Decimal
from typing import Any

from .entities import (
    extract_item_ids,
    extract_order_ids,
    extract_payment_references,
    extract_seller_ids,
    extract_shipment_ids,
)
from .models import CaseFacts, Evidence

TWO_PLACES = Decimal("0.01")


def money(value: Any) -> Decimal:
    """Parse a monetary string/number into a :class:`~decimal.Decimal`."""
    return Decimal(str(value))


# Descriptive, non-authoritative output labels. Refund *amounts* never come from
# these maps: they come from the MCP policy evidence (or are 0 when evidence is
# insufficient).
_CAUSE_CODES: dict[str, list[str]] = {
    "canceled_order_paid": ["ORDER_CANCELED_AFTER_PAYMENT"],
    "unavailable_order_paid": ["ITEM_UNAVAILABLE_AFTER_PAYMENT"],
    "late_delivery_seller": ["SELLER_LATE_HANDOFF"],
    "late_delivery_logistics": ["LOGISTICS_TRANSIT_DELAY"],
    "valid_split_payment": ["VALID_SPLIT_PAYMENT_NO_ISSUE"],
    "payment_mismatch": ["PAYMENT_AMOUNT_MISMATCH"],
    "duplicate_charge": ["PAYMENT_DUPLICATE_CAPTURE"],
    "refund_pending": ["REFUND_PROCESSING_PENDING"],
    "refund_failed": ["REFUND_PROCESSING_FAILED"],
    "unsupported_claim": ["CLAIM_NOT_SUPPORTED_BY_EVIDENCE"],
    "insufficient_evidence": ["INSUFFICIENT_EVIDENCE"],
}

_REASON_CODES: dict[str, str] = {
    "canceled_order_paid": "CANCELED_ORDER_REFUND",
    "unavailable_order_paid": "UNAVAILABLE_ITEM_REFUND",
    "late_delivery_seller": "SELLER_LATE_FREIGHT_REFUND",
    "late_delivery_logistics": "LOGISTICS_LATE_FREIGHT_REFUND",
    "payment_mismatch": "PAYMENT_MISMATCH_ADJUSTMENT",
    "duplicate_charge": "DUPLICATE_CHARGE_REFUND",
    "refund_failed": "FAILED_REFUND_RETRY",
}

_ACTIONS: dict[str, list[str]] = {
    "canceled_order_paid": ["Issue refund for canceled order", "Reverse captured payment"],
    "unavailable_order_paid": ["Issue refund for unavailable item"],
    "late_delivery_seller": ["Refund freight for seller delay"],
    "late_delivery_logistics": ["Refund freight for logistics delay"],
    "valid_split_payment": ["Document valid split payment"],
    "payment_mismatch": ["Reconcile payment mismatch"],
    "duplicate_charge": ["Refund duplicate charge", "Deduplicate payment records"],
    "refund_pending": ["Monitor pending refund"],
    "refund_failed": ["Retry failed refund"],
    "unsupported_claim": ["Document unsupported claim"],
    "insufficient_evidence": ["Request additional evidence"],
}

# Evidence actually cited for each issue, keyed by the evidence kind stored on
# CaseFacts. Irrelevant domains (e.g. shipment on a pure payment case) are dropped to
# keep evidence precision high.
_ISSUE_EVIDENCE: dict[str, list[str]] = {
    "canceled_order_paid": ["order", "payments", "payment_timeline", "policy"],
    "unavailable_order_paid": [
        "order", "items", "sellers", "payments", "payment_timeline", "policy"
    ],
    "late_delivery_seller": ["order", "items", "sellers", "shipment", "policy"],
    "late_delivery_logistics": ["order", "shipment", "policy"],
    "valid_split_payment": ["items", "payments", "payment_timeline", "policy"],
    "payment_mismatch": ["items", "payments", "payment_timeline", "policy"],
    "duplicate_charge": ["payments", "payment_timeline", "policy"],
    "refund_pending": ["refund", "payments", "payment_timeline", "policy"],
    "refund_failed": ["refund", "payments", "payment_timeline", "policy"],
    "unsupported_claim": ["order", "payments", "payment_timeline", "refund", "shipment", "policy"],
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
            if (previous.get("price"), previous.get("freight_value")) != (
                item.get("price"),
                item.get("freight_value"),
            ):
                return None
            continue
        unique_items[item_id] = item
    return sum(
        (money(item["price"]) + money(item["freight_value"]) for item in unique_items.values()),
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
    if _has_duplicate_payment(facts.payments, facts.payment_events):
        return "duplicate_charge", False

    # 3. Late delivery — responsibility comes from the authoritative shipment actor.
    if _is_late(facts.shipment):
        actor = _late_actor(facts.shipment)
        if actor == "seller":
            return "late_delivery_seller", False
        if actor == "logistics_provider":
            return "late_delivery_logistics", False

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
        if abs(paid - expected) > TWO_PLACES:
            return "payment_mismatch", False
        if _has_split(facts.payments) and captured > 0:
            return "valid_split_payment", False

    return "unsupported_claim", False


def has_required_evidence(issue: str, facts: CaseFacts) -> bool:
    """Whether the critical evidence for ``issue`` has been retrieved from MCP."""
    if "order" in facts.missing:
        return False
    payment_scoped = {
        "canceled_order_paid",
        "unavailable_order_paid",
        "duplicate_charge",
        "payment_mismatch",
        "refund_failed",
        "valid_split_payment",
    }
    if issue in payment_scoped and (
        "payments" in facts.missing or "payment_timeline" in facts.missing
    ):
        return False
    if issue in {"payment_mismatch", "valid_split_payment"} and "items" in facts.missing:
        return False
    if issue in {"unavailable_order_paid", "late_delivery_seller"} and (
        "items" in facts.missing or "sellers" in facts.missing
    ):
        return False
    if issue in {"refund_failed", "refund_pending"} and not facts.refund_events:
        return False
    return not (
        issue in {"late_delivery_seller", "late_delivery_logistics"}
        and "shipment" in facts.missing
    )


def policy_rule(facts: CaseFacts, issue: str) -> dict[str, Any] | None:
    """Return the normalized rule for ``issue`` from MCP policy evidence, or None.

    No static fallback amounts are used: if the authoritative policy is missing, the
    caller must downgrade to ``insufficient_evidence`` instead of fabricating a refund.
    """
    rules = facts.policy_rules.get("rules") or {}
    rule = rules.get(issue)
    if rule is None:
        return None
    parties = rule.get("responsible_parties") or []
    return {
        "case_status": rule.get("case_status", "needs_investigation"),
        "recommended_action": rule.get("recommended_action", ""),
        "refund_brl": rule.get("refund_brl", "0.00"),
        "party_type": parties[0].get("party_type") if parties else "unknown",
    }


def _confidence(issue: str, ambiguous: bool, facts: CaseFacts) -> float:
    if issue == "insufficient_evidence" or not has_required_evidence(issue, facts):
        return 0.45
    if ambiguous:
        return 0.88
    if facts.missing:
        return 0.65
    return 0.95


def select_evidence(issue: str, facts: CaseFacts) -> list[Evidence]:
    """Pick only the evidence that directly supports ``issue`` (precision over recall)."""
    kinds = list(_ISSUE_EVIDENCE.get(issue, []))
    if issue == "insufficient_evidence":
        # No issue to support: cite whatever was actually retrieved, honestly.
        return list(facts.evidence.values())
    if any(conflict["field"].startswith("item_amount:") for conflict in _detect_conflicts(facts)):
        kinds.append("items")
    return [facts.evidence[kind] for kind in dict.fromkeys(kinds) if kind in facts.evidence]


def _entity_sets(facts: CaseFacts) -> dict[str, list[str]]:
    return {
        "order_ids": extract_order_ids(facts),
        "item_ids": extract_item_ids(facts),
        "seller_ids": extract_seller_ids(facts),
        "payment_references": extract_payment_references(facts),
        "shipment_ids": extract_shipment_ids(facts),
    }


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
        if previous is not None and (
            previous.get("price"), previous.get("freight_value")
        ) != (item.get("price"), item.get("freight_value")):
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
    issue: str,
    confidence: float,
    refund: Decimal,
    claim_refs: list[str],
    refund_refs: list[str],
) -> list[dict[str, Any]]:
    claims = case.get("customer_request", {}).get("claims", [])
    assessments: list[dict[str, Any]] = []
    for claim in claims[:1]:
        if issue == "insufficient_evidence":
            verdict = "insufficient_evidence"
        elif issue == "unsupported_claim" or (
            issue == "valid_split_payment" and claim.get("topic") != "valid_split_payment"
        ):
            verdict = "unsupported"
        elif claim.get("topic") == issue:
            verdict = "supported"
        else:
            verdict = "unsupported"
        assessments.append(
            {
                "claim_id": claim.get("claim_id") or f"{case['case_id']}-claim-a",
                "verdict": verdict,
                "confidence": confidence,
                "evidence_refs": claim_refs,
            }
        )
    for claim in claims[1:2]:
        if issue == "insufficient_evidence":
            verdict = "insufficient_evidence"
        elif refund > 0 or issue == "refund_pending":
            verdict = "partially_supported"
        else:
            verdict = "unsupported"
        assessments.append(
            {
                "claim_id": claim.get("claim_id") or f"{case['case_id']}-claim-b",
                "verdict": verdict,
                "confidence": confidence,
                "evidence_refs": [ref for ref in refund_refs if ref in claim_refs],
            }
        )
    return assessments


def _refund_evidence_refs(facts: CaseFacts) -> list[str]:
    kinds = ["refund", "payments", "payment_timeline", "policy"]
    return [facts.evidence[kind].evidence_ref for kind in kinds if kind in facts.evidence]


def build_output(case: dict[str, Any], facts: CaseFacts) -> dict[str, Any]:
    case_id = facts.case_id
    issue, ambiguous = determine_issue(facts)

    if issue != "insufficient_evidence" and not has_required_evidence(issue, facts):
        issue = "insufficient_evidence"
        ambiguous = False

    rule = None if issue == "insufficient_evidence" else policy_rule(facts, issue)
    if issue != "insufficient_evidence" and rule is None:
        # Authoritative policy is required for a concrete resolution.
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
        party_type = rule["party_type"]
        recommended_action = rule["recommended_action"]

    confidence = _confidence(issue, ambiguous, facts)

    evidence = select_evidence(issue, facts)
    evidence_refs = [item.evidence_ref for item in evidence]

    refund_lines: list[dict[str, Any]] = []
    if refund > 0:
        refund_lines.append(
            {
                "reason_code": _REASON_CODES.get(issue, "REFUND"),
                "amount_brl": float(refund),
                "entity_id": facts.order_id,
            }
        )

    party_id: str | None = None
    if party_type == "seller":
        seller_ids = extract_seller_ids(facts)
        party_id = seller_ids[0] if seller_ids else None
    if issue == "insufficient_evidence":
        party_id = None

    ranked_causes = [
        {"cause_code": cause, "rank": index}
        for index, cause in enumerate(_CAUSE_CODES[issue], start=1)
    ]

    actions = list(_ACTIONS[issue])
    if recommended_action and recommended_action not in actions:
        actions.insert(0, recommended_action)

    return {
        "schema_version": "day09-l3a-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": issue,
            "case_status": case_status,
            "confidence": confidence,
        },
        "affected_entities": _entity_sets(facts),
        "claim_assessments": _claim_assessments(
            case,
            issue,
            confidence,
            refund,
            evidence_refs,
            _refund_evidence_refs(facts),
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
    payments: list[dict[str, Any]], payment_events: list[dict[str, Any]] | None = None
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
    confirmed_captures = [
        event
        for event in events
        if event.get("event_type") == "captured" and event.get("status") == "confirmed"
    ]
    return (
        len(signatures) >= 2
        and all(count == 2 for count in signatures.values())
        and len(confirmed_captures) == len(payments)
        and Counter(str(event.get("amount_brl")) for event in confirmed_captures)
        == Counter(str(row.get("payment_value")) for row in payments)
    )


def _is_late(shipment: dict[str, Any]) -> bool:
    customer = shipment.get("delivered_customer_at")
    estimated = shipment.get("estimated_delivery_at")
    return bool(customer and estimated and customer > estimated)


def _late_actor(shipment: dict[str, Any]) -> str | None:
    for event in shipment.get("events", []):
        if event.get("event_type") == "delivered_late":
            actor = event.get("actor")
            if actor in {"seller", "logistics_provider"}:
                return actor
    return None
