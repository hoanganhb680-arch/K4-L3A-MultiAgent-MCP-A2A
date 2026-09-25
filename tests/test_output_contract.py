from __future__ import annotations

from pathlib import Path

import pytest

from student_agent.contracts import Contracts
from student_agent.decision import build_output


@pytest.mark.parametrize(
    "scenario",
    [
        "canceled_order_paid", "valid_split_payment", "refund_failed",
        "late_delivery_seller", "insufficient_evidence",
    ],
)
def test_representative_outputs_match_public_schema(
    scenario, make_facts, make_evidence, captured, refund, payment
) -> None:
    root = Path(__file__).resolve().parents[1]
    case = {
        "case_id": "L3A_CASE_001",
        "customer_request": {"claims": [{"claim_id": "claim-1", "topic": scenario}]},
    }
    common = {"order": make_evidence("order", {"order_id": "order-1"}),
              "policy": make_evidence("policy", {})}
    if scenario == "canceled_order_paid":
        facts = make_facts(
            order_status="canceled", payment_events=[captured("79.00")],
            evidence={**common, "payments": make_evidence("payments"),
                      "payment_timeline": make_evidence("payment_timeline")},
        )
    elif scenario == "valid_split_payment":
        facts = make_facts(
            items=[{"order_item_id": 1, "price": "90.00", "freight_value": "10.00"}],
            payments=[payment(1, "card", "60.00"), payment(2, "voucher", "40.00")],
            payment_events=[captured("60.00"), captured("40.00")],
            evidence={**common, "items": make_evidence("items"),
                      "payments": make_evidence("payments"),
                      "payment_timeline": make_evidence("payment_timeline")},
        )
    elif scenario == "refund_failed":
        facts = make_facts(
            refund_events=[refund("52.00", "failed")], refund_lookup_status="found",
            evidence={**common, "refund": make_evidence("refund"),
                      "payments": make_evidence("payments"),
                      "payment_timeline": make_evidence("payment_timeline")},
        )
    elif scenario == "late_delivery_seller":
        facts = make_facts(
            items=[{"order_item_id": 1, "seller_id": "seller-1", "price": "79.00",
                    "freight_value": "18.00", "shipping_limit_date": "2018-01-01T00:00:00-03:00"}],
            sellers=[{"seller_id": "seller-1"}],
            shipment={"delivered_carrier_at": "2018-01-05T00:00:00-03:00",
                      "delivered_customer_at": "2018-01-20T00:00:00-03:00",
                      "estimated_delivery_at": "2018-01-15T00:00:00-03:00",
                      "events": [
                          {"event_type": "delivered_late", "actor": "seller", "status": "confirmed"}
                      ]},
            evidence={**common, "items": make_evidence("items"),
                      "sellers": make_evidence("sellers"),
                      "shipment": make_evidence("shipment")},
        )
    else:
        facts = make_facts(order_status="canceled", evidence=common)
    output = build_output(case, facts)
    assert output["assessment"]["primary_issue"] == scenario
    Contracts(root / "contracts" / "schemas").validate_output(output, scenario)
