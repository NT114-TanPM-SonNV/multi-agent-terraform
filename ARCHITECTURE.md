# Framework Architecture

Pipeline sinh Terraform HCL từ ngôn ngữ tự nhiên, orchestrate bằng **LangGraph StateGraph**.

---

## Tổng quan

```
User prompt
    │
    ▼
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│ A1          │────►│ A2          │────►│ A3          │
│ Architecture│     │ Security    │     │ Engineering │
│ Plan JSON   │     │ CKV IDs     │     │ HCL code    │
└─────────────┘     └─────────────┘     └─────────────┘
                                               │
                          ┌────────────────────┘
                          ▼
                    ┌─────────────┐     ┌─────────────┐
                    │ A4          │────►│ A5          │
                    │ Validation  │     │ Deployment  │
                    │ tf+Checkov  │     │ apply+destroy│
                    └─────────────┘     └─────────────┘
```

Mỗi agent là một LangGraph **node**. Node đọc từ `AgentState`, xử lý, ghi kết quả vào state,
rồi LangGraph dùng **conditional edges** để quyết định node tiếp theo.

---

## State — dữ liệu chảy qua toàn pipeline

`AgentState` (TypedDict trong `core/state.py`) là shared memory duy nhất:

```
prompt                  → input gốc của user (không đổi)
infrastructure_plan     → A1 output: {"resources": [...], "data_sources": [...]}
security_profile        → A2 output: {"type.name": {"type": ..., "checks": [CKV IDs]}}
generated_code          → A3 output: HCL string
fix_feedback            → A4/A5 output: {overall_passed, error_type, fix_instruction, ...}
deployment_result       → A5 output: {success, resources_created, ...}
retries                 → {"arch": {count, ...}, "eng": {count, ...}, "sec": {count, ...}, "deploy": {count, ...}}
total_attempts          → global backstop counter
routing_log             → audit trail mỗi lần fail/retry
eng_error_history       → 2 fix gần nhất để chống oscillation A3
```

---

## Các agent

### A1 — Architecture (`agents/architecture.py`)

**Input:** `state["prompt"]`, optional `fix_feedback` (khi retry)

**Việc làm:**
- Gọi LLM với `prompts/architecture.py` → sinh JSON plan
- Plan liệt kê từng AWS resource: `type`, `name`, `attributes`, `blocks`
- Validate plan structure (thiếu type/name, trùng key → re-prompt in-node 1 lần)
- Khi retry (MISSING_RESOURCE): inject fix_instruction vào prompt để re-plan có chủ đích

**Output:** `state["infrastructure_plan"]`

**Route sau A1:**
```
error_type == INFRASTRUCTURE → requires_human  (LLM fail hoàn toàn)
else                         → security         (edge tĩnh)
```

---

### A2 — Security (`agents/security.py`)

**Input:** `state["prompt"]`, `state["infrastructure_plan"]`

**Việc làm:**
- Nạp `catalog.json` (sinh bởi `build_catalog.py` từ Checkov registry)
- Với mỗi resource trong plan, dựng **menu** các CKV check IDs áp dụng được (nhóm theo category: ENCRYPTION, IAM, NETWORKING, GENERAL_SECURITY, APPLICATION_SECURITY, SECRETS)
- Gọi LLM với menu đó → LLM chọn IDs phù hợp theo intent
- Validate: drop bất kỳ ID không có trong menu (hallucinate)

**Tại sao dùng menu?**
LLM không reliable khi tự nhớ CKV IDs. Menu giới hạn selection về tập IDs thực sự
áp dụng cho đúng resource type → không mis-target, không hallucinate.

**Output:** `state["security_profile"]`
```json
{
  "aws_s3_bucket.main": {"type": "aws_s3_bucket", "checks": ["CKV_AWS_19", "CKV2_AWS_6"]},
  "aws_route53_record.dns": {"type": "aws_route53_record", "checks": []}
}
```

**Route sau A2:** edge tĩnh → engineering (A2 fail không dừng pipeline)

---

### A3 — Engineering (`agents/engineering.py`)

**Input:** `state["infrastructure_plan"]`, `state["security_profile"]`, optional `fix_feedback`

**Việc làm:**
- Render security context: mỗi resource + CKV IDs kèm tên check
  ```
  aws_s3_bucket.main:
    - CKV_AWS_19: Ensure all data stored in the S3 bucket is securely encrypted at rest
    - CKV2_AWS_6: Ensure that S3 bucket has a Public Access block
  ```
- Gọi LLM với `prompts/engineering.py` → sinh HCL đầy đủ (terraform{}, provider{}, resource{})
- Clean output: strip ANSI, markdown fence, preamble text
- Validate có ít nhất 1 `resource "` block (retry in-node 1 lần nếu không)
- Khi retry (SYNTAX/LOGIC/SECURITY từ A4): incremental patch — gửi code cũ + fix instruction

**Output:** `state["generated_code"]`

**Route sau A3:**
```
fix_feedback rỗng (success) → validation
fix_feedback.error_type != None → requires_human
```

---

### A4 — Validation (`agents/validation.py`)

**Input:** `state["generated_code"]`, `state["security_profile"]`

**Việc làm — 4 bước theo thứ tự:**

**Bước 1 — terraform init**
Tải AWS provider plugin. Lock file cũ tự động xóa trước mỗi init (tránh version conflict).

**Bước 2 — terraform validate**
Static syntax check. Lỗi → classify SYNTAX → gửi fix về A3.

**Bước 3 — terraform plan -out=tfplan.out**
Logical check (AWS API). Lỗi:
- Network/auth pattern → INFRASTRUCTURE → requires_human
- "not found" pattern → MISSING_RESOURCE → A1 re-plan
- Khác → LLM classify LOGIC → A3 fix

**Bước 4 — Checkov security gate**
```
terraform show -json tfplan.out → plan.json
checkov -f plan.json --framework terraform_plan --output json
         --check CKV_AWS_19,CKV2_AWS_6,...
```
- Scan trên plan JSON (chính xác hơn source scan: resolved computed values, graph checks đầy đủ)
- Fallback về source scan nếu plan JSON không khả dụng
- So unmet = checks fail ∩ checks A2 đã target cho resource đó
- Phantom = checks targeted nhưng Checkov không trigger (companion thiếu)
- Fail + còn budget → SECURITY → A3 fix (max 2 lần, `retries["sec"]`)
- Fail + hết budget → best-effort accept, ghi `unmet_checks`

**Output:** `state["fix_feedback"]`

**Route sau A4:**
```
overall_passed=True             → deployment
total_attempts >= 5             → requires_human  (global backstop)
error_type == INFRASTRUCTURE    → requires_human
oscillation detected            → requires_human
root_cause == architecture      → A1  (nếu còn budget arch)
root_cause == engineering       → A3  (nếu còn budget eng)
```

---

### A5 — Deployment (`agents/deployment.py`)

**Input:** `state["generated_code"]`

**Việc làm:**
- `terraform apply` thật trên AWS
- Nếu `auto_destroy=True` (eval mode): `terraform destroy` ngay sau apply thành công
- Fail → classify lỗi:
  - TRANSIENT (network/throttle) → retry A5 (max 2 lần)
  - FIXABLE/LOGIC (code sai) → A3 fix
  - MISSING_RESOURCE → A1 re-plan
  - UNKNOWN / hết budget → requires_human

**Output:** `state["deployment_result"]`

---

## Retry & error handling

```
Mỗi loại lỗi có budget riêng trong retries dict:

retries["eng"]    max 3  — SYNTAX / LOGIC / SECURITY → A3
retries["arch"]   max 2  — MISSING_RESOURCE → A1
retries["sec"]    max 2  — Checkov fail → A3 (riêng, không ăn budget eng)
retries["deploy"] max 2  — TRANSIENT → A5

total_attempts    max 5  — global backstop tuyệt đối

Oscillation detection: nếu error signature giống nhau 3 lần liên tiếp → requires_human
(A3 sửa nhưng vẫn fail y chang → không tiếp tục loop)
```

---

## Catalog & Checkov

```
build_catalog.py
    ├── resource_registry (Python)  → CKV_AWS_*  single-resource checks
    └── graph_checks/aws/*.yaml     → CKV2_AWS_* graph checks (companion resource)
    → core/catalog.json
         {resource_type: [{id, name, cat: [...], connected_types: [...]}]}

A2 dùng catalog để:  dựng menu per resource → LLM chọn IDs
A3 dùng catalog để:  tra tên check → render context cho LLM
A4 dùng catalog để:  tra tên check → render fix_instruction cho A3
```

---

## Files liên quan

```
graph.py          → topology, routing functions, build_graph()
core/state.py     → AgentState TypedDict
core/catalog.json → Checkov check catalog (sinh bởi build_catalog.py)
agents/           → logic từng agent
prompts/          → system/user prompt từng agent
core/terraform.py → wrappers: run_terraform, terraform_workdir,
                    run_checkov_on_hcl, run_checkov_on_plan
core/retry_control.py → increment_retry, check_retry_budget, detect_oscillation
```

---

## Chạy nhanh

```powershell
# Compact output
python run.py "Create an S3 bucket with versioning"

# Step-by-step trace
python trace.py "Create an RDS PostgreSQL instance" --no-deploy

# Trace case từ dataset
python trace.py --csv dataset/data-dev.csv --cases 33 --no-deploy
```
