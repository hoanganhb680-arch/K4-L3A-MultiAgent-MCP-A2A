from __future__ import annotations

import asyncio

import pytest

from student_agent.agents import consume_evidence, payment_agent, shipment_agent
from student_agent.mcp_gateway import MCPToolError
from student_agent.workflow import solve_case


class FakeGateway:
    def __init__(self, responses: dict[str, list[object]]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    async def call(self, tool_name: str, *, case_id: str, **_arguments: str) -> dict:
        assert case_id == "L3A_CASE_001"
        self.calls.append(tool_name)
        result = self.responses[tool_name].pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class FakeTrace:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def emit(self, **event: object) -> None:
        self.events.append(event)


def payload(domain: str, data: object) -> dict:
    return {"evidence_ref": "ev_" + domain + "x" * 24, "domain": domain, "data": data}


def fetch(gateway: FakeGateway, trace: FakeTrace):
    return asyncio.run(
        consume_evidence(
            gateway, trace, case_id="L3A_CASE_001", actor="test-agent",
            tool_name="get_order", arguments={"order_id": "order-1"},
        )
    )


def test_success_records_consumed_evidence() -> None:
    gateway = FakeGateway({"get_order": [payload("order", {"order_id": "order-1"})]})
    trace = FakeTrace()
    result = fetch(gateway, trace)
    assert result.status == "found"
    assert result.evidence is not None
    assert trace.events[0]["evidence_refs"] == [result.evidence.evidence_ref]


def test_explicit_not_found_is_distinct_from_error() -> None:
    gateway = FakeGateway({"get_order": [MCPToolError("get_order", "404", kind="not_found")]})
    trace = FakeTrace()
    assert fetch(gateway, trace).status == "not_found"
    assert not trace.events
    assert len(gateway.calls) == 1


def test_generic_tool_error_is_not_retried() -> None:
    gateway = FakeGateway({"get_order": [MCPToolError("get_order", "generic")]})
    assert fetch(gateway, FakeTrace()).status == "permanent_error"
    assert len(gateway.calls) == 1


def test_transient_error_is_retried_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    waits: list[int] = []

    async def fake_sleep(seconds: int) -> None:
        waits.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    gateway = FakeGateway({"get_order": [TimeoutError(), TimeoutError(), payload("order", {})]})
    assert fetch(gateway, FakeTrace()).status == "found"
    assert waits == [2, 5]
    assert len(gateway.calls) == 3


def test_programming_error_propagates() -> None:
    gateway = FakeGateway({"get_order": [KeyError("bug")]})
    with pytest.raises(KeyError, match="bug"):
        fetch(gateway, FakeTrace())


def test_exhausted_transient_error_is_distinct_from_permanent(monkeypatch) -> None:
    async def no_wait(_seconds):
        pass

    monkeypatch.setattr(asyncio, "sleep", no_wait)
    gateway = FakeGateway({"get_order": [TimeoutError() for _ in range(3)]})
    assert fetch(gateway, FakeTrace()).status == "transient_error"


@pytest.mark.parametrize(
    ("refund_response", "expected_status", "missing"),
    [
        (MCPToolError("get_refund_timeline", "404", kind="not_found"), "not_found", False),
        (MCPToolError("get_refund_timeline", "generic"), "permanent_error", True),
    ],
)
def test_refund_lookup_status(make_facts, refund_response, expected_status, missing) -> None:
    gateway = FakeGateway(
        {
            "get_order_payments": [payload("payment", [])],
            "get_payment_timeline": [payload("payment_timeline", {"events": []})],
            "get_refund_timeline": [refund_response],
        }
    )
    facts = make_facts()
    asyncio.run(payment_agent("L3A_CASE_001", "order-1", gateway, FakeTrace(), facts))
    assert facts.refund_lookup_status == expected_status
    assert ("refund" in facts.missing) is missing


def test_handoff_refs_and_missing_policy_decision() -> None:
    gateway = FakeGateway(
        {
            "get_order": [payload("order", {"order_id": "order-1", "order_status": "delivered"})],
            "get_order_items": [payload("item", [])],
            "get_sellers": [payload("seller", [])],
            "get_order_payments": [payload("payment", [])],
            "get_payment_timeline": [payload("payment_timeline", {"events": []})],
            "get_refund_timeline": [
                MCPToolError("get_refund_timeline", "404", kind="not_found")
            ],
            "get_shipment_summary": [payload("shipment", {"order_status": "delivered"})],
            "get_policy": [MCPToolError("get_policy", "server error")],
        }
    )
    trace = FakeTrace()
    case = {
        "case_id": "L3A_CASE_001",
        "policy_version": "EC_POLICY_V1",
        "customer_request": {"claimed_order_id": "order-1", "claims": []},
    }
    output = asyncio.run(solve_case(case, gateway, trace))
    assert output["assessment"]["primary_issue"] == "insufficient_evidence"
    handoffs = [event for event in trace.events if event["event_type"] == "handoff"]
    consumed = {
        ref
        for event in trace.events
        if event["event_type"] == "tool_result_consumed"
        for ref in event["evidence_refs"]
    }
    assert len(handoffs) == 4
    assert {ref for handoff in handoffs for ref in handoff["evidence_refs"]} == consumed
    policy = next(event for event in trace.events if event["event_type"] == "policy_decided")
    assert policy["decision_code"] == "POLICY_UNAVAILABLE"
    assert policy["evidence_refs"] == []


def test_payment_timeout_marks_payment_missing(monkeypatch: pytest.MonkeyPatch, make_facts) -> None:
    async def no_wait(_seconds: int) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", no_wait)
    gateway = FakeGateway(
        {
            "get_order_payments": [TimeoutError() for _ in range(3)],
            "get_payment_timeline": [payload("payment_timeline", {"events": []})],
            "get_refund_timeline": [
                MCPToolError("get_refund_timeline", "404", kind="not_found")
            ],
        }
    )
    facts = make_facts()
    asyncio.run(payment_agent("L3A_CASE_001", "order-1", gateway, FakeTrace(), facts))
    assert "payments" in facts.missing
    assert gateway.calls.count("get_order_payments") == 3


def test_shipment_timeout_marks_shipment_missing(
    monkeypatch: pytest.MonkeyPatch, make_facts
) -> None:
    async def no_wait(_seconds: int) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", no_wait)
    gateway = FakeGateway({"get_shipment_summary": [TimeoutError() for _ in range(3)]})
    facts = make_facts()
    asyncio.run(shipment_agent("L3A_CASE_001", "order-1", gateway, FakeTrace(), facts))
    assert "shipment" in facts.missing
    assert gateway.calls.count("get_shipment_summary") == 3
