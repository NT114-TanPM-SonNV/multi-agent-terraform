"""Agent 5 — Deployment: terraform apply lên AWS.

Fail → cleanup partial state → phân loại → route.
Timeout / auth / provider → INFRASTRUCTURE → requires_human.
Transient → retry in-node (tối đa _MAX_APPLY_TRANSIENT_RETRY lần).
LOGIC → A3. MISSING_RESOURCE → A1. Auto-destroy sau apply trong eval mode.
"""
import json
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from core.state import AgentState
from core.llm import call_llm
from core.parsers import parse_llm_json
from core.terraform import run_terraform, write_terraform_dir, terraform_workdir
from core.retry_control import (
    increment_retry, check_retry_budget,
    MAX_DEPLOY_TOTAL_RETRY, MAX_DEPLOY_ENG_RETRY, MAX_DEPLOY_ARCH_RETRY,
)
from core.errors import (
    matches_any,
    AUTH_CREDENTIAL_PATTERNS,
    PROVIDER_SETUP_PATTERNS,
    TRANSIENT_PATTERNS,
)
from core.destroy import destroy_with_override, enqueue_destroy, _DESTROY_TIMEOUT
from prompts.deployment import SYSTEM_PROMPT as _SYSTEM_PROMPT
from prompts.deployment import CLASSIFY_TEMPLATE

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ──────────────────────────────────────────────────────────────────────────────

# Apply timeout >= destroy: RDS/ElastiCache mất 5-15 phút để tạo. 360s cũ →
# SIGKILL giữa chừng → state corrupt + leaked resource.
_APPLY_TIMEOUT = int(os.environ.get("TF_APPLY_TIMEOUT", "600"))
_STATE_TIMEOUT = 30
_MAX_APPLY_TRANSIENT_RETRY = 1
_APPLY_RETRY_BACKOFF = 5

# ──────────────────────────────────────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────────────────────────────────────

def _extract_error(stdout: str, stderr: str) -> str:
    """stderr + tail stdout, với section Error lines để LLM focus."""
    stderr_clean = (stderr or "").strip()
    stdout_tail = (stdout or "")[-2000:]
    combined = (stderr_clean + "\n" + stdout_tail).strip()
    error_lines = [ln for ln in combined.splitlines() if re.match(r"\s*(?:Error|error):", ln)]
    if error_lines:
        return combined + "\n\n--- Error lines ---\n" + "\n".join(error_lines[-20:])
    return combined


def _state_resources(tmpdir: str) -> list:
    """List resources trong terraform state — kiểm tra partial apply."""
    try:
        r = run_terraform(["terraform", "state", "list"], tmpdir, _STATE_TIMEOUT)
    except subprocess.TimeoutExpired:
        return []
    if r.returncode != 0:
        return []
    return [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]


# ──────────────────────────────────────────────────────────────────────────────
# LLM CLASSIFY
# ──────────────────────────────────────────────────────────────────────────────

def _llm_classify(error_text: str, resource_labels: list[str],
                  failed_resource: str | None, partial: bool,
                  destroyed: bool, retry: int) -> tuple[str, str | None]:
    """LLM phân loại apply error. Fallback UNKNOWN nếu LLM fail."""
    ctx = CLASSIFY_TEMPLATE.format(
        labels=json.dumps(resource_labels),
        failed=failed_resource or "unknown",
        error=error_text[:2000],
        partial=partial, destroyed=destroyed, retry=retry,
    )
    try:
        parsed = parse_llm_json(
            call_llm([{"role": "system", "content": _SYSTEM_PROMPT},
                      {"role": "user", "content": ctx}], agent="deployment"),
            {"error_type": None, "fix_instruction": None},
        )
    except Exception as e:
        logger.warning("Agent 5 LLM classify error (%s) — UNKNOWN", e)
        return "UNKNOWN", None

    et = parsed.get("error_type")
    if et not in ("LOGIC", "MISSING_RESOURCE", "UNKNOWN"):
        et = "UNKNOWN"
    fix = parsed.get("fix_instruction") if et in ("LOGIC", "MISSING_RESOURCE") else None
    return et, (str(fix)[:500] if fix else None)  # UNKNOWN → fix=None


# ──────────────────────────────────────────────────────────────────────────────
# PHASE FUNCTIONS
# ──────────────────────────────────────────────────────────────────────────────

def _handle_failure(state: AgentState, tmpdir: str,
                    apply_stdout: str, apply_stderr: str,
                    is_timeout: bool) -> dict:
    """Classify apply error, cleanup partial state, return routable result."""
    error_text = _extract_error(apply_stdout, apply_stderr)
    created = _state_resources(tmpdir)

    error_type = None
    if is_timeout or matches_any(error_text, AUTH_CREDENTIAL_PATTERNS) or matches_any(error_text, PROVIDER_SETUP_PATTERNS):
        error_type = "INFRASTRUCTURE"

    destroy_ok, destroy_err = destroy_with_override(
        tmpdir, state.get("generated_code", ""), timeout=_DESTROY_TIMEOUT,
    )
    partial_destroyed = destroy_ok
    destroy_failed = not destroy_ok and destroy_err is not None

    fix = None
    if error_type is None:
        plan = state.get("infrastructure_plan") or {}
        labels = [f"{r['type']}.{r['name']}" for r in plan.get("resources", [])]
        error_type, fix = _llm_classify(
            error_text, labels, None, bool(created), partial_destroyed,
            state["retries"].get("deploy_eng", {}).get("count", 0),
        )

    logger.warning(
        "Agent 5: FAIL %s (partial=%s destroyed=%s destroy_failed=%s)",
        error_type, bool(created), partial_destroyed, destroy_failed,
    )

    retry_config = {
        "LOGIC": ("deploy_eng", "engineering"),
        "MISSING_RESOURCE": ("deploy_arch", "architecture"),
    }

    root_cause = None
    predicted_route = "requires_human"
    fix_feedback = None

    if error_type in retry_config:
        retry_key, cause = retry_config[error_type]
        increment_retry(state, retry_key, error_type, error_text[:200])
        root_cause = cause
        predicted_route = cause
        fix_feedback = {
            "overall_passed": False, "error_type": error_type, "root_cause": cause,
            "fix_instruction": fix, "checkov": {"passed_count": 0, "failed": []},
            "validate_passed": True, "plan_passed": True,
        }

    result: dict = {
        "deployment_result": {
            "success": False,
            "error_type": error_type,
            "error_label": error_type,
            "resources_created": created,
            "partial_apply_destroyed": partial_destroyed,
            "destroy_failed": destroy_failed,
            "cleanup_error_label": "DESTROY_FAILED" if destroy_failed else None,
            "destroy_error": destroy_err,
            "fix_instruction": fix,
            "apply_raw_error": error_text[:3000],
        },
        "retries": state["retries"],
        "total_val_attempts": state["total_val_attempts"],
        "total_deploy_attempts": state["total_deploy_attempts"],
    }

    if fix_feedback:
        result["fix_feedback"] = fix_feedback

    result["routing_log"] = state["routing_log"] + [{
        "round": state["total_val_attempts"] + state["total_deploy_attempts"],
        "error_type": error_type,
        "root_cause": root_cause,
        "fix_instruction": fix,
        "predicted_route": predicted_route,
    }]

    return result


def _run_apply_phase(state: AgentState, tmpdir: str):
    """terraform apply với transient retry (follow Agent 4 pattern). Trả apply object (success) hoặc result dict (fail)."""
    apply_passed, apply_err = True, ""
    for attempt in range(_MAX_APPLY_TRANSIENT_RETRY + 1):
        if attempt > 0:
            delay = _APPLY_RETRY_BACKOFF * attempt
            logger.info(
                "Agent 5: retry apply after transient (attempt %d/%d, sleep=%ds)",
                attempt + 1, _MAX_APPLY_TRANSIENT_RETRY + 1, delay,
            )
            time.sleep(delay)

        logger.info("Agent 5: terraform apply (timeout=%ds)", _APPLY_TIMEOUT)
        try:
            apply = run_terraform(
                ["terraform", "apply", "-auto-approve", "-no-color", "-parallelism=4"],
                tmpdir, _APPLY_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return _handle_failure(state, tmpdir, "", "terraform apply timed out", is_timeout=True)

        apply_passed = apply.returncode == 0
        apply_err = _extract_error(apply.stdout or "", apply.stderr or "")

        if apply_passed or not matches_any(apply_err, TRANSIENT_PATTERNS):
            break

        if attempt < _MAX_APPLY_TRANSIENT_RETRY:
            logger.info("Agent 5: apply transient (attempt %d) — retry: %s", attempt + 1, apply_err[:120])
            created = _state_resources(tmpdir)
            if created:
                ok, err = destroy_with_override(
                    tmpdir, state.get("generated_code", ""), timeout=_DESTROY_TIMEOUT,
                )
                if not ok:
                    logger.warning("Agent 5: cannot clean before retry — %s",
                                   err or "terraform destroy timed out")
                    break

    if apply_passed:
        return apply
    return _handle_failure(state, tmpdir, apply.stdout or "", apply.stderr or "", is_timeout=False)


# ──────────────────────────────────────────────────────────────────────────────
# MAIN LOGIC
# ──────────────────────────────────────────────────────────────────────────────

def deployment_node(state: AgentState) -> dict:
    """LangGraph node: terraform apply lên AWS, auto-destroy sau khi thành công."""
    code = state["generated_code"]
    logger.info(
        "Agent 5: deploy_arch_retry=%d deploy_eng_retry=%d",
        state["retries"].get("deploy_arch", {}).get("count", 0),
        state["retries"].get("deploy_eng", {}).get("count", 0),
    )

    run_dir = state.get("run_dir") or ""
    files_dir = (Path(run_dir) / "files") if run_dir else None
    assert run_dir, "Agent 5: run_dir must be set (persistent from A1)"

    with terraform_workdir(run_dir, "tf", reuse=True) as d:
        write_terraform_dir(d, code, files_dir=files_dir)

        apply = _run_apply_phase(state, d)
        if isinstance(apply, dict):
            return apply

        created = _state_resources(d)
        logger.info("Agent 5: APPLY OK — %d resources", len(created))

        # Move tf workdir ra staging dir riêng trước khi enqueue destroy.
        # Tránh race condition với _safe_rmtree(run_dir) ở row runner.
        staging = Path(run_dir).parent / f"_destroy_{Path(run_dir).name}"
        shutil.move(str(d), str(staging))
        logger.info("Agent 5: enqueue destroy → %s", staging.name)
        destroy_ok, destroy_err = enqueue_destroy(staging, code)

        return {
            "deployment_result": {
                "success": True,
                "error_type": None,
                "error_label": None,
                "resources_created": created,
                "partial_apply_destroyed": False,
                "destroy_failed": not destroy_ok,
                "cleanup_error_label": None,
                "destroy_error": destroy_err,
                "fix_instruction": None,
                "apply_raw_error": None,
                "destroyed": True,
            }
        }


# ──────────────────────────────────────────────────────────────────────────────
# ROUTING
# ──────────────────────────────────────────────────────────────────────────────

def route_after_deployment(state: AgentState) -> str:
    """Conditional edge after A5."""
    dr = state["deployment_result"]

    if dr["success"]:
        return "end"

    if state["total_deploy_attempts"] >= MAX_DEPLOY_TOTAL_RETRY:
        logger.info("Agent 5: max deploy attempts (%d >= %d) — requires_human",
                    state["total_deploy_attempts"], MAX_DEPLOY_TOTAL_RETRY)
        return "requires_human"

    error_type = dr["error_type"]

    if error_type == "INFRASTRUCTURE":
        return "requires_human"

    if error_type == "LOGIC":
        can_retry, reason = check_retry_budget(state, "deploy_eng", max_retries=MAX_DEPLOY_ENG_RETRY)
        if can_retry:
            return "engineering"
        logger.info("Agent 5: %s — requires_human", reason)
        return "requires_human"

    if error_type == "MISSING_RESOURCE":
        can_retry, reason = check_retry_budget(state, "deploy_arch", max_retries=MAX_DEPLOY_ARCH_RETRY)
        if can_retry:
            return "architecture"
        logger.info("Agent 5: %s — requires_human", reason)
        return "requires_human"

    logger.info("Agent 5: route requires_human (error_type=%s)", error_type)
    return "requires_human"
