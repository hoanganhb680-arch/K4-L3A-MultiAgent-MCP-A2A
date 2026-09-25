# L3A Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

Luồng dữ liệu từ `inputs/<case_id>.json` → MCP calls → specialist agents → verifier → output + trace:

```text
inputs/<case_id>.json
        │
        ▼
  [Coordinator]
  • emit case_received
  • parse claimed_order_id, claims[]
  • emit task_assigned → specialists
        │
        ├──────────────────┬────────────────┬──────────────┐
        ▼                  ▼                ▼              ▼
  [OrderAgent]      [PaymentAgent]   [ShipmentAgent]  [PolicyAgent]
  get_order         get_payment      get_shipment     get_policy
  get_item          (list payments)  (tracking)       (EC_POLICY_V1)
        │                  │                │              │
        │   emit tool_result_consumed       │              │
        └──────────────────┴────────────────┴──────────────┘
                           │ emit handoff → verifier
                           ▼
                    [VerifierAgent]
                    • cross-check tất cả evidence
                    • phát hiện data_conflicts
                    • tính financial_resolution
                    • emit verification_completed
                    • build output dict (schema v2)
                           │
                           ▼
              [Coordinator] emit case_finalized
                           │
                           ▼
        outputs/<case_id>.json  +  traces/trace.jsonl
```

## 2. Agent ownership

| Actor              | Input                                                                | Trách nhiệm                                                    | Tool được phép        | Output/handoff                                                 |
| ------------------ | -------------------------------------------------------------------- | -------------------------------------------------------------- | --------------------- | -------------------------------------------------------------- |
| **coordinator**    | case dict (case\_id, claimed\_order\_id, claims\[], policy\_version) | Parse input, chia task, tổng hợp kết quả cuối                  | list\_tools           | Phân công task → specialists; nhận evidence summary → verifier |
| **order-agent**    | case\_id, order\_id                                                  | Lấy chi tiết đơn hàng và item                                  | get\_order, get\_item | {status, order\_data, item\_data, evidence\_refs}              |
| **payment-agent**  | case\_id, order\_id                                                  | Lấy thông tin thanh toán                                       | get\_payment          | {status, payment\_data, evidence\_refs}                        |
| **shipment-agent** | case\_id, order\_id                                                  | Lấy thông tin giao hàng và tracking                            | get\_shipment         | {status, shipment\_data, evidence\_refs}                       |
| **policy-agent**   | case\_id, policy\_version                                            | Tra cứu chính sách áp dụng                                     | get\_policy           | {status, policy\_data, applicable\_rules, evidence\_refs}      |
| **verifier**       | Toàn bộ evidence từ 4 agent trên                                     | Cross-check, phát hiện conflict, tính refund, quyết định final | (không gọi thêm MCP)  | Output dict theo l3a-output-v2.schema.json                     |

> Nguyên tắc phân quyền tool:

## 3. A2A protocol

### Message envelope

Agents giao tiếp qua Python async/await trong một process duy nhất. Không có message queue hay HTTP giữa agents. Mỗi specialist trả về:

```python
{
    "status": "ok" | "not_found" | "error",
    "domain": "order" | "payment" | "shipment" | "policy",
    "evidence_refs": ["ev_xxx", ...],
    "data": { ... }
}
```

### Correlation

- Mọi agent đều nhận case\_id làm tham số đầu tiên, truyền vào mọi gateway.call() và trace.emit().
- evidence\_ref từ MCP response phải giữ nguyên — không sửa hay tạo mới.
- Không dùng evidence của case này sang case khác (hard gate → 0 điểm).

### Handoff ordering

```javascript
coordinator   → case_received
coordinator   → task_assigned    → order-agent
coordinator   → task_assigned    → payment-agent
coordinator   → task_assigned    → shipment-agent
coordinator   → task_assigned    → policy-agent
(sequential hoặc concurrent)
order-agent   → handoff          → verifier
payment-agent → handoff          → verifier
shipment-agent→ handoff          → verifier
policy-agent  → handoff          → verifier
verifier      → verification_completed
coordinator   → case_finalized
```

### Required trace events (workflow score)

| Event type              | Actor          | Ghi chú                                                 |
| ----------------------- | -------------- | ------------------------------------------------------- |
| case\_received          | coordinator    | Đầu tiên khi nhận case                                  |
| task\_assigned          | coordinator    | Một event per specialist, target = tên agent            |
| tool\_result\_consumed  | tên specialist | Một event per MCP call, kèm tool\_name + evidence\_refs |
| handoff                 | specialist     | Sau khi xong, chuyển → verifier                         |
| verification\_completed | verifier       | Sau khi hoàn tất kiểm tra                               |
| case\_finalized         | coordinator    | Event cuối cùng                                         |

## 4. Evidence lifecycle

### Thu thập

```python
evidence = await gateway.call("get_order", case_id=case_id, order_id=order_id)
evidence_ref = evidence["evidence_ref"]   # pattern: ^ev_[A-Za-z0-9_-]{20,96}$
order_data   = evidence["data"]
```

### Validate & lưu

gateway.call() tự validate response theo mcp-evidence-response-v1.schema.json. Nếu invalid → raise ValueError → specialist báo lỗi status: error.

### Emit trace

```python
trace.emit(
    case_id=case_id,
    event_type="tool_result_consumed",
    actor="order-agent",
    tool_name="get_order",
    evidence_refs=[evidence_ref],
)
```

### Map evidence → output

- Tất cả evidence\_ref dùng để đưa ra kết luận phải xuất hiện trong output\["evidence\_refs"].
- Mỗi claim\_assessment phải liên kết evidence\_refs trực tiếp hỗ trợ verdict.
- Chỉ trích dẫn evidence thật sự hỗ trợ kết luận — không cite thừa (precision bị penalize).

## 5. Failure policy

| Failure                                 | Retry? | Fallback                                                                                 | Trace event / decision\_code                       |
| --------------------------------------- | ------ | ---------------------------------------------------------------------------------------- | -------------------------------------------------- |
| MCP timeout (>30s)                      | Không  | Đánh dấu domain missing, tiếp tục với evidence còn lại                                   | handoff với attributes.error="timeout"             |
| Not found (empty data)                  | Không  | status: not\_found; verifier map sang insufficient\_evidence nếu thiếu evidence bắt buộc | handoff với decision\_code="not\_found"            |
| Source conflict (2 tool mâu thuẫn)      | Không  | Verifier ghi vào data\_conflicts\[], chọn source theo domain ưu tiên                     | verification\_completed với attributes.conflicts=N |
| Invalid specialist result (schema fail) | Không  | Coordinator log lỗi, verifier xử lý như missing                                          | handoff với decision\_code="invalid\_response"     |



> Tuyệt đối không convert missing evidence thành dữ liệu phỏng đoán hay tự tạo evidence\_ref.

## 6. Verification invariants

Verifier kiểm tra toàn bộ các điều kiện dưới đây trước khi emit case\_finalized:

| # | Invariant                                                                             | Hành động nếu vi phạm                                |
| - | ------------------------------------------------------------------------------------- | ---------------------------------------------------- |
| 1 | case\_id trong output khớp với input                                                  | Raise error — không finalize                         |
| 2 | Tất cả evidence\_refs trong output tồn tại trong danh sách đã thu thập                | Loại ref không hợp lệ                                |
| 3 | Không có evidence\_ref từ case khác                                                   | Raise error — không finalize (cross-scope hard gate) |
| 4 | financial\_resolution.recommended\_refund\_brl = tổng refund\_lines\[].amount\_brl    | Tính lại tổng                                        |
| 5 | case\_status == action\_required khi có action thực tế hoặc refund > 0                | Log warning, điều chỉnh status                       |
| 6 | confidence thuộc \[0.0, 1.0] và calibrated (không để mặc định 1.0 khi evidence thiếu) | Clamp về 0.5 nếu evidence thiếu                      |
| 7 | Mỗi claim\_id trong input có đúng một claim\_assessment trong output                  | Tạo assessment mặc định insufficient\_evidence       |
| 8 | primary\_issue thuộc enum hợp lệ theo schema                                          | Map sang unsupported\_claim nếu không xác định được  |
| 9 | resolution\_actions không trùng nhau (uniqueItems)                                    | Deduplicate trước khi finalize                       |

## 7. Reproducibility

### Model & config

- Không sử dụng external LLM — toàn bộ logic là deterministic rule-based dựa trên MCP evidence.
- Quyết định hoàn toàn từ data trả về từ MCP Gateway, không đoán mò.

### Dependencies

- Python 3.11+
- Dependency pinning: xem pyproject.toml
- MCP client: mcp package, HTTP client: httpx2

### Lệnh chạy

```bash
python3.11 -m venv .venv
.venv\Scripts\activate         # Windows
python -m pip install -e ".[dev]"

cp .env.example .env
# Điền: COMPETITION_TEAM_API_KEY, MCP_ENDPOINT, COMPETITION_API_URL

day09 validate-inputs
day09 run
day09 validate
day09 package --output dist/submission.zip
```

### Giới hạn tài nguyên

- Chạy sequential case by case (không concurrent để tránh cross-scope evidence).
- Timeout mỗi MCP call: 30s (cấu hình tại connect\_gateway).
- Không dùng random seed — không có thành phần probabilistic.
