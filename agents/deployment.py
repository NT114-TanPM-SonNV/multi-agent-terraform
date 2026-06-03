"""Agent 5 — Deployment / Apply (deployment_node)

Thực thi terraform apply lên AWS infrastructure.
Nếu fail → cleanup partial state (destroy) → phân loại lỗi → route.

Workflow:
  1. terraform init (timeout 60s)
  2. terraform apply (timeout 360s)
  3. Nếu success → optional auto-destroy (trong eval mode) → END
  4. Nếu fail:
     a. Cleanup: terraform refresh (best-effort) + terraform destroy
     b. Phân loại lỗi (pattern-based trước, LLM fallback)
     c. Route: retry A5 / route A3 / route A1 / requires_human

Error Classification (Hybrid — Pattern + LLM):
  - INFRASTRUCTURE: timeout/connection pattern → retry 1 lần in-node → requires_human
  - LOGIC: apply fail + LLM phân loại → route engineering (A3) để patch code
  - MISSING_RESOURCE: apply fail + "not found" pattern → route architecture (A1) để re-plan
  - OTHER: PERMISSION / QUOTA / UNKNOWN → route requires_human (terminal)

Cleanup Strategy:
  - Khi apply timeout: terraform refresh (rebuild state từ AWS) rồi destroy
  - Khi apply fail: list resources từ state → destroy (auto cleanup)
  - Nếu destroy fail → requires_human (dirty state, cần manual intervention)

Input: state["generated_code"], state["infrastructure_plan"], state["retries"]
Output: state["deployment_result"] (success/error), state["fix_feedback"] (nếu retry A3/A1)

Retry logic:
  - INFRASTRUCTURE: retry 1 lần in-node (như A4) — không qua graph
  - LOGIC: eng_retry_count max 3 (shared với A4, quay A3 sửa code)
  - MISSING_RESOURCE: arch_retry_count max 2 (shared với A4, quay A1 re-plan)
  - OTHER: không retry (terminal, cần human fix AWS setup)

Auto-destroy (eval mode):
  - Sau apply thành công, patch HCL (disable deletion_protection) rồi destroy
  - Dùng trong batch evaluation để clean up test resources
"""
import json
import logging
import re
import subprocess
from pathlib import Path

# Attributes ngăn AWS resource bị delete — phải disable trước khi terraform destroy.
# Nếu không patch: destroy fail với "DeletionProtectionEnabled" hoặc tương tự.
# Mỗi tuple: (regex_pattern, replacement_string) — áp qua re.sub theo thứ tự.
# Thứ tự quan trọng: final_snapshot_identifier phải xử lý SAU skip_final_snapshot
# vì chúng liên quan nhau.
_DESTROY_PATCHES = [
    # DynamoDB: deletion_protection_enabled=true chặn DeleteTable API
    (r'(deletion_protection_enabled\s*=\s*)true', r'\g<1>false'),
    # RDS / Aurora / DocumentDB / ALB: deletion_protection=true chặn DeleteDBInstance
    (r'(deletion_protection\s*=\s*)true', r'\g<1>false'),
    # RDS: skip_final_snapshot=false yêu cầu tạo snapshot trước khi xóa — eval không cần
    (r'(skip_final_snapshot\s*=\s*)false', r'\g<1>true'),
    # RDS/ElastiCache: final_snapshot_identifier conflicts với skip_final_snapshot=true
    (r'\n[ \t]*final_snapshot_identifier\s*=\s*[^\n]+', ''),
    # RDS: apply_immediately=false = change chỉ áp khi maintenance window → delay destroy
    (r'(apply_immediately\s*=\s*)false', r'\g<1>true'),
    # ElastiCache: automatic_failover_enabled=true cần multi-AZ → disable để destroy nhanh hơn
    (r'(automatic_failover_enabled\s*=\s*)true', r'\g<1>false'),
    (r'(multi_az_enabled\s*=\s*)true', r'\g<1>false'),
]


def _patch_for_destroy(code: str) -> str:
    """Disable deletion-protection attrs để terraform destroy có thể thành công.

    Chỉ dùng trong eval mode (auto_destroy=True). Production code không bao giờ gọi hàm này.
    Áp patches tuần tự — thứ tự đảm bảo consistency giữa skip_final_snapshot và
    final_snapshot_identifier (không thể tồn tại cùng nhau khi skip=true).
    """
    for pattern, replacement in _DESTROY_PATCHES:
        code = re.sub(pattern, replacement, code)
    return code


from core.state import AgentState
from core.llm import call_llm
from core.parsers import parse_llm_json
from core.terraform import run_terraform, write_terraform_dir, terraform_workdir
from core.retry_control import increment_retry, check_retry_budget
from prompts.deployment import SYSTEM_PROMPT as _SYSTEM_PROMPT
from prompts.deployment import (
    TOP_PROMPT as _TOP, BOTTOM_PROMPT as _BOTTOM, CLASSIFY_CONTEXT,
)

logger = logging.getLogger(__name__)

# Timeout constants — cân nhắc giữa tốc độ (ngắn) và tránh false timeout (dài).
# init ngắn hơn A4 vì A5 chạy trên cùng machine đã có provider cache từ A4 → init nhanh hơn.
_INIT_TIMEOUT = 60
# apply timeout: 6 phút — đủ cho các resource chậm (ElastiCache, RDS) nhưng không vô hạn.
_APPLY_TIMEOUT = 360
# destroy timeout: 10 phút — ElastiCache replication group cần 5-10 phút để xóa.
_DESTROY_TIMEOUT = 600
# state list timeout: ngắn, chỉ đọc state file local.
_STATE_TIMEOUT = 30

# Retry budgets cho các error type khác nhau.
# LOGIC: shared với A4 eng_retry_count (max 3 tổng giữa A4 và A5 LOGIC)
# MISSING_RESOURCE: shared với A4 arch_retry_count (max 2 tổng)
_MAX_DEPLOY_ENG_RETRY  = 2
_MAX_DEPLOY_ARCH_RETRY = 2

# Patterns phân loại lỗi apply (tất định — không cần LLM).
# Khác validation.py: A5 dùng bare "timeout"/"eof" vì apply output không có resource
# attribute blocks — false-positive risk thấp hơn so với plan output.
# VPC quota: thường gặp khi batch eval tạo nhiều VPC → retry sau khi cleanup.
_INFRASTRUCTURE_PATTERNS = (
    "connection refused", "connection reset", "could not connect",
    "timeout", "timed out", "i/o timeout", "eof", "no such host",
    "dial tcp", "reset by peer", "context deadline exceeded",
    "requestlimitexceeded", "throttling", "rate exceeded",
    "vpcquotaexceeded", "limitexceeded",
)

# Resource không tồn tại trong plan → A1 re-plan với resource type đúng.
# "not found" xuất hiện khi A1 plan dùng data source trả empty hoặc resource type sai.
_MISSING_RESOURCE_PATTERNS = (
    "not found", "not exist", "does not exist",
    "invalid resource type", "unsupported",
    "unknown resource type", "type not defined",
    "no such resource", "resource cannot be found",
)


def _matches(text: str, patterns: tuple) -> bool:
    """Case-insensitive substring match — tất định, nhanh, không tốn LLM."""
    low = (text or "").lower()
    return any(p in low for p in patterns)


def _extract_error(stdout: str, stderr: str) -> str:
    """Trích error text từ terraform apply output để LLM và pattern matching đọc.

    Vấn đề: terraform ghi plan vào stdout (dài, nhiều noise) và error vào stderr (ngắn, quan trọng).
    Nếu chỉ lấy tail của (stderr+stdout): stderr ngắn bị cắt mất bởi stdout dài.
    Fix: giữ TOÀN BỘ stderr + 2000 ký tự cuối stdout → cả hai đều visible.
    Thêm "--- Error lines ---" section: trích các dòng bắt đầu bằng "Error:" để LLM focus.
    """
    stderr_clean = (stderr or "").strip()
    stdout_tail = (stdout or "")[-2000:]
    combined = (stderr_clean + "\n" + stdout_tail).strip()
    # Trích dòng Error để LLM dễ identify root cause
    error_lines = [ln for ln in combined.splitlines() if re.match(r"\s*(?:Error|error):", ln)]
    if error_lines:
        return combined + "\n\n--- Error lines ---\n" + "\n".join(error_lines[-20:])
    return combined


def _resource_labels(plan: dict) -> list[str]:
    """Tạo list "type.name" từ infrastructure_plan — hint cho LLM classify."""
    return [f"{r['type']}.{r['name']}" for r in plan.get("resources", [])]


def _guess_failed_resource(error_text: str, labels: list[str]) -> str | None:
    """Đoán resource gây lỗi bằng string matching trong error text.

    Cung cấp hint cho LLM: "failed_resource=aws_s3_bucket.main" → LLM focus vào
    resource đó khi sinh fix_instruction, không cần đọc hết plan.
    Match bằng type, name, hoặc full label (type.name) — type thường xuất hiện
    trong error message AWS ("aws_db_instance resource not found").
    Trả None nếu không tìm được → LLM nhận "unknown", vẫn hoạt động được.
    """
    for label in labels:
        rtype, rname = label.split(".", 1)
        if rtype in error_text or rname in error_text or label in error_text:
            return label
    return None


def _deploy_result(success: bool, error_type: str | None, *, fix_instruction=None,
                   resources_created=None, partial_apply_destroyed=False,
                   destroy_failed=False, destroy_error=None, apply_raw_error=None) -> dict:
    """Tạo deployment_result dict đồng nhất cho mọi outcome (success/failure).

    Tại sao có destroy_failed field?
      Nếu apply fail + destroy fail = dirty state (resources tồn tại trên AWS nhưng không
      trong terraform state). Người phải cleanup thủ công → luôn route requires_human.
      route_after_deployment kiểm tra dr["destroy_failed"] trước mọi check khác.

    partial_apply_destroyed: apply bị interrupt nhưng destroy cleanup thành công.
    Trạng thái này an toàn để retry (state clean).
    """
    return {
        "success": success,
        "error_type": error_type,
        "resources_created": resources_created or [],
        "partial_apply_destroyed": partial_apply_destroyed,
        "destroy_failed": destroy_failed,
        "destroy_error": destroy_error,
        "fix_instruction": fix_instruction,
        "apply_raw_error": apply_raw_error,
    }


def _state_resources(tmpdir: str) -> list:
    """List tất cả resource trong terraform state để biết apply đã tạo gì.

    Dùng sau khi apply fail: nếu partial apply xảy ra (một số resource đã tạo),
    danh sách này cho biết cần destroy gì để cleanup.
    Timeout ngắn (30s) vì state list chỉ đọc local state file, không gọi AWS API.
    Return [] nếu error hoặc timeout — caller vẫn chạy destroy (no-op nếu state rỗng).
    """
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
) -> tuple[str, str | None]:
    """LLM phân loại error type từ apply fail + sinh fix_instruction.

    Chỉ gọi khi pattern-based classification không xác định được type (error_type=None).
    Allowed types: LOGIC, MISSING_RESOURCE, OTHER (không phải INFRASTRUCTURE — đã check pattern trước).

    Context cho LLM:
      - labels: resource types trong plan (scope của problem)
      - failed: resource đoán gây lỗi (focus point)
      - error: full error text (cần thiết để classify đúng)
      - partial/destroyed: state hiện tại (ảnh hưởng đến fix_instruction)
      - retry: lần retry thứ mấy (LLM có thể đề xuất khác nhau ở lần 2)

    Fallback: nếu LLM call fail hoặc parse fail → "OTHER" (terminal, safe default).
    Tại sao OTHER thay vì LOGIC?
      LOGIC route A3 có thể loop vô hạn nếu LLM classify sai liên tục.
      OTHER route requires_human — conservative hơn, đảm bảo người can thiệp.
    """
    ctx = _TOP + CLASSIFY_CONTEXT.format(
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
        return "OTHER", None
    et = parsed.get("error_type")
    if et not in ("LOGIC", "MISSING_RESOURCE", "OTHER"):
        et = "OTHER"
    # fix_instruction chỉ có nghĩa với LOGIC/MISSING (cần A3/A1 fix code)
    # OTHER → requires_human → fix_instruction không được dùng
    fix = parsed.get("fix_instruction") if et in ("LOGIC", "MISSING_RESOURCE") else None
    return et, (str(fix)[:500] if fix else None)


def _handle_failure(
    state: AgentState, tmpdir: str,
    apply_stdout: str, apply_stderr: str,
    is_timeout: bool,
) -> dict:
    """Xử lý apply fail: phân loại lỗi, cleanup partial state, trả dict state update.

    Flow chi tiết:
      1. Pattern-based classification (tất định):
           is_timeout → INFRASTRUCTURE
           _INFRASTRUCTURE_PATTERNS match → INFRASTRUCTURE
           _MISSING_RESOURCE_PATTERNS match → MISSING_RESOURCE
           else → None (cần LLM ở bước 3)
      2. Nếu timeout: terraform refresh (rebuild state từ AWS vì state có thể corrupt)
      3. Cleanup: terraform state list → terraform destroy (luôn chạy, no-op nếu state rỗng)
      4. LLM classify (chỉ nếu error_type=None từ bước 1)
      5. Increment retry counter và build result dict
      6. Special handling cho LOGIC và MISSING_RESOURCE (thêm fix_feedback)
    """
    error_text = _extract_error(apply_stdout, apply_stderr)
    plan = state.get("infrastructure_plan") or {}
    resource_labels = _resource_labels(plan)

    # ── Step 1: Pattern-based classification (không cần LLM) ─────────────────
    # Ưu tiên tất định trước LLM: nhanh hơn, không tốn quota, deterministic.
    if is_timeout:
        # apply bị SIGKILL sau _APPLY_TIMEOUT → terraform state có thể bị corrupt
        error_type = "INFRASTRUCTURE"
    elif _matches(error_text, _INFRASTRUCTURE_PATTERNS):
        # Network/auth/quota → không phải code bug → không route A3
        error_type = "INFRASTRUCTURE"
    elif _matches(error_text, _MISSING_RESOURCE_PATTERNS):
        # Resource type không tồn tại/không hỗ trợ → A1 cần re-plan
        error_type = "MISSING_RESOURCE"
    else:
        error_type = None  # không xác định được → cần LLM ở bước sau

    # ── Step 2: Refresh state nếu timeout ────────────────────────────────────
    # Khi terraform apply bị SIGKILL giữa chừng, state file có thể rỗng (terraform
    # chưa kịp commit partial state) hoặc stale (chứa resource đã bị roll back).
    # `terraform refresh` query AWS thực tế → rebuild state → destroy sau đó chính xác.
    if is_timeout:
        try:
            run_terraform(["terraform", "refresh", "-no-color"], tmpdir, 60)
        except subprocess.TimeoutExpired:
            pass  # best-effort: nếu refresh cũng timeout thì destroy vẫn chạy

    # ── Step 3: Cleanup partial state ────────────────────────────────────────
    # LUÔN chạy destroy dù apply fail theo cách nào.
    # Lý do: partial apply có thể tạo một số resource (VPC xong, RDS chưa xong).
    # Nếu không cleanup: resource sót lại → leak AWS cost + conflict lần sau.
    # `terraform state list` trước để biết có resource không (partial=True nếu có).
    created = _state_resources(tmpdir)
    partial = bool(created)
    partial_destroyed = destroy_failed = False
    destroy_error = None

    try:
        destroy = run_terraform(
            ["terraform", "destroy", "-auto-approve", "-no-color"],
            tmpdir, _DESTROY_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        # Destroy timeout → dirty state → người phải can thiệp
        destroy_failed = True
        destroy_error = f"terraform destroy timed out (>{_DESTROY_TIMEOUT}s)"
    else:
        if destroy.returncode == 0:
            partial_destroyed = True  # cleanup thành công
        else:
            destroy_failed = True
            destroy_error = (destroy.stderr or "")[:500]

    # ── Step 4: LLM classify (chỉ khi pattern không xác định được) ──────────
    fix = None
    if error_type is None:
        failed_resource = _guess_failed_resource(error_text, resource_labels)
        error_type, fix = _llm_classify_deploy(
            error_text, resource_labels, failed_resource,
            partial, partial_destroyed, state["retries"]["deploy"]["count"],
        )

    # ── Step 5: Increment retry counter + build base result ──────────────────
    # LOGIC/MISSING: increment "eng"/"arch" bên dưới (sau bước này).
    # INFRASTRUCTURE/OTHER: đều route requires_human (terminal) — increment "deploy"
    # chỉ để bump total_attempts (global backstop) + tracking, không gate routing.
    if error_type not in ("LOGIC", "MISSING_RESOURCE"):
        increment_retry(state, "deploy", error_type or "OTHER", error_text[:200])

    logger.info(
        "Agent 5: FAIL %s (partial=%s destroyed=%s destroy_failed=%s)",
        error_type, partial, partial_destroyed, destroy_failed,
    )

    # Base result dict — LOGIC/MISSING sẽ thêm fix_feedback vào bên dưới.
    # retries và total_attempts: trả về state hiện tại (đã mutate bởi increment_retry).
    result: dict = {
        "deployment_result": _deploy_result(
            False, error_type,
            fix_instruction=fix,
            resources_created=created,
            partial_apply_destroyed=partial_destroyed,
            destroy_failed=destroy_failed,
            destroy_error=destroy_error,
            apply_raw_error=error_text[:3000],
        ),
        "retries": state["retries"],
        "total_attempts": state["total_attempts"],
    }

    # ── Step 6: Special handling cho actionable errors ───────────────────────

    # LOGIC: HCL code logic sai (wrong arg, invalid value, circular dependency).
    # A3 có thể fix bằng cách patch specific attribute → route engineering.
    # Điều kiện: destroy phải thành công (nếu destroy fail → dirty state → requires_human).
    if error_type == "LOGIC" and not destroy_failed:
        increment_retry(state, "eng", "LOGIC_DEPLOY", error_text[:200])
        # fix_feedback với root_cause="engineering" → route_after_deployment → A3
        result["fix_feedback"] = {
            "overall_passed": False,
            "error_type": "LOGIC",
            "root_cause": "engineering",
            "fix_instruction": fix,
            "checkov": {"passed_count": 0, "failed": []},
            # validate_passed/plan_passed=True vì A4 đã pass — lỗi xảy ra ở apply
            "validate_passed": True,
            "plan_passed": True,
        }
        # Cập nhật retries sau khi increment "eng" (total_attempts cũng tăng)
        result["retries"] = state["retries"]
        result["total_attempts"] = state["total_attempts"]

    # MISSING_RESOURCE: resource type không tồn tại → A1 cần re-plan.
    # Ví dụ: A1 plan dùng aws_lambda_event_source_mapping nhưng thiếu aws_sqs_queue.
    elif error_type == "MISSING_RESOURCE" and not destroy_failed:
        increment_retry(state, "arch", "MISSING_RESOURCE_DEPLOY", error_text[:200])
        result["fix_feedback"] = {
            "overall_passed": False,
            "error_type": "MISSING_RESOURCE",
            "root_cause": "architecture",
            "fix_instruction": fix,
            "checkov": {"passed_count": 0, "failed": []},
            "validate_passed": True,
            "plan_passed": True,
        }
        result["retries"] = state["retries"]
        result["total_attempts"] = state["total_attempts"]

    return result


def deployment_node(state: AgentState) -> dict:
    """LangGraph node — thực thi terraform apply lên AWS.

    Flow:
      1. terraform init: tải provider, setup backend.
         Nếu fail → INFRASTRUCTURE error (init failure không fixable bằng code).
      2. terraform apply: tạo resources trên AWS.
         Nếu success → optional auto-destroy (eval mode).
         Nếu fail → _handle_failure: cleanup + classify + route.

    Tại sao init lại sau A4 đã init?
      A4 và A5 dùng different working directories (terraform_workdir tạo temp dir riêng).
      Provider cache được share qua plugin cache → init lần 2 nhanh hơn (không download lại).
    """
    code = state["generated_code"]

    # Log retry count để trace: biết A5 đang ở lần retry thứ mấy
    logger.info(
        "Agent 5: deploy_retry=%d eng_retry=%d",
        state["retries"].get("deploy", {}).get("count", 0),
        state["retries"].get("eng", {}).get("count", 0),
    )

    run_dir = state.get("run_dir") or ""
    # files_dir: stub files (Lambda zip, S3 object content) cần copy vào working dir
    files_dir = (Path(run_dir) / "files") if run_dir else None

    with terraform_workdir(run_dir or None, "a5") as d:
        # Ghi HCL + stubs vào temp directory
        write_terraform_dir(d, code, files_dir=files_dir)

        # ── terraform init + apply — retry 1 lần in-node nếu INFRASTRUCTURE ──
        # Giống A4: transient issue (network/quota) tự hết sau 1 lần chờ → retry ngay.
        # Không qua graph (tránh overhead routing + state cycle không cần thiết).
        for attempt in range(2):
            # ── terraform init ────────────────────────────────────────────────
            logger.info("Agent 5: terraform init attempt=%d (timeout=%ds)", attempt, _INIT_TIMEOUT)
            try:
                init = run_terraform(["terraform", "init", "-no-color"], d, _INIT_TIMEOUT)
            except subprocess.TimeoutExpired:
                if attempt == 0:
                    logger.warning("Agent 5: init timeout — retry in-node")
                    continue
                logger.error("Agent 5: init timeout (sau retry)")
                increment_retry(state, "deploy", "INIT_TIMEOUT", "")
                return {
                    "deployment_result": _deploy_result(
                        False, "INFRASTRUCTURE",
                        fix_instruction=f"terraform init timed out (>{_INIT_TIMEOUT}s)",
                    ),
                    "retries": state["retries"],
                    "total_attempts": state["total_attempts"],
                }

            if init.returncode != 0:
                if attempt == 0:
                    logger.warning("Agent 5: init failed — retry in-node")
                    continue
                logger.error("Agent 5: init FAILED (sau retry)")
                increment_retry(state, "deploy", "INIT_FAILED", init.stderr[:200] if init.stderr else "")
                return {
                    "deployment_result": _deploy_result(
                        False, "INFRASTRUCTURE",
                        fix_instruction=f"terraform init failed: {init.stderr[:300]}",
                    ),
                    "retries": state["retries"],
                    "total_attempts": state["total_attempts"],
                }

            # ── terraform apply ───────────────────────────────────────────────
            logger.info("Agent 5: terraform apply attempt=%d (timeout=%ds)", attempt, _APPLY_TIMEOUT)
            try:
                apply = run_terraform(
                    ["terraform", "apply", "-auto-approve", "-no-color"], d, _APPLY_TIMEOUT
                )
            except subprocess.TimeoutExpired:
                failure = _handle_failure(state, d, "", "terraform apply timed out", is_timeout=True)
                if attempt == 0 and failure["deployment_result"]["error_type"] == "INFRASTRUCTURE":
                    logger.warning("Agent 5: apply timeout — retry in-node")
                    continue
                return failure

            if apply.returncode != 0:
                failure = _handle_failure(state, d, apply.stdout or "", apply.stderr or "", is_timeout=False)
                if attempt == 0 and failure["deployment_result"]["error_type"] == "INFRASTRUCTURE":
                    logger.warning("Agent 5: apply INFRASTRUCTURE — retry in-node")
                    continue
                return failure

            break  # apply success

        if apply.returncode == 0:
            # ── Apply success ─────────────────────────────────────────────────
            # Lấy danh sách resource đã tạo từ terraform state (cho deployment_result).
            created = _state_resources(d)
            logger.info("Agent 5: APPLY OK — %d resources", len(created))

            auto_destroyed = False
            auto_destroy_error = None
            if state.get("auto_destroy"):
                # Eval mode: cleanup resources ngay sau apply thành công.
                # Tại sao patch trước? Deletion protection chặn destroy API.
                logger.info("Agent 5: auto-destroy (eval mode)")
                tf_path = Path(d) / "main.tf"
                original = tf_path.read_text(encoding="utf-8")
                patched = _patch_for_destroy(original)
                if patched != original:
                    logger.info("Agent 5: patching deletion-protection attrs before destroy")
                    tf_path.write_text(patched, encoding="utf-8")
                    # Re-apply patched code để AWS nhận thấy thay đổi attribute trước destroy
                    try:
                        run_terraform(
                            ["terraform", "apply", "-auto-approve", "-no-color"],
                            d, _APPLY_TIMEOUT,
                        )
                    except subprocess.TimeoutExpired:
                        pass  # best-effort: thử destroy dù patch re-apply fail
                # Destroy với timeout dài (ElastiCache/RDS cần 5-10 phút)
                try:
                    cleanup = run_terraform(
                        ["terraform", "destroy", "-auto-approve", "-no-color"],
                        d, _DESTROY_TIMEOUT,
                    )
                    auto_destroyed = cleanup.returncode == 0
                    if not auto_destroyed:
                        auto_destroy_error = (cleanup.stderr or "")[:300]
                        logger.warning("Agent 5: auto-destroy FAILED — %s", auto_destroy_error)
                    else:
                        logger.info("Agent 5: auto-destroy OK")
                except subprocess.TimeoutExpired:
                    auto_destroy_error = f"terraform destroy timed out (>{_DESTROY_TIMEOUT}s)"
                    logger.warning("Agent 5: auto-destroy TIMEOUT")

            result = _deploy_result(True, None, resources_created=created)
            result["auto_destroyed"] = auto_destroyed
            result["auto_destroy_error"] = auto_destroy_error
            # Success: chỉ cần deployment_result (không cần fix_feedback, retries đã ổn)
            return {"deployment_result": result}

        # ── Apply fail → cleanup + classify + route ───────────────────────────
        return _handle_failure(
            state, d, apply.stdout or "", apply.stderr or "", is_timeout=False
        )


def route_after_deployment(state: AgentState) -> str:
    """Conditional edge sau A5 — quyết định node tiếp theo.

    Thứ tự kiểm tra:
      1. Success → end (done)
      2. destroy_failed → requires_human (dirty state, LUÔN cần human)
      3. INFRASTRUCTURE → requires_human (đã retry in-node rồi)
      4. LOGIC → route A3 nếu còn budget (code bug, A3 fix)
      5. MISSING_RESOURCE → route A1 nếu còn budget (resource type sai, A1 re-plan)
      6. Mọi trường hợp còn lại (OTHER, PERMISSION, QUOTA, exhausted) → requires_human
    """
    dr = state["deployment_result"]

    # Success: pipeline hoàn thành → kết thúc
    if dr["success"]:
        return "end"

    # Dirty state: resources tồn tại trên AWS nhưng không thể destroy.
    # Không retry bất kỳ gì — người phải cleanup thủ công trước khi chạy lại.
    if dr.get("destroy_failed"):
        return "requires_human"

    error_type = dr["error_type"]

    # INFRASTRUCTURE: đã retry in-node 1 lần rồi → requires_human.
    if error_type == "INFRASTRUCTURE":
        return "requires_human"

    # LOGIC: HCL code logic sai → A3 patch.
    # Dùng "eng" counter (shared với A4 SYNTAX/LOGIC) để tổng budget A3 không vượt _MAX_DEPLOY_ENG_RETRY.
    if error_type == "LOGIC":
        can_retry, reason = check_retry_budget(state, "eng", max_retries=_MAX_DEPLOY_ENG_RETRY)
        if can_retry:
            return "engineering"
        logger.info("Agent 5: %s — route requires_human", reason)
        return "requires_human"

    # MISSING_RESOURCE: resource type không tồn tại → A1 re-plan.
    # Dùng "arch" counter (shared với A4 MISSING_RESOURCE) để tổng budget A1 không vượt _MAX_DEPLOY_ARCH_RETRY.
    if error_type == "MISSING_RESOURCE":
        can_retry, reason = check_retry_budget(state, "arch", max_retries=_MAX_DEPLOY_ARCH_RETRY)
        if can_retry:
            return "architecture"
        logger.info("Agent 5: %s — route requires_human", reason)
        return "requires_human"

    # OTHER / PERMISSION / QUOTA / UNKNOWN: không có code fix.
    # Ví dụ: IAM permission thiếu, service limit, S3 bucket name conflict.
    # Người phải xem xét và fix AWS setup.
    logger.info("Agent 5: route requires_human (error_type=%s)", error_type)
    return "requires_human"
