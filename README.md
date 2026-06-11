# 🏗️ A Multi-Agent Framework for Secure and Deployable AWS Terraform Code Generation
## 📑 Mục lục
1. [🏗 Architecture]()
1. [⚙️ Yêu cầu & Setup](#️-yêu-cầu--setup)
2. [🚀 Chạy nhanh](#-chạy-nhanh)
3. [🧱 Kiến trúc pipeline](#-kiến-trúc-pipeline)
4. [🔁 Retry & Error handling](#-retry--error-handling)
5. [🧭 Nguyên tắc thiết kế prompt (P1–P7)](#-nguyên-tắc-thiết-kế-prompt-p1p7)
6. [📊 Metric đánh giá](#-metric-đánh-giá)
7. [⚖️ So sánh Baseline vs Pipeline](#️-so-sánh-baseline-vs-pipeline)
8. [📁 Cấu trúc thư mục](#-cấu-trúc-thư-mục)

---

## 🏗 Architecture
<img width="1678" height="1000" alt="Screenshot 2026-06-11 220258" src="https://github.com/user-attachments/assets/92d413f3-a7c8-4352-b877-0d37c37eabf9" />




## ⚙️ Yêu cầu & Setup

### Công cụ cần cài

| Công cụ | Bắt buộc cho | Cách cài |
|---------|--------------|---------|
| Python ≥ 3.11 | pipeline | [python.org](https://www.python.org/downloads/) |
| `terraform` ≥ 1.5 | validate / plan / apply | [terraform.io/install](https://developer.hashicorp.com/terraform/install) |
| `checkov` | security gate | `pip install checkov` (trong requirements.txt) |
| `opa` ≥ 1.0 | chấm Rego (`score.py --rego`) | [GitHub Releases](https://github.com/open-policy-agent/opa/releases) |
| AWS credentials | terraform / Rego eval | `aws configure` hoặc `.env` |

```powershell
# Kiểm tra sau khi cài
terraform version ; checkov --version ; opa version
```

### Cài đặt

```powershell
# 1. Virtual environment
python -m venv venv
.\venv\Scripts\Activate.ps1            # prompt hiện (venv)

# 2. Dependencies
python -m pip install --upgrade pip setuptools wheel
pip install -r requirements.txt

# 3. Cấu hình
Copy-Item .env.example .env
notepad .env                           # điền DEEPSEEK_API_KEY + AWS credentials

# 4. Provider cache offline (init pipeline dùng -plugin-dir, KHÔNG tự tải)
python scripts/populate_provider_cache.py
```

### `.env` tối thiểu

```ini
# LLM
LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=sk-your-key-here
DEEPSEEK_MODEL=deepseek-v4-pro         # v4-pro hoặc v4-flash
LLM_TIMEOUT=300                        # v4-pro cần 300s+

# AWS
AWS_ACCESS_KEY_ID=your-access-key
AWS_SECRET_ACCESS_KEY=your-secret-key
AWS_DEFAULT_REGION=us-east-1

# Terraform (1 nguồn duy nhất — baseline & pipeline dùng chung)
TF_PLAN_TIMEOUT=300
```

---

## 🚀 Chạy nhanh

```powershell
# Một prompt
python graph.py "Create an S3 bucket with versioning and SSE enabled."

# Trace — xem pipeline từng bước
python trace.py "Create an RDS PostgreSQL instance"
python trace.py --csv dataset/data-dev.csv --cases 33 --no-deploy

# Batch eval (no-deploy = nhanh, không tốn AWS)
python evaluate.py --csv dataset/data-dev.csv --no-deploy --out results.json --workers 5
```

### Flags (`evaluate.py` & `trace.py` dùng chung)

| Flag | Mô tả |
|------|-------|
| `--no-deploy` | Dừng sau A4 validation (không tốn AWS) |
| `--no-secu` | Ablation: bỏ Security agent (A2) |
| `--no-destroy` | Giữ resource sau apply |
| `--workers N` | Chạy N row song song |
| `--cases 0 3 7-10` | Chọn row cụ thể từ CSV |
| `--csv FILE` | Dataset (mặc định data-dev.csv) |
| `--out FILE` | Lưu kết quả JSON |
| `--plan-timeout N` | Terraform plan timeout (mặc định lấy TF_PLAN_TIMEOUT) |

---

## 🧱 Kiến trúc pipeline

```
User prompt
    │
    ▼
┌─────────────┐   ┌─────────────┐   ┌─────────────┐   ┌─────────────┐   ┌─────────────┐
│ A1          │──►│ A2          │──►│ A3          │──►│ A4          │──►│ A5          │
│ Architecture│   │ Security    │   │ Engineering │   │ Validation  │   │ Deployment  │
│ Plan JSON   │   │ CKV IDs     │   │ HCL code    │   │ tf+Checkov  │   │ apply+destroy│
└─────────────┘   └─────────────┘   └─────────────┘   └─────────────┘   └─────────────┘
```

Mỗi agent là một LangGraph **node** đọc/ghi `AgentState`; **conditional edges** quyết định
node kế tiếp (pass tiếp / route về sửa / dừng `requires_human`).

### Các agent

| Agent | File | Việc làm | Output |
|-------|------|----------|--------|
| **A1** Architecture | `agents/architecture.py` | LLM sinh JSON plan (resource/data source: type, name, attributes, blocks) | `infrastructure_plan` |
| **A2** Security | `agents/security.py` | Dựng **menu** CKV check áp dụng/resource từ catalog → LLM chọn IDs theo intent | `security_profile` |
| **A3** Engineering | `agents/engineering.py` | Sinh HCL đầy đủ theo plan + security context; **boundary check** khớp plan A1 | `generated_code` |
| **A4** Validation | `agents/validation.py` | init → validate → plan → **Checkov gate** (proportional) | `fix_feedback` |
| **A5** Deployment | `agents/deployment.py` | `terraform apply` thật + auto-destroy (eval mode) | `deployment_result` |

### Topology & routing

```
START → architecture → security → engineering → validation

validation ─→ deployment      (overall_passed)
           ─→ architecture     (MISSING_RESOURCE → re-plan)
           ─→ engineering      (SYNTAX / LOGIC / SECURITY → sửa code)
           ─→ requires_human   (INFRASTRUCTURE / hết backstop)

deployment ─→ END              (success)
           ─→ engineering      (LOGIC → sửa code)
           ─→ architecture     (MISSING_RESOURCE → re-plan)
           ─→ requires_human   (INFRASTRUCTURE / dirty / OTHER / hết backstop)
```

### State (`core/state.py`)

```
prompt · infrastructure_plan · security_profile · generated_code
fix_feedback · deployment_result · retries{...}
total_val_attempts (max 5) · total_deploy_attempts (max 4, độc lập)
security_status (ok|degraded) · routing_log · arch/eng_error_history
terraform_plan_timeout (đọc từ TF_PLAN_TIMEOUT) · run_dir
```

---

## 🔁 Retry & Error handling

```
Per-route budget (chặn 1 đường lặp vô hạn):
  val_eng     max 3   A4 → A3 (SYNTAX/LOGIC/SECURITY)
  val_arch    max 2   A4 → A1 (MISSING_RESOURCE)
  sec         max 1   security gate A4 → A3, hết → best-effort (không block)
  deploy_eng  max 2   A5 → A3 (LOGIC), độc lập val_eng
  deploy_arch max 2   A5 → A1 (MISSING_RESOURCE), độc lập val_arch

Phase backstop (chặn TỔNG):
  total_val_attempts    max 5   (A1/A3/A4 fail)
  total_deploy_attempts max 4   (A5 fail) — ĐỘC LẬP val phase
```

- **Vì sao tổng per-route (3+2+1=6) > backstop (5)?** Để backstop THỰC SỰ cắn (dừng khi tổng
  cao dù chưa route nào max riêng). Nếu backstop ≥ tổng → vô dụng.
- **Vì sao tách 2 backstop?** Chung 1 counter → A4 đốt hết 5 → A5 starve ngay apply đầu. Lỗi
  apply-time là lớp mới (A4 đã pass) → A5 cần ngân sách riêng.

> ✅ Mọi đường fail đều +1 counter → pipeline luôn hội tụ về END / requires_human trong số vòng hữu hạn.

---

## 🧭 Nguyên tắc thiết kế prompt (P1–P7)

Prompt A1 (`prompts/architecture.py`) driven bởi các nguyên tắc — mỗi cái trị một loại lỗi:

| # | Nguyên tắc | Trị lỗi |
|---|-----------|---------|
| **P1** | **Minimal inclusion** — chỉ resource user yêu cầu + dependency bắt buộc | hallucinate resource thừa |
| **P2** | **Dependency completeness** — attachment/association phải đủ 2 đầu | thiếu resource (under-gen) |
| **P3** | **User intent ≠ best practices** — không tự thêm "cho tốt" | over-engineering |
| **P4** | **Reference integrity** — mọi `REF:type.name.attr` phải resolve | unresolved reference |
| **P5** | **Preserve values** — giữ nguyên giá trị user (size, engine, flags...) | đổi giá trị tùy tiện |
| **P6** | **Data sources read-only** — không dùng để né tạo resource | workaround sai |
| **P7** | **Type validity** — chỉ type CÓ THẬT (~>5.0); nested-block (lifecycle/versioning/logging) → trong cha, không tách resource | A1 bịa type niche → deadlock A1↔A3 |

> 🔧 Mở rộng: gặp loại lỗi mới → thêm 1 principle + 1 verification step (xem comment trong file).

---

## 📊 Metric đánh giá

Chấm bằng `score.py` (độc lập với pipeline → chấm lại không tốn LLM). 3 nhóm:

### 🟢 Chất lượng code (không cần deploy)

| Metric | Công thức | Đo | Cao=tốt? |
|--------|-----------|-----|----------|
| **plan_valid** | #(validate+plan pass) / N | hợp lệ + deploy-được-tới-plan | ✅ |
| **resource_f1** ⭐ | mean F1(resource type vs gold) | sinh đúng & đủ resource (đo A1) | ✅ |
| **security_score** | Checkov pass-rate (`mean` \| `aggregate` \| `passed/total`) | posture security (full Checkov) | ⚠️ đọc kèm f1 |
| **semantic_correct** | #(plan_ok ∧ khớp gold Rego) / N | đúng INTENT (đặc tả hình thức) | ✅ |
| **llm_judge** | #(judge=1) / N | adequate (resource+attr, LLM chấm) | ✅ |

### 🟡 Real-world & robustness

| Metric | Đo | Lưu ý |
|--------|-----|-------|
| **deploy_success** | apply AWS thật thành công | env-dependent (quota/auth) — báo RIÊNG |
| **resolved@≤k** | % giải-quyết trong ≤k vòng retry | success-signal đổi theo flag |
| **pass@k** | xác suất ≥1/k run độc lập pass | CHỈ cho ≥2 run CÙNG hệ |
| **bootstrap CI95** | mean ± khoảng tin cậy (resample tasks) | CI hẹp = ổn định |

### ⚠️ Quy tắc đọc bắt buộc

```
1. security_score là TỈ LỆ → GAMEABLE: xây ÍT/đơn giản → % cao "ảo".
   → LUÔN đọc KÈM resource_f1. mean vs aggregate KHÔNG đổi kết luận.
2. Metric có-điều-kiện (security, semantic) chỉ tính trên plan_valid → so trên GIAO để cùng mẫu số.
3. deploy_success KHÔNG so với baseline (baseline không deploy).
4. resolved@k & success-rate đổi theo flag (--rego → semantic | else → plan_valid).
```

---

## ⚖️ So sánh Baseline vs Pipeline

Cô lập giá trị multi-agent: B1 và pipeline **đều có retry** → chênh lệch = THUẦN decomposition.

```powershell
# 1. B1 baseline (retry≤5 khớp backstop pipeline, no-deploy)
python baseline.py --csv dataset/data-dev.csv --retry 5 --out reviews/b1.json --workers 5

# 2. Pipeline no-deploy (cùng điểm dừng với baseline)
python evaluate.py --csv dataset/data-dev.csv --no-deploy --out results_nodeploy.json --workers 5

# 3. Chấm RIÊNG từng file (⚠️ KHÔNG gộp — gộp = pass@k chéo vô nghĩa)
python score.py reviews/b1.json        --csv dataset/data-dev.csv --checkov --llm-judge
python score.py results_nodeploy.json  --csv dataset/data-dev.csv --checkov --llm-judge
```

> **Vì sao no-deploy?** Baseline dừng ở plan; nếu pipeline deploy thì deploy-phase retry sửa thêm
> code → không công bằng. `--no-deploy` cho cả hai dừng cùng điểm.
> **Vì sao không gộp 2 file?** `score.py` coi nhiều file = nhiều RUN của CÙNG hệ (tính pass@k chéo);
> 2 hệ khác nhau → phần cross-run vô nghĩa. Per-file block vẫn đúng — chấm riêng cho sạch.

### Kết quả tham khảo (data-dev, retry≤5, no-deploy)

| Metric | B1 | Pipeline | Ghi chú |
|--------|-----|----------|---------|
| **resource_f1** | 0.570 | **0.819** ⭐ | pipeline +0.25 — giá trị cốt lõi (A1) |
| **llm_judge** | 0.618 | **0.824** ⭐ | pipeline +0.21 — adequate hơn |
| plan_valid | **0.971** | 0.912 | B1 +0.06 (pipeline trả "thuế" boundary/security) |
| security_score | **0.761** | 0.635 | B1 cao — **artifact của f1 thấp**, không phải an toàn hơn |
| semantic_correct | **0.515** | 0.441 | B1 cao — confound (plan_ok + over-plan) |
| time/row | **103s** | 192s | B1 nhanh ~2× |

> 💡 **Luận điểm trung thực:** Pipeline thắng RÕ ở **accuracy + adequacy** (xây đúng & đủ).
> Đổi lại plan_valid thấp hơn nhẹ + chậm 2×. security/semantic của B1 cao hơn là **artifact**
> của việc baseline xây ít/sai (f1=0.57) — KHÔNG kết luận "B1 tốt hơn". Đọc kèm f1.

---

## 📁 Cấu trúc thư mục

```
agents/          5 agent: architecture, security, engineering, validation, deployment
prompts/         system/user prompt từng agent
core/            llm, parsers, terraform/checkov wrapper, catalog, metrics, state, retry_control
dataset/         data-test.csv (gold, 140 rows) · data-dev.csv (tuning, 34 rows)
scripts/         populate_provider_cache.py (đổ provider offline)
graph.py         LangGraph pipeline (topology, routing, build_graph)
trace.py         Walkthrough từng bước — debug/giải thích
evaluate.py      Batch eval song song
baseline.py      Single-LLM baseline B0/B1 (ablation)
score.py         Tính metric đầy đủ
cleanup.py       Dọn tài nguyên AWS rò rỉ

# Tự tạo khi chạy:
tmp/             Terraform working dirs (tự cleanup)
reviews/         Kết quả evaluation JSON
.tf_plugin_cache/ Provider cache offline (đổ bằng scripts/populate_provider_cache.py)

```
