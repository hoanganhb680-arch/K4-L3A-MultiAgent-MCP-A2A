from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import student_agent.provenance as provenance


def test_missing_run_metadata_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="metadata is missing"):
        provenance.verify_run_metadata(tmp_path)


def test_artifact_change_invalidates_run_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outputs = tmp_path / "outputs"
    traces = tmp_path / "traces"
    outputs.mkdir()
    traces.mkdir()
    (outputs / "L3A_CASE_001.json").write_text("original", encoding="utf-8")
    (traces / "trace.jsonl").write_text("original trace", encoding="utf-8")
    source = {"git_commit": "test", "content_sha256": "source"}
    monkeypatch.setattr(provenance, "source_snapshot", lambda _root: source)
    monkeypatch.setattr(
        provenance.Settings, "load", lambda _root: SimpleNamespace(team_api_key="team-key")
    )
    provenance.write_run_metadata(tmp_path, "team-key", source)
    provenance.verify_run_metadata(tmp_path)

    (outputs / "L3A_CASE_001.json").write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="artifacts changed"):
        provenance.verify_run_metadata(tmp_path)


def test_team_key_change_invalidates_run_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "outputs").mkdir()
    (tmp_path / "traces").mkdir()
    (tmp_path / "traces" / "trace.jsonl").write_text("trace", encoding="utf-8")
    source = {"git_commit": "test", "content_sha256": "source"}
    monkeypatch.setattr(provenance, "source_snapshot", lambda _root: source)
    provenance.write_run_metadata(tmp_path, "team-key", source)
    monkeypatch.setattr(
        provenance.Settings, "load", lambda _root: SimpleNamespace(team_api_key="other-team-key")
    )
    with pytest.raises(ValueError, match="Team API Key changed"):
        provenance.verify_run_metadata(tmp_path)
