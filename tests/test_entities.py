from __future__ import annotations

from student_agent.entities import (
    clean_id,
    extract_item_ids,
    extract_order_ids,
    extract_payment_references,
    extract_seller_ids,
    extract_shipment_ids,
)


def test_clean_id_coerces_integer_to_string() -> None:
    assert clean_id(123) == "123"


def test_clean_id_returns_none_for_empty() -> None:
    assert clean_id(None) is None
    assert clean_id("") is None
    assert clean_id("   ") is None


def test_item_ids_are_normalized_strings(make_facts) -> None:
    facts = make_facts(items=[{"order_item_id": 42}, {"order_item_id": "42"}])
    ids = extract_item_ids(facts)
    assert ids == ["42"]
    assert all(isinstance(value, str) for value in ids)


def test_seller_ids_are_unique_and_sorted(make_facts) -> None:
    facts = make_facts(
        sellers=[{"seller_id": "seller-b"}, {"seller_id": "seller-a"}, {"seller_id": "seller-a"}]
    )
    assert extract_seller_ids(facts) == ["seller-a", "seller-b"]


def test_order_ids_extracted(make_facts, make_evidence) -> None:
    facts = make_facts(
        order_id="order-xyz",
        evidence={"order": make_evidence("order", {"order_id": "order-xyz"})},
    )
    assert extract_order_ids(facts) == ["order-xyz"]


def test_claimed_order_id_alone_is_not_authoritative(make_facts) -> None:
    assert extract_order_ids(make_facts(order_id="order-xyz")) == []


def test_payment_references_not_fabricated_from_order_id(make_facts) -> None:
    facts = make_facts(order_id="order-xyz")
    assert extract_payment_references(facts) == []


def test_payment_sequential_is_used_when_mcp_has_no_transaction_id(make_facts) -> None:
    facts = make_facts(
        payments=[
            {"order_id": "order-xyz", "payment_sequential": "1", "payment_value": "79.00"},
            {"order_id": "order-xyz", "payment_sequential": "2", "payment_value": "18.00"},
        ]
    )
    assert extract_payment_references(facts) == ["1", "2"]


def test_repeated_payment_sequential_is_one_reference(make_facts) -> None:
    # Actual MCP shape: payment rows carry a sequence and an order ID, but no
    # transaction or capture ID. Repeated sequence values identify one payment.
    facts = make_facts(payments=[
        {"order_id": "order-xyz", "payment_sequential": "1", "payment_value": "79.00"},
        {"order_id": "order-xyz", "payment_sequential": "1", "payment_value": "18.00"},
    ])
    assert extract_payment_references(facts) == ["1"]


def test_real_shipment_shape_without_identifier_stays_empty(make_facts) -> None:
    facts = make_facts(shipment={
        "order_id": "order-xyz", "delivered_customer_at": "2018-04-07T09:00:00-03:00",
        "events": [{"event_type": "delivered_late", "actor": "logistics_provider"}],
    })
    assert extract_shipment_ids(facts) == []


def test_shipment_ids_empty(make_facts) -> None:
    assert extract_shipment_ids(make_facts()) == []


def test_item_and_seller_ids_respect_schema_limit(make_facts) -> None:
    facts = make_facts(
        items=[{"order_item_id": index, "seller_id": f"seller-{index:02}"} for index in range(25)]
    )
    assert len(extract_item_ids(facts)) == 20
    assert len(extract_seller_ids(facts)) == 20
