from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError

from .contracts import Contracts


class MCPToolError(RuntimeError):
    """A tool-level failure with an explicit, non-authoritative error category."""

    def __init__(self, tool_name: str, message: str, *, kind: str = "error") -> None:
        super().__init__(f"MCP tool {tool_name} failed: {message}")
        self.kind = kind


class EvidenceGateway:
    def __init__(self, session: ClientSession, contracts: Contracts) -> None:
        self._session = session
        self._contracts = contracts

    async def list_tools(self) -> list[str]:
        response = await self._session.list_tools()
        return sorted(tool.name for tool in response.tools)

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        payload = {"case_id": case_id, **arguments}
        try:
            result = await self._session.call_tool(tool_name, arguments=payload)
        except MCPError as exc:
            kind = "error"
            if exc.code == 404:
                kind = "not_found"
            elif exc.code in {500, 502, 503, 504, -32603}:
                kind = "transient"
            raise MCPToolError(tool_name, exc.message, kind=kind) from exc
        if result.is_error:
            message = " ".join(
                block.text for block in result.content if getattr(block, "text", None)
            )
            details = getattr(result, "structured_content", None)
            details = details if isinstance(details, dict) else {}
            code = details.get("code")
            status = details.get("status")
            kind = "error"
            if code in {404, "NOT_FOUND"} or status == "not_found":
                kind = "not_found"
            elif code in {500, 502, 503, 504} or status == "transient":
                kind = "transient"
            raise MCPToolError(tool_name, message or "unknown error", kind=kind)
        evidence = getattr(result, "structuredContent", None)
        if evidence is None:
            evidence = getattr(result, "structured_content", None)
        if evidence is None:
            text_blocks = [block.text for block in result.content if getattr(block, "text", None)]
            if len(text_blocks) != 1:
                raise ValueError(f"MCP tool {tool_name} did not return one evidence object")
            evidence = json.loads(text_blocks[0])
        self._contracts.validate_evidence(evidence, f"MCP tool {tool_name}")
        return evidence


@asynccontextmanager
async def connect_gateway(
    endpoint: str, team_api_key: str, contracts: Contracts
) -> AsyncIterator[EvidenceGateway]:
    headers = {"Authorization": f"Bearer {team_api_key}"}
    timeout = httpx2.Timeout(300.0, connect=30.0, write=30.0, pool=30.0)
    async with (
        httpx2.AsyncClient(headers=headers, timeout=timeout) as http_client,
        streamable_http_client(endpoint, http_client=http_client) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        yield EvidenceGateway(session, contracts)
