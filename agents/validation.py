"""Agent 4 — Validation: terraform init → validate → plan → Checkov gate.

Error types: SYNTAX (validate) | LOGIC/MISSING_RESOURCE (plan, LLM classify)
| SECURITY (Checkov, best-effort) | INFRASTRUCTURE (timeout/auth/network).
Auth/network/provider setup errors route to human instead of A3.
Checkov scan on plan JSON (terraform show -json) — more accurate than source scan.
"""
import json
import logging
import re
import subprocess
import time
from pathlib import Path

from core.state import AgentState
from core.llm import call_llm
from core.parsers import parse_llm_json, RESOURCE_DECL_RE as _RESOURCE_DECL_RE
from core.catalog import get_check_names
from core.terraform import (
    run_terraform, write_terraform_dir, terraform_workdir,
    run_checkov_on_hcl, run_checkov_on_plan, run_terraform_init, _safe_rmtree,
    required_provider_names, installed_provider_names,
)
from core.retry_control import (
    increment_retry, check_retry_budget,
    MAX_VAL_SEC_RETRY,
)
from core.errors import (
    matches_any, extract_error_facts, recent_fix_instructions,
    MISSING_RESOURCE_PATTERNS, INIT_CONFIG_ERROR_PATTERNS,
    TRANSIENT_PATTERNS, AUTH_PATTERNS,
)
from prompts.validation import (
    SYSTEM_PROMPT as _SYSTEM_PROMPT,
    TOP_PROMPT as _TOP, BOTTOM_PROMPT as _BOTTOM,
    PLAN_CONTEXT,
)

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# TEMPLATES: Output formatting for A3/A1 (moved from prompts/)
# ──────────────────────────────────────────────────────────────────────────────

_VALIDATE_FIX_TEMPLATE = (
    "terraform validate failed — fix ALL errors in ONE revision:\n"
    "{validate_err}"
    "{facts}"
    "{code_ctx}"
)

_SECURITY_FIX_TEMPLATE = (
    "These security checks are not yet satisfied. Fix EACH item following your "
    "hardening rules. Do not change anything unrelated:\n{items}"
)

# ──────────────────────────────────────────────────────────────────────────────
# CONSTANTS: Timeouts, Patterns, Budgets
# ──────────────────────────────────────────────────────────────────────────────

_INIT_TIMEOUT = 300  # init download providers from registry (TF_PLUGIN_CACHE_DIR)
_VALIDATE_TIMEOUT = 60  # validate local syntax check
_PLAN_TIMEOUT = 120  # plan create tfplan.out (may query providers)
_SHOW_TIMEOUT = 15  # show -json read plan file (no network)

_MAX_INIT_TRANSIENT_RETRY = 1
_INIT_RETRY_BACKOFF = 3

_MAX_PLAN_TRANSIENT_RETRY = 2
_PLAN_RETRY_BACKOFF = 3

_VALIDATE_PROVIDER_INIT_PATTERNS = (
    "missing required provider",
    "provider isn't available",
    "you may be able to install it automatically by running",
    "terraform init",
)


_DATA_SOURCE_NOT_FOUND_RE = re.compile(
    r"Error:\s+no matching .+ found.*?with\s+data\.([A-Za-z0-9_]+)\.([A-Za-z0-9_]+),",
    re.IGNORECASE | re.DOTALL,
)


def _is_retryable_plan_error(plan_err: str) -> bool:
    """Return True for plan-time transient errors that may clear on retry."""
    return matches_any(plan_err, TRANSIENT_PATTERNS)


def _classify_plan_error_deterministic(state: AgentState, plan_err: str) -> tuple[str, str, str] | None:
    """Classify plan errors where Terraform gives enough structure without LLM."""
    m = _DATA_SOURCE_NOT_FOUND_RE.search(plan_err)
    if m:
        dtype, dname = m.group(1), m.group(2)
        data_label = f"data.{dtype}.{dname}"
        return (
            "MISSING_RESOURCE",
            "architecture",
            f"{data_label} lookup returned no match in this AWS account. "
            "Update the architecture plan so Terraform does not depend on an absent external object: "
            "create the required dependency as a managed resource when it is part of the request, "
            "or keep it as a data_source only if the user explicitly says it already exists.",
        )
    return None

# Code context extraction for error reporting
_CODE_CONTEXT_WINDOW = 4  # Lines before/after error
_CODE_CONTEXT_MAX_ERRORS = 6  # Max errors to extract
_CODE_CONTEXT_MAX_CHARS = 600  # Truncate failing resource body if too long

# ──────────────────────────────────────────────────────────────────────────────
# SECURITY CATALOG: Checkov target checks
# ──────────────────────────────────────────────────────────────────────────────

_CKV_NAME: dict[str, str] = get_check_names()


def _targets_for_plan(profile: dict) -> tuple[set[str], dict[str, set[str]]]:
    """Parse security_profile from A2 → global check IDs + per-resource IDs.

    Returns:
        (global_ids: set of all CKV IDs to check, per_res: dict[resource_addr → set[CKV_IDs]])
    """
    per_res: dict[str, set[str]] = {}
    global_ids: set[str] = set()
    for addr, info in (profile or {}).items():
        ids = set(info.get("checks", []))
        if ids:
            per_res[addr] = ids
            global_ids.update(ids)
    return global_ids, per_res


# ──────────────────────────────────────────────────────────────────────────────
# HELPER FUNCTIONS: Extract & Format for context
# ──────────────────────────────────────────────────────────────────────────────

def _hcl_resource_labels(code: str) -> list[str]:
    """Extract "type.name" labels from HCL code for LLM context."""
    return [f"{t}.{n}" for t, n in _RESOURCE_DECL_RE.findall(code)]


def _extract_code_context(validate_err: str, code: str, window: int = _CODE_CONTEXT_WINDOW,
                          max_errors: int = _CODE_CONTEXT_MAX_ERRORS) -> str:
    """Extract lines around error, mark with >>>. Get all errors in one pass.

    Args:
        validate_err: terraform validate error message
        code: HCL source code
        window: lines to show before/after error (default _CODE_CONTEXT_WINDOW=4)
        max_errors: maximum errors to extract (default _CODE_CONTEXT_MAX_ERRORS=6)

    Returns: formatted code block with error lines marked with >>> (empty if no errors found)
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
        end = min(len(lines), line_num + window)
        parts = []
        for i, ln in enumerate(lines[start:end], start=start + 1):
            marker = ">>>" if i == line_num else "   "
            parts.append(f"{i:3d} {marker} {ln}")
        blocks.append("\n".join(parts))
    return "\n---\n".join(blocks)


def _extract_failing_resource_body(plan_err: str, code: str) -> str:
    """Extract HCL block of resources mentioned in plan error.

    Max 2 resources, truncated to _CODE_CONTEXT_MAX_CHARS (_CODE_CONTEXT_MAX_CHARS=600 chars).
    Helps LLM understand which resource caused the error.
    """
    error_labels = [f"{t}.{n}" for t, n in _RESOURCE_DECL_RE.findall(plan_err)]
    if not error_labels:
        return ""
    blocks: list[str] = []
    seen: set[str] = set()
    for label in error_labels:
        if label in seen or len(blocks) >= 2:
            break
        seen.add(label)
        dot = label.find(".")
        if dot < 0:
            continue
        rtype, rname = label[:dot], label[dot + 1:]
        opener = re.compile(
            rf'resource\s+"({re.escape(rtype)})"\s+"({re.escape(rname)})"\s*\{{',
            re.MULTILINE,
        )
        m = opener.search(code)
        if not m:
            continue
        start, depth = m.start(), 0
        for i, ch in enumerate(code[start:], start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    blocks.append(code[start : i + 1])
                    break
    if not blocks:
        return ""
    body = "\n\n".join(blocks)
    if len(body) > _CODE_CONTEXT_MAX_CHARS:
        body = body[:_CODE_CONTEXT_MAX_CHARS] + "\n  ... (truncated)"
    return f"CURRENT HCL OF AFFECTED RESOURCE(S):\n{body}\n\n"


def _format_prev_fixes(state: AgentState) -> str:
    """Render previously attempted fixes for LLM context (prevent re-trying same fix).

    Shows last 2 fixes from eng_error_history to ground LLM on what already failed.
    Returns: formatted string or empty string if no history.
    """
    fixes = recent_fix_instructions(state.get("eng_error_history"), max_chars=300)
    if not fixes:
        return ""
    lines = ["PREVIOUSLY ATTEMPTED FIXES (already tried — do NOT repeat):"]
    lines += [f"  {i}. {fix}" for i, fix in enumerate(fixes, 1)]
    return "\n".join(lines) + "\n"


# ──────────────────────────────────────────────────────────────────────────────
# RESULT BUILDERS: success, fail, infra, security
# ──────────────────────────────────────────────────────────────────────────────

def _success_result(checkov: dict, applicable_failed: list | None = None,
                    not_applicable: list | None = None,
                    security_degraded: bool = False) -> dict:
    """Return SUCCESS result. Failed applicable checks don't block (best-effort philosophy)."""
    return {
        "fix_feedback": {
            "overall_passed": True, "error_type": None, "root_cause": None,
            "fix_instruction": None, "checkov": checkov,
            "applicable_failed_checks": [{"resource": a, "ckv_id": i, "name": n} for a, i, n in (applicable_failed or [])],
            "not_applicable_checks": list(not_applicable or []),
            "security_degraded": security_degraded,
            "validate_passed": True, "plan_passed": True,
        },
    }


def _infra_result(state: AgentState, fix_instruction: str, checkov: dict,
                  validate_passed: bool, plan_passed: bool, raw_error: str = "") -> dict:
    """Return INFRASTRUCTURE error (timeout, cache missing, etc). Requires human."""
    state["total_val_attempts"] += 1
    new_total = state["total_val_attempts"]
    logger.warning("Agent 4: INFRASTRUCTURE — %s", fix_instruction[:80])
    return {
        "fix_feedback": {
            "overall_passed": False, "error_type": "INFRASTRUCTURE", "root_cause": None,
            "fix_instruction": fix_instruction, "raw_error": raw_error,
            "checkov": checkov,
            "validate_passed": validate_passed, "plan_passed": plan_passed,
        },
        "retries": state["retries"],
        "total_val_attempts": state["total_val_attempts"],
        "routing_log": state["routing_log"] + [{
            "round": new_total, "error_type": "INFRASTRUCTURE", "root_cause": None,
            "fix_instruction": fix_instruction, "predicted_route": "requires_human",
        }],
    }


def _fail_result(state: AgentState, error_type: str, root_cause: str,
                 fix_instruction: str, checkov: dict, validate_passed: bool,
                 plan_passed: bool, raw_error: str = "") -> dict:
    """Return fixable error (SYNTAX, LOGIC, MISSING_RESOURCE). Route to A3/A1 for fix."""
    assert error_type in ("SYNTAX", "LOGIC", "MISSING_RESOURCE"), \
        f"_fail_result: unexpected error_type '{error_type}'"
    new_total = state["total_val_attempts"] + 1
    is_eng = error_type in ("SYNTAX", "LOGIC")
    is_arch = error_type == "MISSING_RESOURCE"

    if is_eng:
        increment_retry(state, "val_eng", error_type, raw_error[:200])
    elif is_arch:
        increment_retry(state, "val_arch", error_type, raw_error[:200])

    return {
        "fix_feedback": {
            "overall_passed": False, "error_type": error_type, "root_cause": root_cause,
            "fix_instruction": fix_instruction, "raw_error": raw_error,
            "checkov": checkov,
            "validate_passed": validate_passed, "plan_passed": plan_passed,
        },
        "retries": state["retries"],
        "total_val_attempts": state["total_val_attempts"],
        "routing_log": state["routing_log"] + [{
            "round": new_total, "error_type": error_type, "root_cause": root_cause,
            "fix_instruction": fix_instruction, "predicted_route": root_cause,
        }],
    }


def _security_result(state: AgentState, applicable_failed: list[tuple[str, str, str]],
                     checkov: dict, not_applicable: list | None = None,
                     retry_allowed: bool = True) -> dict:
    """Return SECURITY error. Applicable failed checks found, route A3 to fix."""
    new_total = state["total_val_attempts"] + 1
    increment_retry(state, "sec", "SECURITY", str(sorted({cid for _a, cid, _n in applicable_failed})))
    fix_instruction = _SECURITY_FIX_TEMPLATE.format(
        items="\n".join(f"- {addr}: {name}" for addr, _id, name in applicable_failed)
    )
    signature = sorted({cid for _a, cid, _n in applicable_failed})
    logger.warning("Agent 4: FAIL SECURITY — %d applicable failed check(s): %s", len(applicable_failed), signature)
    return {
        "fix_feedback": {
            "overall_passed": False, "error_type": "SECURITY", "root_cause": "engineering",
            "fix_instruction": fix_instruction, "raw_error": "",
            "checkov": checkov,
            "applicable_failed_checks": [{"resource": a, "ckv_id": i, "name": n} for a, i, n in applicable_failed],
            "not_applicable_checks": list(not_applicable or []),
            "retry_allowed": retry_allowed,
            "validate_passed": True, "plan_passed": True,
        },
        "retries": state["retries"],
        "total_val_attempts": state["total_val_attempts"],
        "routing_log": state["routing_log"] + [{
            "round": new_total, "error_type": "SECURITY", "root_cause": "engineering",
            "fix_instruction": fix_instruction, "predicted_route": "engineering",
        }],
    }


def _applicable_failed_checks(per_res: dict[str, set[str]], checkov: dict) -> list[tuple[str, str, str]]:
    """Find checks that both failed (Checkov) AND are targeted (A2 security profile).

    Only applicable failed checks that were explicitly chosen by A2 are actionable.
    Returns: list of (resource_addr, ckv_id, ckv_name) tuples, deduped.
    """
    applicable_failed: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for addr, ckv_id in checkov.get("failed_per_resource", []):
        if ckv_id not in per_res.get(addr, ()):
            continue
        key = (addr, ckv_id)
        if key in seen:
            continue
        seen.add(key)
        applicable_failed.append((addr, ckv_id, _CKV_NAME.get(ckv_id, ckv_id)))
    return applicable_failed


# ──────────────────────────────────────────────────────────────────────────────
# LLM CLASSIFY: Hybrid pattern + LLM for plan errors
# ──────────────────────────────────────────────────────────────────────────────

def _llm_classify(context: str, allowed_types: set,
                  default_type: str, default_fix: str) -> tuple[str, str, str]:
    """LLM classify plan error as LOGIC or MISSING_RESOURCE. Fallback to default if LLM fails."""
    def _root(et: str) -> str:
        return {"MISSING_RESOURCE": "architecture"}.get(et, "engineering")

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": context},
    ]
    try:
        raw = call_llm(messages, agent="validation")
        parsed = parse_llm_json(raw, {"error_type": None, "fix_instruction": None})
    except Exception as e:
        logger.warning("Agent 4 LLM classify error (%s) — using default", e)
        return default_type, _root(default_type), default_fix

    et = parsed.get("error_type")
    if et not in allowed_types:
        et = default_type
    fix = str(parsed.get("fix_instruction") or default_fix)[:1500]
    return et, _root(et), fix


# ──────────────────────────────────────────────────────────────────────────────
# PHASE FUNCTIONS: Terraform init → validate → plan → security
# ──────────────────────────────────────────────────────────────────────────────

def _run_init_phase(state: AgentState, d: str, code: str) -> dict | None:
    """Run terraform init with transient retry + provider cache check.

    Returns: error_result dict if fail, None if success.
    """
    dot_tf = Path(d) / ".terraform"
    lock = Path(d) / ".terraform.lock.hcl"
    missing = required_provider_names(code) - installed_provider_names(dot_tf)
    if dot_tf.exists() and lock.exists() and not missing:
        logger.info("Agent 4: reusing previous init — skip terraform init")
        return None

    if dot_tf.exists():
        reason = f"missing providers {sorted(missing)}" if missing else "incomplete terraform init"
        logger.info("Agent 4: %s — re-init", reason)
        _safe_rmtree(dot_tf)
        if lock.exists():
            lock.unlink()

    init_passed, init_err = False, ""
    for attempt in range(_MAX_INIT_TRANSIENT_RETRY + 1):
        try:
            init = run_terraform_init(d, _INIT_TIMEOUT)
        except subprocess.TimeoutExpired:
            _safe_rmtree(dot_tf)
            return _infra_result(state, f"terraform init timed out (>{_INIT_TIMEOUT}s)", {}, False, False)

        init_passed = init.returncode == 0
        init_err = ((init.stderr or "") + "\n" + (init.stdout or "")).strip()
        if init_passed or not matches_any(init_err, TRANSIENT_PATTERNS):
            break
        if attempt < _MAX_INIT_TRANSIENT_RETRY:
            logger.info("Agent 4: init transient (attempt %d) — retry: %s", attempt + 1, init_err[:120])
            time.sleep(_INIT_RETRY_BACKOFF * (attempt + 1))

    if not init_passed:
        _safe_rmtree(dot_tf)
        if matches_any(init_err, INIT_CONFIG_ERROR_PATTERNS):
            logger.warning("Agent 4: FAIL SYNTAX (init config)")
            return _fail_result(state, "SYNTAX", "engineering",
                f"terraform init failed — fix HCL config (backend/required_providers):\n{init_err[:500]}",
                {}, False, False, raw_error=init_err[:2000])
        else:
            return _infra_result(state, f"terraform init failed: {init_err[:500]}", {}, False, False, raw_error=init_err[:2000])

    return None


def _run_validate_phase(state: AgentState, d: str, code: str) -> dict | None:
    """Run terraform validate with syntax error extraction.

    Returns: error_result dict if fail, None if success.
    """
    try:
        val = run_terraform(["terraform", "validate", "-no-color"], d, _VALIDATE_TIMEOUT)
    except subprocess.TimeoutExpired:
        return _infra_result(state, "terraform validate timed out", {}, False, False)

    if val.returncode != 0:
        logger.warning("Agent 4: FAIL SYNTAX (validate)")
        validate_err = ((val.stderr or "") + "\n" + (val.stdout or "")).strip()
        if matches_any(validate_err, _VALIDATE_PROVIDER_INIT_PATTERNS):
            _safe_rmtree(Path(d) / ".terraform")
            lock = Path(d) / ".terraform.lock.hcl"
            if lock.exists():
                lock.unlink()
            return _infra_result(
                state,
                "terraform validate failed because the provider was not initialized; "
                "A4 will require a clean terraform init before validation.",
                {},
                False,
                False,
                raw_error=validate_err[:2000],
            )
        grounded = extract_error_facts(validate_err)
        code_ctx = _extract_code_context(validate_err, code, window=8, max_errors=4)
        fix = _VALIDATE_FIX_TEMPLATE.format(
            validate_err=validate_err[:1500],
            facts=f"\n\n{grounded}" if grounded else "",
            code_ctx=f"\n\nFAILING CODE IN main.tf (>>> = error line):\n{code_ctx}" if code_ctx else "",
        )
        return _fail_result(state, "SYNTAX", "engineering", fix, {}, False, False, raw_error=validate_err[:2000])

    return None


def _run_plan_phase(state: AgentState, d: str) -> tuple[dict | None, str, str | None]:
    """Run terraform plan with transient retry. Return plan JSON if available.

    Returns: (error_result_or_none, plan_err, plan_json_str)
    """
    plan_passed, plan_err = True, ""
    plan_json_str: str | None = None
    plan_cmd = [
        "terraform", "plan",
        "-no-color",
        "-out=tfplan.out",
        "-input=false",
        "-lock-timeout=30s",
        "-parallelism=10"
    ]

    plan_timeout = int(state.get("terraform_plan_timeout") or _PLAN_TIMEOUT)
    for attempt in range(_MAX_PLAN_TRANSIENT_RETRY + 1):
        try:
            plan = run_terraform(plan_cmd, d, plan_timeout)
        except subprocess.TimeoutExpired:
            return _infra_result(state, f"terraform plan timed out (>{plan_timeout}s)", {}, True, False), "", None

        plan_passed = plan.returncode == 0
        plan_err = ((plan.stderr or "") + "\n" + (plan.stdout or "")).strip()
        if plan_passed or not _is_retryable_plan_error(plan_err):
            break
        if attempt < _MAX_PLAN_TRANSIENT_RETRY:
            logger.info("Agent 4: plan infra/transient (attempt %d) — retry: %s", attempt + 1, plan_err[:120])
            time.sleep(_PLAN_RETRY_BACKOFF * (attempt + 1))

    if not plan_passed and matches_any(plan_err, AUTH_PATTERNS):
        return _infra_result(
            state,
            "terraform plan failed due to AWS/provider infrastructure setup: "
            f"{plan_err[:500]}",
            {},
            True,
            False,
            raw_error=plan_err[:2000],
        ), "", None

    if not plan_passed:
        return None, plan_err, None

    # Read plan JSON (best-effort fallback to HCL scan)
    try:
        show = run_terraform(["terraform", "show", "-json", "tfplan.out"], d, 15)
        if show.returncode == 0 and show.stdout:
            try:
                json.loads(show.stdout)
                plan_json_str = show.stdout
                logger.debug("Agent 4: show -json OK (%d bytes)", len(show.stdout))
            except json.JSONDecodeError as e:
                logger.warning("Agent 4: show -json invalid JSON — fallback HCL scan: %s", e)
        else:
            logger.warning("Agent 4: show -json returncode=%d — fallback HCL scan", show.returncode)
    except subprocess.TimeoutExpired:
        logger.warning("Agent 4: show -json timeout — fallback HCL scan")

    return None, "", plan_json_str


def _run_security_gate(state: AgentState, code: str, plan_json_str: str | None) -> dict | None:
    """Run Checkov security gate on plan/HCL. Route to A3 if unmet checks.

    Returns: result dict (success or fail), or None to continue main flow.
    """
    profile = state.get("security_profile") or {}
    target_ids, per_res = _targets_for_plan(profile)

    # Skip security gate if A2 degraded or no targets
    if not target_ids:
        degraded = state.get("security_status") == "degraded"
        logger.warning("Agent 4: security gate SKIPPED — A2 degraded") if degraded else logger.info("Agent 4: PASS (no security target)")
        result = _success_result({}, security_degraded=degraded)
        if degraded:
            result["routing_log"] = state["routing_log"] + [{
                "round": state["total_val_attempts"],
                "error_type": None, "root_cause": None,
                "fix_instruction": "security gate bypassed — A2 degraded",
                "predicted_route": "deployment",
            }]
        return result

    # Run Checkov scan (plan JSON preferred, fallback to HCL)
    try:
        checkov = run_checkov_on_plan(plan_json_str, check_ids=sorted(target_ids)) if plan_json_str else None
        if not checkov or checkov["total_checks"] == 0:
            logger.debug("Agent 4: plan scan 0 checks — fallback HCL scan")
            checkov = run_checkov_on_hcl(code, check_ids=sorted(target_ids))
    except Exception as e:
        logger.error("Agent 4: Checkov CRASH (%s) — gate degraded", type(e).__name__)
        result = _success_result({}, security_degraded=True)
        result["routing_log"] = state["routing_log"] + [{
            "round": state["total_val_attempts"],
            "error_type": None, "root_cause": None,
            "fix_instruction": f"security gate bypassed — Checkov error ({type(e).__name__})",
            "predicted_route": "deployment",
        }]
        return result

    # Evaluate applicable failed checks (only targeted checks)
    applicable_failed = _applicable_failed_checks(per_res, checkov)
    evaluated = set(checkov.get("passed_ckv_ids", [])) | set(checkov.get("failed_ckv_ids", []))
    not_applicable = sorted(target_ids - evaluated)

    # Route: applicable failed + budget → A3 fix | else → success (best-effort)
    if applicable_failed:
        can_retry, reason = check_retry_budget(state, "sec", max_retries=MAX_VAL_SEC_RETRY)
        if can_retry:
            return _security_result(state, applicable_failed, checkov, not_applicable, retry_allowed=True)
        logger.info("Agent 4: PASS (best-effort) — %s; not_applicable=%d", reason, len(not_applicable))

    logger.info("Agent 4: PASS — security ok; not_applicable=%d", len(not_applicable))
    return _success_result(checkov, applicable_failed, not_applicable)

# ──────────────────────────────────────────────────────────────────────────────
# MAIN LOGIC: validation_node — orchestrate phases
# ──────────────────────────────────────────────────────────────────────────────

def validation_node(state: AgentState) -> dict:
    """LangGraph node: orchestrate init → validate → plan → security gate.

    Flow: Each phase returns early on error, proceeding to next phase only on success.
    """
    code = state["generated_code"]
    _no_checkov = {"passed_count": 0, "failed": []}

    # Guard: empty code = A3 failed. Route A1 to re-plan.
    if not (code or "").strip():
        return _fail_result(
            state, "MISSING_RESOURCE", "architecture",
            "generated_code empty — Engineering agent failed to generate HCL.",
            _no_checkov, False, False,
        )

    run_dir = state.get("run_dir") or ""
    files_dir = (Path(run_dir) / "files") if run_dir else None

    with terraform_workdir(run_dir or None, "tf", reuse=bool(run_dir)) as d:
        write_terraform_dir(d, code, files_dir=files_dir)

        # ── Phase 1: Init ──────────────────────────────────────────────────────
        error_result = _run_init_phase(state, d, code)
        if error_result:
            return error_result

        # ── Phase 2: Validate ──────────────────────────────────────────────────
        error_result = _run_validate_phase(state, d, code)
        if error_result:
            return error_result

        # ── Phase 3: Plan ──────────────────────────────────────────────────────
        error_result, plan_err, plan_json_str = _run_plan_phase(state, d)
        if error_result:
            return error_result

        # Plan succeeded — continue to security gate
        if plan_err:
            # Plan fail with error classification needed
            deterministic = _classify_plan_error_deterministic(state, plan_err)
            if deterministic:
                error_type, root_cause, fix_instruction = deterministic
                logger.warning("Agent 4: FAIL %s (plan deterministic)", error_type)
                return _fail_result(state, error_type, root_cause, fix_instruction,
                                  _no_checkov, True, False, raw_error=plan_err[:2000])

            eng_history = (state.get("retries") or {}).get("val_eng", {}).get("error_history", [])
            prev_fixes_str = _format_prev_fixes(state)
            failing_body = _extract_failing_resource_body(plan_err, code)
            grounded = extract_error_facts(plan_err)
            ctx = _TOP + grounded + PLAN_CONTEXT.format(
                prompt=state.get("prompt", ""),
                plan=json.dumps(state.get("infrastructure_plan") or {}, ensure_ascii=False),
                plan_err=plan_err[:1500],
                labels=_hcl_resource_labels(code),
                failing_resource_body=failing_body,
                history=json.dumps(eng_history[-3:]),
                prev_fixes=prev_fixes_str,
            ) + _BOTTOM
            error_type, root_cause, fix_instruction = _llm_classify(
                ctx, {"SYNTAX", "LOGIC", "MISSING_RESOURCE"}, "LOGIC",
                f"terraform plan failed: {plan_err[:300]}")
            logger.warning("Agent 4: FAIL %s (plan)", error_type)
            return _fail_result(state, error_type, root_cause, fix_instruction,
                              _no_checkov, True, False, raw_error=plan_err[:2000])

        # ── Phase 4: Security Gate ────────────────────────────────────────────
        result = _run_security_gate(state, code, plan_json_str)
        return result


# ──────────────────────────────────────────────────────────────────────────────
# ROUTING: route_after_validation
# ──────────────────────────────────────────────────────────────────────────────

def route_after_validation(state: AgentState) -> str:
    """Conditional edge after A4 — determine next node."""
    fb = state.get("fix_feedback") or {}
    if fb.get("overall_passed"):
        return "deployment"
    if state.get("total_val_attempts", 0) >= 5:
        return "requires_human"
    if fb.get("error_type") == "INFRASTRUCTURE":
        return "requires_human"
    if fb.get("error_type") == "SECURITY":
        return "engineering" if fb.get("retry_allowed") else "deployment"

    rc = fb.get("root_cause")
    if rc not in ("engineering", "architecture"):
        return "requires_human"

    can_retry, _ = check_retry_budget(state, "val_eng" if rc == "engineering" else "val_arch")
    if not can_retry:
        return "requires_human"

    return "engineering" if rc == "engineering" else "architecture"
