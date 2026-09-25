from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from student_agent.contracts import ContractError, Contracts
from student_agent.mcp_gateway import EvidenceGateway, MCPToolError


class FakeSession:
    def __init__(self, result: object) -> None:
        self.result = result
        self.calls = 0

    async def call_tool(self, _name: str, *, arguments: dict) -> object:
        self.calls += 1
        assert arguments["case_id"] == "L3A_CASE_001"
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def evidence() -> dict:
    return {
        "schema_version": "day09-mcp-evidence-v1",
        "evidence_ref": "ev_" + "a" * 24,
        "result_hash": "sha256:" + "b" * 64,
        "domain": "order",
        "data": {"order_id": "order-1"},
    }


def gateway(result: object) -> EvidenceGateway:
    root = Path(__file__).resolve().parents[1]
    return EvidenceGateway(FakeSession(result), Contracts(root / "contracts" / "schemas"))


def call(result: object) -> dict:
    return asyncio.run(
        gateway(result).call("get_order", case_id="L3A_CASE_001", order_id="order-1")
    )


def test_structured_content_is_validated() -> None:
    result = SimpleNamespace(is_error=False, structured_content=evidence(), content=[])
    assert call(result)["data"]["order_id"] == "order-1"


def test_text_json_fallback_is_validated() -> None:
    result = SimpleNamespace(
        is_error=False,
        structured_content=None,
        content=[SimpleNamespace(text=json.dumps(evidence()))],
    )
    assert call(result)["domain"] == "order"


def test_generic_tool_error_is_not_mistaken_for_not_found() -> None:
    result = SimpleNamespace(
        is_error=True,
        structured_content=None,
        content=[SimpleNamespace(text="Error executing tool get_order")],
    )
    with pytest.raises(MCPToolError) as caught:
        call(result)
    assert caught.value.kind == "error"


def test_explicit_not_found_status_is_preserved() -> None:
    result = SimpleNamespace(
        is_error=True,
        structured_content={"status": "not_found"},
        content=[SimpleNamespace(text="not found")],
    )
    with pytest.raises(MCPToolError) as caught:
        call(result)
    assert caught.value.kind == "not_found"


def test_timeout_propagates_for_retry_layer() -> None:
    with pytest.raises(TimeoutError):
        call(TimeoutError("temporary timeout"))


def test_malformed_text_is_not_hidden() -> None:
    result = SimpleNamespace(
        is_error=False, structured_content=None, content=[SimpleNamespace(text="not-json")]
    )
    with pytest.raises(json.JSONDecodeError):
        call(result)


def test_schema_invalid_response_is_not_hidden() -> None:
    invalid = evidence()
    invalid["evidence_ref"] = "made-up"
    result = SimpleNamespace(is_error=False, structured_content=invalid, content=[])
    with pytest.raises(ContractError):
        call(result)
