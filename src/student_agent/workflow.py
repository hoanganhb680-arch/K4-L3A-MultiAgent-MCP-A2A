from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

ACTOR_BY_TOOL = {
    "get_order": "order-agent",
    "get_order_items": "order-agent",
    "get_order_payments": "payment-agent",
    "get_payment_timeline": "payment-agent",
    "get_refund_timeline": "payment-agent",
    "get_shipment_summary": "shipment-agent",
    "get_sellers": "order-agent",
    "get_policy": "policy-agent",
}

REQUIRED_LIFECYCLE_EVENTS = {
    "task_assigned",
    "tool_result_consumed",
    "handoff",
    "policy_decided",
    "verification_completed",
}

PAYMENT_ISSUES = {"valid_split_payment", "payment_mismatch", "duplicate_charge"}
REFUND_ISSUES = {"refund_pending", "refund_failed"}
SHIPMENT_ISSUES = {"late_delivery_seller", "late_delivery_logistics"}


@dataclass(frozen=True)
class PolicyDecision:
    issue: str
    rule: dict[str, Any]
    evidence_refs: list[str]


def _strings(values: Iterable[Any]) -> list[str]:
    return list(dict.fromkeys(str(value) for value in values if value is not None and str(value)))


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _number(value: Decimal) -> int | float:
    return int(value) if value == value.to_integral_value() else float(value)


def _events(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        return []
    raw = data.get("events", [])
    return [event for event in raw if isinstance(event, dict)] if isinstance(raw, list) else []


def _claim_topic(request: dict[str, Any]) -> str:
    claims = request.get("claims", [])
    if not isinstance(claims, list):
        raise ValueError("customer_request.claims must be a list")
    topics = [claim.get("topic") for claim in claims if isinstance(claim, dict)]
    allowed = {
        "canceled_order_paid",
        "unavailable_order_paid",
        "late_delivery_seller",
        "late_delivery_logistics",
        "valid_split_payment",
        "payment_mismatch",
        "duplicate_charge",
        "refund_pending",
        "refund_failed",
        "unsupported_claim",
    }
    valid_topics = (
        topic for topic in topics if isinstance(topic, str) and topic in allowed
    )
    return next(valid_topics, "unsupported_claim")


def _supported_issue(
    claimed_issue: str,
    order: dict[str, Any],
    payment: dict[str, Any] | None,
    shipment: dict[str, Any] | None,
    refund: dict[str, Any] | None,
) -> str:
    payment_events = _events(payment)
    shipment_events = _events(shipment)
    refund_events = _events(refund)
    has_captured_payment = any(
        event.get("event_type") == "captured" for event in payment_events
    )

    if claimed_issue == "canceled_order_paid":
        return (
            "canceled_order_paid"
            if order.get("order_status") == "canceled" and has_captured_payment
            else "unsupported_claim"
        )
    if claimed_issue == "unavailable_order_paid":
        return (
            "unavailable_order_paid"
            if order.get("order_status") == "unavailable" and has_captured_payment
            else "unsupported_claim"
        )
    if claimed_issue in SHIPMENT_ISSUES:
        is_supported = any(
            event.get("event_type") == "delivered_late"
            and event.get("actor") == claimed_issue.removeprefix("late_delivery_")
            for event in shipment_events
        )
        return claimed_issue if is_supported else "unsupported_claim"
    if claimed_issue == "payment_mismatch":
        return (
            "payment_mismatch"
            if any(event.get("event_type") == "reconciliation_mismatch" for event in payment_events)
            else "unsupported_claim"
        )
    if claimed_issue == "refund_pending":
        return (
            "refund_pending"
            if any(event.get("status") == "pending" for event in refund_events)
            else "unsupported_claim"
        )
    if claimed_issue == "refund_failed":
        return (
            "refund_failed"
            if any(event.get("status") == "failed" for event in refund_events)
            else "unsupported_claim"
        )
    if claimed_issue == "duplicate_charge":
        captured = [event for event in payment_events if event.get("event_type") == "captured"]
        amounts = [_decimal(event.get("amount_brl")) for event in captured]
        is_duplicate = len(amounts) >= 2 and len(set(amounts)) == 1 and len(amounts) % 2 == 0
        return "duplicate_charge" if is_duplicate else "unsupported_claim"
    if claimed_issue == "valid_split_payment":
        captured = [event for event in payment_events if event.get("event_type") == "captured"]
        amounts = [_decimal(event.get("amount_brl")) for event in captured]
        is_duplicate = len(amounts) >= 2 and len(set(amounts)) == 1 and len(amounts) % 2 == 0
        mismatch = any(
            event.get("event_type") == "reconciliation_mismatch"
            for event in payment_events
        )
        if captured and not is_duplicate and not mismatch:
            return "valid_split_payment"
        return "unsupported_claim"
    return "unsupported_claim"


async def _consume(
    gateway: EvidenceGateway,
    trace: TraceWriter,
    *,
    case_id: str,
    tool_name: str,
    **arguments: str,
) -> dict[str, Any]:
    evidence = await gateway.call(tool_name, case_id=case_id, **arguments)
    trace.emit(
        case_id=case_id,
        event_type="tool_result_consumed",
        actor=ACTOR_BY_TOOL[tool_name],
        tool_name=tool_name,
        evidence_refs=[evidence["evidence_ref"]],
    )
    return evidence


def _calibrate_confidence(
    issue: str, evidence_by_tool: dict[str, dict[str, Any]], conflicts: list[dict[str, Any]]
) -> float:
    expected_tools = {"get_order", "get_policy"}
    if issue in {"canceled_order_paid", "unavailable_order_paid", *PAYMENT_ISSUES}:
        expected_tools.add("get_payment_timeline")
    if issue in SHIPMENT_ISSUES:
        expected_tools.add("get_shipment_summary")
    if issue in REFUND_ISSUES:
        expected_tools.add("get_refund_timeline")
    coverage = len(expected_tools & set(evidence_by_tool)) / len(expected_tools)
    confidence = 0.76 + 0.19 * coverage - 0.12 * len(conflicts)
    return round(max(0.35, min(0.95, confidence)), 2)


def _verify_output(
    output: dict[str, Any],
    decision: PolicyDecision,
    evidence_by_tool: dict[str, dict[str, Any]],
    emitted_events: set[str],
) -> tuple[list[dict[str, Any]], float]:
    errors: list[str] = []
    rule = decision.rule
    financial = output["financial_resolution"]
    expected_refund = _decimal(rule.get("refund_brl", 0))
    actual_refund = _decimal(financial["recommended_refund_brl"])
    line_total = sum(
        (_decimal(line["amount_brl"]) for line in financial["refund_lines"]), Decimal("0")
    )

    if output["assessment"]["primary_issue"] != decision.issue:
        errors.append("PRIMARY_ISSUE_POLICY_MISMATCH")
    if output["assessment"]["case_status"] != rule.get("case_status"):
        errors.append("CASE_STATUS_POLICY_MISMATCH")
    if output["root_cause_analysis"]["responsible_parties"] != rule.get(
        "responsible_parties", []
    ):
        errors.append("RESPONSIBILITY_POLICY_MISMATCH")
    if output["resolution_actions"] != _strings([rule.get("recommended_action")]):
        errors.append("ACTION_POLICY_MISMATCH")
    if financial["currency"] != "BRL" or actual_refund != expected_refund:
        errors.append("REFUND_POLICY_MISMATCH")
    if line_total != actual_refund:
        errors.append("REFUND_LINE_TOTAL_MISMATCH")
    if set(output["evidence_refs"]) != set(decision.evidence_refs):
        errors.append("OUTPUT_EVIDENCE_MISMATCH")
    consumed_refs = {evidence["evidence_ref"] for evidence in evidence_by_tool.values()}
    if not set(output["evidence_refs"]).issubset(consumed_refs):
        errors.append("UNCONSUMED_EVIDENCE_REF")
    if REQUIRED_LIFECYCLE_EVENTS - emitted_events:
        errors.append("MISSING_LIFECYCLE_EVENT")

    conflicts = (
        [
            {
                "field": "verifier",
                "sources": ["policy-agent", "verifier"],
                "selected_source": "policy-agent",
                "resolution_code": "_".join(sorted(errors)),
            }
        ]
        if errors
        else []
    )
    return conflicts, _calibrate_confidence(decision.issue, evidence_by_tool, conflicts)


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Route each claim to the minimum authoritative evidence set and policy rule."""
    case_id = str(case["case_id"])
    request = case.get("customer_request", {})
    if not isinstance(request, dict) or not isinstance(request.get("claimed_order_id"), str):
        raise ValueError(f"{case_id}: missing customer_request.claimed_order_id")
    order_id = request["claimed_order_id"]
    policy_version = case.get("policy_version")
    if not isinstance(policy_version, str) or not policy_version:
        raise ValueError(f"{case_id}: missing policy_version")
    claimed_issue = _claim_topic(request)
    emitted_events = {"task_assigned"}

    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="order-agent",
        decision_code="LOAD_CASE_SCOPE",
    )
    order_evidence = await _consume(
        gateway, trace, case_id=case_id, tool_name="get_order", order_id=order_id
    )
    policy_evidence = await _consume(
        gateway,
        trace,
        case_id=case_id,
        tool_name="get_policy",
        policy_version=policy_version,
    )
    emitted_events.add("tool_result_consumed")

    evidence_by_tool = {"get_order": order_evidence, "get_policy": policy_evidence}
    order_data = order_evidence["data"]
    policy_data = policy_evidence["data"]
    if not isinstance(order_data, dict) or not isinstance(policy_data, dict):
        raise ValueError(f"{case_id}: MCP returned an unexpected evidence payload")

    payment_evidence: dict[str, Any] | None = None
    payment_rows_evidence: dict[str, Any] | None = None
    items_evidence: dict[str, Any] | None = None
    sellers_evidence: dict[str, Any] | None = None
    shipment_evidence: dict[str, Any] | None = None
    refund_evidence: dict[str, Any] | None = None
    if claimed_issue in {"canceled_order_paid", "unavailable_order_paid", *PAYMENT_ISSUES}:
        trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor="order-agent",
            target="payment-agent",
            decision_code="PAYMENT_EVIDENCE_REQUIRED",
            evidence_refs=[order_evidence["evidence_ref"]],
        )
        emitted_events.add("handoff")
        payment_evidence = await _consume(
            gateway,
            trace,
            case_id=case_id,
            tool_name="get_payment_timeline",
            order_id=order_id,
        )
        evidence_by_tool["get_payment_timeline"] = payment_evidence
        if claimed_issue in PAYMENT_ISSUES:
            payment_rows_evidence = await _consume(
                gateway,
                trace,
                case_id=case_id,
                tool_name="get_order_payments",
                order_id=order_id,
            )
            evidence_by_tool["get_order_payments"] = payment_rows_evidence
    elif claimed_issue in SHIPMENT_ISSUES:
        trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor="order-agent",
            target="shipment-agent",
            decision_code="SHIPMENT_EVIDENCE_REQUIRED",
            evidence_refs=[order_evidence["evidence_ref"]],
        )
        emitted_events.add("handoff")
        shipment_evidence = await _consume(
            gateway,
            trace,
            case_id=case_id,
            tool_name="get_shipment_summary",
            order_id=order_id,
        )
        evidence_by_tool["get_shipment_summary"] = shipment_evidence
        if claimed_issue == "late_delivery_seller":
            items_evidence = await _consume(
                gateway, trace, case_id=case_id, tool_name="get_order_items", order_id=order_id
            )
            sellers_evidence = await _consume(
                gateway, trace, case_id=case_id, tool_name="get_sellers", order_id=order_id
            )
            evidence_by_tool["get_order_items"] = items_evidence
            evidence_by_tool["get_sellers"] = sellers_evidence
    elif claimed_issue in REFUND_ISSUES:
        trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor="order-agent",
            target="payment-agent",
            decision_code="REFUND_EVIDENCE_REQUIRED",
            evidence_refs=[order_evidence["evidence_ref"]],
        )
        emitted_events.add("handoff")
        refund_evidence = await _consume(
            gateway,
            trace,
            case_id=case_id,
            tool_name="get_refund_timeline",
            order_id=order_id,
        )
        evidence_by_tool["get_refund_timeline"] = refund_evidence
    else:
        trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor="order-agent",
            target="policy-agent",
            decision_code="ORDER_EVIDENCE_SUFFICIENT",
            evidence_refs=[order_evidence["evidence_ref"]],
        )
        emitted_events.add("handoff")

    payment_data = payment_evidence["data"] if payment_evidence is not None else None
    payment_rows_data = (
        payment_rows_evidence["data"] if payment_rows_evidence is not None else None
    )
    items_data = items_evidence["data"] if items_evidence is not None else None
    sellers_data = sellers_evidence["data"] if sellers_evidence is not None else None
    shipment_data = shipment_evidence["data"] if shipment_evidence is not None else None
    refund_data = refund_evidence["data"] if refund_evidence is not None else None
    issue = _supported_issue(
        claimed_issue, order_data, payment_data, shipment_data, refund_data
    )
    rules = policy_data.get("rules", {})
    if not isinstance(rules, dict) or not isinstance(rules.get(issue), dict):
        raise ValueError(f"{case_id}: policy has no usable rule for {issue}")
    rule = rules[issue]
    evidence_refs = _strings(evidence["evidence_ref"] for evidence in evidence_by_tool.values())
    decision = PolicyDecision(issue=issue, rule=rule, evidence_refs=evidence_refs)

    trace.emit(
        case_id=case_id,
        event_type="policy_decided",
        actor="policy-agent",
        decision_code=issue.upper(),
        evidence_refs=evidence_refs,
    )
    emitted_events.add("policy_decided")
    emitted_events.add("verification_completed")

    refund_amount = _decimal(rule.get("refund_brl", 0))
    responsible = rule.get("responsible_parties", [])
    if not isinstance(responsible, list):
        responsible = []
    payment_rows = payment_data.get("payments", []) if isinstance(payment_data, dict) else []
    if isinstance(payment_rows_data, list):
        payment_rows = payment_rows_data
    shipment_ids = []
    if isinstance(shipment_data, dict):
        shipment_ids = _strings(
            entry.get("order_item_id")
            for entry in shipment_data.get("shipping_limits", [])
            if isinstance(entry, dict)
        )
    claims = request.get("claims", [])
    output = {
        "schema_version": "day09-l3a-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": issue,
            "case_status": rule.get("case_status", "needs_investigation"),
            "confidence": 0.0,
        },
        "affected_entities": {
            "order_ids": _strings([order_data.get("order_id")]),
            "item_ids": (
                _strings(
                    item.get("order_item_id")
                    for item in items_data
                    if isinstance(item, dict)
                )
                if isinstance(items_data, list)
                else []
            ),
            "seller_ids": (
                _strings(
                    seller.get("seller_id")
                    for seller in sellers_data
                    if isinstance(seller, dict)
                )
                if isinstance(sellers_data, list)
                else []
            ),
            "payment_references": _strings(
                row.get("payment_sequential") for row in payment_rows if isinstance(row, dict)
            ),
            "shipment_ids": shipment_ids,
        },
        "claim_assessments": [],
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": issue.upper(), "rank": 1}],
            "responsible_parties": responsible,
        },
        "evidence_refs": evidence_refs,
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": _number(refund_amount),
            "refund_lines": (
                [
                    {
                        "reason_code": issue.upper(),
                        "amount_brl": _number(refund_amount),
                        "entity_id": order_data.get("order_id"),
                    }
                ]
                if refund_amount > 0
                else []
            ),
        },
        "resolution_actions": _strings([rule.get("recommended_action")]),
    }
    for claim in claims if isinstance(claims, list) else []:
        if not isinstance(claim, dict) or not isinstance(claim.get("claim_id"), str):
            continue
        output["claim_assessments"].append(
            {
                "claim_id": claim["claim_id"],
                "verdict": "supported" if claim.get("topic") == issue else "unsupported",
                "confidence": 0.0,
                "evidence_refs": evidence_refs,
            }
        )

    conflicts, confidence = _verify_output(output, decision, evidence_by_tool, emitted_events)
    output["data_conflicts"] = conflicts
    output["assessment"]["confidence"] = confidence
    for claim in output["claim_assessments"]:
        claim["confidence"] = confidence if claim["verdict"] == "supported" else round(
            max(0.3, confidence - 0.1), 2
        )
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier",
        decision_code="VERIFIED" if not conflicts else "CONFLICT_RESOLVED",
        evidence_refs=evidence_refs,
        attributes={"confidence": confidence, "conflict_count": len(conflicts)},
    )
    return output
