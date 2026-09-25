from __future__ import annotations

import pytest

from student_agent.models import CaseFacts, Evidence

_ISSUE_SPEC = {
    "canceled_order_paid": ("action_required", "issue_refund", "79.00", "platform"),
    "unavailable_order_paid": ("action_required", "issue_refund", "89.00", "seller"),
    "late_delivery_seller": ("action_required", "refund_freight", "18.00", "seller"),
    "late_delivery_logistics": ("action_required", "refund_freight", "16.00", "logistics_provider"),
    "valid_split_payment": ("no_action", "document_no_action", "0.00", "customer"),
    "payment_mismatch": ("action_required", "reconcile_payment", "35.00", "payment_provider"),
    "duplicate_charge": ("action_required", "refund_duplicate_charge", "64.00", "payment_provider"),
    "refund_pending": ("needs_investigation", "monitor_refund", "0.00", "payment_provider"),
    "refund_failed": ("action_required", "retry_refund", "52.00", "payment_provider"),
    "unsupported_claim": ("no_action", "document_no_action", "0.00", "customer"),
    "insufficient_evidence": ("needs_investigation", "", "0.00", "unknown"),
}


@pytest.fixture
def policy() -> dict:
    rules = {}
    for issue, (status, action, refund, party) in _ISSUE_SPEC.items():
        rules[issue] = {
            "case_status": status,
            "recommended_action": action,
            "refund_brl": float(refund),
            "responsible_parties": [{"party_type": party, "party_id": None}],
        }
    return {"rules": rules, "currency": "BRL", "policy_version": "EC_POLICY_V1"}


@pytest.fixture
def make_facts(policy):
    def factory(**overrides: object) -> CaseFacts:
        base: dict = {
            "case_id": "L3A_CASE_001",
            "order_id": "order-abc123",
            "topic": "",
            "policy_version": "EC_POLICY_V1",
            "order_status": "delivered",
            "items": [],
            "sellers": [],
            "payments": [],
            "payment_events": [],
            "refund_events": [],
            "shipment": {},
            "policy_rules": policy,
            "evidence": {},
        }
        base.update(overrides)
        return CaseFacts(**base)

    return factory


@pytest.fixture
def make_evidence():
    def factory(kind: str, data: object = None) -> Evidence:
        return Evidence(evidence_ref=f"ev_{kind}" + "x" * 20, domain=kind, data=data)

    return factory


@pytest.fixture
def captured():
    def factory(amount: str, status: str = "confirmed") -> dict:
        return {"event_type": "captured", "amount_brl": amount, "status": status}

    return factory


@pytest.fixture
def refund():
    def factory(amount: str, status: str) -> dict:
        return {"event_type": "refund_requested", "amount_brl": amount, "status": status}

    return factory


@pytest.fixture
def mismatch():
    def factory(amount: str) -> dict:
        return {"event_type": "reconciliation_mismatch", "amount_brl": amount, "status": "open"}

    return factory


@pytest.fixture
def payment():
    def factory(sequential: int, ptype: str, value: str) -> dict:
        return {
            "payment_sequential": str(sequential),
            "payment_type": ptype,
            "payment_installments": "1",
            "payment_value": value,
        }

    return factory


@pytest.fixture
def case() -> dict:
    return {
        "case_id": "L3A_CASE_001",
        "customer_request": {
            "claims": [
                {"claim_id": "claim-a", "topic": "canceled_order_paid"},
                {"claim_id": "claim-b", "topic": "requested_full_refund"},
            ]
        },
    }