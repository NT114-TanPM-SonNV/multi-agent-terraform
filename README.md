# Multi-Agent Terraform Generation

Sinh Terraform HCL từ mô tả ngôn ngữ tự nhiên bằng pipeline **5 agent chuyên biệt**
(Architecture → Security → Engineering → Validation → Deployment) orchestrate bằng
**LangGraph**, có các vòng tự sửa (retry) và deploy thật lên AWS.

---

## 1. Yêu cầu môi trường

| Công cụ | Bắt buộc cho | Cách cài |
|---------|--------------|---------|
| Python ≥ 3.11 | pipeline | [python.org](https://www.python.org/downloads/) |
| `terraform` ≥ 1.5 | validate / plan / apply | [terraform.io/install](https://developer.hashicorp.com/terraform/install) |
| `checkov` | security gate | `pip install checkov` (có trong requirements.txt) |
| `opa` ≥ 1.0 | chấm Rego (`score.py --rego`) | [GitHub Releases](https://github.com/open-policy-agent/opa/releases) |
| AWS credentials | terraform / Rego eval | `aws configure` hoặc `.env` |

## 2. Setup nhanh

```powershell
# 1. Virtual environment
python -m venv venv
venv\Scripts\activate

# 2. Cài dependencies
pip install -r requirements.txt

# 3. Configure
Copy-Item .env.example .env
notepad .env    # điền DEEPSEEK_API_KEY + AWS credentials
```

**Chi tiết:** xem [`SETUP.md`](SETUP.md)

## 3. Chạy 1 prompt

```powershell
python graph.py "Create an S3 bucket with versioning and SSE enabled."
```

## 4. Trace — xem pipeline từng bước

```powershell
# Trace 1 prompt (full walkthrough)
python trace.py "Create an RDS PostgreSQL instance"

# Trace case từ dataset (không deploy)
python trace.py --csv dataset/data-dev.csv --cases 33 --no-deploy

# Trace nhiều case song song, lưu log
python trace.py --csv dataset/data-dev.csv --cases 0 1 2 5 7 10 15 16 17 20 23 25 27 29 33 \
    --no-deploy --workers 3 --log-dir logs/ --out results.json
```

## 5. Đánh giá đầy đủ

Dataset: `dataset/data-test.csv` (140 rows), `dataset/data-dev.csv` (34 rows — tuning only).

```powershell
# (a) Chạy pipeline 3 lần độc lập
for($i=1; $i -le 3; $i++) {
    python evaluate.py --csv dataset/data-test.csv --out reviews/run$i.json
}

# (b) Baseline single-LLM
python baseline.py --csv dataset/data-test.csv --out reviews/baseline.json

# (c) Chấm metric
python score.py reviews/run1.json reviews/run2.json reviews/run3.json `
    --csv dataset/data-test.csv --rego --checkov --out reviews/report_pipeline.json
python score.py reviews/baseline.json `
    --csv dataset/data-test.csv --rego --checkov --out reviews/report_baseline.json
```

### Flags hữu ích

`evaluate.py` và `trace.py` dùng cùng bộ flags:

| Flag | Mô tả |
|------|-------|
| `--no-deploy` | Dừng sau A4 validation (không tốn AWS, nhanh hơn) |
| `--no-secu` | Ablation: bỏ Security agent (A2) |
| `--no-destroy` | Giữ resource sau apply |
| `--workers N` | Chạy N row song song |
| `--cases 0 3 7-10` | Chọn row cụ thể từ CSV |
| `--csv FILE` | Dataset path (mặc định: data-dev.csv) |
| `--out FILE` | Lưu kết quả JSON |
| `--log-dir DIR` | *(trace only)* Lưu trace log từng row vào file riêng |
| `--plan-timeout N` | Terraform plan timeout (giây) |

## 6. Cấu trúc thư mục

```
agents/          5 agent: architecture, security, engineering, validation, deployment
prompts/         system/user prompt cho từng agent
core/            llm, parsers, terraform/checkov wrapper, catalog, metrics, state
dataset/         data-test.csv (gold), data-dev.csv (tuning)
graph.py         LangGraph pipeline
trace.py         Walkthrough từng bước — debug và giải thích pipeline
evaluate.py      Batch eval song song
baseline.py      Single-LLM baseline (ablation)
score.py         Tính metric: plan_valid, semantic_correct (Rego), security, deploy
run_metric.py    Multi-run harness → mean ± CI95
cleanup.py       Dọn tài nguyên AWS rò rỉ
```

## 7. Dọn tài nguyên AWS

Mặc định (`auto-destroy`) tự `terraform destroy` sau mỗi apply thành công. Khi destroy fail
hoặc row timeout giữa apply → dùng `cleanup.py` để dọn thủ công.
