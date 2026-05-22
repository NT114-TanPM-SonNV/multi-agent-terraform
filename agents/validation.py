"""Validation Agent — Agent 4 trong pipeline.

Kiểm tra generated_code: terraform validate (static) → Checkov + terraform plan
(khi Floci reachable). Nếu pass → route Agent 5. Nếu fail → phân loại lỗi
(error_type/root_cause) + sinh fix_instruction, route về đúng agent để retry.

Phân loại hybrid (giảm phụ thuộc LLM, tăng độ tin cậy):
  - terraform validate fail → SYNTAX/engineering (TẤT ĐỊNH, fix = lỗi terraform).
    Bỏ qua Checkov/plan vì HCL hỏng thì chạy tiếp vô nghĩa.
  - terraform plan fail vì Floci (unsupported / data source rỗng / timeout) → INFRA
    (KHÔNG route về agent — không phải lỗi code).
  - terraform plan fail vì code → LLM phân loại LOGIC (engineering) / MISSING_RESOURCE (arch).
  - Checkov required check fail → LLM phân loại SECURITY (engineering) / WRONG_CONSTRAINT (security).

Output: validation_result + cập nhật counters/error_history/routing_log.
LangGraph pattern: RETURN dict update, không mutation trực tiếp state.
"""
import json
import logging
import re
import subprocess
import tempfile
from pathlib import Path

from core.state import AgentState
from core.llm import call_llm
from core.parsers import parse_llm_json
from core.terraform import run_terraform, substitute_endpoint, run_checkov_on_hcl
from prompts.validation import SYSTEM_PROMPT as _SYSTEM_PROMPT
from prompts.validation import TOP_PROMPT as _TOP, BOTTOM_PROMPT as _BOTTOM

logger = logging.getLogger(__name__)

_INIT_TIMEOUT = 300       # cold cache lần đầu có thể tải provider
_VALIDATE_TIMEOUT = 60

# Lỗi từ MÔI TRƯỜNG Floci (mock) chứ không phải lỗi code → INFRA → requires_human.
# Gồm cả data source không tìm thấy (Floci community không có AMI mẫu...) và lỗi kết nối.
_FLOCI_ERR_PATTERNS = (
    "unsupportedoperation", "not supported", "not implemented", "notimplemented",
    "501", "could not connect", "connection refused", "connection reset",
    "your query returned no results", "returned no results", "no matching",
    "querying ec2 for ami",
)
_VALID_ROOT_CAUSES = {"architecture", "security", "engineering"}


def _is_floci_error(text: str) -> bool:
    low = (text or "").lower()
    return any(p in low for p in _FLOCI_ERR_PATTERNS)


def _extract_code_context(validate_err: str, code: str, window: int = 4) -> str:
    """Trích dòng code xung quanh vị trí lỗi từ terraform validate stderr.

    Giúp Agent 3 thấy chính xác đoạn code cần sửa thay vì chỉ nhận line number.
    """
    m = re.search(r"on main\.tf line (\d+)", validate_err)
    if not m:
        return ""
    line_num = int(m.group(1))
    lines = code.split("\n")
    start = max(0, line_num - window - 1)
    end   = min(len(lines), line_num + window)
    parts = []
    for i, ln in enumerate(lines[start:end], start=start + 1):
        marker = ">>>" if i == line_num else "   "
        parts.append(f"{i:3d} {marker} {ln}")
    return "\n".join(parts)


def _extract_resource_block(hcl: str, res_type: str, res_name: str) -> str | None:
    """Trích nội dung block `resource "type" "name" { ... }` bằng bracket matching.

    Trả None nếu không tìm thấy resource label đó trong HCL.
    """
    pattern = re.compile(
        rf'resource\s+"{re.escape(res_type)}"\s+"{re.escape(res_name)}"\s*\{{',
        re.MULTILINE,
    )
    m = pattern.search(hcl)
    if not m:
        return None
    open_idx = m.end() - 1
    depth, end_idx = 0, None
    for i in range(open_idx, len(hcl)):
        if hcl[i] == "{":
            depth += 1
        elif hcl[i] == "}":
            depth -= 1
            if depth == 0:
                end_idx = i + 1
                break
    return hcl[open_idx:end_idx] if end_idx else hcl[open_idx:]


def _check_injected_attrs(code: str, constraints: dict) -> list[tuple[str, str, any]]:
    """Kiểm tra attrs A2 đã inject có xuất hiện trong đúng resource block không.

    Tìm trong block cụ thể (bracket matching) thay vì toàn file — tránh false positive
    khi attr trùng tên xuất hiện ở resource khác.
    Trả list (resource_label, attr, expected_value) cho attrs bị thiếu.
    """
    missing = []
    for resource_label, attrs in constraints.items():
        parts = resource_label.split(".", 1)
        if len(parts) != 2:
            continue
        res_type, res_name = parts
        block = _extract_resource_block(code, res_type, res_name)
        if block is None:
            for attr, val in attrs.items():
                missing.append((resource_label, attr, val))
            continue
        for attr, val in attrs.items():
            if not re.search(rf'^\s*{re.escape(attr)}\s*=', block, re.MULTILINE):
                missing.append((resource_label, attr, val))
    return missing


def _error_signature(error_type: str, validate_err: str, plan_err: str,
                     failed_security: list) -> list:
    """Signature cho error_history (dùng để phát hiện repeat/oscillation).

    Security: dùng tập "resource_label.attr" thiếu. Syntax/logic: dùng các dòng
    'Error: ...' đã chuẩn hoá để 2 lần CÙNG lỗi khớp nhau, 2 lỗi KHÁC nhau
    (đang tiến triển) không bị nhầm là 'exact repeat'.
    """
    if error_type in ("SECURITY", "WRONG_CONSTRAINT"):
        return sorted(f"{lbl}.{attr}" for lbl, attr, _ in failed_security)
    text = validate_err if error_type == "SYNTAX" else plan_err
    errs = re.findall(r"Error:\s*(.+)", text or "")
    sig = sorted({e.strip()[:80] for e in errs})
    return sig or [((text or "").strip()[:80] or error_type)]


def _success_result(checkov: dict) -> dict:
    return {
        "validation_result": {
            "overall_passed": True, "error_type": None, "root_cause": None,
            "fix_instruction": None, "checkov": checkov,
            "validate_passed": True, "plan_passed": True,
        }
    }


def _infra_return(state: AgentState, fix_instruction: str, checkov: dict,
                  validate_passed: bool, plan_passed: bool) -> dict:
    """INFRA: lỗi môi trường (Floci/timeout/init). Không tăng per-loop counter."""
    new_total = state["total_retry_count"] + 1
    logger.info("Agent 4: INFRA — %s", fix_instruction[:80])
    return {
        "validation_result": {
            "overall_passed": False, "error_type": "INFRA", "root_cause": None,
            "fix_instruction": fix_instruction, "checkov": checkov,
            "validate_passed": validate_passed, "plan_passed": plan_passed,
        },
        "total_retry_count": new_total,
        "error_history": state["error_history"] + [{
            "round": new_total, "error_type": "INFRA", "failed_checks": [],
        }],
        "routing_log": state["routing_log"] + [{
            "round": new_total, "error_type": "INFRA", "root_cause": None,
            "fix_instruction": fix_instruction, "predicted_route": "requires_human",
        }],
    }


def _fail_return(state: AgentState, error_type: str, root_cause: str,
                 fix_instruction: str, checkov: dict, validate_passed: bool,
                 plan_passed: bool, signature: list) -> dict:
    new_total = state["total_retry_count"] + 1
    return {
        "validation_result": {
            "overall_passed": False, "error_type": error_type, "root_cause": root_cause,
            "fix_instruction": fix_instruction, "checkov": checkov,
            "validate_passed": validate_passed, "plan_passed": plan_passed,
        },
        "total_retry_count": new_total,
        "eng_retry_count":  state["eng_retry_count"]  + (1 if error_type in ("SECURITY", "SYNTAX", "LOGIC") else 0),
        "arch_retry_count": state["arch_retry_count"] + (1 if error_type == "MISSING_RESOURCE" else 0),
        "sec_retry_count":  state["sec_retry_count"]  + (1 if error_type == "WRONG_CONSTRAINT" else 0),
        "error_history": state["error_history"] + [{
            "round": new_total, "error_type": error_type, "failed_checks": signature,
        }],
        "routing_log": state["routing_log"] + [{
            "round": new_total, "error_type": error_type, "root_cause": root_cause,
            "fix_instruction": fix_instruction, "predicted_route": root_cause,
        }],
    }


def _llm_classify(context: str, allowed_types: set,
                  default_type: str, default_fix: str) -> tuple[str, str, str]:
    """LLM phân loại error_type + fix_instruction. Root_cause suy ra TẤT ĐỊNH từ
    error_type (mapping bắt buộc). Fallback an toàn nếu LLM lỗi/sai spec."""
    def _root(et: str) -> str:
        return {"MISSING_RESOURCE": "architecture", "WRONG_CONSTRAINT": "security"}.get(et, "engineering")

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": context},
    ]
    try:
        raw = call_llm(messages)
        parsed = parse_llm_json(raw, {
            "error_type": None, "root_cause": None, "fix_instruction": None,
        })
    except Exception as e:
        logger.warning("Agent 4 LLM classify lỗi (%s) — dùng default", e)
        return default_type, _root(default_type), default_fix

    et = parsed.get("error_type")
    if et not in allowed_types:
        et = default_type
    fix = str(parsed.get("fix_instruction") or default_fix)[:500]
    return et, _root(et), fix


def validation_node(state: AgentState) -> dict:
    """LangGraph node function cho Validation Agent (Agent 4)."""
    code = state["generated_code"]
    constraints  = state.get("security_constraints") or {}
    # ckv_ids_map: {"type.name": {"attr": "CKV_AWS_17"}} — chỉ attrs có CKV ID
    ckv_ids_map  = state.get("security_ckv_ids") or {}
    # Tập CKV IDs duy nhất để truyền vào checkov --check
    all_ckv_ids  = sorted({ckv for res in ckv_ids_map.values() for ckv in res.values()})
    # Tập attrs đã được Checkov cover — không cần text check
    ckv_covered  = {lbl: set(attr_ckv.keys()) for lbl, attr_ckv in ckv_ids_map.items()}
    plan_obj = state.get("infrastructure_plan") or {}
    plan_timeout = state.get("terraform_plan_timeout", 120)
    endpoint = state.get("floci_endpoint")
    _no_checkov = {"passed_count": 0, "failed": []}

    with tempfile.TemporaryDirectory() as d:
        tf_code = substitute_endpoint(code, endpoint) if endpoint else code
        (Path(d) / "main.tf").write_text(tf_code, encoding="utf-8")

        # ── terraform init ─────────────────────────────────────────────────────
        try:
            init = run_terraform(["terraform", "init", "-no-color"], d, _INIT_TIMEOUT)
        except subprocess.TimeoutExpired:
            return _infra_return(state, f"terraform init timed out (>{_INIT_TIMEOUT}s)", _no_checkov, False, False)
        if init.returncode != 0:
            return _infra_return(state, f"terraform init failed: {init.stderr[:500]}", _no_checkov, False, False)

        # ── terraform validate ─────────────────────────────────────────────────
        try:
            val = run_terraform(["terraform", "validate", "-no-color"], d, _VALIDATE_TIMEOUT)
        except subprocess.TimeoutExpired:
            return _infra_return(state, "terraform validate timed out", _no_checkov, False, False)

        if val.returncode != 0:
            validate_err = (val.stderr or val.stdout or "").strip()
            sig = _error_signature("SYNTAX", validate_err, "", [])
            logger.info("Agent 4: FAIL SYNTAX (validate)")
            code_ctx = _extract_code_context(validate_err, code)
            # Dùng LLM để sinh fix cụ thể: nói ĐÚNG phải viết gì thay thế,
            # không chỉ paste lại error message (Agent 3 không tự suy ra được).
            syntax_ctx = (
                _TOP
                + f"TERRAFORM VALIDATE FAILED:\n{validate_err[:600]}\n\n"
                + (f"FAILING CODE CONTEXT:\n{code_ctx}\n\n" if code_ctx else "")
                + f"RESOURCE TYPES IN PLAN: "
                  f"{sorted({r['type'] for r in plan_obj.get('resources', [])})}\n"
                + f"ERROR HISTORY: {json.dumps(state['error_history'][-2:])}"
                + _BOTTOM
            )
            _, _, fix = _llm_classify(
                syntax_ctx, {"SYNTAX"}, "SYNTAX",
                f"terraform validate failed — fix the HCL: {validate_err[:300]}"
            )
            return _fail_return(
                state, "SYNTAX", "engineering", fix,
                _no_checkov, False, False, sig)

        # ── Validate passed → Security check (static, trước plan) ────────────────
        # Lớp 1: Checkov --check <ckv_ids> — chỉ rules A2 chỉ định, không false positive
        ck_failed_ids: list[str] = []
        if all_ckv_ids:
            try:
                ck = run_checkov_on_hcl(code, timeout=60, check_ids=all_ckv_ids)
                ck_failed_ids = ck["failed_ckv_ids"]
                checkov = {"passed_count": ck["passed_count"], "failed": sorted(ck_failed_ids)}
            except RuntimeError as e:
                logger.warning("Checkov không chạy được (%s) — fallback sang attr check", e)
                checkov = dict(_no_checkov)
        else:
            checkov = dict(_no_checkov)

        # Lớp 2: text check trong resource block — attrs A2 không có CKV ID
        constraints_no_ckv = {
            lbl: {attr: val for attr, val in attrs.items()
                  if attr not in ckv_covered.get(lbl, set())}
            for lbl, attrs in constraints.items()
        }
        constraints_no_ckv = {k: v for k, v in constraints_no_ckv.items() if v}
        missing_attrs = _check_injected_attrs(code, constraints_no_ckv)

        security_ok = not ck_failed_ids and not missing_attrs
        if not security_ok:
            # Security fail → return ngay, không cần chạy plan
            missing_desc = [
                f"{lbl}: `{attr} = {json.dumps(val)}`"
                for lbl, attr, val in missing_attrs
            ]
            ctx = (
                _TOP
                + "TERRAFORM VALIDATE: passed\n\n"
                + (f"CHECKOV FAILED (A2 rules): {sorted(ck_failed_ids)}\n\n"
                   if ck_failed_ids else "")
                + ("SECURITY ATTRS MISSING FROM HCL (no CKV ID):\n"
                   + "\n".join(missing_desc) + "\n\n" if missing_desc else "")
                + f"SECURITY CONSTRAINTS:\n{json.dumps(constraints, indent=2)}\n\n"
                + f"ERROR HISTORY: {json.dumps(state['error_history'][-3:])}"
                + _BOTTOM
            )
            all_missing = missing_attrs + [(None, ckv, None) for ckv in ck_failed_ids]
            error_type, root_cause, fix_instruction = _llm_classify(
                ctx, {"SECURITY", "WRONG_CONSTRAINT"}, "SECURITY",
                f"Fix security: checkov={sorted(ck_failed_ids)}, missing={missing_desc}")
            sig = _error_signature(error_type, "", "", all_missing)
            logger.info("Agent 4: FAIL %s (checkov=%s, missing=%s)",
                        error_type, ck_failed_ids, [(lbl, attr) for lbl, attr, _ in missing_attrs])
            return _fail_return(state, error_type, root_cause, fix_instruction,
                                checkov, True, True, sig)

        # ── Security passed → terraform plan ───────────────────────────────────
        plan_passed, plan_err = True, ""
        try:
            plan = run_terraform(["terraform", "plan", "-no-color"], d, plan_timeout)
        except subprocess.TimeoutExpired:
            return _infra_return(state, f"terraform plan timed out (>{plan_timeout}s)", checkov, True, False)
        plan_passed = plan.returncode == 0
        plan_err = (plan.stderr or plan.stdout or "").strip()
        if not plan_passed and _is_floci_error(plan_err):
            return _infra_return(state, f"Floci/connection error khi plan: {plan_err[:300]}", checkov, True, False)

    # ── overall_passed ──────────────────────────────────────────────────────────
    if plan_passed:
        logger.info("Agent 4: PASS")
        return _success_result(checkov)

    # ── Plan failed → phân loại LOGIC / MISSING_RESOURCE ─────────────────────
    res_labels = [f"{r['type']}.{r['name']}" for r in plan_obj.get("resources", [])]
    ctx = (
        _TOP
        + f"TERRAFORM VALIDATE: passed\nTERRAFORM PLAN: FAILED\n{plan_err[:1500]}\n\n"
        + f"INFRASTRUCTURE PLAN resources: {res_labels}\n"
        + f"ERROR HISTORY: {json.dumps(state['error_history'][-3:])}"
        + _BOTTOM
    )
    error_type, root_cause, fix_instruction = _llm_classify(
        ctx, {"LOGIC", "MISSING_RESOURCE"}, "LOGIC",
        f"terraform plan failed: {plan_err[:300]}")
    sig = _error_signature(error_type, "", plan_err, [])
    logger.info("Agent 4: FAIL %s (plan)", error_type)

    return _fail_return(state, error_type, root_cause, fix_instruction,
                        checkov, True, False, sig)


def route_after_validation(state: AgentState) -> str:
    """Conditional edge: node kế tiếp sau Agent 4. KHÔNG ghi state."""
    if state["validation_result"]["overall_passed"]:
        return "agent5"

    history = state["error_history"]
    if len(history) >= 2 and history[-1]["failed_checks"] == history[-2]["failed_checks"]:
        return "requires_human"
    if len(history) >= 3:
        current = history[-1]["failed_checks"]
        if any(h["failed_checks"] == current for h in history[:-1]):
            return "requires_human"
    if state["total_retry_count"] >= 5:
        return "requires_human"

    error_type = state["validation_result"]["error_type"]
    if error_type == "INFRA":
        return "requires_human"
    if error_type == "MISSING_RESOURCE" and state["arch_retry_count"] >= 2:
        return "requires_human"
    if error_type == "WRONG_CONSTRAINT" and state["sec_retry_count"] >= 2:
        return "requires_human"
    if error_type in ("SECURITY", "SYNTAX", "LOGIC") and state["eng_retry_count"] >= 3:
        return "requires_human"

    _ROUTE_MAP = {"architecture": "architecture", "security": "security", "engineering": "engineering"}
    root_cause = state["validation_result"]["root_cause"]
    if root_cause not in _ROUTE_MAP:
        logger.error("Invalid root_cause '%s' — route requires_human", root_cause)
        return "requires_human"
    return _ROUTE_MAP[root_cause]
