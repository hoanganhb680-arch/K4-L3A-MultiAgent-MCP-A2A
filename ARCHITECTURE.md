# L3A Architecture Record

Team cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

```text
Input (inputs/<case_id>.json)
  → Coordinator (extract identifiers, plan specialists)
      → Order/Item Agent  ──┐
      → Payment Agent     ──┼──→ MCP Evidence Gateway (authoritative data)
      → Shipment Agent    ──┤
      → Policy Agent      ──┘
  → Coordinator (synthesize candidate)
  → Verifier (schema + invariants)
  → Output (outputs/<case_id>.json)
Trace (traces/trace.jsonl) is written in parallel with every observable lifecycle step.
```

Luồng được triển khai tại `src/student_agent/`:

- `workflow.py` — `solve_case()` là entry point của coordinator; chạy specialist, quyết định, verify.
- `agents.py` — `consume_evidence()` (helper MCP trung tâm) và 4 specialist agents.
- `decision.py` — pure decision logic: xác định primary issue, chính sách, refund, responsibility, causes.
- `models.py` — `Evidence` và `CaseFacts` (kết quả có cấu trúc, không free-form reasoning).

Customer message KHÔNG phải ground truth. Mọi kết luận nghiệp vụ phải dựa trên evidence lấy từ MCP. Policy evidence (`get_policy`) là nguồn thẩm quyền cho status/action/refund/party.

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Output/handoff |
| --- | --- | --- | --- |
| Coordinator | raw case | extract `claimed_order_id`, `policy_version`, claim topics; lập kế hoạch specialist; tổng hợp; gọi verifier | `task_assigned` → specialist; nhận `handoff`; trả output |
| Order/Item Agent (`order-agent`) | `order_id` | `get_order`, `get_order_items`, `get_sellers` → order status, customer, items, sellers | `handoff` (order facts + evidence refs) |
| Payment Agent (`payment-agent`) | `order_id` | `get_order_payments`, `get_payment_timeline`, `get_refund_timeline` → payments, captured/refund events | `handoff` (payment facts) |
| Shipment Agent (`shipment-agent`) | `order_id` | `get_shipment_summary` → delivery timestamps, shipping limits, `delivered_late` actor | `handoff` (shipment facts) |
| Policy Agent (`policy-agent`) | `policy_version` | `get_policy` → machine-readable rules cho 10 issue | `handoff` (policy rules) |
| Verifier | candidate output + facts | kiểm tra invariants (schema, entity scope, evidence provenance, money totals) | `verification_completed` |

Tool permission / ownership:

- `order-agent`: `get_order`, `get_order_items`, `get_sellers`.
- `payment-agent`: `get_order_payments`, `get_payment_timeline`, `get_refund_timeline`.
- `shipment-agent`: `get_shipment_summary`.
- `policy-agent`: `get_policy`.
- Không agent nào được gọi tool của agent khác. `get_customer_history` (cần `customer_unique_id` không có trong input) và `get_product_context` (danh mục sản phẩm không dùng cho 10 issue) không được gọi — tránh evidence không liên quan.

## 3. A2A protocol

- Message envelope: specialist nhận task qua function signature (`case_id`, `order_id`, `policy_version`) và trả `CaseFacts` được populate. Không có network hop giữa các agent — chúng chạy trong cùng event loop.
- Correlation theo `case_id`: mọi trace event và mọi MCP call đều mang đúng `case_id` của case đang xử lý; evidence không được tái sử dụng chéo case.
- `task_assigned` (actor=`coordinator`, target=`<specialist>`) trước mỗi specialist; `handoff` (actor=`<specialist>`, target=`coordinator`) sau khi specialist hoàn thành.
- Chỉ trace sự kiện/decision code quan sát được (`task_assigned`, `handoff`, `tool_result_consumed`, `policy_decided`, `verification_completed`); không trace suy luận riêng.

## 4. case_id correlation

- `case_id` lấy từ `case["case_id"]`, truyền nguyên vẹn vào mọi `gateway.call(case_id=...)` và `trace.emit(case_id=...)`.
- `order_id` lấy từ `customer_request.claimed_order_id`; `get_order` trả về `order_id` thẩm quyền được dùng để cross-check.
- Verifier khẳng định `output.case_id == case_id` và mọi evidence ref đều đến từ MCP call của current case.

## 5. Handoff conditions

- Order agent handoff khi order status + items + sellers được materialize.
- Payment agent handoff khi payments + payment timeline + (nếu có) refund timeline được materialize; `get_refund_timeline` trả "no record" được coi là kết quả hợp lệ (không phải lỗi).
- Shipment agent handoff khi shipment summary có mặt.
- Policy agent handoff khi policy rules được fetch.
## 6. Loop prevention

- Workflow là DAG một chiều: Coordinator → specialist → Coordinator → verifier → output. Không có callback loop.
- Mỗi agent chỉ được gọi đúng một lần cho một case; retry chỉ áp dụng cho cùng một MCP call (idempotent read), không retry cả agent.
- Không có vòng lặp "hỏi lại agent" hoặc "phụ thuộc vòng".

## 7. Timeout / retry

- `connect_gateway` dùng `httpx2.Timeout(300.0, connect=30.0, ...)` cho toàn bộ request.
- `consume_evidence` retry tối đa 1 lần (tổng 2 attempt) cho tool bắt buộc; `get_refund_timeline` là optional tool được gọi 1 lần vì "no refund record" là outcome hợp lệ.
- Retry là idempotent (các MCP tool đều là read-only); không infinite loop.

## 8. Evidence lifecycle

1. `consume_evidence` gọi `gateway.call(tool_name, case_id=...)`.
2. `EvidenceGateway.call` validate response theo `mcp-evidence-response-v1.schema.json` (dùng `Contracts.validate_evidence`).
3. Trích `evidence_ref` (không tự tạo/sửa) và `data`; `evidence_ref` được lưu trong `CaseFacts.evidence`.
4. Mỗi evidence thực sự dùng đều emit `tool_result_consumed` với actor, tool_name và `evidence_refs`.
5. `build_output` chỉ đưa vào `evidence_refs` các ref đã được trả về và thực sự hỗ trợ kết luận; verifier xác nhận ref ∈ refs đã MCP trả về và đúng case.
6. Evidence không được tái sử dụng giữa các case (mọi call và trace scope theo `case_id`).

## 9. Failure policy

| Failure | Retry? | Fallback | Trace event/code |
| --- | --- | --- | --- |
| MCP timeout/connection | 1 retry (idempotent) | missing → `insufficient_evidence` nếu critical | không emit `tool_result_consumed` cho evidence bỏ |
| Not found (`get_refund_timeline`) | Không | coi là "no refund record" | không emit (không dùng) |
| Source conflict (order vs shipment status) | — | giữ cả hai; ghi `data_conflicts` | decision_code conflict |
| Invalid MCP response | Không chấp nhận im lặng | `Contracts.validate_evidence` raise; ghi missing | — |
| Invalid specialist result | Không retry agent | verifier raise trước finalize (dev bug bị lộ sớm) | — |

Retry phải có giới hạn và idempotent. Không chuyển missing evidence thành dữ liệu phỏng đoán: khi thiếu evidence quan trọng, output ưu tiên `insufficient_evidence`.

## 10. Verification invariants

Verifier (`workflow._verify`) kiểm tra trước khi finalize:

1. `case_id` đúng current case.
2. Output đúng `day09-l3a-output-v2` (CLI cũng validate lại bằng jsonschema).
3. `primary_issue` ∈ allowed enum.
4. `case_status` ∈ {action_required, no_action, needs_investigation}.
5. `confidence` ∈ [0, 1].
6. Entity IDs không bị trộn case khác (chỉ từ evidence của case).
7. Evidence refs hợp lệ (`ev_...`), không duplicate, đã được MCP trả về.
8. `sum(refund_lines.amount_brl) == recommended_refund_brl`.
9. `currency == BRL`.
10. Responsible party type hợp lệ; `party_id` chỉ điền khi party_type == seller (lấy seller id thật từ evidence).
11. `resolution_actions` không duplicate.
12. Nếu thiếu evidence quan trọng → `insufficient_evidence` + confidence thấp.

## 11. Reproducibility

- Python >= 3.11; dependencies pin trong `pyproject.toml` (`httpx2`, `jsonschema`, `mcp`, `python-dotenv`, dev: `pytest`, `ruff`).
- Không có random seed trong decision (deterministic); thứ tự xử lý theo `case_set.case_ids`.
- Concurrency: chạy tuần tự từng case trong 1 MCP session (một "run" = một session; tránh tách session làm vỡ provenance evidence theo run).
- Các lệnh: `python -m pip install -e ".[dev]"`; `day09 validate-inputs`; `day09 mcp-tools`; `day09 run`; `day09 validate`; `day09 package --output dist/submission.zip`.
- Không lưu API key, input, secret hoặc debug log vào submission ZIP.