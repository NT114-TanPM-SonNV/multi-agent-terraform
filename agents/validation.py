"""Agent 4 — Validation: terraform init → validate → plan → Checkov gate.

Error types: SYNTAX (validate) | LOGIC/MISSING_RESOURCE (plan, LLM classify)
| SECURITY (Checkov, best-effort) | INFRASTRUCTURE (timeout/auth/network).
Auth/network/provider setup errors route to human instead of A3.
Checkov scan on plan JSON (terraform show -json) — more accurate than source scan.
"""
import json
import logging
import subprocess
import time
from pathlib import Path

from core.state import AgentState
from core.llm import call_llm
from core.parsers import parse_llm_json
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
    matches_any,
    INIT_CONFIG_ERROR_PATTERNS,
    TRANSIENT_PATTERNS, AUTH_CREDENTIAL_PATTERNS, PROVIDER_SETUP_PATTERNS,
)
from prompts.validation import (
    SYSTEM_PROMPT as _SYSTEM_PROMPT,
    TOP_PROMPT as _TOP, BOTTOM_PROMPT as _BOTTOM,
    PLAN_CONTEXT, VALIDATE_FIX_TEMPLATE as _VALIDATE_FIX_TEMPLATE,
    SECURITY_FIX_TEMPLATE as _SECURITY_FIX_TEMPLATE,
)

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# CONSTANTS: Timeouts, Patterns, Budgets
# ──────────────────────────────────────────────────────────────────────────────

_INIT_TIMEOUT = 300  
_VALIDATE_TIMEOUT = 60  
_PLAN_TIMEOUT = 120  
_SHOW_TIMEOUT = 30  

_MAX_INIT_TRANSIENT_RETRY = 1
_INIT_RETRY_BACKOFF = 3

_MAX_PLAN_TRANSIENT_RETRY = 1
_PLAN_RETRY_BACKOFF = 3

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
    """LLM classify plan error. Fallback to UNKNOWN-safe default if LLM fails."""
    def _root(et: str) -> str:
        return {"MISSING_RESOURCE": "architecture", "UNKNOWN": "requires_human"}.get(et, "engineering")

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
    if et == "UNKNOWN":
        return "UNKNOWN", "requires_human", str(parsed.get("fix_instruction") or default_fix)[:1500]
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
        return _infra_result(state, f"terraform init failed: {init_err[:500]}", {}, False, False, raw_error=init_err[:2000])

    return None


def _run_validate_phase(state: AgentState, d: str) -> dict | None:
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
        fix = _VALIDATE_FIX_TEMPLATE.format(
            validate_err=validate_err[:1500],
            facts="",
            code_ctx="",
        )
        return _fail_result(state, "SYNTAX", "engineering", fix, {}, False, False, raw_error=validate_err[:2000])

    return None


def _run_plan_phase(state: AgentState, d: str) -> tuple[dict | None, str, str | None]:
    """Run terraform plan with transient retry. Return plan JSON if available.

    Returns: (error_result_or_none, plan_err, plan_json_str)
    """
    plan_passed, plan_err = True, ""
    plan_json_str: str | None = None
    plan_cmd = ["terraform", "plan", "-no-color", "-out=tfplan.out", "-input=false", "-lock-timeout=30s", "-parallelism=10"]

    plan_timeout = _PLAN_TIMEOUT
    for attempt in range(_MAX_PLAN_TRANSIENT_RETRY + 1):
        try:
            plan = run_terraform(plan_cmd, d, plan_timeout)
        except subprocess.TimeoutExpired:
            return _infra_result(state, f"terraform plan timed out (>{plan_timeout}s)", {}, True, False), "", None

        plan_passed = plan.returncode == 0
        plan_err = ((plan.stderr or "") + "\n" + (plan.stdout or "")).strip()
        if plan_passed or not matches_any(plan_err, TRANSIENT_PATTERNS):
            break
        if attempt < _MAX_PLAN_TRANSIENT_RETRY:
            logger.info("Agent 4: plan infra/transient (attempt %d) — retry: %s", attempt + 1, plan_err[:120])
            time.sleep(_PLAN_RETRY_BACKOFF * (attempt + 1))

    if not plan_passed and matches_any(plan_err, AUTH_CREDENTIAL_PATTERNS):
        return _infra_result(
            state,
            "terraform plan failed due to AWS credential or permission setup: "
            f"{plan_err[:500]}",
            {},
            True,
            False,
            raw_error=plan_err[:2000],
        ), "", None

    if not plan_passed and matches_any(plan_err, PROVIDER_SETUP_PATTERNS):
        return _infra_result(
            state,
            "terraform plan failed due to AWS provider/plugin setup: "
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
        show = run_terraform(["terraform", "show", "-json", "tfplan.out"], d, _SHOW_TIMEOUT)
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
        logger.warning("Agent 4: security gate SKIPPED — A2 degraded") if degraded else logger.info("Agent 4: security gate SKIPPED — no security target")
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
        return _infra_result(
            state,
            f"security gate failed — Checkov crash ({type(e).__name__})",
            {},
            True,
            True,
            raw_error=type(e).__name__,
        )

    # Evaluate applicable failed checks (only targeted checks)
    applicable_failed = _applicable_failed_checks(per_res, checkov)
    evaluated = set(checkov.get("passed_ckv_ids", [])) | set(checkov.get("failed_ckv_ids", []))
    not_applicable = sorted(target_ids - evaluated)

    # Route: applicable failed + budget → A3 fix | else → best-effort pass
    if applicable_failed:
        can_retry, reason = check_retry_budget(state, "sec", max_retries=MAX_VAL_SEC_RETRY)
        if can_retry:
            return _security_result(state, applicable_failed, checkov, not_applicable, retry_allowed=True)
        logger.info("Agent 4: security gate BEST-EFFORT — %s; not_applicable=%d", reason, len(not_applicable))

    logger.info("Agent 4: security gate BEST-EFFORT — security ok; not_applicable=%d", len(not_applicable))
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

    run_dir = state.get("run_dir") or ""
    files_dir = (Path(run_dir) / "files") if run_dir else None

    with terraform_workdir(run_dir or None, "tf", reuse=bool(run_dir)) as d:
        write_terraform_dir(d, code, files_dir=files_dir)

        # ── Phase 1: Init ──────────────────────────────────────────────────────
        error_result = _run_init_phase(state, d, code)
        if error_result:
            return error_result

        # ── Phase 2: Validate ──────────────────────────────────────────────────
        error_result = _run_validate_phase(state, d)
        if error_result:
            return error_result

        # ── Phase 3: Plan ──────────────────────────────────────────────────────
        error_result, plan_err, plan_json_str = _run_plan_phase(state, d)
        if error_result:
            return error_result

        # Plan succeeded — continue to security gate
        if plan_err:
            # Plan fail with error classification needed
            ctx = _TOP + PLAN_CONTEXT.format(
                prompt=state.get("prompt", ""),
                plan=json.dumps(state.get("infrastructure_plan") or {}, ensure_ascii=False),
                plan_err=plan_err[:1500],
            ) + _BOTTOM
            error_type, root_cause, fix_instruction = _llm_classify(
                ctx, {"SYNTAX", "LOGIC", "MISSING_RESOURCE", "UNKNOWN"}, "UNKNOWN",
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
