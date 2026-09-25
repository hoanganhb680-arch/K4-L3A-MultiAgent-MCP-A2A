from __future__ import annotations

"""
L3A Multi-Agent Workflow
========================
Architecture:  Coordinator → [OrderAgent | PaymentAgent | ShipmentAgent | PolicyAgent]
                                              → VerifierAgent → Output

Each specialist is a plain async function that:
  1. Calls the MCP gateway (one call per domain tool).
  2. Emits `tool_result_consumed` trace events.
  3. Returns a typed result dict and emits a `handoff` event.

The Verifier synthesises all results into the L3A output schema (v2) and
emits `verification_completed`. The Coordinator wraps the whole lifecycle.

Rules enforced here:
  - evidence_ref values are never invented; they come verbatim from gateway.call().
  - No evidence is shared across cases.
  - Verifier never calls the MCP gateway.
  - Confidence is calibrated based on evidence completeness.
"""

import asyncio
import logging
from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers & constants
# ---------------------------------------------------------------------------

PRIMARY_ISSUE_ENUM = {
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
    "insufficient_evidence",
}

VERDICT_ENUM = {"supported", "unsupported", "partially_supported", "insufficient_evidence"}

PARTY_TYPE_ENUM = {"seller", "platform", "logistics_provider", "payment_provider", "customer", "unknown"}


def _safe_status(result: dict[str, Any]) -> str:
    return result.get("status", "error")


def _refs(result: dict[str, Any]) -> list[str]:
    return result.get("evidence_refs", [])


# ---------------------------------------------------------------------------
# Specialist agents
# ---------------------------------------------------------------------------


async def _order_agent(
    case_id: str,
    order_id: str,
    gateway: EvidenceGateway,
    trace: TraceWriter,
) -> dict[str, Any]:
    """Fetch order + item evidence."""
    collected_refs: list[str] = []
    order_data: dict[str, Any] = {}
    item_data: dict[str, Any] = {}

    try:
        ev = await gateway.call("get_order", case_id=case_id, order_id=order_id)
        ref = ev["evidence_ref"]
        order_data = ev.get("data", {})
        collected_refs.append(ref)
        trace.emit(
            case_id=case_id,
            event_type="tool_result_consumed",
            actor="order-agent",
            tool_name="get_order",
            evidence_refs=[ref],
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("order-agent get_order failed: %s", exc)

    try:
        ev2 = await gateway.call("get_item", case_id=case_id, order_id=order_id)
        ref2 = ev2["evidence_ref"]
        item_data = ev2.get("data", {})
        collected_refs.append(ref2)
        trace.emit(
            case_id=case_id,
            event_type="tool_result_consumed",
            actor="order-agent",
            tool_name="get_item",
            evidence_refs=[ref2],
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("order-agent get_item failed: %s", exc)

    status = "ok" if order_data else "not_found"
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="order-agent",
        target="verifier",
        decision_code=status,
    )
    return {
        "status": status,
        "domain": "order",
        "evidence_refs": collected_refs,
        "data": {"order": order_data, "item": item_data},
    }


async def _payment_agent(
    case_id: str,
    order_id: str,
    gateway: EvidenceGateway,
    trace: TraceWriter,
) -> dict[str, Any]:
    """Fetch payment evidence."""
    collected_refs: list[str] = []
    payment_data: dict[str, Any] = {}

    try:
        ev = await gateway.call("get_payment", case_id=case_id, order_id=order_id)
        ref = ev["evidence_ref"]
        payment_data = ev.get("data", {})
        collected_refs.append(ref)
        trace.emit(
            case_id=case_id,
            event_type="tool_result_consumed",
            actor="payment-agent",
            tool_name="get_payment",
            evidence_refs=[ref],
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("payment-agent get_payment failed: %s", exc)

    status = "ok" if payment_data else "not_found"
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="payment-agent",
        target="verifier",
        decision_code=status,
    )
    return {
        "status": status,
        "domain": "payment",
        "evidence_refs": collected_refs,
        "data": {"payment": payment_data},
    }


async def _shipment_agent(
    case_id: str,
    order_id: str,
    gateway: EvidenceGateway,
    trace: TraceWriter,
) -> dict[str, Any]:
    """Fetch shipment evidence."""
    collected_refs: list[str] = []
    shipment_data: dict[str, Any] = {}

    try:
        ev = await gateway.call("get_shipment", case_id=case_id, order_id=order_id)
        ref = ev["evidence_ref"]
        shipment_data = ev.get("data", {})
        collected_refs.append(ref)
        trace.emit(
            case_id=case_id,
            event_type="tool_result_consumed",
            actor="shipment-agent",
            tool_name="get_shipment",
            evidence_refs=[ref],
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("shipment-agent get_shipment failed: %s", exc)

    status = "ok" if shipment_data else "not_found"
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="shipment-agent",
        target="verifier",
        decision_code=status,
    )
    return {
        "status": status,
        "domain": "shipment",
        "evidence_refs": collected_refs,
        "data": {"shipment": shipment_data},
    }


async def _policy_agent(
    case_id: str,
    policy_version: str,
    gateway: EvidenceGateway,
    trace: TraceWriter,
) -> dict[str, Any]:
    """Fetch applicable policy."""
    collected_refs: list[str] = []
    policy_data: dict[str, Any] = {}

    try:
        ev = await gateway.call("get_policy", case_id=case_id, policy_version=policy_version)
        ref = ev["evidence_ref"]
        policy_data = ev.get("data", {})
        collected_refs.append(ref)
        trace.emit(
            case_id=case_id,
            event_type="tool_result_consumed",
            actor="policy-agent",
            tool_name="get_policy",
            evidence_refs=[ref],
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("policy-agent get_policy failed: %s", exc)

    status = "ok" if policy_data else "not_found"
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="policy-agent",
        target="verifier",
        decision_code=status,
    )
    return {
        "status": status,
        "domain": "policy",
        "evidence_refs": collected_refs,
        "data": {"policy": policy_data},
    }


# ---------------------------------------------------------------------------
# Verifier — deterministic rule engine, NO MCP calls
# ---------------------------------------------------------------------------


def _determine_primary_issue(
    order_res: dict[str, Any],
    payment_res: dict[str, Any],
    shipment_res: dict[str, Any],
    claims: list[dict[str, Any]],
) -> str:
    """Map evidence to a primary_issue enum value."""
    order = order_res.get("data", {}).get("order", {})
    payment = payment_res.get("data", {}).get("payment", {})
    shipment = shipment_res.get("data", {}).get("shipment", {})

    # No evidence at all
    if not order and not payment:
        return "insufficient_evidence"

    order_status = str(order.get("order_status", "")).lower()
    has_payment = bool(payment)

    # Check claim topics for guidance
    topics = {c.get("topic", "") for c in claims}

    # canceled order that was paid
    if order_status == "canceled" and has_payment:
        return "canceled_order_paid"

    # unavailable / missing order record but payment exists
    if not order and has_payment:
        return "unavailable_order_paid"

    # Shipment-related
    if shipment:
        carrier_date = shipment.get("order_delivered_carrier_date")
        estimated_date = shipment.get("order_estimated_delivery_date")
        customer_date = shipment.get("order_delivered_customer_date")
        if carrier_date and estimated_date and not customer_date:
            # Delivered to carrier but not customer — likely late_delivery_logistics
            return "late_delivery_logistics"
        if "late_delivery_seller" in topics:
            return "late_delivery_seller"

    # Payment anomalies
    if "payment_mismatch" in topics:
        return "payment_mismatch"
    if "duplicate_charge" in topics:
        return "duplicate_charge"
    if "refund_pending" in topics:
        return "refund_pending"
    if "refund_failed" in topics:
        return "refund_failed"
    if "valid_split_payment" in topics:
        return "valid_split_payment"

    # Catch-all topic match
    for topic in topics:
        if topic in PRIMARY_ISSUE_ENUM:
            return topic

    return "unsupported_claim"


def _determine_refund(
    primary_issue: str,
    payment_res: dict[str, Any],
) -> tuple[float, list[dict[str, Any]]]:
    """Return (recommended_refund_brl, refund_lines)."""
    payment = payment_res.get("data", {}).get("payment", {})

    # Try to extract total paid — payment can be a list or dict
    total_paid = 0.0
    payment_records: list[dict] = []
    if isinstance(payment, list):
        payment_records = payment
    elif isinstance(payment, dict):
        payment_records = [payment]

    for rec in payment_records:
        val = rec.get("payment_value") or rec.get("amount") or 0
        try:
            total_paid += float(val)
        except (TypeError, ValueError):
            pass

    FULL_REFUND_ISSUES = {
        "canceled_order_paid",
        "unavailable_order_paid",
        "duplicate_charge",
        "refund_pending",
        "refund_failed",
    }
    PARTIAL_REFUND_ISSUES = {
        "payment_mismatch",
        "late_delivery_seller",
        "late_delivery_logistics",
    }

    if primary_issue in FULL_REFUND_ISSUES and total_paid > 0:
        return total_paid, [
            {
                "reason_code": primary_issue,
                "amount_brl": total_paid,
                "entity_id": payment_records[0].get("order_id") if payment_records else None,
            }
        ]
    if primary_issue in PARTIAL_REFUND_ISSUES and total_paid > 0:
        partial = round(total_paid * 0.5, 2)
        return partial, [
            {
                "reason_code": primary_issue,
                "amount_brl": partial,
                "entity_id": payment_records[0].get("order_id") if payment_records else None,
            }
        ]
    return 0.0, []


def _determine_responsible_party(
    primary_issue: str,
    order_res: dict[str, Any],
) -> list[dict[str, Any]]:
    order = order_res.get("data", {}).get("order", {})
    seller_id = order.get("seller_id") or None

    SELLER_ISSUES = {"late_delivery_seller", "canceled_order_paid", "unavailable_order_paid"}
    LOGISTICS_ISSUES = {"late_delivery_logistics"}
    PLATFORM_ISSUES = {"payment_mismatch", "duplicate_charge", "refund_pending", "refund_failed"}

    if primary_issue in SELLER_ISSUES:
        return [{"party_type": "seller", "party_id": seller_id}]
    if primary_issue in LOGISTICS_ISSUES:
        return [{"party_type": "logistics_provider", "party_id": None}]
    if primary_issue in PLATFORM_ISSUES:
        return [{"party_type": "platform", "party_id": None}]
    return [{"party_type": "unknown", "party_id": None}]


def _assess_claims(
    claims: list[dict[str, Any]],
    primary_issue: str,
    all_refs: list[str],
    order_res: dict[str, Any],
    payment_res: dict[str, Any],
    shipment_res: dict[str, Any],
) -> list[dict[str, Any]]:
    """Build claim_assessments list."""
    assessments = []
    has_evidence = bool(all_refs)

    for claim in claims:
        claim_id = claim.get("claim_id", "")
        topic = claim.get("topic", "")

        # Use refs relevant to this domain
        domain_refs = all_refs[:5]  # cap at 5 per claim

        if not has_evidence:
            verdict = "insufficient_evidence"
            confidence = 0.3
        elif topic == primary_issue:
            verdict = "supported"
            confidence = 0.85
        elif topic == "requested_full_refund":
            if primary_issue in {
                "canceled_order_paid", "unavailable_order_paid",
                "duplicate_charge", "refund_pending", "refund_failed",
            }:
                verdict = "supported"
                confidence = 0.80
            elif primary_issue in {"late_delivery_seller", "late_delivery_logistics", "payment_mismatch"}:
                verdict = "partially_supported"
                confidence = 0.60
            else:
                verdict = "unsupported"
                confidence = 0.70
        elif topic == "unsupported_claim":
            verdict = "unsupported"
            confidence = 0.75
        elif topic in PRIMARY_ISSUE_ENUM:
            # topic is a known issue but not the detected primary
            verdict = "partially_supported" if has_evidence else "insufficient_evidence"
            confidence = 0.55
        else:
            verdict = "insufficient_evidence"
            confidence = 0.40

        assessments.append({
            "claim_id": claim_id,
            "verdict": verdict,
            "confidence": round(confidence, 2),
            "evidence_refs": domain_refs,
        })
    return assessments


def _calibrate_confidence(
    primary_issue: str,
    n_ok_domains: int,
    total_domains: int,
) -> float:
    """Calibrate confidence based on evidence completeness."""
    base = 0.9 if primary_issue not in {"unsupported_claim", "insufficient_evidence"} else 0.55
    coverage = n_ok_domains / max(total_domains, 1)
    return round(base * coverage, 2)


def _verifier(
    case_id: str,
    claims: list[dict[str, Any]],
    order_res: dict[str, Any],
    payment_res: dict[str, Any],
    shipment_res: dict[str, Any],
    policy_res: dict[str, Any],
    trace: TraceWriter,
) -> dict[str, Any]:
    """Build and return the final L3A v2 output. No MCP calls allowed here."""

    # Gather all evidence refs, deduplicated, preserving order
    seen: set[str] = set()
    all_refs: list[str] = []
    for res in [order_res, payment_res, shipment_res, policy_res]:
        for ref in _refs(res):
            if ref not in seen:
                seen.add(ref)
                all_refs.append(ref)

    # Determine primary issue
    primary_issue = _determine_primary_issue(order_res, payment_res, shipment_res, claims)

    # Count OK domains for confidence calibration
    domain_results = [order_res, payment_res, shipment_res, policy_res]
    n_ok = sum(1 for r in domain_results if _safe_status(r) == "ok")

    confidence = _calibrate_confidence(primary_issue, n_ok, len(domain_results))

    # Determine case_status
    if primary_issue in {"unsupported_claim", "insufficient_evidence"}:
        case_status = "no_action" if primary_issue == "unsupported_claim" else "needs_investigation"
    else:
        case_status = "action_required"

    # Financial resolution
    recommended_refund, refund_lines = _determine_refund(primary_issue, payment_res)

    # If no refund but status is action_required, reconsider
    if case_status == "action_required" and recommended_refund == 0.0:
        case_status = "needs_investigation"

    # Entities
    order_data = order_res.get("data", {}).get("order", {})
    item_data = order_res.get("data", {}).get("item", {})
    payment_data = payment_res.get("data", {}).get("payment", {})
    shipment_data = shipment_res.get("data", {}).get("shipment", {})

    order_id = order_data.get("order_id") or (
        payment_data[0].get("order_id") if isinstance(payment_data, list) and payment_data
        else payment_data.get("order_id") if isinstance(payment_data, dict) else None
    )
    item_ids = []
    if isinstance(item_data, list):
        item_ids = [i.get("order_item_id") or i.get("product_id") for i in item_data if isinstance(i, dict)]
        item_ids = [str(i) for i in item_ids if i is not None]
    seller_ids = []
    if isinstance(item_data, list):
        seller_ids = list({i.get("seller_id") for i in item_data if isinstance(i, dict) and i.get("seller_id")})
    elif isinstance(order_data, dict) and order_data.get("seller_id"):
        seller_ids = [order_data["seller_id"]]
    payment_refs = []
    if isinstance(payment_data, list):
        payment_refs = [str(p.get("payment_sequential", i)) for i, p in enumerate(payment_data)]
    shipment_id = shipment_data.get("order_id") or shipment_data.get("shipment_id") if isinstance(shipment_data, dict) else None

    # Claim assessments
    claim_assessments = _assess_claims(
        claims, primary_issue, all_refs, order_res, payment_res, shipment_res
    )

    # Root cause
    cause_map = {
        "canceled_order_paid": "ORDER_CANCELED_BEFORE_DELIVERY",
        "unavailable_order_paid": "ORDER_RECORD_UNAVAILABLE",
        "late_delivery_seller": "SELLER_DISPATCH_DELAY",
        "late_delivery_logistics": "LOGISTICS_DELIVERY_DELAY",
        "valid_split_payment": "SPLIT_PAYMENT_APPLIED",
        "payment_mismatch": "PAYMENT_AMOUNT_MISMATCH",
        "duplicate_charge": "DUPLICATE_PAYMENT_DETECTED",
        "refund_pending": "REFUND_NOT_YET_PROCESSED",
        "refund_failed": "REFUND_PROCESSING_FAILED",
        "unsupported_claim": "CLAIM_NOT_SUBSTANTIATED",
        "insufficient_evidence": "EVIDENCE_INCOMPLETE",
    }
    cause_code = cause_map.get(primary_issue, "CLAIM_NOT_SUBSTANTIATED")

    # Resolution actions
    action_map = {
        "canceled_order_paid": ["issue_full_refund", "notify_customer", "close_case"],
        "unavailable_order_paid": ["escalate_to_platform", "issue_full_refund", "notify_customer"],
        "late_delivery_seller": ["notify_seller", "issue_partial_refund", "notify_customer"],
        "late_delivery_logistics": ["notify_logistics_provider", "issue_partial_refund", "notify_customer"],
        "payment_mismatch": ["audit_payment_records", "issue_partial_refund", "notify_customer"],
        "duplicate_charge": ["reverse_duplicate_charge", "notify_customer"],
        "refund_pending": ["expedite_refund", "notify_customer"],
        "refund_failed": ["retry_refund", "notify_customer", "escalate_to_payment_provider"],
        "valid_split_payment": ["confirm_payment_valid", "notify_customer", "close_case"],
        "unsupported_claim": ["close_case", "notify_customer"],
        "insufficient_evidence": ["request_additional_evidence", "notify_customer"],
    }
    resolution_actions = action_map.get(primary_issue, ["close_case"])

    data_conflicts: list[dict[str, Any]] = []

    output: dict[str, Any] = {
        "schema_version": "day09-l3a-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": primary_issue,
            "case_status": case_status,
            "confidence": confidence,
        },
        "affected_entities": {
            "order_ids": [order_id] if order_id else [],
            "item_ids": item_ids[:20],
            "seller_ids": seller_ids[:20],
            "payment_references": payment_refs[:20],
            "shipment_ids": [shipment_id] if shipment_id else [],
        },
        "claim_assessments": claim_assessments,
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": cause_code, "rank": 1}],
            "responsible_parties": _determine_responsible_party(primary_issue, order_res),
        },
        "evidence_refs": all_refs[:30],
        "data_conflicts": data_conflicts,
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": recommended_refund,
            "refund_lines": refund_lines,
        },
        "resolution_actions": list(dict.fromkeys(resolution_actions))[:8],
    }

    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier",
        decision_code=primary_issue,
        evidence_refs=all_refs[:20],
        attributes={
            "n_domains_ok": n_ok,
            "primary_issue": primary_issue,
            "confidence": confidence,
            "refund_brl": recommended_refund,
        },
    )
    return output


# ---------------------------------------------------------------------------
# Coordinator — entrypoint
# ---------------------------------------------------------------------------


async def solve_case(
    case: dict[str, Any],
    gateway: EvidenceGateway,
    trace: TraceWriter,
) -> dict[str, Any]:
    """L3A coordinator: orchestrate specialists → verifier → return output."""
    case_id: str = case["case_id"]
    customer_req: dict[str, Any] = case.get("customer_request", {})
    order_id: str = customer_req.get("claimed_order_id", "")
    claims: list[dict[str, Any]] = customer_req.get("claims", [])
    policy_version: str = case.get("policy_version", "EC_POLICY_V1")

    # --- case_received ---
    trace.emit(
        case_id=case_id,
        event_type="case_received",
        actor="coordinator",
        attributes={"order_id": order_id, "claim_count": len(claims)},
    )

    # --- task_assigned to each specialist ---
    for agent_name in ("order-agent", "payment-agent", "shipment-agent", "policy-agent"):
        trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target=agent_name,
        )

    # --- Run specialists (sequential to avoid cross-scope risk) ---
    log.info("[%s] running order-agent", case_id)
    order_res = await _order_agent(case_id, order_id, gateway, trace)

    log.info("[%s] running payment-agent", case_id)
    payment_res = await _payment_agent(case_id, order_id, gateway, trace)

    log.info("[%s] running shipment-agent", case_id)
    shipment_res = await _shipment_agent(case_id, order_id, gateway, trace)

    log.info("[%s] running policy-agent", case_id)
    policy_res = await _policy_agent(case_id, policy_version, gateway, trace)

    # --- Verifier ---
    log.info("[%s] running verifier", case_id)
    output = _verifier(case_id, claims, order_res, payment_res, shipment_res, policy_res, trace)

    # --- case_finalized ---
    trace.emit(
        case_id=case_id,
        event_type="case_finalized",
        actor="coordinator",
        decision_code=output["assessment"]["primary_issue"],
        evidence_refs=output["evidence_refs"][:20],
        attributes={
            "case_status": output["assessment"]["case_status"],
            "confidence": output["assessment"]["confidence"],
            "refund_brl": output["financial_resolution"]["recommended_refund_brl"],
        },
    )

    return output
