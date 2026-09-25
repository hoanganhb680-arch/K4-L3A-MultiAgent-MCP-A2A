from __future__ import annotations

"""
L3A Multi-Agent Workflow
========================
Architecture:  Coordinator -> [OrderAgent | PaymentAgent | ShipmentAgent | PolicyAgent]
                                              -> VerifierAgent -> Output

Actual MCP tools (from `day09 mcp-tools`):
  get_order            - order header
  get_order_items      - line items + seller_id
  get_order_payments   - payment records
  get_payment_timeline - payment state history
  get_refund_timeline  - refund state history
  get_shipment_summary - shipment + delivery dates
  get_sellers          - seller details
  get_customer_history - customer past orders
  get_product_context  - product catalogue info
  get_policy           - EC_POLICY rules

Rules enforced here:
  - evidence_ref values come verbatim from gateway.call() -- never invented.
  - No evidence is shared across cases.
  - Verifier never calls the MCP gateway.
  - Confidence is calibrated based on evidence completeness.
"""

import logging
from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
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

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _safe_status(result: dict[str, Any]) -> str:
    return result.get("status", "error")


def _refs(result: dict[str, Any]) -> list[str]:
    return result.get("evidence_refs", [])


async def _call(
    tool: str,
    gateway: EvidenceGateway,
    trace: TraceWriter,
    actor: str,
    case_id: str,
    **kwargs: str,
) -> tuple[dict[str, Any], str | None]:
    """Call one MCP tool, emit tool_result_consumed trace, return (data, ref).
    Returns ({}, None) on any failure instead of raising."""
    try:
        ev = await gateway.call(tool, case_id=case_id, **kwargs)
        ref: str = ev["evidence_ref"]
        data: dict[str, Any] = ev.get("data", {})
        trace.emit(
            case_id=case_id,
            event_type="tool_result_consumed",
            actor=actor,
            tool_name=tool,
            evidence_refs=[ref],
        )
        return data, ref
    except Exception as exc:  # noqa: BLE001
        log.warning("[%s] %s -> %s failed: %s", case_id, actor, tool, exc)
        raise RuntimeError(f"MCP tool failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Specialist agents
# ---------------------------------------------------------------------------


async def _order_agent(
    case_id: str,
    order_id: str,
    gateway: EvidenceGateway,
    trace: TraceWriter,
    topics: set[str] | None = None,
) -> dict[str, Any]:
    """Fetch order header and line items selectively."""
    actor = "order-agent"

    order_data, ref_order = await _call(
        "get_order", gateway, trace, actor, case_id, order_id=order_id
    )

    items_data, ref_items = {}, None
    # Only fetch order items if order is available or needed
    if order_data and not order_data.get("error"):
        items_data, ref_items = await _call(
            "get_order_items", gateway, trace, actor, case_id, order_id=order_id
        )

    collected_refs = [r for r in [ref_order, ref_items] if r is not None]
    status = "ok" if (order_data and not order_data.get("error")) else "not_found"

    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor=actor,
        target="verifier",
        decision_code=status,
    )
    return {
        "status": status,
        "domain": "order",
        "evidence_refs": collected_refs,
        "data": {
            "order": order_data,
            "items": items_data,
        },
    }


async def _payment_agent(
    case_id: str,
    order_id: str,
    gateway: EvidenceGateway,
    trace: TraceWriter,
    topics: set[str] | None = None,
) -> dict[str, Any]:
    """Fetch payment records, payment timeline and refund timeline selectively."""
    actor = "payment-agent"
    topics = topics or set()

    payments_data, ref_payments = await _call(
        "get_order_payments", gateway, trace, actor, case_id, order_id=order_id
    )

    pay_timeline_data, ref_pay_tl = {}, None
    # Call payment timeline if useful for mismatch / duplicate / canceled
    if topics.intersection({"payment_mismatch", "duplicate_charge", "canceled_order_paid", "valid_split_payment"}):
        pay_timeline_data, ref_pay_tl = await _call(
            "get_payment_timeline", gateway, trace, actor, case_id, order_id=order_id
        )

    refund_timeline_data, ref_ref_tl = {}, None
    # ONLY call get_refund_timeline if claim relates to refund_pending or refund_failed
    if topics.intersection({"refund_pending", "refund_failed"}):
        refund_timeline_data, ref_ref_tl = await _call(
            "get_refund_timeline", gateway, trace, actor, case_id, order_id=order_id
        )

    collected_refs = [r for r in [ref_payments, ref_pay_tl, ref_ref_tl] if r is not None]
    status = "ok" if payments_data else "not_found"

    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor=actor,
        target="verifier",
        decision_code=status,
    )
    return {
        "status": status,
        "domain": "payment",
        "evidence_refs": collected_refs,
        "data": {
            "payments": payments_data,
            "payment_timeline": pay_timeline_data,
            "refund_timeline": refund_timeline_data,
        },
    }


async def _shipment_agent(
    case_id: str,
    order_id: str,
    gateway: EvidenceGateway,
    trace: TraceWriter,
    topics: set[str] | None = None,
) -> dict[str, Any]:
    """Fetch shipment summary selectively for delivery claims."""
    actor = "shipment-agent"
    topics = topics or set()

    shipment_data, ref_ship = {}, None
    # ONLY call get_shipment_summary if topic relates to delivery issues
    if not topics or topics.intersection({"late_delivery_seller", "late_delivery_logistics"}):
        shipment_data, ref_ship = await _call(
            "get_shipment_summary", gateway, trace, actor, case_id, order_id=order_id
        )

    collected_refs = [r for r in [ref_ship] if r is not None]
    status = "ok" if shipment_data else "not_found"

    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor=actor,
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
    """Fetch applicable e-commerce policy rules."""
    actor = "policy-agent"

    policy_data, ref_policy = await _call(
        "get_policy", gateway, trace, actor, case_id, policy_version=policy_version
    )

    collected_refs = [r for r in [ref_policy] if r is not None]
    status = "ok" if policy_data else "not_found"

    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor=actor,
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
# Verifier -- deterministic rule engine, NO MCP calls allowed
# ---------------------------------------------------------------------------


def _extract_order_status(order_res: dict[str, Any]) -> str:
    order = order_res.get("data", {}).get("order", {})
    if isinstance(order, dict):
        return str(order.get("order_status", "")).lower()
    return ""


def _extract_payments(payment_res: dict[str, Any]) -> list[dict[str, Any]]:
    raw = payment_res.get("data", {}).get("payments", {})
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        for v in raw.values():
            if isinstance(v, list):
                return v
        return [raw] if raw else []
    return []


def _extract_total_paid(payments: list[dict[str, Any]]) -> float:
    total = 0.0
    for rec in payments:
        val = rec.get("payment_value") or rec.get("amount") or 0
        try:
            total += float(val)
        except (TypeError, ValueError):
            pass
    return round(total, 2)


def _extract_shipment(shipment_res: dict[str, Any]) -> dict[str, Any]:
    raw = shipment_res.get("data", {}).get("shipment", {})
    return raw if isinstance(raw, dict) else {}


def _extract_items(order_res: dict[str, Any]) -> list[dict[str, Any]]:
    raw = order_res.get("data", {}).get("items", {})
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        for v in raw.values():
            if isinstance(v, list):
                return v
    return []


def _extract_refund_status(payment_res: dict[str, Any]) -> str:
    """Derive refund status from refund_timeline data."""
    timeline = payment_res.get("data", {}).get("refund_timeline", {})
    if not timeline:
        return "none"
    if isinstance(timeline, list) and timeline:
        last = timeline[-1]
        return str(last.get("status", "")).lower()
    if isinstance(timeline, dict):
        return str(timeline.get("status", "")).lower()
    return "none"


def _detect_duplicate_charge(payments: list[dict[str, Any]]) -> bool:
    """Return True if multiple payment records share the same type and amount."""
    if len(payments) < 2:
        return False
    seen: dict[tuple, int] = {}
    for p in payments:
        key = (p.get("payment_type"), p.get("payment_value"))
        seen[key] = seen.get(key, 0) + 1
    return any(v > 1 for v in seen.values())


def _determine_primary_issue(
    order_res: dict[str, Any],
    payment_res: dict[str, Any],
    shipment_res: dict[str, Any],
    claims: list[dict[str, Any]],
) -> str:
    """Map evidence to a primary_issue enum value -- deterministic rule-based."""
    order_status = _extract_order_status(order_res)
    payments = _extract_payments(payment_res)
    has_payment = bool(payments)
    total_paid = _extract_total_paid(payments)
    shipment = _extract_shipment(shipment_res)
    refund_status = _extract_refund_status(payment_res)
    topics = {c.get("topic", "") for c in claims}

    # Verify if valid order object actually exists
    order_obj = order_res.get("data", {}).get("order")
    has_order = False
    if isinstance(order_obj, dict):
        if order_obj.get("order_id") and not order_obj.get("error"):
            has_order = True
        elif order_status != "":
            has_order = True

    # 1. No evidence at all
    if not has_order and not has_payment:
        return "insufficient_evidence"

    # 2. Order record missing or unavailable but payment exists -> unavailable_order_paid
    # (MUST be evaluated BEFORE duplicate charge detection)
    if (not has_order or order_status == "unavailable") and has_payment:
        return "unavailable_order_paid"

    # 3. Canceled order that was paid -> full refund owed
    if order_status == "canceled" and has_payment:
        return "canceled_order_paid"

    # 4. Refund already initiated but pending
    if refund_status in {"pending", "processing"}:
        return "refund_pending"

    # 5. Refund failed
    if refund_status == "failed":
        return "refund_failed"

    # 6. Duplicate charge detection
    if _detect_duplicate_charge(payments):
        return "duplicate_charge"

    # 7. Shipment-based delivery issues
    if shipment:
        carrier_date = shipment.get("order_delivered_carrier_date")
        estimated_date = shipment.get("order_estimated_delivery_date")
        customer_date = shipment.get("order_delivered_customer_date")
        if carrier_date and not customer_date:
            if "late_delivery_seller" in topics:
                return "late_delivery_seller"
            return "late_delivery_logistics"
        if carrier_date and customer_date and estimated_date:
            if customer_date > estimated_date:
                if "late_delivery_seller" in topics:
                    return "late_delivery_seller"
                return "late_delivery_logistics"

    # 8. Payment amount mismatch from timeline
    pay_tl = payment_res.get("data", {}).get("payment_timeline", {})
    if pay_tl and isinstance(pay_tl, dict):
        expected = pay_tl.get("expected_amount") or pay_tl.get("order_value")
        if expected:
            try:
                if abs(float(expected) - total_paid) > 0.01:
                    return "payment_mismatch"
            except (TypeError, ValueError):
                pass

    # 9. Valid split payment
    if len(payments) > 1 and "valid_split_payment" in topics:
        return "valid_split_payment"

    # 10. Topic-driven fallback (priority ordered)
    priority_topics = [
        "unavailable_order_paid", "canceled_order_paid", "duplicate_charge",
        "refund_pending", "refund_failed", "late_delivery_seller",
        "late_delivery_logistics", "payment_mismatch", "valid_split_payment",
    ]
    for t in priority_topics:
        if t in topics:
            return t

    if "unsupported_claim" in topics:
        return "unsupported_claim"

    return "unsupported_claim"


def _determine_refund(
    primary_issue: str,
    payments: list[dict[str, Any]],
    order_res: dict[str, Any],
) -> tuple[float, list[dict[str, Any]]]:
    total_paid = _extract_total_paid(payments)
    order_data = order_res.get("data", {}).get("order", {})
    order_id = order_data.get("order_id") if isinstance(order_data, dict) else None
    entity_id = order_id or (payments[0].get("order_id") if payments else None)

    FULL_REFUND = {
        "canceled_order_paid", "unavailable_order_paid",
        "duplicate_charge", "refund_pending", "refund_failed",
    }
    PARTIAL_REFUND = {"payment_mismatch", "late_delivery_seller", "late_delivery_logistics"}

    if primary_issue in FULL_REFUND and total_paid > 0:
        return total_paid, [
            {"reason_code": primary_issue, "amount_brl": total_paid, "entity_id": entity_id}
        ]
    if primary_issue in PARTIAL_REFUND and total_paid > 0:
        partial = round(total_paid * 0.5, 2)
        return partial, [
            {"reason_code": primary_issue, "amount_brl": partial, "entity_id": entity_id}
        ]
    return 0.0, []


def _determine_responsible_party(
    primary_issue: str,
    order_res: dict[str, Any],
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    seller_id: str | None = None
    if items:
        seller_id = items[0].get("seller_id")
    if not seller_id:
        order_data = order_res.get("data", {}).get("order", {})
        if isinstance(order_data, dict):
            seller_id = order_data.get("seller_id")

    SELLER = {"late_delivery_seller", "canceled_order_paid", "unavailable_order_paid"}
    LOGISTICS = {"late_delivery_logistics"}
    PLATFORM = {"payment_mismatch", "duplicate_charge", "refund_pending", "refund_failed"}

    if primary_issue in SELLER:
        return [{"party_type": "seller", "party_id": seller_id}]
    if primary_issue in LOGISTICS:
        return [{"party_type": "logistics_provider", "party_id": None}]
    if primary_issue in PLATFORM:
        return [{"party_type": "platform", "party_id": None}]
    return [{"party_type": "unknown", "party_id": None}]


def _calibrate_confidence(primary_issue: str, n_ok: int, total: int) -> float:
    base = 0.95 if primary_issue not in {"unsupported_claim", "insufficient_evidence"} else 0.5
    coverage = n_ok / max(total, 1)
    return round(max(0.1, base * coverage), 2)


def _assess_claims(
    claims: list[dict[str, Any]],
    primary_issue: str,
    all_refs: list[str],
) -> list[dict[str, Any]]:
    has_evidence = bool(all_refs)
    assessments = []

    for claim in claims:
        claim_id = claim.get("claim_id", "")
        topic = claim.get("topic", "")
        domain_refs = all_refs[:5]

        if not has_evidence:
            verdict, conf = "insufficient_evidence", 0.3
        elif topic == primary_issue:
            verdict, conf = "supported", 0.85
        elif topic == "requested_full_refund":
            if primary_issue in {
                "canceled_order_paid", "unavailable_order_paid",
                "duplicate_charge", "refund_pending", "refund_failed",
            }:
                verdict, conf = "supported", 0.80
            elif primary_issue in {"late_delivery_seller", "late_delivery_logistics", "payment_mismatch"}:
                verdict, conf = "partially_supported", 0.60
            else:
                verdict, conf = "unsupported", 0.70
        elif topic == "unsupported_claim":
            verdict, conf = "unsupported", 0.75
        elif topic in PRIMARY_ISSUE_ENUM:
            verdict = "partially_supported" if has_evidence else "insufficient_evidence"
            conf = 0.55
        else:
            verdict, conf = "insufficient_evidence", 0.40

        assessments.append({
            "claim_id": claim_id,
            "verdict": verdict,
            "confidence": round(conf, 2),
            "evidence_refs": domain_refs,
        })
    return assessments


def _verifier(
    case_id: str,
    claims: list[dict[str, Any]],
    order_res: dict[str, Any],
    payment_res: dict[str, Any],
    shipment_res: dict[str, Any],
    policy_res: dict[str, Any],
    trace: TraceWriter,
) -> dict[str, Any]:
    """Synthesise all evidence into L3A output v2. NO MCP calls here."""

    # Collect all refs, deduplicated, order-preserving
    seen: set[str] = set()
    all_refs: list[str] = []
    for res in [order_res, payment_res, shipment_res, policy_res]:
        for ref in _refs(res):
            if ref not in seen:
                seen.add(ref)
                all_refs.append(ref)

    payments = _extract_payments(payment_res)
    items = _extract_items(order_res)
    shipment = _extract_shipment(shipment_res)
    order_data = order_res.get("data", {}).get("order", {})

    primary_issue = _determine_primary_issue(order_res, payment_res, shipment_res, claims)

    domain_results = [order_res, payment_res, shipment_res, policy_res]
    n_ok = sum(1 for r in domain_results if _safe_status(r) == "ok")
    confidence = _calibrate_confidence(primary_issue, n_ok, len(domain_results))

    # case_status
    if primary_issue == "unsupported_claim":
        case_status = "no_action"
    elif primary_issue == "insufficient_evidence":
        case_status = "needs_investigation"
    else:
        case_status = "action_required"

    # Financial resolution
    recommended_refund, refund_lines = _determine_refund(primary_issue, payments, order_res)
    if case_status == "action_required" and recommended_refund == 0.0:
        case_status = "needs_investigation"

    # Entities
    order_id_val = order_data.get("order_id") if isinstance(order_data, dict) else None
    if not order_id_val and payments:
        order_id_val = payments[0].get("order_id")

    item_ids: list[str] = []
    seller_ids_set: set[str] = set()
    for it in items:
        if isinstance(it, dict):
            iid = it.get("order_item_id") or it.get("product_id")
            if iid:
                item_ids.append(str(iid))
            sid = it.get("seller_id")
            if sid:
                seller_ids_set.add(str(sid))
    if not seller_ids_set and isinstance(order_data, dict) and order_data.get("seller_id"):
        seller_ids_set.add(order_data["seller_id"])

    item_ids = list(dict.fromkeys(item_ids))

    payment_refs = list(dict.fromkeys(str(p.get("payment_sequential", i)) for i, p in enumerate(payments)))

    ship_id = None
    if isinstance(shipment, dict):
        ship_id = shipment.get("order_id") or shipment.get("shipment_id")

    # Root cause mapping
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
    resolution_actions = list(dict.fromkeys(action_map.get(primary_issue, ["close_case"])))[:8]

    claim_assessments = _assess_claims(claims, primary_issue, all_refs)

    output: dict[str, Any] = {
        "schema_version": "day09-l3a-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": primary_issue,
            "case_status": case_status,
            "confidence": confidence,
        },
        "affected_entities": {
            "order_ids": [order_id_val] if order_id_val else [],
            "item_ids": item_ids[:20],
            "seller_ids": list(seller_ids_set)[:20],
            "payment_references": payment_refs[:20],
            "shipment_ids": [str(ship_id)] if ship_id else [],
        },
        "claim_assessments": claim_assessments,
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": cause_code, "rank": 1}],
            "responsible_parties": _determine_responsible_party(primary_issue, order_res, items),
        },
        "evidence_refs": all_refs[:30],
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": recommended_refund,
            "refund_lines": refund_lines,
        },
        "resolution_actions": resolution_actions,
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
# Coordinator -- public entrypoint
# ---------------------------------------------------------------------------


async def solve_case(
    case: dict[str, Any],
    gateway: EvidenceGateway,
    trace: TraceWriter,
) -> dict[str, Any]:
    """L3A coordinator: orchestrate specialists -> verifier -> return output dict."""
    case_id: str = case["case_id"]
    customer_req: dict[str, Any] = case.get("customer_request", {})
    order_id: str = customer_req.get("claimed_order_id", "")
    claims: list[dict[str, Any]] = customer_req.get("claims", [])
    policy_version: str = case.get("policy_version", "EC_POLICY_V1")
    topics = {c.get("topic", "") for c in claims}

    # Lifecycle: case_received
    trace.emit(
        case_id=case_id,
        event_type="case_received",
        actor="coordinator",
        attributes={"order_id": order_id, "claim_count": len(claims)},
    )

    # Lifecycle: task_assigned (one per specialist)
    for agent_name in ("order-agent", "payment-agent", "shipment-agent", "policy-agent"):
        trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target=agent_name,
        )

    # Run specialists sequentially (avoids cross-scope evidence risk)
    log.info("[%s] order-agent starting", case_id)
    order_res = await _order_agent(case_id, order_id, gateway, trace, topics)

    log.info("[%s] payment-agent starting", case_id)
    payment_res = await _payment_agent(case_id, order_id, gateway, trace, topics)

    log.info("[%s] shipment-agent starting", case_id)
    shipment_res = await _shipment_agent(case_id, order_id, gateway, trace, topics)

    log.info("[%s] policy-agent starting", case_id)
    policy_res = await _policy_agent(case_id, policy_version, gateway, trace)

    # Verifier
    log.info("[%s] verifier starting", case_id)
    output = _verifier(case_id, claims, order_res, payment_res, shipment_res, policy_res, trace)

    # Lifecycle: case_finalized
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
