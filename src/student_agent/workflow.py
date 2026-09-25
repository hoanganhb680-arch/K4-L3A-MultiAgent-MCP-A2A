from __future__ import annotations

from decimal import Decimal
from typing import Any

from .agents import gather_facts
from .decision import build_output
from .entities import extract_seller_ids
from .mcp_gateway import EvidenceGateway
from .models import CaseFacts
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
_NO_ACTION_ISSUES = frozenset({"valid_split_payment", "unsupported_claim"})


def _verify(case_id: str, output: dict[str, Any], facts: CaseFacts) -> None:
    """Enforce the invariants that must hold before an output is finalized."""
    if output.get("case_id") != case_id:
        raise ValueError("verifier: mismatched case_id")

    assessment = output["assessment"]
    issue = assessment["primary_issue"]
    if issue not in _ALLOWED_ISSUES:
        raise ValueError("verifier: primary_issue outside the allowed enum")
    if assessment["case_status"] not in _ALLOWED_STATUS:
        raise ValueError("verifier: invalid case_status")
    if not 0 <= assessment["confidence"] <= 1:
        raise ValueError("verifier: confidence out of [0, 1]")

    # Status/issue cross-consistency.
    if issue in _NO_ACTION_ISSUES and assessment["case_status"] != "no_action":
        raise ValueError(f"verifier: {issue} must be no_action")
    if issue == "insufficient_evidence" and assessment["case_status"] != "needs_investigation":
        raise ValueError("verifier: insufficient_evidence must be needs_investigation")

    # Affected entities: all ids must be normalized strings and unique.
    for key, ids in output["affected_entities"].items():
        if not all(isinstance(value, str) for value in ids):
            raise ValueError(f"verifier: {key} contains a non-string id")
        if len(ids) != len(set(ids)):
            raise ValueError(f"verifier: {key} contains duplicate ids")

    financial = output["financial_resolution"]
    if financial["currency"] != "BRL":
        raise ValueError("verifier: currency must be BRL")
    recommended = Decimal(str(financial["recommended_refund_brl"]))
    if recommended < 0:
        raise ValueError("verifier: negative recommended refund")
    refund_total = sum(Decimal(str(line["amount_brl"])) for line in financial["refund_lines"])
    if refund_total != recommended:
        raise ValueError("verifier: refund lines do not sum to recommended_refund_brl")
    if recommended == 0 and financial["refund_lines"]:
        raise ValueError("verifier: zero refund must have empty refund_lines")
    if issue in _NO_ACTION_ISSUES and recommended != 0:
        raise ValueError(f"verifier: {issue} must have zero refund")

    parties = output["root_cause_analysis"]["responsible_parties"]
    for party in parties:
        if party["party_type"] not in _ALLOWED_PARTIES:
            raise ValueError("verifier: unknown responsible party type")
        if party["party_type"] == "seller" and not party.get("party_id"):
            raise ValueError("verifier: seller party must carry a real seller id")
        if party["party_type"] == "seller" and party["party_id"] not in extract_seller_ids(facts):
            raise ValueError("verifier: seller party id is absent from MCP evidence")

    actions = output["resolution_actions"]
    if len(set(actions)) != len(actions):
        raise ValueError("verifier: duplicate resolution actions")

    causes = output["root_cause_analysis"]["ranked_causes"]
    ranks = [cause["rank"] for cause in causes]
    if ranks != sorted(set(ranks)) or (ranks and ranks[0] != 1):
        raise ValueError("verifier: cause ranks must be unique and start at 1")

    resolved_refs = {evidence.evidence_ref for evidence in facts.evidence.values()}
    evidence_refs = output.get("evidence_refs", [])
    if len(set(evidence_refs)) != len(evidence_refs):
        raise ValueError("verifier: duplicate evidence refs")
    for ref in evidence_refs:
        if not isinstance(ref, str) or not ref.startswith("ev_"):
            raise ValueError("verifier: malformed evidence ref")
        if ref not in resolved_refs:
            raise ValueError("verifier: evidence ref was never returned by MCP")

    for claim in output.get("claim_assessments", []):
        for ref in claim.get("evidence_refs", []):
            if ref not in resolved_refs:
                raise ValueError("verifier: claim cites an unknown evidence ref")
            if ref not in evidence_refs:
                raise ValueError("verifier: claim evidence must appear in final evidence refs")


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
