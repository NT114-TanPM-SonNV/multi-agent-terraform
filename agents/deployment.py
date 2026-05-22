"""Deployment Agent — Agent 5 trong pipeline.

Thực thi `terraform apply` lên Floci. Nếu fail:
  - kiểm tra partial apply (state list) → terraform destroy để cleanup dirty state;
  - phân loại lỗi rồi route.

Phân loại (hybrid):
  - apply timeout / connection → TRANSIENT (retry Agent 5 nếu còn budget).
  - Floci không hỗ trợ operation (UnsupportedOperation/501/not supported) → FLOCI_LIMIT
    → requires_human (bản community giới hạn, KHÔNG phải lỗi code).
  - còn lại → LLM phân loại FIXABLE (lỗi code plan không bắt → Agent 3) / UNKNOWN.
  - destroy fail (dirty state không cleanup được) → LUÔN requires_human.

Output: deployment_result + deploy_retry_count. LangGraph pattern: RETURN dict update.
"""
import logging
import subprocess
import tempfile
from pathlib import Path

from core.state import AgentState
from core.llm import call_llm
from core.parsers import parse_llm_json
from core.terraform import run_terraform, substitute_endpoint, check_floci_health
from prompts.deployment import SYSTEM_PROMPT as _SYSTEM_PROMPT
from prompts.deployment import TOP_PROMPT as _TOP, BOTTOM_PROMPT as _BOTTOM

logger = logging.getLogger(__name__)

_INIT_TIMEOUT = 60        # cache warm — chỉ resolve lock
_APPLY_TIMEOUT = 240
_DESTROY_TIMEOUT = 180
_STATE_TIMEOUT = 30

_FLOCI_LIMIT_PATTERNS = (
    "unsupportedoperation", "not supported", "not implemented", "notimplemented", "501",
)
_TRANSIENT_PATTERNS = (
    "connection refused", "connection reset", "could not connect",
    "timeout", "timed out", "i/o timeout", "eof", "no such host",
)


def _matches(text: str, patterns) -> bool:
    low = (text or "").lower()
    return any(p in low for p in patterns)


def _deploy_result(success: bool, error_type: str | None, *, fix_instruction=None,
                   resources_created=None, partial_apply_destroyed=False,
                   destroy_failed=False, destroy_error=None) -> dict:
    return {
        "success": success,
        "error_type": error_type,
        "resources_created": resources_created or [],
        "partial_apply_destroyed": partial_apply_destroyed,
        "destroy_failed": destroy_failed,
        "destroy_error": destroy_error,
        "fix_instruction": fix_instruction,
    }


def _state_resources(tmpdir: str) -> list:
    """Đọc resource đã tạo qua `terraform state list` (đáng tin hơn parse stdout)."""
    try:
        r = run_terraform(["terraform", "state", "list"], tmpdir, _STATE_TIMEOUT)
    except subprocess.TimeoutExpired:
        return []
    if r.returncode != 0:
        return []
    return [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]


def _llm_classify_deploy(apply_err: str, partial: bool, destroyed: bool, retry: int) -> tuple[str, str | None]:
    """LLM phân loại FIXABLE / UNKNOWN (+ fix_instruction). Fallback UNKNOWN."""
    ctx = (
        _TOP
        + f"APPLY ERROR:\n{apply_err[:1500]}\n\n"
        + f"PARTIAL APPLY: {partial}\nDESTROYED: {destroyed}\nDEPLOY RETRY COUNT: {retry}"
        + _BOTTOM
    )
    try:
        parsed = parse_llm_json(
            call_llm([{"role": "system", "content": _SYSTEM_PROMPT},
                      {"role": "user", "content": ctx}]),
            {"error_type": None, "fix_instruction": None},
        )
    except Exception as e:
        logger.warning("Agent 5 LLM classify lỗi (%s) — UNKNOWN", e)
        return "UNKNOWN", None
    et = parsed.get("error_type")
    if et not in ("FIXABLE", "UNKNOWN"):
        et = "UNKNOWN"
    fix = parsed.get("fix_instruction") if et == "FIXABLE" else None
    return et, (str(fix)[:500] if fix else None)


def _handle_failure(state: AgentState, tmpdir: str, apply_err: str, is_timeout: bool) -> dict:
    """Xử lý apply fail/timeout: cleanup partial state, phân loại, build return."""
    # Phân loại error_type trước — FLOCI_LIMIT không cần destroy
    fix = None
    if is_timeout:
        error_type = "TRANSIENT"
    elif _matches(apply_err, _FLOCI_LIMIT_PATTERNS):
        error_type = "FLOCI_LIMIT"
    elif _matches(apply_err, _TRANSIENT_PATTERNS):
        error_type = "TRANSIENT"
    else:
        error_type = None

    created = _state_resources(tmpdir)
    partial = bool(created)

    partial_destroyed = destroy_failed = False
    destroy_error = None
    # FLOCI_LIMIT = unsupported resource → skip destroy (resource không thực tạo)
    if partial and error_type != "FLOCI_LIMIT":
        try:
            destroy = run_terraform(["terraform", "destroy", "-auto-approve", "-no-color"], tmpdir, _DESTROY_TIMEOUT)
        except subprocess.TimeoutExpired:
            destroy_failed, destroy_error = True, f"terraform destroy timed out (>{_DESTROY_TIMEOUT}s)"
        else:
            if destroy.returncode == 0:
                partial_destroyed = True
            else:
                destroy_failed, destroy_error = True, (destroy.stderr or "")[:500]

    # LLM classify chỉ khi chưa có error_type rõ ràng
    if error_type is None:
        error_type, fix = _llm_classify_deploy(apply_err, partial, partial_destroyed, state["deploy_retry_count"])

    logger.info("Agent 5: FAIL %s (partial=%s destroyed=%s destroy_failed=%s)",
                error_type, partial, partial_destroyed, destroy_failed)
    result = {
        "deployment_result": _deploy_result(
            False, error_type, fix_instruction=fix, resources_created=created,
            partial_apply_destroyed=partial_destroyed,
            destroy_failed=destroy_failed, destroy_error=destroy_error),
        "deploy_retry_count": state["deploy_retry_count"] + 1,
    }
    # FIXABLE → route engineering. Agent 3 đọc fix qua validation_result.fix_instruction,
    # nhưng validation_result hiện là kết quả PASS của Agent 4 → phải đẩy fix sang kênh đó
    # + tăng eng_retry_count để engineering_node dùng retry prompt (shared budget với Agent 4).
    if error_type == "FIXABLE" and not destroy_failed:
        result["validation_result"] = {
            "overall_passed": False, "error_type": "LOGIC", "root_cause": "engineering",
            "fix_instruction": fix, "checkov": {"passed_count": 0, "failed": []},
            "validate_passed": True, "plan_passed": True,
        }
        result["eng_retry_count"] = state["eng_retry_count"] + 1
    return result


def deployment_node(state: AgentState) -> dict:
    """LangGraph node function cho Deployment Agent (Agent 5)."""
    code = state["generated_code"]
    endpoint = state.get("floci_endpoint")

    logger.info("Agent 5: Starting deployment (retry=%d)", state.get("deploy_retry_count", 0))

    # Floci phải reachable để apply — nếu không, TRANSIENT (có thể đang khởi động)
    logger.info("Agent 5: Checking Floci health...")
    try:
        health_ok = endpoint and check_floci_health(endpoint)
    except Exception as e:
        logger.warning("Agent 5: Floci health check error: %s", e)
        health_ok = False

    if not health_ok:
        logger.warning("Agent 5: Floci không reachable — TRANSIENT")
        return {
            "deployment_result": _deploy_result(False, "TRANSIENT",
                                                fix_instruction="Floci endpoint không reachable"),
            "deploy_retry_count": state["deploy_retry_count"] + 1,
        }

    with tempfile.TemporaryDirectory() as d:
        logger.info("Agent 5: Writing HCL to %s", d)
        tf_code = substitute_endpoint(code, endpoint) if endpoint else code
        (Path(d) / "main.tf").write_text(tf_code, encoding="utf-8")

        logger.info("Agent 5: Running terraform init (timeout=%ds)...", _INIT_TIMEOUT)
        try:
            init = run_terraform(["terraform", "init", "-no-color"], d, _INIT_TIMEOUT)
        except subprocess.TimeoutExpired:
            logger.error("Agent 5: terraform init TIMEOUT")
            return {"deployment_result": _deploy_result(False, "TRANSIENT",
                    fix_instruction=f"terraform init timed out (>{_INIT_TIMEOUT}s)"),
                    "deploy_retry_count": state["deploy_retry_count"] + 1}
        except Exception as e:
            logger.error("Agent 5: terraform init ERROR: %s", e)
            raise

        if init.returncode != 0:
            logger.error("Agent 5: terraform init failed (returncode=%d)", init.returncode)
            # init fail = môi trường (cache/provider) → TRANSIENT để thử lại
            return {"deployment_result": _deploy_result(False, "TRANSIENT",
                    fix_instruction=f"terraform init failed: {init.stderr[:300]}"),
                    "deploy_retry_count": state["deploy_retry_count"] + 1}

        logger.info("Agent 5: Running terraform apply (timeout=%ds)...", _APPLY_TIMEOUT)
        try:
            apply = run_terraform(["terraform", "apply", "-auto-approve", "-no-color"], d, _APPLY_TIMEOUT)
        except subprocess.TimeoutExpired:
            logger.error("Agent 5: terraform apply TIMEOUT")
            return _handle_failure(state, d, "terraform apply timed out", is_timeout=True)
        except Exception as e:
            logger.error("Agent 5: terraform apply ERROR: %s", e)
            raise

        if apply.returncode == 0:
            created = _state_resources(d)
            logger.info("Agent 5: APPLY OK — %d resources", len(created))
            return {"deployment_result": _deploy_result(True, None, resources_created=created)}

        apply_err = (apply.stderr or "") + "\n" + (apply.stdout or "")
        return _handle_failure(state, d, apply_err, is_timeout=False)


def route_after_deployment(state: AgentState) -> str:
    """Conditional edge sau Agent 5. KHÔNG ghi state."""
    dr = state["deployment_result"]
    if dr["success"]:
        return "end"

    # Dirty state không cleanup được → luôn cần người can thiệp trước khi chạy lại
    if dr.get("destroy_failed"):
        return "requires_human"

    error_type = dr["error_type"]
    retry = state["deploy_retry_count"]   # đã +1 trong node trước khi route

    # TRANSIENT: tối đa 3 lần chạy Agent 5 (1 initial + 2 retry)
    if error_type == "TRANSIENT" and retry <= 2:
        return "agent5"
    # FIXABLE: route về engineering tối đa 1 lần
    if error_type == "FIXABLE" and retry <= 1:
        return "engineering"
    # FLOCI_LIMIT, UNKNOWN, hết budget → requires_human
    return "requires_human"
