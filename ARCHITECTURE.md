# L3A Architecture Record

## 1. System overview

```text
case input
  → coordinator
  → order-agent ── get_order / get_order_items / get_sellers
  → payment-agent ── get_payment_timeline [/ get_refund_timeline]
  → shipment-agent ── get_shipment_summary
  → policy-agent ── get_policy
  → verifier → schema-valid output

Every authoritative MCP response → tool_result_consumed trace event
```

The coordinator treats customer claims as requests to verify, never as facts. It passes the exact input `case_id` to every gateway call.

## 2. Agent ownership

| Actor | Allowed MCP tools | Responsibility | Handoff |
| --- | --- | --- | --- |
| Coordinator | none | Route the case and assemble verified specialist findings. | Assigns order, payment, shipment, and policy tasks. |
| order-agent | `get_order`, `get_order_items`, `get_sellers` | Establish order status and affected order/item/seller IDs. | `ORDER_CONTEXT_READY` to payment. |
| payment-agent | `get_payment_timeline`, `get_refund_timeline` | Establish captured payments, mismatch, and refund state. | `EVIDENCE_READY_FOR_POLICY` to policy. |
| shipment-agent | `get_shipment_summary` | Identify a confirmed late delivery and its actor. | Evidence available to policy. |
| policy-agent | `get_policy` | Select the authoritative rule for the evidence-backed primary issue: status, responsibility, refund and action. | `policy_decided`. |
| verifier | none | Enforce policy/output consistency, evidence linkage, money totals, lifecycle coverage, and calibrated confidence. | `verification_completed`. |

## 3. A2A protocol

The implicit envelope is `{case_id, actor, target, decision_code, evidence_refs}`. The same `case_id` is used for all calls and trace events. Handoffs are one-way in a fixed sequence, so there is no loop. The workflow is sequential and uses the MCP client's bounded connection/read timeouts; an unrecoverable required lookup fails the case instead of fabricating a result.

## 4. Evidence lifecycle

`EvidenceGateway.call` validates every server response against the public evidence schema. The workflow keeps `evidence_ref` exactly as returned, associates it with the tool and case in memory only, and immediately emits `tool_result_consumed` using that unchanged reference. Output and claim references are selected only from this per-case evidence map. Refs are never constructed, edited, persisted across cases, or copied from inputs.

## 5. Failure policy

| Failure | Retry? | Fallback | Trace/result behavior |
| --- | --- | --- | --- |
| Required MCP error or timeout | No automatic retry | Stop case; do not infer missing fact. | Exception reaches runner. |
| No refund record | No | Treat refund-specific claim as unsupported unless other authoritative evidence proves it. | No invented evidence ref. |
| Source conflict | No | Use the issue rule requiring the relevant authoritative domain; output no synthetic conflict. | `policy_decided` after policy mapping. |
| Invalid MCP payload | No | Stop case. | Contract validation rejects it. |

## 6. Verification invariants

- Each output ref was received through the gateway in the same `case_id` and has a corresponding `tool_result_consumed` event.
- Primary issue must be supported by order/payment/shipment/refund evidence and have a policy rule.
- Refund currency is BRL; refund line total equals `recommended_refund_brl`.
- Responsible parties, actions, status, and refund amount come from the selected policy rule.
- The verifier requires task assignment, evidence consumption, handoff, policy decision, and verification lifecycle events before output finalization.`n- Confidence is calibrated from coverage of issue evidence plus policy evidence, reduced for conflicts, and capped below `1.0`.

## 7. Reproducibility

The workflow is deterministic and has no model sampling or random decision path. It uses the pinned project dependencies and processes one case at a time. Run with `python -m student_agent.cli run`, then `python -m student_agent.cli validate`.