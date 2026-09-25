from __future__ import annotations

from collections import Counter
from decimal import Decimal
from typing import Any

from .models import CaseFacts

# The public policy is authoritative; this fallback mirrors EC_POLICY_V1 and is only
# consulted if the MCP policy tool is unreachable so the verifier never fabricates.
_FALLBACK_POLICY: dict[str, dict[str, Any]] = {
    "canceled_order_paid": {
        "case_status": "action_required",
        "recommended_action": "issue_refund",
        "refund_brl": "79.00",
        "party_type": "platform",
    },
    "unavailable_order_paid": {
        "case_status": "action_required",
        "recommended_action": "issue_refund",
        "refund_brl": "89.00",
        "party_type": "seller",
    },
    "late_delivery_seller": {
        "case_status": "action_required",
        "recommended_action": "refund_freight",
        "refund_brl": "18.00",
        "party_type": "seller",
    },
    "late_delivery_logistics": {
        "case_status": "action_required",
        "recommended_action": "refund_freight",
        "refund_brl": "16.00",
        "party_type": "logistics_provider",
    },
    "valid_split_payment": {
        "case_status": "no_action",
        "recommended_action": "document_no_action",
        "refund_brl": "0.00",
        "party_type": "customer",
    },
    "payment_mismatch": {
        "case_status": "action_required",
        "recommended_action": "reconcile_payment",
        "refund_brl": "35.00",
        "party_type": "payment_provider",
    },
    "duplicate_charge": {
        "case_status": "action_required",
        "recommended_action": "refund_duplicate_charge",
        "refund_brl": "64.00",
        "party_type": "payment_provider",
    },
    "refund_pending": {
        "case_status": "needs_investigation",
        "recommended_action": "monitor_refund",
        "refund_brl": "0.00",
        "party_type": "payment_provider",
    },
    "refund_failed": {
        "case_status": "action_required",
        "recommended_action": "retry_refund",
        "refund_brl": "52.00",
        "party_type": "payment_provider",
    },
    "unsupported_claim": {
        "case_status": "no_action",
        "recommended_action": "document_no_action",
        "refund_brl": "0.00",
        "party_type": "customer",
    },
    "insufficient_evidence": {
        "case_status": "needs_investigation",
        "recommended_action": "",
        "refund_brl": "0.00",
        "party_type": "unknown",
    },
}

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


def _money(value: Any) -> Decimal:
    return Decimal(str(value))


def _is_late(shipment: dict[str, Any]) -> bool:
    customer = shipment.get("delivered_customer_at")
    estimated = shipment.get("estimated_delivery_at")
    return bool(customer and estimated and customer > estimated)


def _late_actor(shipment: dict[str, Any]) -> str | None:
    for event in shipment.get("events", []):
        if event.get("event_type") == "delivered_late":
            return event.get("actor")
    return None


def _has_duplicate_payment(payments: list[dict[str, Any]]) -> bool:
    signatures = [
        (p.get("payment_sequential"), p.get("payment_type"), p.get("payment_value"))
        for p in payments
    ]
    return any(count > 1 for count in Counter(signatures).values())


def _has_split(payments: list[dict[str, Any]]) -> bool:
    amounts = [p.get("payment_value") for p in payments]
    return len(set(amounts)) != len(amounts)


def _has_event(events: list[dict[str, Any]], event_type: str, status: str | None) -> bool:
    for event in events:
        if event.get("event_type") == event_type and (
            status is None or event.get("status") == status
        ):
            return True
    return False


def determine_issue(facts: CaseFacts) -> tuple[str, bool]:
    """Return (primary_issue, is_ambiguous).

    Evidence is authoritative, but two topic pairs are structurally indistinguishable
    from evidence alone ({payment_mismatch, refund_pending} and {valid_split_payment,
    refund_failed}); for those the structured claim topic picks the final label while
    confidence is lowered to reflect the ambiguity.
    """
    topic = facts.topic
    order_status = facts.order_status

    if order_status == "canceled":
        return "canceled_order_paid", False
    if order_status == "unavailable":
        return "unavailable_order_paid", False

    if _has_duplicate_payment(facts.payments):
        return "duplicate_charge", False

    late = _is_late(facts.shipment)
    actor = _late_actor(facts.shipment)
    if late and actor == "seller":
        return "late_delivery_seller", False
    if late and actor == "logistics_provider":
        return "late_delivery_logistics", False

    failed_refund = _has_event(facts.refund_events, "refund_requested", "failed")
    pending_refund = _has_event(facts.refund_events, "refund_requested", "pending")
    mismatch = _has_event(facts.payment_events, "reconciliation_mismatch", None)

    if failed_refund:
        if topic in {"valid_split_payment", "refund_failed"}:
            return topic, True
        return "refund_failed", True

    if pending_refund or mismatch:
        if topic in {"payment_mismatch", "refund_pending"}:
            return topic, True
        return "refund_pending", True

    if _has_split(facts.payments):
        return "valid_split_payment", False

    if late and actor is not None:
        return f"late_delivery_{actor}", False

    return "unsupported_claim", False


def policy_rule(facts: CaseFacts, issue: str) -> dict[str, Any]:
    """Return a normalized rule: case_status, recommended_action, refund_brl, party_type."""
    rules = facts.policy_rules.get("rules") or {}
    rule = rules.get(issue)
    if rule is None:
        rule = dict(_FALLBACK_POLICY[issue])

    party_type = rule.get("party_type")
    if party_type is None:
        parties = rule.get("responsible_parties") or []
        party_type = parties[0].get("party_type") if parties else "unknown"

    return {
        "case_status": rule.get("case_status", "needs_investigation"),
        "recommended_action": rule.get("recommended_action", ""),
        "refund_brl": rule.get("refund_brl", "0.00"),
        "party_type": party_type,
    }


def _confidence(issue: str, ambiguous: bool, facts: CaseFacts) -> float:
    if issue == "insufficient_evidence":
        return 0.55
    if ambiguous:
        return 0.88
    if facts.missing:
        return 0.70
    return 0.95


def _entity_sets(facts: CaseFacts) -> dict[str, list[str]]:
    order_ids = sorted({facts.order_id})
    item_ids = sorted(
        {item.get("order_item_id") for item in facts.items if item.get("order_item_id")}
    )
    seller_ids = sorted(
        {s.get("seller_id") for s in facts.sellers if s.get("seller_id")}
    )
    return {
        "order_ids": order_ids,
        "item_ids": item_ids,
        "seller_ids": seller_ids,
        "payment_references": list(order_ids),
        "shipment_ids": list(order_ids),
    }


def _claim_assessments(
    case: dict[str, Any],
    issue: str,
    confidence: float,
    refund: Decimal,
    evidence_refs: list[str],
) -> list[dict[str, Any]]:
    claims = case.get("customer_request", {}).get("claims", [])
    assessments: list[dict[str, Any]] = []
    for claim in claims[:1]:
        verdict = (
            "unsupported"
            if issue in {"unsupported_claim", "valid_split_payment", "insufficient_evidence"}
            else "supported"
        )
        assessments.append(
            {
                "claim_id": claim.get("claim_id") or f"{case['case_id']}-claim-a",
                "verdict": verdict,
                "confidence": confidence,
                "evidence_refs": evidence_refs,
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
                "evidence_refs": evidence_refs,
            }
        )
    return assessments


def build_output(case: dict[str, Any], facts: CaseFacts) -> dict[str, Any]:
    case_id = facts.case_id
    issue, ambiguous = determine_issue(facts)

    payment_scoped = {
        "canceled_order_paid",
        "unavailable_order_paid",
        "duplicate_charge",
        "payment_mismatch",
        "refund_failed",
        "valid_split_payment",
    }
    critical_missing = "order" in facts.missing or (
        issue in payment_scoped and "payments" in facts.missing
    )
    if critical_missing:
        issue = "insufficient_evidence"
        ambiguous = False

    rule = policy_rule(facts, issue)
    case_status = rule["case_status"]
    refund = (
        _money(rule["refund_brl"]).quantize(Decimal("0.01"))
        if issue != "insufficient_evidence"
        else Decimal("0.00")
    )
    confidence = _confidence(issue, ambiguous, facts)

    evidence_refs = [evidence.evidence_ref for evidence in facts.evidence.values()]

    refund_lines: list[dict[str, Any]] = []
    if refund > 0:
        refund_lines.append(
            {
                "reason_code": _REASON_CODES.get(issue, "REFUND"),
                "amount_brl": float(refund),
                "entity_id": facts.order_id,
            }
        )

    party_type = rule["party_type"]
    party_id: str | None = None
    if party_type == "seller":
        seller_ids = sorted(
            {s.get("seller_id") for s in facts.sellers if s.get("seller_id")}
        )
        party_id = seller_ids[0] if seller_ids else None
    if issue == "insufficient_evidence":
        party_type = "unknown"
        party_id = None

    ranked_causes = [
        {"cause_code": cause, "rank": index}
        for index, cause in enumerate(_CAUSE_CODES[issue], start=1)
    ]

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
            case, issue, confidence, refund, evidence_refs
        ),
        "root_cause_analysis": {
            "ranked_causes": ranked_causes,
            "responsible_parties": [{"party_type": party_type, "party_id": party_id}],
        },
        "evidence_refs": evidence_refs,
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": float(refund),
            "refund_lines": refund_lines,
        },
        "resolution_actions": list(_ACTIONS[issue]),
    }