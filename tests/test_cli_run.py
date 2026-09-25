from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

import student_agent.cli as cli


def test_empty_evidence_cases_complete_without_replacing_stale_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outputs = tmp_path / "outputs"
    traces = tmp_path / "traces"
    outputs.mkdir()
    traces.mkdir()
    (outputs / "old.json").write_text("previous output", encoding="utf-8")
    (traces / "trace.jsonl").write_text("previous trace", encoding="utf-8")
    (tmp_path / ".day09-run.json").write_text("previous metadata", encoding="utf-8")

    class FakeContracts:
        def __init__(self, _root: Path) -> None:
            pass

        def validate_trace(self, _event: dict, _label: str) -> None:
            pass

        def validate_output(self, _output: dict, _label: str) -> None:
            pass

    class FakeGateway:
        async def list_tools(self) -> list[str]:
            return ["get_order"]

    @asynccontextmanager
    async def fake_connection(_endpoint: str, _key: str, _contracts: FakeContracts):
        yield FakeGateway()

    async def fake_solve(case: dict, _gateway: FakeGateway, _trace: cli.TraceWriter) -> dict:
        return {"case_id": case["case_id"], "evidence_refs": []}

    case_ids = tuple(f"L3A_CASE_{index:03}" for index in range(1, 4))
    case_set = SimpleNamespace(
        case_ids=case_ids,
        cases={case_id: {"case_id": case_id} for case_id in case_ids},
    )
    monkeypatch.setattr(cli.Settings, "load", lambda _root: SimpleNamespace(
        mcp_endpoint="endpoint", team_api_key="key"
    ))
    monkeypatch.setattr(cli, "load_case_set", lambda _root: case_set)
    monkeypatch.setattr(cli, "Contracts", FakeContracts)
    monkeypatch.setattr(cli, "connect_gateway", fake_connection)
    monkeypatch.setattr(cli, "solve_case", fake_solve)
    monkeypatch.setattr(cli, "source_snapshot", lambda _root: {"source": "current"})
    monkeypatch.setattr(cli, "write_run_metadata", lambda *_args: None)
    monkeypatch.setattr(cli, "source_snapshot", lambda _root: {"git_commit": "test"})
    monkeypatch.setattr(cli, "write_run_metadata", lambda *_args: None)

    asyncio.run(cli._run(tmp_path))
    assert not (outputs / "old.json").exists()
    assert {path.stem for path in outputs.glob("*.json")} == set(case_ids)
    assert "case_finalized" in (traces / "trace.jsonl").read_text(encoding="utf-8")
    assert (tmp_path / ".day09-run.json").read_text(encoding="utf-8") == "previous metadata"


def test_success_replaces_previous_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outputs = tmp_path / "outputs"
    traces = tmp_path / "traces"
    outputs.mkdir()
    traces.mkdir()
    (outputs / "old.json").write_text("previous output", encoding="utf-8")
    (traces / "trace.jsonl").write_text("previous trace", encoding="utf-8")
    connections = []
    solved_cases = []

    class FakeContracts:
        def __init__(self, _root: Path) -> None:
            pass

        def validate_trace(self, _event: dict, _label: str) -> None:
            pass

        def validate_output(self, _output: dict, _label: str) -> None:
            pass

    class FakeGateway:
        async def list_tools(self) -> list[str]:
            return ["get_order"]

    @asynccontextmanager
    async def fake_connection(_endpoint: str, _key: str, _contracts: FakeContracts):
        connections.append(True)
        yield FakeGateway()

    async def fake_solve(case: dict, _gateway: FakeGateway, _trace: cli.TraceWriter) -> dict:
        solved_cases.append(case["case_id"])
        return {"case_id": case["case_id"], "evidence_refs": ["ev_" + "a" * 24]}

    case_ids = ("L3A_CASE_001", "L3A_CASE_002")
    case_set = SimpleNamespace(
        case_ids=case_ids,
        cases={case_id: {"case_id": case_id} for case_id in case_ids},
    )
    monkeypatch.setattr(cli.Settings, "load", lambda _root: SimpleNamespace(
        mcp_endpoint="endpoint", team_api_key="key"
    ))
    monkeypatch.setattr(cli, "load_case_set", lambda _root: case_set)
    monkeypatch.setattr(cli, "Contracts", FakeContracts)
    monkeypatch.setattr(cli, "connect_gateway", fake_connection)
    monkeypatch.setattr(cli, "solve_case", fake_solve)
    monkeypatch.setattr(cli, "source_snapshot", lambda _root: {"source": "current"})
    monkeypatch.setattr(cli, "write_run_metadata", lambda *_args: None)
    monkeypatch.setattr(cli, "source_snapshot", lambda _root: {"git_commit": "test"})
    monkeypatch.setattr(cli, "write_run_metadata", lambda *_args: None)

    asyncio.run(cli._run(tmp_path))
    assert len(connections) == 1
    assert solved_cases == list(case_ids)
    assert not (outputs / "old.json").exists()
    assert {path.stem for path in outputs.glob("*.json")} == set(case_ids)
    assert "case_finalized" in (traces / "trace.jsonl").read_text(encoding="utf-8")
