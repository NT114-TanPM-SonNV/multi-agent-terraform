"""Agent 4 — Validation / Testing (validation_node)

Validate + plan generated HCL. Flow: terraform init → validate → plan → Checkov gate.

Workflow:
  1. terraform init: tải provider, setup working directory
  2. terraform validate: static syntax check
  3. terraform plan -out=tfplan.out: logical check + lưu plan file
  4. terraform show -json tfplan.out: chuyển plan sang JSON để Checkov scan chính xác hơn
  5. Checkov gate (nếu plan pass): enforce CKV IDs mà A2 chọn, scan trên plan JSON
  6. Route: deployment (success) hoặc retry agents (failure)

Error Classification (Hybrid — Pattern + LLM):
  - SYNTAX: terraform validate fail → TẤT ĐỊNH → route engineering (A3)
  - LOGIC: terraform plan fail + LLM phân loại → route engineering (A3)
  - MISSING_RESOURCE: plan fail + "not found" pattern → TẤT ĐỊNH → route architecture (A1)
  - SECURITY: Checkov check fail → TẤT ĐỊNH → route engineering (A3) hay best-effort deploy
  - INFRASTRUCTURE: timeout/connection pattern → TẤT ĐỊNH → route requires_human
  - (PASS): tất cả check pass → route deployment (A5)

Security Gate:
  - Enforce đúng tập CKV IDs mà A2 chọn per resource (grounded bằng catalog menu)
  - Scan trên plan JSON (terraform show -json) → chính xác hơn source scan
  - Fallback về source scan nếu plan JSON không khả dụng
  - Nếu fail + có budget → retry A3 (max 2 lần, retries["sec"])
  - Nếu fail + hết budget → best-effort deploy + ghi unmet_checks

Input: state["generated_code"], state["infrastructure_plan"], state["security_profile"]
Output: state["fix_feedback"] (success/error), state["retries"] (retry tracking)

Retry logic:
  - SYNTAX/LOGIC: retries["eng"] max 3 (từ A4 + A5 A3 retry)
  - MISSING_RESOURCE: retries["arch"] max 2 (từ A4 + A5 A1 retry)
  - INFRASTRUCTURE: 1 lần retry terraform plan transient (inside node, không qua graph)
  - Oscillation detection: 3 lỗi cùng loại liên tiếp → requires_human

Note: Full Checkov scoring là score.py's job (independent full scan).
      A4 chỉ enforce tập check A2 đã chọn để hướng A3 fix.
"""
import json
import logging
import re
import subprocess
import time
from pathlib import Path

from core.state import AgentState
from core.llm import call_llm
from core.parsers import parse_llm_json
from core.terraform import (
    run_terraform, write_terraform_dir, terraform_workdir,
    run_checkov_on_hcl, run_checkov_on_plan,
)
from core.retry_control import increment_retry, check_retry_budget, detect_oscillation
from prompts.validation import (
    SYSTEM_PROMPT as _SYSTEM_PROMPT,
    TOP_PROMPT as _TOP, BOTTOM_PROMPT as _BOTTOM,
    SYNTAX_CONTEXT, FAILING_CODE_CONTEXT, SYNTAX_FIX_FALLBACK, INIT_FIX,
    PLAN_CONTEXT, SECURITY_FIX,
)

logger = logging.getLogger(__name__)

# Regex tìm tất cả resource declarations trong HCL: `resource "type" "name"`
_RESOURCE_DECL_RE = re.compile(r'resource\s+"([^"]+)"\s+"([^"]+)"')

# Timeout (giây) cho từng terraform command.
# init dài hơn vì phải download provider plugin (~300MB cho AWS).
_INIT_TIMEOUT = 300
# validate là static check (không cần network) → nhanh.
_VALIDATE_TIMEOUT = 60

# Retry budget thống nhất (đồng bộ với FRAMEWORK.md)
_MAX_TOTAL_RETRY = 5     # global backstop — sau N attempt tổng thể, dừng hẳn
_MAX_ENG_RETRY   = 3     # budget riêng cho A3 (SYNTAX/LOGIC/SECURITY)
_MAX_ARCH_RETRY  = 2     # budget riêng cho A1 (MISSING_RESOURCE)
_MAX_SECURITY_RETRY = 2  # budget riêng cho security gate — hết → best-effort accept (không block deploy)


# ── Security gate catalog ──────────────────────────────────────────────────────
# Thiết kế mới (không posture scalar):
#   - A2 chọn trực tiếp CKV IDs per resource (grounded bằng catalog menu)
#   - _targets_for_plan chỉ đọc profile["checks"] — không cần level/tier trung gian
#   - _load_check_names dùng để render fix_instruction human-readable cho A3
#
# Đường dẫn đến 2 catalog file:
#   .check_targets.json: single-resource checks (CKV_AWS_*)
#   .check_graph.json:   graph checks (CKV2_AWS_*)
_CATALOG_FILE = Path(__file__).parent.parent / "core" / "catalog.json"


def _load_check_names() -> dict[str, str]:
    """Nạp {check_id → check_name} từ checks.json — dùng để sinh fix_instruction."""
    names: dict[str, str] = {}
    try:
        data = json.loads(_CATALOG_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("Không nạp được checks.json (%s) — fix_instruction sẽ dùng ID trần", e)
        return names
    for checks in data.values():
        for c in checks:
            cid = c.get("id")
            if cid and cid not in names:
                names[cid] = c.get("name", cid)
    return names


# Module-level load: chạy 1 lần khi import.
_CKV_NAME: dict[str, str] = _load_check_names()


def _targets_for_plan(profile: dict) -> tuple[set[str], dict[str, set[str]]]:
    """Từ security_profile A2 → tập CKV ID cần verify, toàn cục + theo từng resource addr.

    Thiết kế mới (bỏ posture scalar):
      - A2 đã chọn trực tiếp CKV IDs per resource (grounded bằng catalog menu + FLOOR)
      - Hàm này chỉ đọc profile["checks"] và build per_res + global_ids
      - Không cần level/tier/posture_level trung gian nữa

    Returns:
        global_ids: tập tất cả IDs cần pass vào run_checkov_on_hcl
        per_res:    {resource_addr → set(ids)} — để _enforceable_unmet biết
                    check nào là bắt buộc cho resource cụ thể nào
    """
    per_res: dict[str, set[str]] = {}
    global_ids: set[str] = set()
    for addr, info in (profile or {}).items():
        ids = set(info.get("checks", []))
        if ids:
            per_res[addr] = ids
            global_ids.update(ids)
    return global_ids, per_res


# Patterns phân loại lỗi terraform plan (tất định, không cần LLM).
# Tại sao tách TRANSIENT và AUTH?
#   TRANSIENT: lỗi mạng tạm thời (connection reset, throttle) → retry plan trong node
#   AUTH: lỗi credential/provider → không retry (sửa code vô nghĩa, cần AWS setup)
#   INFRA = TRANSIENT ∪ AUTH: cả hai đều route requires_human sau khi xử lý xong
#
# Tại sao không dùng bare "timeout"?
#   aws_db_instance có attribute `timeout {}` block — bare "timeout" sẽ false-positive.
#   Dùng cụm cụ thể: "i/o timeout", "context deadline exceeded", "timed out".
_PLAN_TRANSIENT_PATTERNS = (
    "connection refused", "connection reset", "could not connect",
    "i/o timeout", "dial tcp", "no such host", "context deadline exceeded",
    "tls handshake timeout", "requesttimeout",
    "throttling", "requestlimitexceeded", "rate exceeded", "limitexceeded",
)
_PLAN_AUTH_PATTERNS = (
    "no valid credential", "nocredentialproviders", "could not load credentials",
    "expired token", "invalidclienttokenid", "authfailure",
    "unauthorizedoperation", "accessdenied", "requesterror",
    "failed to instantiate provider", "could not load plugin",
)
_PLAN_INFRA_PATTERNS = _PLAN_TRANSIENT_PATTERNS + _PLAN_AUTH_PATTERNS

# Patterns phát hiện MISSING_RESOURCE từ terraform plan output (tất định).
# "not found" thường xuất hiện khi A1 plan nhắm tới resource type không tồn tại
# hoặc data source trả về empty.
_MISSING_RESOURCE_PATTERNS = (
    "not found", "not exist", "does not exist",
    "invalid resource type", "unsupported",
    "unknown resource type", "type not defined",
)

# Retry terraform plan tối đa 1 lần nếu gặp transient error.
# Plan là read-only/idempotent → retry rẻ, không tốn state.
# Lý do giới hạn 1: nếu transient vẫn xảy ra sau 1 retry → infra issue, không nên spam.
_MAX_PLAN_TRANSIENT_RETRY = 1
_PLAN_RETRY_BACKOFF = 3  # giây chờ giữa các retry (tăng theo attempt để giảm tải)


def _hcl_resource_labels(code: str) -> list[str]:
    """Trích list "type.name" từ HCL code — cung cấp context cho LLM classify."""
    return [f"{t}.{n}" for t, n in _RESOURCE_DECL_RE.findall(code)]


def _matches(text: str, patterns: tuple) -> bool:
    """Case-insensitive substring match — tất định, không dùng LLM.

    Tại sao lowercase? Terraform/AWS error messages không nhất quán case:
    "AccessDenied" vs "accessdenied" vs "access denied" đều cần bắt được.
    """
    low = (text or "").lower()
    return any(p in low for p in patterns)


def _extract_code_context(validate_err: str, code: str, window: int = 4,
                          max_errors: int = 6) -> str:
    """Trích đoạn code xung quanh các dòng lỗi để LLM classify SYNTAX error.

    Terraform validate báo lỗi theo format "on main.tf line N" → parse line number.
    Lấy window=4 dòng trước/sau dòng lỗi → LLM thấy đủ context để hiểu vấn đề.
    Marker ">>>" đánh dấu dòng lỗi chính xác.
    max_errors=6 để tránh prompt quá dài khi có nhiều lỗi đồng thời.

    Tại sao dùng re.finditer thay vì re.search?
      re.search chỉ lấy lỗi ĐẦU TIÊN → nếu file có 3 lỗi, LLM chỉ thấy 1 → fix 1 → retry →
      fix tiếp... = whack-a-mole (cạn budget trước khi fix hết). finditer lấy tất cả lỗi
      → LLM sửa hết trong 1 lần → tiết kiệm retry budget.
    """
    line_nums: list[int] = []
    seen: set[int] = set()
    for m in re.finditer(r"on main\.tf line (\d+)", validate_err):
        ln = int(m.group(1))
        if ln not in seen:
            seen.add(ln)
            line_nums.append(ln)
        if len(line_nums) >= max_errors:
            break
    if not line_nums:
        return ""
    lines = code.split("\n")
    blocks = []
    for line_num in line_nums:
        start = max(0, line_num - window - 1)
        end   = min(len(lines), line_num + window)
        parts = []
        for i, ln in enumerate(lines[start:end], start=start + 1):
            marker = ">>>" if i == line_num else "   "
            parts.append(f"{i:3d} {marker} {ln}")
        blocks.append("\n".join(parts))
    # Ngăn cách các block bằng "---" để LLM biết đây là các vị trí lỗi độc lập
    return "\n---\n".join(blocks)


def _error_signature(error_type: str, text: str) -> list:
    """Trích danh sách error message ngắn gọn — dùng để phát hiện oscillation.

    Oscillation: A3 sửa, A4 fail đúng cùng lỗi → A3 lại sửa → loop vô hạn.
    detect_oscillation() so sánh signatures: nếu 3 lần liên tiếp cùng signature → dừng.
    Dedup + sort để signature ổn định (không thay đổi theo thứ tự xuất hiện).
    Truncate 80 ký tự mỗi lỗi → tránh signature quá dài, khó so sánh.
    """
    errs = re.findall(r"Error:\s*(.+)", text or "")
    sig = sorted({e.strip()[:80] for e in errs})
    return sig or [((text or "").strip()[:80] or error_type)]


def _success_result(checkov: dict, unmet: list | None = None,
                    phantom: list | None = None) -> dict:
    """Tạo fix_feedback cho trường hợp validation PASS.

    overall_passed=True: pipeline đã validate + plan OK → cho phép route về A5.
    unmet_checks: security check chưa đạt nhưng hết retry budget → báo cáo, không block.
    phantom_checks: check được target nhưng Checkov không trigger (resource không tồn tại
                    hoặc companion resource thiếu) → không phải fail, nhưng cần monitor.

    Tại sao unmet không block?
      Triết lý "working IaC": code chạy được > code không deploy được + security cao hơn.
      Nếu block: A3 không thể fix check impossible (vd CKV_AWS_145 cần companion resource
      không được phép thêm) → pipeline stuck → không có deliverable. Báo cáo tốt hơn block.
    """
    return {
        "fix_feedback": {
            "overall_passed": True, "error_type": None, "root_cause": None,
            "fix_instruction": None, "checkov": checkov,
            "unmet_checks": [{"resource": a, "ckv_id": i, "name": n} for a, i, n in (unmet or [])],
            "phantom_checks": list(phantom or []),
            "validate_passed": True, "plan_passed": True,
        },
    }


def _enforceable_unmet(per_res: dict[str, set[str]], checkov: dict) -> list[tuple[str, str, str]]:
    """Tìm check thực sự bị vi phạm — failed VÀ nằm trong tập target của resource đó.

    Logic join:
      failed_per_resource: [(addr, ckv_id)] — Checkov báo fail
      per_res:             {addr → set(ids)} — tập A4 muốn enforce cho addr đó
      unmet = intersection: check vừa fail vừa được target

    Tại sao cần per_res thay vì chỉ kiểm failed_ckv_ids?
      Ví dụ: Checkov fail CKV_AWS_18 (S3 access logging) trên aws_s3_bucket.main.
      Nếu posture = standard: _targets_for_plan không include CKV_AWS_18 (level 2, strict only).
      → Không phải unmet → không cần A3 fix → không block → đúng.
      Nếu posture = strict: _targets_for_plan include CKV_AWS_18 → unmet → A3 phải fix.

    Dedup (seen set): Checkov đôi khi báo cùng (addr, id) nhiều lần nếu resource xuất hiện
    trong nhiều context → dedup để fix_instruction không repeat cùng check.
    """
    unmet: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for addr, ckv_id in checkov.get("failed_per_resource", []):
        if ckv_id not in per_res.get(addr, ()):
            continue  # check fail nhưng không phải target của resource này → bỏ qua
        key = (addr, ckv_id)
        if key in seen:
            continue
        seen.add(key)
        unmet.append((addr, ckv_id, _CKV_NAME.get(ckv_id, ckv_id)))
    return unmet


def _security_return(state: AgentState, unmet: list[tuple[str, str, str]],
                     checkov: dict, phantom: list | None = None) -> dict:
    """Tạo fix_feedback khi Checkov fail và còn budget retry.

    Route: SECURITY → engineering (A3 sửa attributes/companion resources).
    Budget riêng _MAX_SECURITY_RETRY = 2 (tách khỏi _MAX_ENG_RETRY để SECURITY
    không ăn budget của SYNTAX/LOGIC — hai loại lỗi độc lập, không nên dùng chung budget).

    fix_instruction format: "- addr: check_name" (ngôn ngữ người, không chỉ ID).
    Tại sao cần tên check?
      CKV_AWS_145 không nói được A3 cần làm gì. "Ensure S3 bucket has encryption enabled"
      thì rõ ràng hơn rất nhiều. A3 nhận tên → tra best practice → implement đúng.
    """
    new_total = state["total_attempts"] + 1
    increment_retry(state, "sec", "SECURITY", str(sorted({cid for _a, cid, _n in unmet})))
    fix_instruction = SECURITY_FIX.format(
        items="\n".join(f"- {addr}: {name}" for addr, _id, name in unmet)
    )
    signature = sorted({cid for _a, cid, _n in unmet})
    logger.info("Agent 4: FAIL SECURITY — %d unmet check(s): %s", len(unmet), signature)
    return {
        "fix_feedback": {
            "overall_passed": False, "error_type": "SECURITY", "root_cause": "engineering",
            "fix_instruction": fix_instruction, "raw_error": "",
            "checkov": checkov,
            "unmet_checks": [{"resource": a, "ckv_id": i, "name": n} for a, i, n in unmet],
            "phantom_checks": list(phantom or []),
            "validate_passed": True, "plan_passed": True,
        },
        "retries": state["retries"],
        "total_attempts": state["total_attempts"],
        "routing_log": state["routing_log"] + [{
            "round": new_total, "error_type": "SECURITY", "root_cause": "engineering",
            "fix_instruction": fix_instruction, "predicted_route": "engineering",
        }],
    }


def _infra_return(state: AgentState, fix_instruction: str, checkov: dict,
                  validate_passed: bool, plan_passed: bool, raw_error: str = "") -> dict:
    """Tạo fix_feedback khi gặp lỗi INFRASTRUCTURE (timeout, auth, network).

    INFRASTRUCTURE không route retry — không có gì để sửa ở code level.
    Route thẳng requires_human: người cần kiểm tra AWS credentials, network, quota.
    Vẫn ghi routing_log để audit trail.
    """
    # INFRASTRUCTURE → requires_human (terminal). Chỉ bump total_attempts (global backstop),
    # KHÔNG increment per-agent counter — tránh nhiễu error_history dùng cho oscillation detection.
    state["total_attempts"] += 1
    new_total = state["total_attempts"]
    logger.info("Agent 4: INFRASTRUCTURE — %s", fix_instruction[:80])
    return {
        "fix_feedback": {
            "overall_passed": False, "error_type": "INFRASTRUCTURE", "root_cause": None,
            "fix_instruction": fix_instruction, "raw_error": raw_error,
            "checkov": checkov,
            "validate_passed": validate_passed, "plan_passed": plan_passed,
        },
        "retries": state["retries"],
        "total_attempts": state["total_attempts"],
        "routing_log": state["routing_log"] + [{
            "round": new_total, "error_type": "INFRASTRUCTURE", "root_cause": None,
            "fix_instruction": fix_instruction, "predicted_route": "requires_human",
        }],
    }


def _fail_return(state: AgentState, error_type: str, root_cause: str,
                 fix_instruction: str, checkov: dict, validate_passed: bool,
                 plan_passed: bool, signature: list, raw_error: str = "") -> dict:
    """Tạo fix_feedback cho SYNTAX, LOGIC, MISSING_RESOURCE errors.

    Là hàm chung cho cả 3 loại lỗi "fixable":
      - SYNTAX: code không parse được → A3 sửa syntax
      - LOGIC: plan logic sai (reference hỏng, giá trị sai) → A3 sửa logic
      - MISSING_RESOURCE: resource type không tồn tại → A1 re-plan

    Tracking: increment retry counter theo root_cause (eng hoặc arch).
    routing_log: append entry để audit trail, oscillation detection.
    """
    new_total = state["total_attempts"] + 1
    is_eng = error_type in ("SYNTAX", "LOGIC")
    is_arch = error_type == "MISSING_RESOURCE"

    # Increment đúng counter theo loại lỗi (để check_retry_budget trong route_after_validation)
    if is_eng:
        increment_retry(state, "eng", error_type, raw_error[:200])
    elif is_arch:
        increment_retry(state, "arch", error_type, raw_error[:200])
    else:
        # Fallback an toàn cho error type không xác định — ghi vào arch counter
        increment_retry(state, "arch", error_type, raw_error[:200])

    return {
        "fix_feedback": {
            "overall_passed": False, "error_type": error_type, "root_cause": root_cause,
            "fix_instruction": fix_instruction, "raw_error": raw_error,
            "checkov": checkov,
            "validate_passed": validate_passed, "plan_passed": plan_passed,
        },
        "retries": state["retries"],
        "total_attempts": state["total_attempts"],
        "routing_log": state["routing_log"] + [{
            "round": new_total, "error_type": error_type, "root_cause": root_cause,
            "fix_instruction": fix_instruction, "predicted_route": root_cause,
        }],
    }


def _llm_classify(context: str, allowed_types: set,
                  default_type: str, default_fix: str) -> tuple[str, str, str]:
    """LLM phân loại error type và sinh fix_instruction từ terraform error context.

    Hybrid approach: pattern matching trước (tất định, không tốn LLM), LLM sau (cho
    những lỗi ambiguous mà pattern không bắt được).

    allowed_types: tập error type LLM được phép trả.
      - Terraform validate → {"SYNTAX"} (validate chỉ bắt syntax)
      - Terraform plan → {"LOGIC", "MISSING_RESOURCE"} (plan có thể cả hai)
    Nếu LLM trả type ngoài allowed → dùng default_type (safe fallback).

    Fallback: nếu LLM call fail (timeout, parse error) → (default_type, root, default_fix).
    Không raise exception để không làm sập pipeline vì LLM classify error.

    Returns: (error_type, root_cause, fix_instruction)
    """
    def _root(et: str) -> str:
        # MISSING_RESOURCE → A1 (architecture); SYNTAX/LOGIC → A3 (engineering)
        return {"MISSING_RESOURCE": "architecture"}.get(et, "engineering")

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": context},
    ]
    try:
        raw = call_llm(messages, agent="validation")
        parsed = parse_llm_json(raw, {"error_type": None, "fix_instruction": None})
    except Exception as e:
        logger.warning("Agent 4 LLM classify lỗi (%s) — dùng default", e)
        return default_type, _root(default_type), default_fix

    et = parsed.get("error_type")
    if et not in allowed_types:
        et = default_type
    fix = str(parsed.get("fix_instruction") or default_fix)[:500]
    return et, _root(et), fix


def validation_node(state: AgentState) -> dict:
    """LangGraph node — validate + plan. Security grading độc lập ở score.py."""
    code = state["generated_code"]
    plan_timeout = state.get("terraform_plan_timeout", 120)
    # Checkov result rỗng dùng khi không chạy đến Checkov stage
    _no_checkov = {"passed_count": 0, "failed": []}

    # Guard: generated_code rỗng = A3 fail tạo code. Route về A1 re-plan
    # (vì nếu code rỗng, khả năng là A1 plan sai → A3 không có gì để serialize).
    if not (code or "").strip():
        return _fail_return(
            state, "MISSING_RESOURCE", "architecture",
            "generated_code rỗng — Engineering agent không sinh được HCL.",
            _no_checkov, False, False, ["empty_code"],
        )

    run_dir = state.get("run_dir") or ""
    # files_dir: thư mục chứa stub files (Lambda zip, etc.) để write_terraform_dir copy vào
    files_dir = (Path(run_dir) / "files") if run_dir else None

    with terraform_workdir(run_dir or None, "a4") as d:
        # Ghi HCL + stubs vào working directory (main.tf + stub files cho Lambda/S3)
        write_terraform_dir(d, code, files_dir=files_dir)

        # ── terraform init ─────────────────────────────────────────────────────
        # init tải AWS provider plugin (~300MB) → timeout dài (300s).
        # Nếu timeout: INFRA error (network/slow download, không phải code bug).
        # Nếu returncode != 0 với "Invalid" prefix: có thể là SYNTAX trong provider block → A3 fix.
        try:
            init = run_terraform(["terraform", "init", "-no-color"], d, _INIT_TIMEOUT)
        except subprocess.TimeoutExpired:
            return _infra_return(state, f"terraform init timed out (>{_INIT_TIMEOUT}s)", _no_checkov, False, False)
        if init.returncode != 0:
            init_err = ((init.stderr or "") + "\n" + (init.stdout or "")).strip()
            # "problems with the configuration" / "Error: Invalid" = code syntax issue
            if "problems with the configuration" in init_err or init_err.startswith("Error: Invalid"):
                sig = _error_signature("SYNTAX", init_err)
                return _fail_return(
                    state, "SYNTAX", "engineering",
                    INIT_FIX.format(err=init_err[:600]),
                    _no_checkov, False, False, sig, raw_error=init_err[:2000],
                )
            # Còn lại: provider download fail, network issue, plugin cache issue → INFRA
            return _infra_return(state, f"terraform init failed: {init_err[:500]}", _no_checkov, False, False, raw_error=init_err[:2000])

        # ── terraform validate ─────────────────────────────────────────────────
        # Static check: không cần network, không gọi AWS API.
        # Phát hiện: typo attribute name, missing required argument, wrong type, etc.
        # Luôn là SYNTAX error (validate không biết resource có tồn tại hay không).
        try:
            val = run_terraform(["terraform", "validate", "-no-color"], d, _VALIDATE_TIMEOUT)
        except subprocess.TimeoutExpired:
            return _infra_return(state, "terraform validate timed out", _no_checkov, False, False)

        if val.returncode != 0:
            validate_err = (val.stderr or val.stdout or "").strip()
            sig = _error_signature("SYNTAX", validate_err)
            logger.info("Agent 4: FAIL SYNTAX (validate)")
            # Trích code context (lines xung quanh dòng lỗi) cho LLM → fix chính xác hơn
            code_ctx = _extract_code_context(validate_err, code)
            code_block = FAILING_CODE_CONTEXT.format(code_ctx=code_ctx) if code_ctx else ""
            # Build LLM context với full error + code context + resource labels + lịch sử lỗi
            # Lịch sử lỗi (error_history) từ retries["eng"] — tránh LLM lặp lại sai lầm cũ
            eng_history = (state.get("retries") or {}).get("eng", {}).get("error_history", [])
            syntax_ctx = _TOP + SYNTAX_CONTEXT.format(
                validate_err=validate_err[:2500],
                code_context=code_block,
                labels=_hcl_resource_labels(code),
                history=json.dumps(eng_history[-2:]),
            ) + _BOTTOM
            # LLM classify với allowed_types={"SYNTAX"} — validate chỉ biết syntax
            _, _, fix = _llm_classify(
                syntax_ctx, {"SYNTAX"}, "SYNTAX",
                SYNTAX_FIX_FALLBACK.format(err=validate_err[:600])
            )
            return _fail_return(
                state, "SYNTAX", "engineering", fix,
                _no_checkov, False, False, sig, raw_error=validate_err[:2000])

        # ── terraform plan ─────────────────────────────────────────────────────
        # -out=tfplan.out: lưu binary plan để terraform show -json bên dưới.
        # Tại sao lưu plan? Checkov scan plan JSON chính xác hơn scan source:
        #   resolved computed values, for_each expansion, graph checks đầy đủ.
        plan_passed, plan_err = True, ""
        for attempt in range(_MAX_PLAN_TRANSIENT_RETRY + 1):
            try:
                plan = run_terraform(
                    ["terraform", "plan", "-no-color", "-out=tfplan.out"], d, plan_timeout)
            except subprocess.TimeoutExpired:
                return _infra_return(state, f"terraform plan timed out (>{plan_timeout}s)", _no_checkov, True, False)
            plan_passed = plan.returncode == 0
            plan_err = (plan.stderr or plan.stdout or "").strip()
            if plan_passed or not _matches(plan_err, _PLAN_TRANSIENT_PATTERNS):
                break
            if attempt < _MAX_PLAN_TRANSIENT_RETRY:
                logger.info("Agent 4: plan transient (attempt %d) — retry: %s",
                            attempt + 1, plan_err[:120])
                time.sleep(_PLAN_RETRY_BACKOFF * (attempt + 1))

        # ── terraform show -json (bên trong workdir, trước khi context exit) ──
        # Phải chạy trước khi ra khỏi `with terraform_workdir` vì tfplan.out nằm trong d.
        plan_json_str: str | None = None
        if plan_passed:
            try:
                show = run_terraform(["terraform", "show", "-json", "tfplan.out"], d, 60)
                if show.returncode == 0 and show.stdout:
                    plan_json_str = show.stdout
            except Exception as e:
                logger.warning("Agent 4: terraform show -json lỗi (%s) — sẽ fallback source scan", e)

    # ── Post-plan processing ───────────────────────────────────────────────────
    # (ra khỏi terraform_workdir context manager — workdir đã được cleanup)
    if plan_passed:
        # ── Security gate: Checkov targeted scan ─────────────────────────────
        # Chỉ chạy sau khi plan PASS (code hợp lệ về mặt Terraform).
        profile = state.get("security_profile") or {}
        target_ids, per_res = _targets_for_plan(profile)
        if not target_ids:
            logger.info("Agent 4: PASS (no security target)")
            return _success_result(_no_checkov)

        try:
            if plan_json_str:
                # Ưu tiên: scan plan JSON — chính xác hơn source scan.
                checkov = run_checkov_on_plan(plan_json_str, check_ids=sorted(target_ids))
                # Fallback nếu plan framework trả rỗng (check không support plan scan)
                if checkov["total_checks"] == 0:
                    logger.info("Agent 4: plan scan trả 0 checks — fallback source scan")
                    checkov = run_checkov_on_hcl(code, check_ids=sorted(target_ids))
            else:
                checkov = run_checkov_on_hcl(code, check_ids=sorted(target_ids))
        except Exception as e:
            logger.warning("Agent 4: Checkov scan lỗi (%s) — bỏ qua security gate", e)
            return _success_result(_no_checkov)

        # Tìm unmet: checks fail VÀ nằm trong target của resource đó
        unmet = _enforceable_unmet(per_res, checkov)
        # Phantom: target nhưng Checkov không evaluate (resource không trigger check).
        # Ví dụ: CKV_AWS_70 (S3 bucket policy) — nếu không có aws_s3_bucket_policy companion
        # thì Checkov SKIP (không pass, không fail) → phantom enforcement.
        evaluated = set(checkov.get("passed_ckv_ids", [])) | set(checkov.get("failed_ckv_ids", []))
        phantom = sorted(target_ids - evaluated)

        if unmet:
            # Kiểm tra budget trước khi retry
            can_retry, reason = check_retry_budget(state, "sec", max_retries=_MAX_SECURITY_RETRY)
            if can_retry:
                # Còn budget → route A3 để fix security (hết retry → fall through bên dưới)
                return _security_return(state, unmet, checkov, phantom)
            else:
                logger.info("Agent 4: PASS (best-effort) — %s; phantom=%d", reason, len(phantom))

        # Reach here: unmet=[] (pass clean) HOẶC unmet có nhưng hết budget (best-effort)
        if unmet:
            logger.info("Agent 4: PASS (best-effort) — %d unmet sau %d retry; phantom=%d",
                        len(unmet), _MAX_SECURITY_RETRY, len(phantom))
        else:
            logger.info("Agent 4: PASS — security enforced ok; phantom=%d", len(phantom))
        # Trả success với unmet (nếu có) → evaluate.py ghi vào val_result["unmet_checks"]
        return _success_result(checkov, unmet, phantom)

    # ── Plan fail handling ─────────────────────────────────────────────────────
    # Infrastructure patterns: network/auth/throttle → không thể fix ở code level
    if _matches(plan_err, _PLAN_INFRA_PATTERNS):
        return _infra_return(state, f"terraform plan failed (infra): {plan_err[:300]}",
                             _no_checkov, True, False, raw_error=plan_err[:2000])

    # MISSING_RESOURCE: pattern-based detection (tất định, không cần LLM)
    # "not found"/"does not exist" = resource type không tồn tại trong AWS provider
    # → A1 cần re-plan với resource type đúng
    if _matches(plan_err, _MISSING_RESOURCE_PATTERNS):
        error_type, root_cause = "MISSING_RESOURCE", "architecture"
        fix_instruction = f"terraform plan: resource not found or unsupported: {plan_err[:300]}"
        sig = _error_signature(error_type, plan_err)
        logger.info("Agent 4: FAIL MISSING_RESOURCE (plan pattern)")
        return _fail_return(state, error_type, root_cause, fix_instruction,
                            _no_checkov, True, False, sig, raw_error=plan_err[:2000])

    # LLM classify: lỗi plan không khớp pattern nào → LLM phán LOGIC hay MISSING_RESOURCE
    # Cung cấp: full error text + resource labels + lịch sử lỗi (tránh lặp lại sai lầm)
    eng_history = (state.get("retries") or {}).get("eng", {}).get("error_history", [])
    ctx = _TOP + PLAN_CONTEXT.format(
        plan_err=plan_err[:1500],
        labels=_hcl_resource_labels(code),
        history=json.dumps(eng_history[-3:]),
    ) + _BOTTOM
    error_type, root_cause, fix_instruction = _llm_classify(
        ctx, {"LOGIC", "MISSING_RESOURCE"}, "LOGIC",
        f"terraform plan failed: {plan_err[:300]}")
    sig = _error_signature(error_type, plan_err)
    logger.info("Agent 4: FAIL %s (plan)", error_type)

    return _fail_return(state, error_type, root_cause, fix_instruction,
                        _no_checkov, True, False, sig, raw_error=plan_err[:2000])


def route_after_validation(state: AgentState) -> str:
    """Conditional edge sau A4 — quyết định node tiếp theo.

    Thứ tự kiểm tra (quan trọng — không được đổi):
      1. overall_passed → deployment (kết thúc nhanh, không kiểm thêm)
      2. total_attempts >= MAX → requires_human (global backstop, ưu tiên cao)
      3. INFRASTRUCTURE → requires_human (terminal, không có code fix)
      4. Oscillation detection → requires_human (phát hiện loop vô hạn)
      5. Budget check → requires_human nếu cạn budget
      6. Route theo root_cause (architecture → A1, engineering → A3)
    """
    # ── Pass: route deployment ─────────────────────────────────────────────────
    if state["fix_feedback"]["overall_passed"]:
        return "deployment"

    error_type = state["fix_feedback"]["error_type"]

    # ── Global backstop: hết total_attempts → dừng mọi routing ──────────────
    # Đây là safety net cuối cùng — ngăn pipeline loop vô hạn dù mọi check khác fail.
    # total_attempts tăng mỗi lần _fail_return/_security_return/_infra_return được gọi.
    if state["total_attempts"] >= _MAX_TOTAL_RETRY:
        logger.info("Route: max total attempts reached (%d >= %d)", state["total_attempts"], _MAX_TOTAL_RETRY)
        return "requires_human"

    # ── INFRASTRUCTURE: không retry (không có code fix) ───────────────────────
    if error_type == "INFRASTRUCTURE":
        logger.info("Route: INFRASTRUCTURE error — requires_human")
        return "requires_human"

    # ── Oscillation detection: phát hiện A3 sửa → A4 fail cùng lỗi → A3 lại sửa ──
    # Nếu 3 lần liên tiếp cùng error signature → A3 không thể fix → dừng.
    if error_type in ("SYNTAX", "LOGIC"):
        if detect_oscillation(state, "eng", error_type):
            logger.info("Route: oscillation detected for eng — requires_human")
            return "requires_human"

    if error_type == "MISSING_RESOURCE":
        if detect_oscillation(state, "arch", error_type):
            logger.info("Route: oscillation detected for arch — requires_human")
            return "requires_human"

    # ── Budget checks: hết retry budget → requires_human ─────────────────────
    # Kiểm MISSING_RESOURCE trước SYNTAX/LOGIC vì cùng bản thân error type,
    # MISSING_RESOURCE có budget nhỏ hơn (2 vs 3).
    if error_type == "MISSING_RESOURCE":
        can_retry, reason = check_retry_budget(state, "arch", max_retries=_MAX_ARCH_RETRY)
        if not can_retry:
            logger.info("Route: %s — requires_human", reason)
            return "requires_human"

    if error_type in ("SYNTAX", "LOGIC"):
        can_retry, reason = check_retry_budget(state, "eng", max_retries=_MAX_ENG_RETRY)
        if not can_retry:
            logger.info("Route: %s — requires_human", reason)
            return "requires_human"

    # ── Normal routing theo root_cause ────────────────────────────────────────
    # root_cause được set bởi _fail_return: "architecture" hoặc "engineering"
    _ROUTE_MAP = {"architecture": "architecture", "engineering": "engineering"}
    root_cause = state["fix_feedback"]["root_cause"]
    if root_cause not in _ROUTE_MAP:
        logger.error("Invalid root_cause '%s' — route requires_human", root_cause)
        return "requires_human"
    return _ROUTE_MAP[root_cause]
