# Hướng dẫn Setup

Chạy multi-agent Terraform generation trên Windows 10/11.

## 1. Yêu cầu cài đặt trước

| Công cụ | Phiên bản | Cách cài |
|---------|----------|---------|
| Python | ≥ 3.11 | [python.org](https://www.python.org/downloads/) |
| Git | (bất kỳ) | [git-scm.com](https://git-scm.com/) |
| Terraform | ≥ 1.5 | [developer.hashicorp.com/terraform/install](https://developer.hashicorp.com/terraform/install) |
| Checkov | (pip install) | Cài qua requirements.txt |
| OPA | ≥ 1.0 | [github.com/open-policy-agent/opa/releases](https://github.com/open-policy-agent/opa/releases) |

```powershell
# Kiểm tra sau khi cài
terraform version
checkov --version
opa version
```

---

## 2. Virtual Environment

```powershell
cd D:\2-6

python -m venv venv
venv\Scripts\activate    # prompt hiển thị (venv)
```

---

## 3. Cài dependencies

```powershell
venv\Scripts\activate

python -m pip install --upgrade pip setuptools wheel
pip install -r requirements.txt
```

---

## 4. Cấu hình `.env`

```powershell
Copy-Item .env.example .env
notepad .env
```

Điền vào `.env`:

```ini
# LLM
LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=sk-your-key-here
DEEPSEEK_MODEL=deepseek-chat

# AWS
AWS_ACCESS_KEY_ID=your-access-key
AWS_SECRET_ACCESS_KEY=your-secret-key
AWS_DEFAULT_REGION=us-east-1
AWS_PROFILE=noseyug

# Terraform / Checkov
TF_PLAN_TIMEOUT=300
CHECKOV_BIN=checkov

# LLM tokens per agent (tăng nếu dùng reasoning model)
LLM_MAX_TOKENS_SECU=2048
LLM_MAX_TOKENS_ENGI=4096
LLM_MAX_TOKENS_ARCHI=2048
```

---

## 5. Chạy

### Một prompt nhanh

```powershell
python graph.py "Create an S3 bucket with versioning and SSE enabled."
```

### Trace — xem pipeline từng bước

```powershell
# Full walkthrough 1 prompt
python trace.py "Create an RDS PostgreSQL instance"

# Case từ dataset, không deploy
python trace.py --csv dataset/data-dev.csv --cases 33 --no-deploy

# Nhiều case song song, lưu log chi tiết
python trace.py --csv dataset/data-dev.csv --cases 0 1 2 5 7 --no-deploy \
    --workers 3 --log-dir logs/
```

### Batch evaluation

```powershell
# Không deploy (nhanh, không tốn AWS)
python evaluate.py --no-deploy --csv dataset/data-dev.csv --out reviews/test.json

# Chọn row cụ thể
python evaluate.py --no-deploy --cases 0 3 5-10 --csv dataset/data-test.csv

# Chạy song song
python evaluate.py --workers 3 --csv dataset/data-test.csv --out reviews/run1.json
```

---

## 6. Thư mục tự tạo sau khi chạy

```
tmp/             Terraform working directories (tự cleanup)
logs/            Trace logs (--log-dir)
reviews/         Kết quả evaluation JSON
.tf_plugin_cache/ Terraform provider cache (dùng chung mọi run)
```

---

## 7. Troubleshooting

**"python is not recognized"**
→ Python chưa trong PATH. Cài lại và tick "Add to PATH".

**"venv\Scripts\activate" không tìm thấy**
→ Chạy `python -m venv venv` từ thư mục dự án.

**Checkov / Terraform không tìm thấy**
```powershell
where terraform
where checkov
# Nếu không thấy → thêm vào PATH hoặc set CHECKOV_BIN=... trong .env
```

**AWS credentials không hợp lệ**
```powershell
aws configure --profile noseyug
```

**terraform init failed: locked provider ... does not match constraint**
→ Lock file cũ bị cache. Xóa và chạy lại:
```powershell
Remove-Item -Recurse tmp\trace\.terraform -ErrorAction SilentlyContinue
Remove-Item tmp\trace\.terraform.lock.hcl -ErrorAction SilentlyContinue
```
*(Pipeline tự xóa lock file từ version hiện tại — chỉ cần dọn thủ công lần đầu.)*

**OPA không tìm thấy**
→ Tải `opa_windows_amd64.exe` từ GitHub releases, đổi tên thành `opa.exe`, đặt vào PATH.

---

## 8. Tắt venv

```powershell
deactivate
```
