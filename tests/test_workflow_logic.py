from __future__ import annotations

import pytest

from student_agent.decision import build_output
from student_agent.workflow import _verify


def _canceled_output(make_facts, make_evidence, case, captured, topic: str = "canceled_order_paid"):
    facts = make_facts(
        topic=topic,
        order_status="canceled",
        payment_events=[captured("79.00")],
        evidence={
            "order": make_evidence("order", {"order_status": "canceled"}),
            "payments": make_evidence("payments", []),
            "payment_timeline": make_evidence("payment_timeline", {"events": []}),
            "policy": make_evidence("policy", {}),
        },
    )
    return facts, build_output(case, facts)


def test_verify_accepts_valid_output(make_facts, make_evidence, case, captured) -> None:
    facts, output = _canceled_output(make_facts, make_evidence, case, captured)
    _verify("L3A_CASE_001", output, facts)  # should not raise


def test_verify_rejects_non_string_ids(make_facts, make_evidence, case, captured) -> None:
    facts, output = _canceled_output(make_facts, make_evidence, case, captured)
    output["affected_entities"]["order_ids"] = [123]  # type: ignore[list-item]
    with pytest.raises(ValueError):
        _verify("L3A_CASE_001", output, facts)


def test_verify_rejects_duplicate_ids(make_facts, make_evidence, case, captured) -> None:
    facts, output = _canceled_output(make_facts, make_evidence, case, captured)
    output["affected_entities"]["item_ids"] = ["x", "x"]
    with pytest.raises(ValueError):
        _verify("L3A_CASE_001", output, facts)


def test_verify_rejects_more_than_twenty_ids(make_facts, make_evidence, case, captured) -> None:
    facts, output = _canceled_output(make_facts, make_evidence, case, captured)
    output["affected_entities"]["item_ids"] = [str(index) for index in range(21)]
    with pytest.raises(ValueError, match="public id limits"):
        _verify("L3A_CASE_001", output, facts)


def test_verify_rejects_unknown_evidence_ref(make_facts, make_evidence, case, captured) -> None:
    facts, output = _canceled_output(make_facts, make_evidence, case, captured)
    output["evidence_refs"] = ["ev_" + "z" * 20]
    with pytest.raises(ValueError):
        _verify("L3A_CASE_001", output, facts)


def test_verify_rejects_refund_sum_mismatch(make_facts, make_evidence, case, captured) -> None:
    facts, output = _canceled_output(make_facts, make_evidence, case, captured)
    output["financial_resolution"]["refund_lines"] = [
        {"reason_code": "X", "amount_brl": 1.0, "entity_id": None}
    ]
    with pytest.raises(ValueError):
        _verify("L3A_CASE_001", output, facts)


def test_verify_rejects_seller_party_without_id(make_facts, make_evidence, case, captured) -> None:
    facts = make_facts(
        order_status="unavailable",
        payment_events=[captured("89.00")],
        sellers=[],
        items=[],
        evidence={
            "order": make_evidence("order", {"order_status": "unavailable"}),
            "items": make_evidence("items", []),
            "sellers": make_evidence("sellers", []),
            "payments": make_evidence("payments", []),
            "payment_timeline": make_evidence("payment_timeline", {"events": []}),
            "policy": make_evidence("policy", {}),
        },
    )
    output = build_output(case, facts)
    assert output["assessment"]["primary_issue"] == "insufficient_evidence"
    output["root_cause_analysis"]["responsible_parties"] = [
        {"party_type": "seller", "party_id": None}
    ]
    with pytest.raises(ValueError):
        _verify("L3A_CASE_001", output, facts)


def test_customer_claim_does_not_override_mcp_evidence(make_facts, refund) -> None:
    # Authoritative refund-failed evidence wins even when the claim topic is unrelated.
    from student_agent.decision import determine_issue

    facts = make_facts(topic="late_delivery_seller", refund_events=[refund("52.00", "failed")])
    issue, _ = determine_issue(facts)
    assert issue == "refund_failed"


def test_ranked_causes_unique_and_start_at_one(make_facts, make_evidence, case, captured) -> None:
    facts, output = _canceled_output(make_facts, make_evidence, case, captured)
    causes = output["root_cause_analysis"]["ranked_causes"]
    ranks = [cause["rank"] for cause in causes]
    assert len(ranks) == len(set(ranks))
    assert ranks[0] == 1
