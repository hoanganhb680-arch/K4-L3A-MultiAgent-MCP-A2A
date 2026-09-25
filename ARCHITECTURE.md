# L3A architecture

## Execution and ownership

`day09 run` validates the input set, opens one authenticated MCP session, and processes
cases in input order. `solve_case` coordinates four in-process specialists, builds a
candidate output, and calls the verifier before the CLI writes the JSON. The order
specialist reads order, items, and sellers; the payment specialist reads payments,
payment timeline, and optional refund timeline; the shipment specialist reads the
shipment summary; the policy specialist reads the requested policy version. These are
real MCP calls, not locally synthesized evidence.

The coordinator emits `task_assigned` and each specialist emits `handoff`. Every
successfully parsed evidence response produces a `tool_result_consumed` event with its
unmodified MCP ref. The CLI emits `case_received` and `case_finalized`; the workflow
emits `policy_decided` and `verification_completed`. All events carry the input
`case_id`; no private reasoning is included. A fetched result may be used to rule out
an issue even if its ref is absent from the final issue-specific citation list.

## Decision rules

Customer claims identify what to assess; they never select the primary issue. The
classifier prioritizes paid canceled/unavailable orders, corroborated duplicate
captures, confirmed late delivery actor, failed/pending refund timeline, payment
reconciliation, and then valid split payment. If none applies, it returns
`unsupported_claim`. Missing critical evidence or a missing policy rule downgrades the
output to `insufficient_evidence`, `needs_investigation`, and zero refund.

Captured payment means a `captured` timeline event with `confirmed` status and positive
amount. A repeated payment amount or type alone does not establish a duplicate. The
duplicate check accepts an explicit marker, repeated capture identifier, or a whole
repeated multi-method payment batch corroborated by the captured timeline. Refund
`failed` and `pending` come only from refund timeline events. Late seller/logistics
classification requires a confirmed `delivered_late` shipment event with an allowed
actor; unknown actors do not produce a dynamic enum value.

For reconciliation, the expected amount is the sum of distinct MCP item prices and
freight; the paid amount is the sum of MCP payment rows. Repeated item IDs with
conflicting amounts make the expected amount indeterminate and are recorded as a data
conflict. Calculations use `Decimal`. Because
the observed policy has no tolerance field, a deterministic BRL tolerance of 0.01 is
used. A split requires at least two payment rows, matching total within this
tolerance, successful capture, and no higher-priority failure evidence. The system
does not infer a canonical transaction ID from payment sequence or amount.

## Policy, money, and parties

The MCP policy supplies action, case status, and responsible party type. The policy
action code appears in `resolution_actions` alongside readable follow-up steps. There is no
hard-coded financial policy fallback. For paid canceled/unavailable orders the
recommended refund uses the confirmed captured amount. For a failed refund it uses
the failed request amount. An explicit duplicate event can supply the duplicate
amount. Other issues use the policy's `refund_brl` when no finer transaction amount
is established. Refund lines sum to the recommended total. Seller party IDs are read
from seller/item evidence; platform and provider IDs remain null when MCP does not
provide one. If an issue's required evidence is missing, no positive financial
recommendation is issued.

## Evidence and entities

Final evidence refs are selected by issue from the MCP responses for the current
case. Claim evidence refs are a subset of final refs. The verifier rejects unknown or
cross-issue claim refs, duplicate refs, duplicate actions, malformed IDs, incorrect
refund totals, and inconsistent issue/status combinations. The CLI also validates
every output and trace event against the public JSON schemas.

Entity extraction takes order/item/seller/payment/shipment IDs from their actual MCP
fields, converts present values to strings, removes duplicates, and respects schema
limits. The claimed order ID is not emitted as an affected entity without evidence.
No order ID is reused as a payment or shipment identifier. If MCP supplies no
payment reference or shipment ID, those lists stay empty.

`data_conflicts` records disagreement between order status and the shipment summary's
order status (selecting order), or conflicting amounts for the same item ID (selecting
neither row). Payment reconciliation
differences are classified as `payment_mismatch`; they are not also recorded as a
source conflict. No conflict entry is created merely to populate the field.

## Failure behavior and reproducibility

Mandatory MCP reads have bounded retries with waits of 5, 10, 20, 40, and 80 seconds.
`get_refund_timeline` is optional and is called once because no refund record is a
valid outcome. An exhausted read is marked missing; its ref is never fabricated.
The current gateway may return a generic tool error with no detail for both an absent
refund and temporary service failure, so absence of a refund record cannot be
distinguished from those failures by the available response alone.

Run locally with the repository virtual environment:

```powershell
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\pytest.exe -q
.\.venv\Scripts\day09.exe validate-inputs
.\.venv\Scripts\day09.exe mcp-tools
.\.venv\Scripts\day09.exe run
.\.venv\Scripts\day09.exe validate
.\.venv\Scripts\day09.exe package --output dist/submission.zip
```

The input, output, trace, environment, and ZIP artifacts are ignored by Git. Only
source, tests, and this architecture record belong in the commit.
