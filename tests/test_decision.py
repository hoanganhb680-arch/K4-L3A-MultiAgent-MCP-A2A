from __future__ import annotations

from student_agent.decision import (
    _has_duplicate_payment,
    _has_split,
    build_output,
    determine_issue,
)


def test_canceled_with_payment(make_facts, captured) -> None:
    facts = make_facts(order_status="canceled", payment_events=[captured("79.00")])
    assert determine_issue(facts) == ("canceled_order_paid", False)


def test_canceled_without_payment_is_insufficient(make_facts) -> None:
    facts = make_facts(order_status="canceled", payment_events=[])
    assert determine_issue(facts) == ("insufficient_evidence", False)


def test_unavailable_with_payment(make_facts, captured) -> None:
    facts = make_facts(order_status="unavailable", payment_events=[captured("89.00")])
    assert determine_issue(facts) == ("unavailable_order_paid", False)


def test_60_40_split_is_valid_split(make_facts, make_evidence, captured, payment) -> None:
    facts = make_facts(
        items=[{"price": "90.00", "freight_value": "10.00"}],
        payments=[payment(1, "credit_card", "60.00"), payment(2, "voucher", "40.00")],
        payment_events=[captured("60.00"), captured("40.00")],
        evidence={
            "items": make_evidence("items"),
            "payments": make_evidence("payments"),
        },
    )
    assert determine_issue(facts) == ("valid_split_payment", False)


def test_same_amount_is_not_automatically_duplicate(payment) -> None:
    payments = [payment(1, "credit_card", "50.00"), payment(2, "voucher", "50.00")]
    assert _has_duplicate_payment(payments) is False
    assert _has_split(payments) is True


def test_duplicate_rows_need_explicit_capture_evidence(make_facts, captured, payment) -> None:
    payments = [
        payment(1, "credit_card", "64.00"),
        payment(2, "voucher", "64.00"),
        payment(1, "credit_card", "64.00"),
        payment(2, "voucher", "64.00"),
    ]
    facts = make_facts(payments=payments, payment_events=[])
    assert determine_issue(facts) == ("unsupported_claim", False)
    facts.payment_events = [captured("64.00") for _ in range(4)]
    assert determine_issue(facts) == ("duplicate_charge", False)


def test_refund_failed_not_overridden_by_unrelated_topic(make_facts, refund) -> None:
    facts = make_facts(topic="late_delivery_seller", refund_events=[refund("52.00", "failed")])
    assert determine_issue(facts) == ("refund_failed", False)


def test_refund_pending(make_facts, refund) -> None:
    facts = make_facts(refund_events=[refund("89.00", "pending")])
    assert determine_issue(facts) == ("refund_pending", False)


def test_payment_mismatch(make_facts, mismatch) -> None:
    facts = make_facts(payment_events=[mismatch("35.00")])
    assert determine_issue(facts) == ("payment_mismatch", False)


def test_reconciliation_detects_mismatch_without_claim(make_facts, make_evidence, payment) -> None:
    facts = make_facts(
        topic="valid_split_payment",
        items=[{"price": "90.00", "freight_value": "10.00"}],
        payments=[payment(1, "credit_card", "60.00"), payment(2, "voucher", "35.00")],
        evidence={
            "items": make_evidence("items"),
            "payments": make_evidence("payments"),
        },
    )
    assert determine_issue(facts) == ("payment_mismatch", False)


def test_conflicting_duplicate_item_amount_does_not_create_mismatch(
    make_facts, make_evidence, payment, case
) -> None:
    facts = make_facts(
        items=[
            {"order_item_id": "item-1", "price": "79.00", "freight_value": "10.00"},
            {"order_item_id": "item-1", "price": "79.00", "freight_value": "18.00"},
        ],
        payments=[payment(1, "credit_card", "89.00"), payment(1, "credit_card", "16.00")],
        evidence={
            "items": make_evidence("items"),
            "payments": make_evidence("payments"),
        },
    )
    assert determine_issue(facts) == ("unsupported_claim", False)
    output = build_output(case, facts)
    assert output["data_conflicts"][0]["resolution_code"] == "CONFLICTING_ITEM_ROWS"


def test_failed_refund_wins_over_split_claim(make_facts, refund, payment) -> None:
    facts = make_facts(
        topic="valid_split_payment",
        payments=[payment(1, "credit_card", "44.50"), payment(2, "voucher", "44.50")],
        refund_events=[refund("52.00", "failed")],
    )
    assert determine_issue(facts) == ("refund_failed", False)


def test_pending_refund_wins_over_mismatch_claim(make_facts, refund, mismatch) -> None:
    facts = make_facts(
        topic="payment_mismatch",
        refund_events=[refund("89.00", "pending")],
        payment_events=[mismatch("35.00")],
    )
    assert determine_issue(facts) == ("refund_pending", False)


def test_unsupported_claim_distinct_from_insufficient(make_facts) -> None:
    # Delivered, on-time, no payment/refund/shipment error -> unsupported_claim.
    assert determine_issue(make_facts())[0] == "unsupported_claim"


def test_insufficient_evidence_claim_verdict(make_facts, case) -> None:
    facts = make_facts(order_status="canceled", payment_events=[])
    output = build_output(case, facts)
    assert output["assessment"]["primary_issue"] == "insufficient_evidence"
    assert output["assessment"]["case_status"] == "needs_investigation"
    assert output["financial_resolution"]["recommended_refund_brl"] == 0
    assert output["claim_assessments"][0]["verdict"] == "insufficient_evidence"


def test_refund_lines_sum_to_total(make_facts, make_evidence, captured, case) -> None:
    facts = make_facts(
        order_status="canceled",
        payment_events=[captured("79.00")],
        sellers=[{"seller_id": "seller-abc"}],
        evidence={
            "order": make_evidence("order", {"order_status": "canceled"}),
            "payments": make_evidence("payments", []),
            "payment_timeline": make_evidence("payment_timeline", {"events": []}),
            "policy": make_evidence("policy", {}),
        },
    )
    output = build_output(case, facts)
    recommended = output["financial_resolution"]["recommended_refund_brl"]
    assert recommended == 79.0
    total = sum(line["amount_brl"] for line in output["financial_resolution"]["refund_lines"])
    assert abs(total - recommended) < 1e-9
    assert "issue_refund" in output["resolution_actions"]


def test_evidence_selector_drops_unrelated_domains(make_facts, make_evidence, case) -> None:
    facts = make_facts(
        order_status="canceled",
        payment_events=[{"event_type": "captured", "amount_brl": "79.00", "status": "confirmed"}],
        evidence={
            "order": make_evidence("order", {}),
            "items": make_evidence("items", {}),
            "sellers": make_evidence("sellers", {}),
            "shipment": make_evidence("shipment", {}),
            "payments": make_evidence("payments", []),
            "payment_timeline": make_evidence("payment_timeline", {"events": []}),
            "policy": make_evidence("policy", {}),
        },
    )
    output = build_output(case, facts)
    cited = output["evidence_refs"]
    # canceled_order_paid should not cite shipment/items/sellers.
    assert facts.evidence["order"].evidence_ref in cited
    assert facts.evidence["payments"].evidence_ref in cited
    assert facts.evidence["policy"].evidence_ref in cited
    assert facts.evidence["shipment"].evidence_ref not in cited


def test_no_action_issue_has_no_refund(make_facts, case) -> None:
    facts = make_facts()
    output = build_output(case, facts)
    assert output["assessment"]["primary_issue"] == "unsupported_claim"
    assert output["assessment"]["case_status"] == "no_action"
    assert output["financial_resolution"]["recommended_refund_brl"] == 0
    assert output["financial_resolution"]["refund_lines"] == []
