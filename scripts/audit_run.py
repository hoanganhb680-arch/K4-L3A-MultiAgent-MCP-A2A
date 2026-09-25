"""Audit local Day09 artifacts beyond the public JSON schema.

Run after ``day09 run``: ``python scripts/audit_run.py``.
"""

from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import student_agent
from student_agent.cases import load_case_set
from student_agent.contracts import Contracts
from student_agent.provenance import METADATA_NAME
from student_agent.submission import validate_artifacts

REQUIRED_EVENTS = {
    "case_received", "task_assigned", "handoff", "verification_completed", "case_finalized"
}


def audit(root: Path) -> dict:
    case_set = load_case_set(root)
    contracts = Contracts(root / "contracts" / "schemas")
    outputs, lines = validate_artifacts(root, case_set, contracts)
    events_by_case: dict[str, list[dict]] = defaultdict(list)
    for line in lines:
        event = json.loads(line)
        events_by_case[event["case_id"]].append(event)

    issues: Counter[str] = Counter()
    statuses: Counter[str] = Counter()
    confidence: Counter[str] = Counter()
    actions: Counter[str] = Counter()
    causes: Counter[str] = Counter()
    claims: Counter[str] = Counter()
    entity_nonempty: Counter[str] = Counter()
    total_refs = 0
    tool_calls = 0
    for case_id, output in outputs.items():
        events = events_by_case[case_id]
        kinds = {event["event_type"] for event in events}
        missing_events = REQUIRED_EVENTS - kinds
        if missing_events:
            raise ValueError(f"{case_id}: missing lifecycle events {sorted(missing_events)}")
        if (
            events[0]["event_type"] != "case_received"
            or events[-1]["event_type"] != "case_finalized"
        ):
            raise ValueError(f"{case_id}: invalid event ordering")
        consumed = {
            ref
            for event in events
            if event["event_type"] == "tool_result_consumed"
            for ref in event.get("evidence_refs", [])
        }
        refs = output["evidence_refs"]
        total_refs += len(refs)
        tool_calls += sum(event["event_type"] == "tool_result_consumed" for event in events)
        if not set(refs) <= consumed:
            raise ValueError(f"{case_id}: final evidence lacks consumed trace linkage")
        if len(refs) != len(set(refs)):
            raise ValueError(f"{case_id}: duplicate final refs")
        for claim in output.get("claim_assessments", []):
            if not set(claim["evidence_refs"]) <= set(refs):
                raise ValueError(f"{case_id}: claim refs are outside final refs")
        for field, ids in output["affected_entities"].items():
            if ids:
                entity_nonempty[field] += 1
            if len(ids) > 20 or len(ids) != len(set(ids)) or any(
                not isinstance(value, str) or not value for value in ids
            ):
                raise ValueError(f"{case_id}: invalid {field}")
        assessment = output["assessment"]
        financial = output["financial_resolution"]
        refund = Decimal(str(financial["recommended_refund_brl"]))
        line_total = sum(
            (Decimal(str(line["amount_brl"])) for line in financial["refund_lines"]),
            Decimal("0"),
        )
        if refund != line_total or (refund == 0 and financial["refund_lines"]):
            raise ValueError(f"{case_id}: inconsistent refund lines")
        if assessment["case_status"] == "no_action" and refund > 0:
            raise ValueError(f"{case_id}: no_action with positive refund")
        if assessment["primary_issue"] == "insufficient_evidence" and (
            assessment["case_status"] != "needs_investigation" or refund > 0
        ):
            raise ValueError(f"{case_id}: insufficient_evidence has a final resolution")
        for party in output["root_cause_analysis"]["responsible_parties"]:
            if party["party_type"] == "seller" and party["party_id"] not in output[
                "affected_entities"
            ]["seller_ids"]:
                raise ValueError(f"{case_id}: seller party has no affected seller ID")
        if len(output["resolution_actions"]) != len(set(output["resolution_actions"])):
            raise ValueError(f"{case_id}: duplicate actions")
        issues[assessment["primary_issue"]] += 1
        statuses[assessment["case_status"]] += 1
        confidence[str(assessment["confidence"])] += 1
        actions.update(output["resolution_actions"])
        causes.update(
            cause["cause_code"] for cause in output["root_cause_analysis"]["ranked_causes"]
        )
        claims.update(claim["verdict"] for claim in output.get("claim_assessments", []))

    zip_path = root / "dist" / "submission.zip"
    with zipfile.ZipFile(zip_path) as archive:
        names = set(archive.namelist())
        expected = {"manifest.json", "trace.jsonl"} | {
            f"outputs/{case_id}.json" for case_id in case_set.case_ids
        }
        if names != expected:
            raise ValueError("ZIP entries do not match the fresh run")
        manifest = json.loads(archive.read("manifest.json"))
        if archive.read("trace.jsonl").decode("utf-8").splitlines() != lines:
            raise ValueError("ZIP trace differs from the validated trace")
        for case_id, output in outputs.items():
            if json.loads(archive.read(f"outputs/{case_id}.json")) != output:
                raise ValueError(f"ZIP output differs for {case_id}")

    def timestamp(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    first_trace = json.loads(lines[0])["occurred_at"]
    last_trace = json.loads(lines[-1])["occurred_at"]
    commit_time = subprocess.run(
        ["git", "show", "-s", "--format=%cI", "HEAD"],
        cwd=root, check=True, capture_output=True, text=True,
    ).stdout.strip()
    if timestamp(first_trace) <= timestamp(commit_time):
        raise ValueError("trace predates the current Git commit")
    if timestamp(manifest["generated_at"]) < timestamp(last_trace):
        raise ValueError("manifest predates the trace")
    metadata = json.loads((root / METADATA_NAME).read_text(encoding="utf-8"))
    return {
        "git_sha": metadata["source"]["git_commit"],
        "python_executable": sys.executable,
        "student_agent_source": student_agent.__file__,
        "team_key_fingerprint": metadata["team_key_fingerprint"],
        "outputs": len(outputs),
        "trace_events": len(lines),
        "tool_calls": tool_calls,
        "final_evidence_refs": total_refs,
        "average_evidence_refs_per_case": total_refs / len(outputs),
        "primary_issue": dict(issues),
        "entity_nonempty": dict(entity_nonempty),
        "actions": dict(actions),
        "causes": dict(causes),
        "claim_verdicts": dict(claims),
        "case_status": dict(statuses),
        "confidence": dict(confidence),
        "trace_first": first_trace,
        "trace_last": last_trace,
        "manifest_generated_at": manifest["generated_at"],
        "zip_path": str(zip_path),
    }


if __name__ == "__main__":
    print(json.dumps(audit(Path(__file__).resolve().parents[1]), indent=2))
