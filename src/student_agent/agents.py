from __future__ import annotations

import asyncio
from typing import Any

import anyio
import httpx2

from .mcp_gateway import EvidenceGateway, MCPToolError
from .models import CaseFacts, Evidence, FetchResult
from .trace import TraceWriter


async def consume_evidence(
    gateway: EvidenceGateway,
    trace: TraceWriter,
    *,
    case_id: str,
    actor: str,
    tool_name: str,
    arguments: dict[str, str],
) -> FetchResult:
    """Read evidence; retry only known transient failures and preserve lookup status."""
    delays = [2, 5]
    for attempt in range(len(delays) + 1):
        try:
            payload = await gateway.call(tool_name, case_id=case_id, **arguments)
            evidence_ref = payload["evidence_ref"]
            trace.emit(
                case_id=case_id,
                event_type="tool_result_consumed",
                actor=actor,
                tool_name=tool_name,
                evidence_refs=[evidence_ref],
            )
            return FetchResult(
                "found",
                Evidence(
                    evidence_ref=evidence_ref,
                    domain=payload["domain"],
                    data=payload.get("data"),
                ),
            )
        except MCPToolError as exc:
            if exc.kind == "not_found":
                return FetchResult("not_found")
            if exc.kind != "transient":
                return FetchResult("error")
        except (
            httpx2.TransportError,
            TimeoutError,
            ConnectionError,
            anyio.EndOfStream,
            anyio.BrokenResourceError,
        ):
            pass
        if attempt < len(delays):
            await asyncio.sleep(delays[attempt])
    return FetchResult("error")


async def order_agent(
    case_id: str,
    order_id: str,
    gateway: EvidenceGateway,
    trace: TraceWriter,
    facts: CaseFacts,
) -> None:
    task = {"order_id": order_id}
    order_fetch = await consume_evidence(
        gateway,
        trace,
        case_id=case_id,
        actor="order-agent",
        tool_name="get_order",
        arguments=task,
    )
    order = order_fetch.evidence
    if order is None:
        facts.missing.append("order")
    else:
        facts.evidence["order"] = order
        facts.order_status = (order.data or {}).get("order_status", "unknown")
        facts.customer_id = (order.data or {}).get("customer_id")

    items_fetch = await consume_evidence(
        gateway,
        trace,
        case_id=case_id,
        actor="order-agent",
        tool_name="get_order_items",
        arguments=task,
    )
    items = items_fetch.evidence
    if items is None:
        facts.missing.append("items")
    else:
        facts.evidence["items"] = items
        facts.items = items.data if isinstance(items.data, list) else []

    sellers_fetch = await consume_evidence(
        gateway,
        trace,
        case_id=case_id,
        actor="order-agent",
        tool_name="get_sellers",
        arguments=task,
    )
    sellers = sellers_fetch.evidence
    if sellers is None:
        facts.missing.append("sellers")
    else:
        facts.evidence["sellers"] = sellers
        facts.sellers = sellers.data if isinstance(sellers.data, list) else []


async def payment_agent(
    case_id: str,
    order_id: str,
    gateway: EvidenceGateway,
    trace: TraceWriter,
    facts: CaseFacts,
) -> None:
    task = {"order_id": order_id}
    payments_fetch = await consume_evidence(
        gateway,
        trace,
        case_id=case_id,
        actor="payment-agent",
        tool_name="get_order_payments",
        arguments=task,
    )
    payments = payments_fetch.evidence
    if payments is None:
        facts.missing.append("payments")
    else:
        facts.evidence["payments"] = payments
        facts.payments = payments.data if isinstance(payments.data, list) else []

    timeline_fetch = await consume_evidence(
        gateway,
        trace,
        case_id=case_id,
        actor="payment-agent",
        tool_name="get_payment_timeline",
        arguments=task,
    )
    timeline = timeline_fetch.evidence
    if timeline is None:
        facts.missing.append("payment_timeline")
    else:
        facts.evidence["payment_timeline"] = timeline
        facts.payment_events = (timeline.data or {}).get("events", [])

    refund_fetch = await consume_evidence(
        gateway,
        trace,
        case_id=case_id,
        actor="payment-agent",
        tool_name="get_refund_timeline",
        arguments=task,
    )
    facts.refund_lookup_status = refund_fetch.status
    refund = refund_fetch.evidence
    if refund_fetch.status == "not_found":
        facts.refund_events = []
    elif refund is None:
        facts.missing.append("refund")
    else:
        facts.evidence["refund"] = refund
        facts.refund_events = (refund.data or {}).get("events", [])


async def shipment_agent(
    case_id: str,
    order_id: str,
    gateway: EvidenceGateway,
    trace: TraceWriter,
    facts: CaseFacts,
) -> None:
    shipment_fetch = await consume_evidence(
        gateway,
        trace,
        case_id=case_id,
        actor="shipment-agent",
        tool_name="get_shipment_summary",
        arguments={"order_id": order_id},
    )
    shipment = shipment_fetch.evidence
    if shipment is None:
        facts.missing.append("shipment")
    else:
        facts.evidence["shipment"] = shipment
        facts.shipment = shipment.data if isinstance(shipment.data, dict) else {}


async def policy_agent(
    case_id: str,
    policy_version: str,
    gateway: EvidenceGateway,
    trace: TraceWriter,
    facts: CaseFacts,
) -> None:
    policy_fetch = await consume_evidence(
        gateway,
        trace,
        case_id=case_id,
        actor="policy-agent",
        tool_name="get_policy",
        arguments={"policy_version": policy_version},
    )
    policy = policy_fetch.evidence
    if policy is None:
        facts.missing.append("policy")
    else:
        facts.evidence["policy"] = policy
        facts.policy_rules = policy.data if isinstance(policy.data, dict) else {}


async def gather_facts(
    case: dict[str, Any],
    gateway: EvidenceGateway,
    trace: TraceWriter,
) -> CaseFacts:
    """Coordinator-driven fan-out: run each specialist and collect structured facts.

    This is where the observable A2A protocol is recorded: the coordinator emits a
    ``task_assigned`` event before each specialist and the specialist reports back with
    a ``handoff`` event once its facts are materialized.
    """
    case_id = case["case_id"]
    request = case.get("customer_request", {})
    order_id = request.get("claimed_order_id") or ""
    topic = (request.get("claims") or [{}])[0].get("topic", "")
    policy_version = case.get("policy_version", "EC_POLICY_V1")

    facts = CaseFacts(
        case_id=case_id,
        order_id=order_id,
        topic=topic,
        policy_version=policy_version,
    )

    specialists: list[tuple[str, Any]] = [
        ("order-agent", order_agent(case_id, order_id, gateway, trace, facts)),
        ("payment-agent", payment_agent(case_id, order_id, gateway, trace, facts)),
        ("shipment-agent", shipment_agent(case_id, order_id, gateway, trace, facts)),
        ("policy-agent", policy_agent(case_id, policy_version, gateway, trace, facts)),
    ]

    for actor, coroutine in specialists:
        trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target=actor,
            decision_code=f"assign-{actor}",
        )
        before = set(facts.evidence)
        await coroutine
        handoff_refs = [
            evidence.evidence_ref
            for kind, evidence in facts.evidence.items()
            if kind not in before
        ]
        trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor=actor,
            target="coordinator",
            decision_code=f"{actor}-complete",
            evidence_refs=handoff_refs,
        )

    return facts
