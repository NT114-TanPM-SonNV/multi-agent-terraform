"""Agent 5 — Deployment: terraform apply lên AWS.

Fail → cleanup partial state (destroy) → phân loại → route.
INFRASTRUCTURE: in-node retry 1 lần. LOGIC → A3. MISSING_RESOURCE → A1.
OTHER/dirty state → requires_human. Auto-destroy sau apply trong eval mode.
"""
import json
import logging
import os
import re
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
from core.errors import matches_any, MISSING_RESOURCE_PATTERNS, AUTH_PATTERNS, extract_error_facts, TRANSIENT_PATTERNS
from core.destroy import patch_for_destroy, destroy_resources, _DESTROY_TIMEOUT
from prompts.deployment import SYSTEM_PROMPT as _SYSTEM_PROMPT
from prompts.deployment import (
    TOP_PROMPT as _TOP, BOTTOM_PROMPT as _BOTTOM, CLASSIFY_CONTEXT,
)

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# CONSTANTS: Apply timeouts + retry budgets + error patterns
# ──────────────────────────────────────────────────────────────────────────────

# Apply phải >= destroy: TẠO RDS/ElastiCache lâu hơn (hoặc bằng) XÓA chúng (5-15 phút).
# Cũ = 360s khiến apply RDS bị SIGKILL giữa chừng → state corrupt + leaked resource,
# rồi transient-retry lặp lại 2 lần → leak thêm. Mặc định 600s (bằng _DESTROY_TIMEOUT),
# override qua TF_APPLY_TIMEOUT khi cần (vd RDS Multi-AZ).
_APPLY_TIMEOUT   = int(os.environ.get("TF_APPLY_TIMEOUT", "600"))
_STATE_TIMEOUT = 30
_MAX_APPLY_TRANSIENT_RETRY = 2
_APPLY_RETRY_BACKOFF = 5

# AUTH_PATTERNS: terminal credential/permission errors, không retry.
# TRANSIENT_PATTERNS: retryable network/throttle/timeout errors.
# Dùng cụm cụ thể (không bare "timeout"/"eof") vì apply output echo cả config HCL
# có thể chứa `timeouts {}` hay heredoc "EOF" → false positive nếu dùng bare keyword.

# Destroy constants + patch logic moved to core.destroy (DRY shared with A4 cleanup)

# ──────────────────────────────────────────────────────────────────────────────
# HELPERS: Error extraction + resource labeling + classification
# ──────────────────────────────────────────────────────────────────────────────

def _extract_error(stdout: str, stderr: str) -> str:
    """stderr đầy đủ + tail của stdout (stderr ngắn bị cắt nếu chỉ lấy tail combined).
    Thêm section "Error lines" để LLM focus.
    """
    stderr_clean = (stderr or "").strip()
    stdout_tail = (stdout or "")[-2000:]
    combined = (stderr_clean + "\n" + stdout_tail).strip()
    error_lines = [ln for ln in combined.splitlines() if re.match(r"\s*(?:Error|error):", ln)]
    if error_lines:
        return combined + "\n\n--- Error lines ---\n" + "\n".join(error_lines[-20:])
    return combined


def _resource_labels(plan: dict) -> list[str]:
    """Tạo list "type.name" từ infrastructure_plan — hint cho LLM classify."""
    return [f"{r['type']}.{r['name']}" for r in plan.get("resources", [])]


def _guess_failed_resource(error_text: str, labels: list[str]) -> str | None:
    """Match resource trong error text theo 3 tầng: full label → type → name (word-boundary,
    len>3 để tránh "main"/"this" match bừa).
    """
    for label in labels:
        if label in error_text:
            return label
    for label in labels:
        if label.split(".", 1)[0] in error_text:
            return label
    for label in labels:
        rname = label.split(".", 1)[1]
        if len(rname) > 3 and re.search(rf"\b{re.escape(rname)}\b", error_text):
            return label
    return None


def _deploy_result(success: bool, error_type: str | None, *, fix_instruction=None,
                   resources_created=None, partial_apply_destroyed=False,
                   destroy_failed=False, destroy_error=None, apply_raw_error=None,
                   error_label: str | None = None, cleanup_error_label: str | None = None) -> dict:
    # destroy_failed → dirty state → route_after_deployment force requires_human.
    return {
        "success": success,
        "error_type": error_type,
        "error_label": error_label,
        "resources_created": resources_created or [],
        "partial_apply_destroyed": partial_apply_destroyed,
        "destroy_failed": destroy_failed,
        "cleanup_error_label": cleanup_error_label,
        "destroy_error": destroy_error,
        "fix_instruction": fix_instruction,
        "apply_raw_error": apply_raw_error,
    }


def _state_resources(tmpdir: str) -> list:
    """List resources trong terraform state — dùng để biết partial apply đã tạo gì."""
    try:
        r = run_terraform(["terraform", "state", "list"], tmpdir, _STATE_TIMEOUT)
    except subprocess.TimeoutExpired:
        return []
    if r.returncode != 0:
        return []
    return [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]


def _llm_classify_deploy(
    error_text: str,
    resource_labels: list[str],
    failed_resource: str | None,
    partial: bool,
    destroyed: bool,
    retry: int,
) -> tuple[str, str | None, str]:
    """Phân loại apply error khi pattern matching không xác định được type.
    Fallback về "OTHER" (terminal) nếu LLM fail — tránh loop LOGIC → A3 sai.

    Tại sao OTHER thay vì LOGIC?
      LOGIC route A3 có thể loop vô hạn nếu LLM classify sai liên tục.
      OTHER route requires_human — conservative hơn, đảm bảo người can thiệp.
    """
    grounded = extract_error_facts(error_text)
    ctx = _TOP + grounded + CLASSIFY_CONTEXT.format(
        labels=json.dumps(resource_labels),
        failed=failed_resource or 'unknown',
        error=error_text[:2000],
        partial=partial, destroyed=destroyed, retry=retry,
    ) + _BOTTOM
    try:
        parsed = parse_llm_json(
            call_llm([{"role": "system", "content": _SYSTEM_PROMPT},
                      {"role": "user", "content": ctx}], agent="deployment"),
            {"error_type": None, "fix_instruction": None},
        )
    except Exception as e:
        logger.warning("Agent 5 LLM classify error (%s) — OTHER", e)
        return "OTHER", None, "LLM_OTHER"
    et = parsed.get("error_type")
    if et not in ("LOGIC", "MISSING_RESOURCE", "OTHER"):
        et = "OTHER"
    # fix_instruction chỉ có nghĩa với LOGIC/MISSING (cần A3/A1 fix code)
    # OTHER → requires_human → fix_instruction không được dùng
    fix = parsed.get("fix_instruction") if et in ("LOGIC", "MISSING_RESOURCE") else None
    return et, (str(fix)[:500] if fix else None), f"LLM_{et}"


def _deploy_error_label(error_text: str, is_timeout: bool, error_type: str,
                        classified_by_llm: bool) -> str:
    if is_timeout:
        return "APPLY_TIMEOUT"
    if matches_any(error_text, AUTH_PATTERNS):
        return "AUTH"
    if matches_any(error_text, TRANSIENT_PATTERNS):
        return "TRANSIENT"
    if matches_any(error_text, MISSING_RESOURCE_PATTERNS):
        return "MISSING_RESOURCE"
    if classified_by_llm:
        return f"LLM_{error_type or 'OTHER'}"
    return "UNKNOWN"


def _route_back_fix_feedback(error_type: str, root_cause: str, fix: str | None) -> dict:
    """fix_feedback chuẩn khi A5 route ngược: LOGIC→A3 (engineering), MISSING→A1 (architecture).

    validate_passed/plan_passed=True vì A4 đã pass — lỗi chỉ xảy ra ở apply-time.
    Gộp 2 block LOGIC/MISSING vốn giống hệt nhau (chỉ khác error_type + root_cause).
    """
    return {
        "overall_passed": False,
        "error_type": error_type,
        "root_cause": root_cause,
        "fix_instruction": fix,
        "checkov": {"passed_count": 0, "failed": []},
        "validate_passed": True,
        "plan_passed": True,
    }


def _routing_log_append(state: AgentState, error_type: str | None,
                        root_cause: str | None, fix: str | None,
                        predicted_route: str) -> list:
    """Thêm 1 entry audit vào routing_log (đối xứng A4 — routing_log là audit chung).

    Trước đây A5 KHÔNG ghi routing_log → audit trail mù toàn bộ vòng deploy.
    `round` = total_val_attempts + total_deploy_attempts (round toàn cục đơn điệu xuyên 2 pha;
    deploy fail bump total_deploy_attempts nên dùng tổng để round vẫn tăng đều ở pha deploy).
    """
    return state["routing_log"] + [{
        "round": state["total_val_attempts"] + state["total_deploy_attempts"],
        "error_type": error_type,
        "root_cause": root_cause,
        "fix_instruction": fix,
        "predicted_route": predicted_route,
    }]


def _classify_error_pattern(error_text: str, is_timeout: bool) -> str | None:
    """Step 1: Deterministic error classification via pattern matching.

    Returns error_type ∈ {INFRASTRUCTURE, OTHER, MISSING_RESOURCE, None}.
      - is_timeout → INFRASTRUCTURE (terraform kill → state corrupt)
      - AUTH_PATTERNS → OTHER (terminal, no retry, no code fix)
      - TRANSIENT_PATTERNS → INFRASTRUCTURE (in-node retry viable)
      - MISSING_RESOURCE_PATTERNS → MISSING_RESOURCE (A1 re-plan needed)
      - else → None (ambiguous, needs LLM ở _handle_failure step 4)
    """
    if is_timeout:
        return "INFRASTRUCTURE"
    if matches_any(error_text, AUTH_PATTERNS):
        return "OTHER"
    if matches_any(error_text, TRANSIENT_PATTERNS):
        return "INFRASTRUCTURE"
    if matches_any(error_text, MISSING_RESOURCE_PATTERNS):
        return "MISSING_RESOURCE"
    return None


def _run_apply_with_transient_retry(state: AgentState, tmpdir: str):
    """Run terraform apply and absorb short-lived AWS/network failures."""
    last_stdout = ""
    last_stderr = ""
    last_timeout = False

    for attempt in range(_MAX_APPLY_TRANSIENT_RETRY + 1):
        if attempt > 0:
            delay = _APPLY_RETRY_BACKOFF * attempt
            logger.info(
                "Agent 5: retry terraform apply after transient failure "
                "(attempt %d/%d, sleep=%ds)",
                attempt + 1,
                _MAX_APPLY_TRANSIENT_RETRY + 1,
                delay,
            )
            time.sleep(delay)

        logger.info("Agent 5: terraform apply (timeout=%ds)", _APPLY_TIMEOUT)
        try:
            apply = run_terraform(
                ["terraform", "apply", "-auto-approve", "-no-color", "-parallelism=4"],
                tmpdir,
                _APPLY_TIMEOUT,
            )
            last_stdout = apply.stdout or ""
            last_stderr = apply.stderr or ""
            last_timeout = False
        except subprocess.TimeoutExpired:
            apply = None
            last_stdout = ""
            last_stderr = "terraform apply timed out"
            last_timeout = True

        if apply is not None and apply.returncode == 0:
            return apply

        error_text = _extract_error(last_stdout, last_stderr)
        if _classify_error_pattern(error_text, last_timeout) != "INFRASTRUCTURE":
            break
        if attempt >= _MAX_APPLY_TRANSIENT_RETRY:
            break

        if _state_resources(tmpdir):
            destroy_success, destroy_error = destroy_resources(tmpdir, timeout=_DESTROY_TIMEOUT)
            if not destroy_success:
                logger.warning(
                    "Agent 5: cannot clean partial apply before retry — %s",
                    destroy_error or "terraform destroy timed out",
                )
                break
        elif last_timeout:
            try:
                run_terraform(["terraform", "refresh", "-no-color"], tmpdir, 60)
            except subprocess.TimeoutExpired:
                pass

    return _handle_failure(state, tmpdir, last_stdout, last_stderr, is_timeout=last_timeout)




def _handle_failure(
    state: AgentState, tmpdir: str,
    apply_stdout: str, apply_stderr: str,
    is_timeout: bool,
) -> dict:
    """Handle terraform apply failure: classify error, cleanup partial state, route next node.

    3 flows:
      Flow 1: Pattern Match (deterministic) — _classify_error_pattern returns error_type ∈
              {INFRASTRUCTURE, OTHER, MISSING_RESOURCE, None}
      Flow 2: LLM Classify (only if pattern miss) — call _llm_classify_deploy when error_type=None
      Flow 3: Route Decision — based on error_type + retry budget + destroy_failed flag
    """
    error_text = _extract_error(apply_stdout, apply_stderr)
    plan = state.get("infrastructure_plan") or {}
    resource_labels = _resource_labels(plan)
    created = _state_resources(tmpdir)

    # ─────────────────────────────────────────────────────────────────────────────
    # FLOW 1: Pattern Match (deterministic classification)
    # ─────────────────────────────────────────────────────────────────────────────
    error_type = _classify_error_pattern(error_text, is_timeout)

    # Refresh state if timeout (SIGKILL → state corrupt)
    if is_timeout:
        try:
            run_terraform(["terraform", "refresh", "-no-color"], tmpdir, 60)
        except subprocess.TimeoutExpired:
            pass  # best-effort

    # Cleanup partial state unconditionally
    partial = bool(created)
    destroy_success, destroy_error = destroy_resources(tmpdir, timeout=_DESTROY_TIMEOUT)
    partial_destroyed = destroy_success
    destroy_failed = not destroy_success and destroy_error is not None

    # ─────────────────────────────────────────────────────────────────────────────
    # FLOW 2: LLM Classify (only if pattern miss)
    # ─────────────────────────────────────────────────────────────────────────────
    fix = None
    error_label = _deploy_error_label(error_text, is_timeout, error_type or "OTHER", False)
    if error_type is None:
        failed_resource = _guess_failed_resource(error_text, resource_labels)
        error_type, fix, error_label = _llm_classify_deploy(
            error_text, resource_labels, failed_resource,
            partial, partial_destroyed, state["retries"]["deploy_eng"]["count"],
        )
    else:
        error_label = _deploy_error_label(error_text, is_timeout, error_type, False)

    logger.warning(
        "Agent 5: FAIL %s [%s] (partial=%s destroyed=%s destroy_failed=%s cleanup=%s)",
        error_type, error_label, partial, partial_destroyed, destroy_failed,
        "DESTROY_FAILED" if destroy_failed else "",
    )

    # Base result dict
    result: dict = {
        "deployment_result": _deploy_result(
            False, error_type,
            fix_instruction=fix,
            resources_created=created,
            partial_apply_destroyed=partial_destroyed,
            destroy_failed=destroy_failed,
            destroy_error=destroy_error,
            apply_raw_error=error_text[:3000],
            error_label=error_label,
            cleanup_error_label="DESTROY_FAILED" if destroy_failed else None,
        ),
        "retries": state["retries"],
        "total_val_attempts": state["total_val_attempts"],
        "total_deploy_attempts": state["total_deploy_attempts"],
    }

    # ─────────────────────────────────────────────────────────────────────────────
    # FLOW 3: Route Decision (error_type + retry budget + destroy_failed)
    # ─────────────────────────────────────────────────────────────────────────────
    if destroy_failed:
        # Destroy failed → human intervention required
        predicted_route = "requires_human"
    elif error_type == "LOGIC":
        # Route back to engineering if budget available
        increment_retry(state, "deploy_eng", "LOGIC_DEPLOY", error_text[:200])
        result["fix_feedback"] = _route_back_fix_feedback("LOGIC", "engineering", fix)
        result["retries"] = state["retries"]
        result["total_deploy_attempts"] = state["total_deploy_attempts"]
        predicted_route = "engineering"
    elif error_type == "MISSING_RESOURCE":
        # Route back to architecture if budget available
        increment_retry(state, "deploy_arch", "MISSING_RESOURCE_DEPLOY", error_text[:200])
        result["fix_feedback"] = _route_back_fix_feedback("MISSING_RESOURCE", "architecture", fix)
        result["retries"] = state["retries"]
        result["total_deploy_attempts"] = state["total_deploy_attempts"]
        predicted_route = "architecture"
    else:
        # OTHER, INFRASTRUCTURE, etc. → requires human
        predicted_route = "requires_human"

    # Audit
    result["routing_log"] = _routing_log_append(
        state, error_type, result.get("fix_feedback", {}).get("root_cause"),
        fix, predicted_route,
    )

    return result


# ──────────────────────────────────────────────────────────────────────────────
# MAIN LOGIC: terraform apply + auto-destroy in eval mode + in-node transient retry
# ──────────────────────────────────────────────────────────────────────────────

def deployment_node(state: AgentState) -> dict:
    """LangGraph node — execute terraform apply on AWS infrastructure.

    Flow:
      1. Reuse persistent terraform workdir (set by A4), skip init (already done).
      2. Write HCL + stub files (Lambda zip, S3 objects) to working directory.
      3. terraform apply with in-node transient retry:
         - Timeout (>360s) → _handle_failure (cleanup partial + classify + route)
         - Error (rc ≠ 0) → _handle_failure (cleanup partial + classify + route)
         - Success → always destroy (cleanup resources)
      4. Always destroy after apply success:
         - Patch deletion protection attrs (skip_final_snapshot, deletion_protection, etc)
         - terraform apply (patch reload to AWS)
         - terraform destroy (timeout 600s for ElastiCache/RDS)
         - Return success with destroyed=True/False

    Returns: deployment_result dict (success, error_type, resources_created, destroyed, etc.)
      or failure dict with fix_feedback (route to A3/A1) / requires_human.
    """
    code = state["generated_code"]

    # Log retry count để trace: biết A5 đang ở lần retry thứ mấy
    logger.info(
        "Agent 5: deploy_arch_retry=%d deploy_eng_retry=%d",
        state["retries"].get("deploy_arch", {}).get("count", 0),
        state["retries"].get("deploy_eng", {}).get("count", 0),
    )

    run_dir = state.get("run_dir") or ""
    # files_dir: stub files (Lambda zip, S3 object content) cần copy vào working dir
    files_dir = (Path(run_dir) / "files") if run_dir else None

    # A5 luôn có run_dir (set từ đầu pipeline) → dùng thư mục persistent "tf" của A4
    # (reuse=True, không xóa .terraform/). A4 đã guarantee init thành công
    # (init fail → A4 return early với error, không tới A5), nên A5 không cần init.
    # Chỉ cần apply — terraform apply tự link provider từ .terraform/ A4 tạo.
    assert run_dir, "Agent 5: run_dir must be set (persistent from A1)"
    with terraform_workdir(run_dir, "tf", reuse=True) as d:
        # Ghi HCL + stubs vào directory (giống A4)
        write_terraform_dir(d, code, files_dir=files_dir)

        # ── terraform apply (with in-node transient retry) ────────────────
        apply = _run_apply_with_transient_retry(state, d)
        if isinstance(apply, dict):
            return apply

        # ── Apply success ─────────────────────────────────────────────────
        # Lấy danh sách resource đã tạo từ terraform state (cho deployment_result).
        created = _state_resources(d)
        logger.info("Agent 5: APPLY OK — %d resources", len(created))

        # ── Always destroy (cleanup resources after apply success) ──────────
        # Tại sao patch trước? Deletion protection chặn destroy API.
        logger.info("Agent 5: destroying resources (cleanup after apply)")
        tf_path = Path(d) / "main.tf"
        original = tf_path.read_text(encoding="utf-8")
        patched = patch_for_destroy(original)
        if patched != original:
            logger.info("Agent 5: patching deletion-protection attrs before destroy")
            tf_path.write_text(patched, encoding="utf-8")
            # Re-apply patched code để AWS nhận thấy thay đổi attribute trước destroy
            try:
                run_terraform(
                    ["terraform", "apply", "-auto-approve", "-no-color", "-parallelism=4"],
                    d, _APPLY_TIMEOUT,
                )
            except subprocess.TimeoutExpired:
                pass  # best-effort: thử destroy dù patch re-apply fail

        # Destroy với timeout dài (ElastiCache/RDS cần 5-10 phút)
        destroy_ok, destroy_err = destroy_resources(d, timeout=_DESTROY_TIMEOUT)
        if not destroy_ok:
            destroy_error = destroy_err or f"terraform destroy timed out (>{_DESTROY_TIMEOUT}s)"
            logger.warning("Agent 5: destroy FAILED — %s", destroy_error)
        else:
            logger.info("Agent 5: destroy OK")

        result = _deploy_result(True, None, resources_created=created)
        result["destroyed"] = destroy_ok
        result["destroy_error"] = destroy_err
        # Success: chỉ cần deployment_result (không cần fix_feedback, retries đã ổn)
        return {"deployment_result": result}


# ──────────────────────────────────────────────────────────────────────────────
# ROUTING: Conditional edge sau A5 — decide next node
# ──────────────────────────────────────────────────────────────────────────────

def route_after_deployment(state: AgentState) -> str:
    """Conditional edge after A5 — decide routing to next node.

    Routing decision order (symmetric to route_after_validation in A4):
      1. success=True → "end" (pipeline done)
      2. destroy_failed=True → "requires_human" (dirty state, resources leaked, manual cleanup needed)
      3. total_deploy_attempts >= MAX_DEPLOY_TOTAL_RETRY → "requires_human" (deploy-phase backstop, ≤5 attempts)
      4. INFRASTRUCTURE error → "requires_human" (already retried in-node, timeout or setup issue)
      5. LOGIC error + budget available → "engineering" (A3 fix code)
      6. MISSING_RESOURCE + budget available → "architecture" (A1 re-plan)
      7. OTHER, QUOTA, or budget exhausted → "requires_human"

    Budget checks: deploy_eng ≤ MAX_DEPLOY_ENG_RETRY (2), deploy_arch ≤ MAX_DEPLOY_ARCH_RETRY (2).
    """
    dr = state["deployment_result"]

    # Success: pipeline hoàn thành → kết thúc
    if dr["success"]:
        return "end"

    # Dirty state: resources tồn tại trên AWS nhưng không thể destroy.
    # Không retry bất kỳ gì — người phải cleanup thủ công trước khi chạy lại.
    # Đặt TRƯỚC global cap vì đây là vấn đề an toàn (dirty state) chứ không phải budget.
    if dr.get("destroy_failed"):
        return "requires_human"

    # Deploy-phase backstop — ĐỘC LẬP với total_val_attempts của validation phase.
    # total_deploy_attempts chỉ tăng bởi fail của A5 (increment_retry deploy_* + các nhánh
    # infra/timeout trong node). Tách khỏi total_val_attempts để A4 đốt hết budget của nó
    # KHÔNG starve A5: lỗi apply-time là lớp mới, A5 phải có lượt sửa riêng.
    # Per-counter deploy_eng/deploy_arch (≤2) vẫn là sub-limit của riêng lớp deploy;
    # backstop này chống explosion khi re-plan reset các per-agent counter.
    if state["total_deploy_attempts"] >= MAX_DEPLOY_TOTAL_RETRY:
        logger.info("Agent 5: max deploy attempts (%d >= %d) — requires_human",
                    state["total_deploy_attempts"], MAX_DEPLOY_TOTAL_RETRY)
        return "requires_human"

    error_type = dr["error_type"]

    # INFRASTRUCTURE: đã retry in-node 1 lần rồi → requires_human.
    if error_type == "INFRASTRUCTURE":
        return "requires_human"

    if error_type == "LOGIC":
        can_retry, reason = check_retry_budget(state, "deploy_eng", max_retries=MAX_DEPLOY_ENG_RETRY)
        if can_retry:
            return "engineering"
        logger.info("Agent 5: %s — route requires_human", reason)
        return "requires_human"

    if error_type == "MISSING_RESOURCE":
        can_retry, reason = check_retry_budget(state, "deploy_arch", max_retries=MAX_DEPLOY_ARCH_RETRY)
        if can_retry:
            return "architecture"
        logger.info("Agent 5: %s — route requires_human", reason)
        return "requires_human"

    # OTHER / QUOTA / UNKNOWN: không có code fix.
    # Ví dụ: IAM permission thiếu, service limit, S3 bucket name conflict.
    # Người phải xem xét và fix AWS setup.
    logger.info("Agent 5: route requires_human (error_type=%s)", error_type)
    return "requires_human"
