from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class Evidence:
    """One MCP evidence payload returned and actually consumed by a specialist."""

    evidence_ref: str
    domain: str
    data: Any


@dataclass(frozen=True)
class FetchResult:
    """Distinguish confirmed absence from a failed MCP read."""

    status: Literal["found", "not_found", "transient_error", "permanent_error"]
    evidence: Evidence | None = None


@dataclass
class CaseFacts:
    """Structured, evidence-backed facts that specialist agents populate for one case."""

    case_id: str
    order_id: str
    topic: str
    policy_version: str
    order_status: str = "unknown"
    customer_id: str | None = None
    items: list[dict[str, Any]] = field(default_factory=list)
    sellers: list[dict[str, Any]] = field(default_factory=list)
    payments: list[dict[str, Any]] = field(default_factory=list)
    payment_events: list[dict[str, Any]] = field(default_factory=list)
    refund_events: list[dict[str, Any]] = field(default_factory=list)
    refund_lookup_status: Literal[
        "unknown", "found", "not_found", "transient_error", "permanent_error"
    ] = "unknown"
    shipment: dict[str, Any] = field(default_factory=dict)
    policy_rules: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Evidence] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
