from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from student_agent.contracts import Contracts
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


def test_duplicate_rows_need_explicit_capture_evidence(
    make_facts, make_evidence, captured, payment
) -> None:
    payments = [
        payment(1, "credit_card", "64.00"),
        payment(2, "voucher", "64.00"),
        payment(1, "credit_card", "64.00"),
        payment(2, "voucher", "64.00"),
    ]
    facts = make_facts(
        items=[{"order_item_id": 1, "price": "79.00", "freight_value": "10.00"}],
        payments=payments,
        payment_events=[],
        evidence={"items": make_evidence("items")},
    )
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
    assert facts.evidence["payment_timeline"].evidence_ref in cited
    assert facts.evidence["payments"].evidence_ref not in cited
    assert facts.evidence["policy"].evidence_ref in cited
    assert facts.evidence["shipment"].evidence_ref not in cited


def test_no_action_issue_has_no_refund(make_facts, make_evidence, case) -> None:
    facts = make_facts(
        refund_lookup_status="not_found",
        evidence={
            kind: make_evidence(kind)
            for kind in ("order", "items", "payments", "payment_timeline", "shipment", "policy")
        },
    )
    output = build_output(case, facts)
    assert output["assessment"]["primary_issue"] == "unsupported_claim"
    assert output["assessment"]["case_status"] == "no_action"
    assert output["financial_resolution"]["recommended_refund_brl"] == 0
    assert output["financial_resolution"]["refund_lines"] == []


def test_unsupported_primary_issue_marks_claim_unsupported(
    make_facts, make_evidence
) -> None:
    case = {
        "case_id": "L3A_CASE_001",
        "customer_request": {"claims": [{"claim_id": "claim-1", "topic": "unsupported_claim"}]},
    }
    facts = make_facts(
        topic="unsupported_claim",
        evidence={
            kind: make_evidence(kind)
            for kind in ("order", "items", "payments", "payment_timeline", "shipment", "policy")
        },
    )
    output = build_output(case, facts)
    assert output["assessment"]["primary_issue"] == "unsupported_claim"
    assert output["claim_assessments"][0]["verdict"] == "unsupported"


def test_unsupported_claim_requires_payment_evidence(make_facts, make_evidence, case) -> None:
    facts = make_facts(
        evidence={"order": make_evidence("order"), "shipment": make_evidence("shipment")},
        missing=["payments", "payment_timeline"],
    )
    assert build_output(case, facts)["assessment"]["primary_issue"] == "insufficient_evidence"


def test_unsupported_late_claim_requires_shipment(make_facts, make_evidence) -> None:
    case = {
        "case_id": "L3A_CASE_001",
        "customer_request": {"claims": [{"claim_id": "late", "topic": "late_delivery_seller"}]},
    }
    facts = make_facts(evidence={"order": make_evidence("order")}, missing=["shipment"])
    assert build_output(case, facts)["assessment"]["primary_issue"] == "insufficient_evidence"


def test_refund_lookup_error_prevents_unsupported_verdict(make_facts, make_evidence) -> None:
    case = {
        "case_id": "L3A_CASE_001",
        "customer_request": {"claims": [{"claim_id": "refund", "topic": "refund_failed"}]},
    }
    facts = make_facts(
        refund_lookup_status="error",
        missing=["refund"],
        evidence={
            kind: make_evidence(kind)
            for kind in ("order", "payments", "payment_timeline", "policy")
        },
    )
    assert build_output(case, facts)["assessment"]["primary_issue"] == "insufficient_evidence"


def test_split_requires_captured_total_to_match_payment_rows(
    make_facts, make_evidence, captured, payment
) -> None:
    facts = make_facts(
        items=[{"price": "90.00", "freight_value": "10.00"}],
        payments=[payment(1, "credit_card", "60.00"), payment(2, "voucher", "40.00")],
        payment_events=[captured("40.00")],
        evidence={
            kind: make_evidence(kind)
            for kind in ("order", "items", "payments", "payment_timeline", "policy")
        },
    )
    assert determine_issue(facts)[0] == "insufficient_evidence"


def test_five_claims_and_claim_specific_refs(
    make_facts, make_evidence, captured, case
) -> None:
    case["customer_request"]["claims"] = [
        {"claim_id": f"claim-{index}", "topic": topic}
        for index, topic in enumerate(
            [
                "canceled_order_paid",
                "late_delivery_seller",
                "requested_full_refund",
                "duplicate_charge",
                "refund_pending",
            ]
        )
    ]
    facts = make_facts(
        order_status="canceled",
        payment_events=[captured("79.00")],
        refund_lookup_status="not_found",
        evidence={
            kind: make_evidence(kind)
            for kind in (
                "order", "items", "sellers", "payments", "payment_timeline", "shipment", "policy"
            )
        },
    )
    output = build_output(case, facts)
    claims = output["claim_assessments"]
    assert len(claims) == 5
    assert claims[1]["verdict"] == "unsupported"
    assert facts.evidence["shipment"].evidence_ref in claims[1]["evidence_refs"]
    assert facts.evidence["payments"].evidence_ref not in claims[1]["evidence_refs"]
    assert set(claims[1]["evidence_refs"]) <= set(output["evidence_refs"])
    root = Path(__file__).resolve().parents[1]
    Contracts(root / "contracts" / "schemas").validate_output(output, "five claims")


def test_invalid_policy_shape_downgrades_resolution(
    make_facts, make_evidence, captured, case
) -> None:
    facts = make_facts(
        order_status="canceled",
        payment_events=[captured("79.00")],
        policy_rules={"rules": [{"issue": "canceled_order_paid"}]},
        evidence={
            kind: make_evidence(kind)
            for kind in ("order", "payments", "payment_timeline", "policy")
        },
    )
    output = build_output(case, facts)
    assert output["assessment"]["primary_issue"] == "insufficient_evidence"
    assert output["financial_resolution"]["recommended_refund_brl"] == 0


def test_nonfinite_policy_amount_downgrades_without_exception(
    make_facts, make_evidence, captured, case, policy
) -> None:
    invalid_policy = deepcopy(policy)
    invalid_policy["rules"]["canceled_order_paid"]["refund_brl"] = "NaN"
    facts = make_facts(
        order_status="canceled", payment_events=[captured("79.00")],
        policy_rules=invalid_policy,
        evidence={
            kind: make_evidence(kind)
            for kind in ("order", "payments", "payment_timeline", "policy")
        },
    )
    assert build_output(case, facts)["assessment"]["primary_issue"] == "insufficient_evidence"


def test_seller_late_refund_uses_affected_freight(make_facts, make_evidence, case) -> None:
    facts = make_facts(
        items=[
            {
                "order_item_id": 1, "seller_id": "seller-1", "price": "79.00",
                "freight_value": "11.00",
                "shipping_limit_date": "2018-01-01T00:00:00-03:00",
            },
            {
                "order_item_id": 2, "seller_id": "seller-1", "price": "79.00",
                "freight_value": "19.00",
                "shipping_limit_date": "2018-01-10T00:00:00-03:00",
            },
        ],
        shipment={
            "delivered_carrier_at": "2018-01-05T00:00:00-03:00",
            "delivered_customer_at": "2018-01-20T00:00:00-03:00",
            "estimated_delivery_at": "2018-01-15T00:00:00-03:00",
            "events": [{"event_type": "delivered_late", "actor": "seller", "status": "confirmed"}],
        },
        sellers=[{"seller_id": "seller-1"}],
        evidence={
            kind: make_evidence(kind)
            for kind in ("order", "items", "sellers", "shipment", "policy")
        },
    )
    output = build_output(case, facts)
    assert output["assessment"]["primary_issue"] == "late_delivery_seller"
    assert output["financial_resolution"]["recommended_refund_brl"] == 11.0


def test_seller_late_without_affected_seller_is_insufficient(
    make_facts, make_evidence, case
) -> None:
    facts = make_facts(
        items=[{
            "order_item_id": 1, "seller_id": "seller-1", "price": "79.00",
            "freight_value": "10.00", "shipping_limit_date": "2018-01-10T00:00:00-03:00",
        }],
        sellers=[{"seller_id": "seller-1"}],
        shipment={
            "delivered_carrier_at": "2018-01-05T00:00:00-03:00",
            "delivered_customer_at": "2018-01-20T00:00:00-03:00",
            "estimated_delivery_at": "2018-01-15T00:00:00-03:00",
            "events": [{"event_type": "delivered_late", "actor": "seller", "status": "confirmed"}],
        },
        evidence={
            kind: make_evidence(kind)
            for kind in ("order", "items", "sellers", "shipment", "policy")
        },
    )
    output = build_output(case, facts)
    assert output["assessment"]["primary_issue"] == "insufficient_evidence"
    assert output["financial_resolution"]["recommended_refund_brl"] == 0


def test_unknown_late_actor_is_not_invented(make_facts) -> None:
    facts = make_facts(
        shipment={
            "delivered_customer_at": "2018-01-20T00:00:00-03:00",
            "estimated_delivery_at": "2018-01-15T00:00:00-03:00",
            "events": [
                {"event_type": "delivered_late", "actor": "unknown-carrier", "status": "confirmed"}
            ],
        },
    )
    assert determine_issue(facts)[0] == "insufficient_evidence"


def test_unconfirmed_late_event_does_not_assign_responsibility(make_facts) -> None:
    facts = make_facts(
        shipment={
            "delivered_customer_at": "2018-01-20T00:00:00-03:00",
            "estimated_delivery_at": "2018-01-15T00:00:00-03:00",
            "events": [{"event_type": "delivered_late", "actor": "seller", "status": "pending"}],
        },
    )
    assert determine_issue(facts)[0] == "insufficient_evidence"


def test_delivery_dates_use_absolute_time_across_offsets(make_facts) -> None:
    facts = make_facts(
        shipment={
            "delivered_customer_at": "2018-01-20T00:30:00+02:00",
            "estimated_delivery_at": "2018-01-19T23:00:00+00:00",
            "events": [{"event_type": "delivered_late", "actor": "seller", "status": "confirmed"}],
        },
    )
    # 00:30+02:00 precedes 23:00+00:00 despite its later calendar date.
    assert determine_issue(facts)[0] != "late_delivery_seller"


def test_case_004_style_confirmed_logistics_delay_is_not_rejected(
    make_facts, make_evidence, captured
) -> None:
    case = {
        "case_id": "L3A_CASE_004",
        "customer_request": {"claims": [
            {"claim_id": "claim-004-a", "topic": "late_delivery_logistics"},
            {"claim_id": "claim-004-b", "topic": "requested_full_refund"},
        ]},
    }
    facts = make_facts(
        case_id="L3A_CASE_004", order_status="delivered",
        payment_events=[captured("100.00")],
        shipment={
            "order_id": "order-004",
            "delivered_carrier_at": "2018-03-25T09:00:00-03:00",
            "delivered_customer_at": "2018-04-07T09:00:00-03:00",
            "estimated_delivery_at": "2018-04-02T09:00:00-03:00",
            "events": [{"event_type": "delivered_late", "actor": "logistics_provider",
                        "status": "confirmed"}],
        },
        evidence={kind: make_evidence(kind) for kind in (
            "order", "shipment", "payment_timeline", "policy"
        )},
    )
    output = build_output(case, facts)
    assert output["assessment"]["primary_issue"] == "late_delivery_logistics"
    assert output["claim_assessments"][1]["verdict"] == "unsupported"
    assert output["financial_resolution"]["recommended_refund_brl"] == 16.0
    assert output["affected_entities"]["shipment_ids"] == []


def test_policy_codes_and_issue_scoped_entities_are_exact(
    make_facts, make_evidence, policy, captured, case
) -> None:
    rule = policy["rules"]["canceled_order_paid"]
    rule["cause_code"] = "POLICY_CAUSE_CODE"
    rule["refund_reason_code"] = "POLICY_REFUND_REASON"
    rule["recommended_action"] = "policy_action"
    facts = make_facts(
        order_status="canceled", payment_events=[captured("79.00")],
        payments=[{"order_id": "order-abc123", "payment_reference": "payment-ref-1"}],
        items=[{"order_item_id": "item-1", "seller_id": "seller-1"}],
        sellers=[{"seller_id": "seller-1"}],
        shipment={"shipment_id": "shipment-1"}, policy_rules=policy,
        evidence={
            "order": make_evidence("order", {"order_id": "order-abc123"}),
            **{kind: make_evidence(kind) for kind in (
                "items", "sellers", "payments", "payment_timeline", "shipment", "policy"
            )},
        },
    )
    output = build_output(case, facts)
    assert output["root_cause_analysis"]["ranked_causes"] == [
        {"cause_code": "POLICY_CAUSE_CODE", "rank": 1}
    ]
    assert output["financial_resolution"]["refund_lines"][0]["reason_code"] == (
        "POLICY_REFUND_REASON"
    )
    assert output["resolution_actions"] == ["policy_action"]
    assert output["affected_entities"] == {
        "order_ids": ["order-abc123"], "item_ids": [], "seller_ids": [],
        "payment_references": ["payment-ref-1"], "shipment_ids": [],
    }


def test_real_policy_without_cause_code_uses_issue_code(
    make_facts, make_evidence, captured, case
) -> None:
    facts = make_facts(
        order_status="canceled", payment_events=[captured("79.00")],
        evidence={kind: make_evidence(kind) for kind in (
            "order", "payment_timeline", "policy"
        )},
    )
    output = build_output(case, facts)
    assert output["root_cause_analysis"]["ranked_causes"][0]["cause_code"] == (
        "CANCELED_ORDER_PAID"
    )
    assert output["financial_resolution"]["refund_lines"][0]["reason_code"] == (
        "CANCELED_ORDER_PAID"
    )


def test_repeated_payment_batch_without_excess_is_not_duplicate(payment, captured) -> None:
    payments = [
        payment(1, "card", "25.00"), payment(2, "voucher", "25.00"),
        payment(1, "card", "25.00"), payment(2, "voucher", "25.00"),
    ]
    events = [captured("25.00") for _ in range(4)]
    assert _has_duplicate_payment(payments, events, expected_total=100) is False


def test_secondary_refund_claim_uses_refund_evidence_even_with_canceled_primary(
    make_facts, make_evidence, captured, refund
) -> None:
    case = {
        "case_id": "L3A_CASE_001",
        "customer_request": {"claims": [
            {"claim_id": "claim-order", "topic": "canceled_order_paid"},
            {"claim_id": "claim-refund", "topic": "refund_failed"},
        ]},
    }
    facts = make_facts(
        order_status="canceled", payment_events=[captured("79.00")],
        refund_events=[refund("52.00", "failed")], refund_lookup_status="found",
        evidence={
            kind: make_evidence(kind)
            for kind in ("order", "payments", "payment_timeline", "refund", "policy")
        },
    )
    output = build_output(case, facts)
    assert output["assessment"]["primary_issue"] == "canceled_order_paid"
    assert output["claim_assessments"][1]["verdict"] == "supported"
    assert facts.evidence["refund"].evidence_ref in output["claim_assessments"][1]["evidence_refs"]


def test_equivalent_money_formats_do_not_create_item_conflict(
    make_facts, make_evidence, payment, captured
) -> None:
    facts = make_facts(
        items=[
            {"order_item_id": 1, "price": "79", "freight_value": "10.0"},
            {"order_item_id": 1, "price": "79.00", "freight_value": "10.00"},
        ],
        payments=[payment(1, "card", "89.00")],
        payment_events=[captured("89.00")],
        evidence={kind: make_evidence(kind) for kind in ("items", "payments")},
    )
    assert determine_issue(facts)[0] == "unsupported_claim"
