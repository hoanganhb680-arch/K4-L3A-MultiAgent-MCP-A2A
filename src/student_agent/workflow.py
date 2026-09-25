from __future__ import annotations

from decimal import Decimal
from typing import Any

from .agents import gather_facts
from .decision import build_output
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

_ALLOWED_ISSUES = frozenset(
    {
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
)
_ALLOWED_STATUS = frozenset({"action_required", "no_action", "needs_investigation"})
_ALLOWED_PARTIES = frozenset(
    {"seller", "platform", "logistics_provider", "payment_provider", "customer", "unknown"}
)


def _verify(case_id: str, output: dict[str, Any], facts: Any) -> None:
    """Enforce the invariants that must hold before an output is finalized."""
    if output.get("case_id") != case_id:
        raise ValueError("verifier: mismatched case_id")

    assessment = output["assessment"]
    if assessment["primary_issue"] not in _ALLOWED_ISSUES:
        raise ValueError("verifier: primary_issue outside the allowed enum")
    if assessment["case_status"] not in _ALLOWED_STATUS:
        raise ValueError("verifier: invalid case_status")
    if not 0 <= assessment["confidence"] <= 1:
        raise ValueError("verifier: confidence out of [0, 1]")

    financial = output["financial_resolution"]
    refund_total = sum(Decimal(str(line["amount_brl"])) for line in financial["refund_lines"])
    recommended = Decimal(str(financial["recommended_refund_brl"]))
    if refund_total != recommended:
        raise ValueError("verifier: refund lines do not sum to recommended_refund_brl")

    if financial["currency"] != "BRL":
        raise ValueError("verifier: currency must be BRL")

    for party in output["root_cause_analysis"]["responsible_parties"]:
        if party["party_type"] not in _ALLOWED_PARTIES:
            raise ValueError("verifier: unknown responsible party type")

    if len(set(output["resolution_actions"])) != len(output["resolution_actions"]):
        raise ValueError("verifier: duplicate resolution actions")

    evidence_refs = output.get("evidence_refs", [])
    if evidence_refs and len(set(evidence_refs)) != len(evidence_refs):
        raise ValueError("verifier: duplicate evidence refs")
    for ref in evidence_refs:
        if not isinstance(ref, str) or not ref.startswith("ev_"):
            raise ValueError("verifier: malformed evidence ref")

    resolved_refs = {evidence.evidence_ref for evidence in facts.evidence.values()}
    if evidence_refs or assessment["primary_issue"] != "insufficient_evidence":
        for ref in evidence_refs:
            if ref not in resolved_refs:
                raise ValueError("verifier: evidence ref was never returned by MCP")


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Coordinator entry point: gather facts, decide, verify and return the output.

    The CLI already emits ``case_received`` before this call and ``case_finalized``
    after it; the workflow adds ``task_assigned``/``handoff`` (inside
    :func:`~student_agent.agents.gather_facts`), ``policy_decided`` and
    ``verification_completed`` to complete the observable lifecycle.
    """
    case_id = case["case_id"]
    facts = await gather_facts(case, gateway, trace)

    output = build_output(case, facts)

    policy = facts.evidence.get("policy")
    policy_refs = [policy.evidence_ref] if policy is not None else []
    trace.emit(
        case_id=case_id,
        event_type="policy_decided",
        actor="policy-agent",
        decision_code=output["assessment"]["primary_issue"],
        evidence_refs=policy_refs,
    )

    _verify(case_id, output, facts)

    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier",
        decision_code="verified",
    )
    return output
