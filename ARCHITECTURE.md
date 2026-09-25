# L3A architecture

## Execution and ownership

`day09 run` validates the input set, opens one authenticated MCP session, and processes
cases in input order. `solve_case` coordinates four in-process specialists, builds a
candidate output, and calls the verifier before the CLI writes the JSON. The order
specialist reads order, items, and sellers; the payment specialist reads payments,
payment timeline, and optional refund timeline; the shipment specialist reads the
shipment summary; the policy specialist reads the requested policy version. These are
real MCP calls, not locally synthesized evidence.

The CLI writes each case into a temporary run directory. It replaces `outputs/` and
`traces/trace.jsonl` only after the full run completes. Three consecutive cases with
no citable evidence abort the run as a likely gateway outage; previous artifacts
remain in place. A local `.day09-run.json` records the source/input digest, artifact
digest, team key fingerprint and completion time. Validation and packaging reject
artifacts when this metadata is absent or no longer matches. The metadata file is
ignored by Git and excluded from the submission ZIP. `scripts/audit_run.py` performs
extra artifact and trace checks after a completed run.

The coordinator emits `task_assigned` and each specialist emits `handoff`. Every
successfully parsed evidence response produces a `tool_result_consumed` event with its
unmodified MCP ref. The CLI emits `case_received` and `case_finalized`; the workflow
emits `policy_decided` and `verification_completed`. All events carry the input
`case_id`; no private reasoning is included. Handoff events include the evidence refs
fetched by that specialist. If policy is unavailable, `policy_decided` uses the
`POLICY_UNAVAILABLE` code with no policy ref. A fetched result may be used to rule out
an issue even if its ref is absent from the final citation list.

## Decision rules

Customer claims identify what to assess; they never select the primary issue. The
classifier prioritizes paid canceled/unavailable orders, corroborated duplicate
captures, confirmed late delivery actor, failed/pending refund timeline, payment
reconciliation, and then valid split payment. If none applies, it returns
`unsupported_claim` only when every claimed domain has enough evidence to reject its
claim. A failed relevant tool read, indeterminate late actor, inconsistent captured
total, or missing policy rule downgrades the output to `insufficient_evidence`,
`needs_investigation`, and zero refund.

Captured payment means a `captured` timeline event with `confirmed` status and positive
amount. A repeated payment amount or type alone does not establish a duplicate. The
duplicate check accepts an explicit marker, repeated capture identifier, or a whole
repeated multi-method payment batch corroborated by the captured timeline and a known
excess over the order obligation. Refund `failed` and `pending` come only from refund
timeline events. Late seller/logistics
classification requires a confirmed `delivered_late` shipment event with an allowed
actor; unknown actors do not produce a dynamic enum value.

For reconciliation, the expected amount is the sum of distinct MCP item prices and
freight; the paid amount is the sum of MCP payment rows. Repeated item IDs with
conflicting amounts make the expected amount indeterminate and are recorded as a data
conflict. Equivalent amounts such as `79` and `79.00` compare equal. Calculations use
`Decimal`. Because the observed policy has no tolerance field, a BRL tolerance of 0.01 is
used. A split requires at least two payment rows, matching total within this
tolerance, a matching confirmed captured total, and no higher-priority failure
evidence. The system does not infer a canonical transaction ID from payment sequence
or amount.

## Policy, money, and parties

The MCP policy supplies action, case status, and responsible party type. Its action
code appears in `resolution_actions`. The
observed `rules` object is keyed by issue; malformed or incomplete rules are rejected.
There is no hard-coded financial policy fallback. For paid canceled/unavailable orders,
the recommended refund uses the confirmed captured amount. For a failed refund it uses
the failed request amount. An explicit duplicate event can supply the duplicate
amount. Other issues use the policy's `refund_brl` when no finer transaction amount
is established. Seller-late freight uses the affected item freight when the handoff
and shipping limit identify it. A computed payment overage determines the mismatch
refund when item and payment totals are reliable. Refund lines sum to the recommended
total. Seller party IDs are read from the affected seller IDs in item evidence;
cases without an identifiable affected seller become `insufficient_evidence`.
Platform and provider IDs remain null when MCP does not
provide one. If an issue's required evidence is missing, no positive financial
recommendation is issued.

## Evidence and entities

Final evidence refs are selected for the issue and each assessed claim from MCP
responses for the current case. Up to five claims are assessed by topic, with claim
refs limited to their relevant domains and to the final ref set. The verifier rejects unknown or
cross-issue claim refs, duplicate refs, duplicate actions, malformed IDs, incorrect
refund totals, and inconsistent issue/status combinations. The CLI also validates
every output and trace event against the public JSON schemas.

Entity extraction takes order/item/seller/payment/shipment IDs from their actual MCP
fields, converts present values to strings, removes duplicates, and respects schema
limits, including 20 IDs per set. Output entities are scoped to the issue: affected
item and seller IDs appear for unavailable orders and seller late handoff; payment
references appear for payment issues; shipment IDs appear for late deliveries. The
claimed order ID is not emitted as an affected entity without evidence.
No order ID is reused as a payment or shipment identifier. If MCP supplies no
payment reference or shipment ID, those lists stay empty.

`data_conflicts` records disagreement between order status and the shipment summary's
order status (selecting order), or conflicting amounts for the same item ID (selecting
neither row). Payment reconciliation
differences are classified as `payment_mismatch`; they are not also recorded as a
source conflict. No conflict entry is created merely to populate the field.

## Failure behavior and reproducibility

All MCP reads use an explicit result status: `found`, `not_found`, or `error`.
Only explicit MCP 404/not-found metadata establishes `not_found`. Timeout, transport,
or explicit transient server errors get at most two retries, after 2 and 5 seconds.
Generic tool errors are not retried or treated as absence. Parser mistakes and
schema-invalid responses propagate as errors. A failed refund read leaves the refund
state unknown and marks that domain missing; no refund timeline is invented. The
current gateway sometimes returns only `Error executing tool get_refund_timeline`
with no code, so that response cannot establish whether no record exists.

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
