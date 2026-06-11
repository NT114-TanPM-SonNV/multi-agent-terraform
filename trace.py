"""trace.py — walkthrough có chú giải, giải thích framework cho người đọc code lần đầu.

Mỗi bước in:
  • Vai trò của agent này là gì trong pipeline
  • Nó nhận được gì từ state (dữ liệu thực tế)
  • Nó đã làm gì và kết quả ra sao
  • Tại sao pipeline đi tiếp theo hướng đó

Chạy:
    python trace.py "Create a Lambda function with SQS trigger"
    python trace.py --no-deploy "Create a VPC with public and private subnets"
    python trace.py --csv dataset/data-dev.csv --case 17
"""
import argparse
import json
import re
import sys
from pathlib import Path

# Force UTF-8 stdout/stderr trên Windows để tránh UnicodeEncodeError với tiếng Việt
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))


from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

import logging
logging.basicConfig(level=logging.WARNING, force=True)
for _n in ("httpx", "httpcore", "openai", "botocore", "boto3", "urllib3",
           "agents.architecture", "agents.security", "agents.engineering",
           "agents.validation", "agents.deployment", "checkov", "litellm",
           "langgraph", "langchain"):
    logging.getLogger(_n).setLevel(logging.ERROR)

from graph import build_initial_state, RECURSION_LIMIT
from core.terraform import _safe_rmtree
from core.eval_utils import (
    BOLD,
    DIM,
    R,
    blue,
    bold,
    cyan,
    dim,
    green,
    load_trace_prompt,
    magenta,
    red,
    white,
    yellow,
)
from core.retry_control import (
    MAX_VAL_ARCH_RETRY, MAX_DEPLOY_ARCH_RETRY,
    MAX_VAL_ENG_RETRY,  MAX_DEPLOY_ENG_RETRY,
    MAX_VAL_SEC_RETRY,  MAX_DEPLOY_TOTAL_RETRY,
    MAX_VAL_TOTAL_RETRY,
)
_MAX_ARCH_RETRY = MAX_VAL_ARCH_RETRY + MAX_DEPLOY_ARCH_RETRY  # 4 tổng 2 phase
_MAX_ENG_RETRY  = MAX_VAL_ENG_RETRY  + MAX_DEPLOY_ENG_RETRY   # 5 tổng 2 phase
_MAX_SEC_RETRY  = MAX_VAL_SEC_RETRY                            # 2
_MAX_DEPLOY_TOTAL_RETRY = MAX_DEPLOY_TOTAL_RETRY               # 4 deploy-phase backstop
_MAX_VAL_TOTAL_RETRY = MAX_VAL_TOTAL_RETRY                     # 5 val-phase backstop
from evaluate import _select_graph


def _clean_trace_tf_dir(run_dir: Path) -> None:
    """Remove stale Terraform workdir before a trace case starts.

    Trace reuses tmp/trace/tf across runs so old terraform.tfstate can make the
    next row refresh unrelated resources. Clean only the local workdir before a
    case; A5 still keeps state during the run if destroy fails.
    """
    _safe_rmtree(run_dir / "tf")


# ── ANSI ─────────────────────────────────────────────────────────────────────

_AGENT_COLORS = {
    "architecture":  ("\033[94m", "A1"),
    "security":      ("\033[95m", "A2"),
    "engineering":   ("\033[93m", "A3"),
    "validation":    ("\033[96m", "A4"),
    "deployment":    ("\033[92m", "A5"),
    "requires_human":("\033[91m", "!!"),
}

_AGENT_NAMES = {
    "architecture":  "Architecture Agent",
    "security":      "Security Agent",
    "engineering":   "Engineering Agent",
    "validation":    "Validation Agent",
    "deployment":    "Deployment Agent",
    "requires_human":"Requires Human",
}

_AGENT_ROLE = {
    "architecture": (
        "Đọc prompt của user, gọi LLM để phân tích và lập kế hoạch hạ tầng.\n"
        "  Output là một JSON plan liệt kê từng AWS resource cần tạo (type, name, attributes).\n"
        "  Đây là 'bản thiết kế' — các agent sau chỉ thực thi theo plan này, không tự thêm resource."
    ),
    "security": (
        "Đọc infrastructure_plan (A1) + prompt user, gọi LLM để chọn Checkov check IDs\n"
        "  cần enforce cho từng resource dựa trên intent.\n"
        "  LLM chọn từ menu catalog (chỉ IDs hợp lệ cho đúng resource type) — không hallucinate.\n"
        "  Output: danh sách CKV IDs per resource → A3 implement, A4 verify."
    ),
    "engineering": (
        "Nhận infrastructure_plan (A1) + security_profile (A2), gọi LLM để sinh Terraform HCL.\n"
        "  LLM viết full HCL hoàn chỉnh: terraform{}, provider{}, resource{} cho mọi resource trong plan.\n"
        "  Nếu có fix_instruction từ vòng retry trước, A3 chỉ sửa đúng phần đó, giữ nguyên phần còn lại."
    ),
    "validation": (
        "Kiểm tra HCL sinh bởi A3 qua 4 bước tuần tự (bước sau chỉ chạy nếu bước trước pass):\n"
        "  1. terraform init     — tải AWS provider plugin\n"
        "  2. terraform validate — cú pháp HCL có đúng không?\n"
        "  3. terraform plan     — AWS provider có chấp nhận config này không?\n"
        "  4. Checkov gate       — các CKV IDs mà A2 đã chọn có pass không?\n"
        "     (scan trên plan JSON từ terraform show -json — chính xác hơn source scan)\n"
        "  Nếu fail: phân loại lỗi, sinh fix_instruction, route về agent phù hợp."
    ),
    "deployment": (
        "Chạy terraform init + terraform apply để tạo resource thật trên AWS.\n"
        "  Nếu apply fail: kiểm tra partial apply (terraform state list), destroy nếu dirty,\n"
        "  phân loại lỗi rồi route:\n"
        "    TRANSIENT        → retry A5 (network/throttle tạm thời)\n"
        "    LOGIC            → A3 fix code rồi apply lại\n"
        "    MISSING_RESOURCE → A1 re-plan (AWS resource phụ thuộc thiếu)\n"
        "    OTHER            → requires_human (lỗi không xác định hoặc hết budget)\n"
        "  Sau apply thành công: terraform destroy ngay để không tốn tiền AWS."
    ),
    "requires_human": (
        "Pipeline không thể tự giải quyết — hết budget retry hoặc gặp lỗi không thể tự sửa.\n"
        "  Lý do được lưu trong fix_feedback / deployment_result để người dùng xem xét."
    ),
}

_W = 72


# ── Layout helpers ────────────────────────────────────────────────────────────

def _divider(char="─", color=DIM):
    print(f"{color}{char * _W}{R}")

def _agent_header(node: str, step: int, repeat: int) -> None:
    col, tag = _AGENT_COLORS.get(node, ("\033[97m", "??"))
    name = _AGENT_NAMES.get(node, node)
    rep  = f"  ← retry #{repeat}" if repeat > 1 else ""
    print(f"\n{col}{BOLD}{'━' * _W}{R}")
    print(f"{col}{BOLD}  [{tag}]  STEP {step}  —  {name}{rep}{R}")
    print(f"{col}{BOLD}{'━' * _W}{R}")

def _block(title: str, color_fn=cyan) -> None:
    print(f"\n  {color_fn(bold(title))}")
    print(f"  {dim('·' * (_W - 2))}")

def _item(label: str, value, *, color=white, indent=4) -> None:
    pad = " " * indent
    s = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
    if len(s) > 100 or "\n" in s:
        print(f"{pad}{dim(label + ':')}")
        for line in s.splitlines():
            print(f"{pad}  {color(line)}")
    else:
        print(f"{pad}{dim(label + ':')} {color(s)}")

def _note(text: str, indent: int = 4) -> None:
    pad = " " * indent
    print(f"{pad}{dim(text)}")

def _check_mark(ok: bool | None, label: str, detail: str = "") -> None:
    if ok is True:
        marker = green("✓")
        col = green
    elif ok is False:
        marker = red("✗")
        col = red
    else:
        marker = dim("·")
        col = dim
    d = f"  {dim(detail)}" if detail else ""
    print(f"    {marker}  {col(label)}{d}")

def _hcl(code: str) -> None:
    kw  = re.compile(r'\b(resource|data|terraform|provider|variable|output|module|required_providers|required_version)\b')
    str_re = re.compile(r'"[^"]*"')
    ref_re = re.compile(r'\b([a-z][a-z_]+\.[a-z][a-z_]+\.[a-z][a-z_]+)\b')
    bool_re = re.compile(r'\b(true|false|null)\b')

    print(f"\n  {dim('┌' + '─' * (_W - 4) + '┐')}")
    for line in code.splitlines():
        if re.match(r'\s*#', line):
            print(f"  {dim('│')} {dim(line)}")
            continue
        hl = line
        # thứ tự: ref trước str để tránh overwrite
        hl = str_re.sub(lambda m: green(m.group()), hl)
        hl = kw.sub(lambda m: _c("\033[94m" + BOLD, m.group()), hl)
        hl = ref_re.sub(lambda m: cyan(m.group()), hl)
        hl = bool_re.sub(lambda m: yellow(m.group()), hl)
        print(f"  {dim('│')} {hl}")
    print(f"  {dim('└' + '─' * (_W - 4) + '┘')}")

def _arrow_next(src: str, dst: str, explanation: str) -> None:
    src_col, src_tag = _AGENT_COLORS.get(src, ("\033[97m", "??"))
    dst_col, dst_tag = _AGENT_COLORS.get(dst, ("\033[97m", "??"))
    dst_name = _AGENT_NAMES.get(dst, dst)
    if dst == "END":
        dst_col, dst_tag, dst_name = "\033[92m", "✓", "END"
    print(f"\n    {src_col}[{src_tag}]{R} {yellow('──►')} "
          f"{dst_col}{BOLD}[{dst_tag}] {dst_name}{R}")
    print(f"    {dim('lý do: ' + explanation)}")


# ── Retry counter helper ─────────────────────────────────────────────────────

def _rc(state: dict, key: str) -> int:
    """Đọc retry counter từ state.
    key: 'total' | 'deploy_total' | 'eng' | 'arch' | 'sec' | 'deploy'
    'total' = validation-phase backstop; 'deploy_total' = deploy-phase backstop.
    eng/arch aggregate cả val_ và deploy_ phase.
    """
    if key == "total":
        return state.get("total_val_attempts", 0)
    if key == "deploy_total":
        return state.get("total_deploy_attempts", 0)
    r = state.get("retries") or {}
    if key == "eng":
        return r.get("val_eng", {}).get("count", 0) + r.get("deploy_eng", {}).get("count", 0)
    if key == "arch":
        return r.get("val_arch", {}).get("count", 0) + r.get("deploy_arch", {}).get("count", 0)
    if key == "deploy":
        return r.get("deploy_eng", {}).get("count", 0) + r.get("deploy_arch", {}).get("count", 0)
    return r.get(key, {}).get("count", 0)


# ── Per-agent commentary ──────────────────────────────────────────────────────

def _explain_input(node: str, state: dict) -> None:
    _block("ĐỌC TỪ STATE", cyan)

    if node == "architecture":
        _item("state['prompt']", state.get("prompt", "")[:200])
        fb = state.get("fix_feedback") or {}
        if fb.get("root_cause") == "architecture" and fb.get("fix_instruction"):
            print()
            _note("→ đây là lần retry: A1 nhận thêm fix_instruction để biết cần re-plan như thế nào")
            _item("state['fix_feedback']['fix_instruction']", fb["fix_instruction"][:300], color=yellow)
            _item("retries[val_arch+deploy_arch]['count']", _rc(state, "arch"), color=yellow)
        else:
            _note("→ lần chạy đầu tiên, chưa có fix_instruction nào")

    elif node == "security":
        plan = state.get("infrastructure_plan") or {}
        resources = plan.get("resources", [])
        _note("→ A2 đọc cả prompt lẫn infrastructure_plan để hiểu intent + biết resource type")
        _item("state['prompt']", state.get("prompt", "")[:120], color=dim)
        print()
        _item("state['infrastructure_plan']['resources']",
              f"{len(resources)} resources: " + ", ".join(f"{r.get('type')}.{r.get('name')}" for r in resources))

    elif node == "engineering":
        plan = state.get("infrastructure_plan") or {}
        resources = plan.get("resources", [])
        prof = state.get("security_profile") or {}

        _note("→ A3 nhận 2 thứ: plan từ A1 (cái gì cần tạo) + security_profile từ A2 (tạo như thế nào)")
        print()
        _item("state['infrastructure_plan']['resources']",
              f"{len(resources)} resources")
        for r in resources:
            attrs = list(r.get("attributes", {}).keys())
            blocks = list(r.get("blocks", {}).keys())
            all_k = attrs + [f"[block]{k}" for k in blocks]
            short = ", ".join(all_k[:5]) + ("…" if len(all_k) > 5 else "")
            print(f"      {cyan('•')} {bold(r.get('type', ''))}.{r.get('name', '')}  "
                  f"{dim(short)}")

        if prof:
            print()
            _note("→ security_profile: A3 enforce các CKV check IDs mà A2 đã chọn cho từng resource")
            for label, info in prof.items():
                checks = info.get("checks", [])
                print(f"      {cyan('•')} {label}  checks={cyan(str(checks)) if checks else dim('[]')}")
        else:
            _note("→ security_profile trống (A2 bị skip hoặc fail): không enforce check nào")

        fb = state.get("fix_feedback") or {}
        if fb.get("root_cause") == "engineering" and fb.get("fix_instruction"):
            print()
            _note("→ đây là lần retry: A3 nhận fix_instruction, chỉ sửa phần đó, giữ nguyên phần còn lại")
            _item("state['fix_feedback']['fix_instruction']", fb["fix_instruction"][:300], color=yellow)
            _item("retries[val_eng+deploy_eng]['count']", _rc(state, "eng"), color=yellow)

    elif node == "validation":
        code = state.get("generated_code", "")
        res_count = len(re.findall(r'resource\s+"[^"]+"\s+"[^"]+"', code))
        _note("→ A4 chỉ đọc generated_code từ A3, không cần biết prompt hay plan")
        _item("state['generated_code']",
              f"{len(code)} chars, {res_count} resource blocks")
        total_r = _rc(state, "total")
        eng_r   = _rc(state, "eng")
        sec_r   = _rc(state, "sec")
        print()
        _note("→ A4 cũng kiểm tra retry counters để biết còn budget để retry không")
        col = yellow if total_r > 0 else dim
        _item("retry budget còn lại",
              f"total={total_r}/5  eng={eng_r}/3  sec={sec_r}/2",
              color=col if total_r > 0 else dim)

    elif node == "deployment":
        code = state.get("generated_code", "")
        _note("→ A5 nhận generated_code (đã qua validation A4) và chạy terraform apply thật")
        _item("state['generated_code']", f"{len(code)} chars")
        _note("→ sau apply thành công, A5 destroy ngay để không tốn tiền AWS")
        deploy_r = _rc(state, "deploy")
        if deploy_r > 0:
            _item("retries[deploy_eng+deploy_arch]['count']", deploy_r, color=yellow)
        fb = state.get("fix_feedback") or {}
        if fb.get("root_cause") == "engineering" and fb.get("fix_instruction"):
            print()
            _note("→ đây là route từ A5 FIXABLE: A3 đã sửa code, A5 chạy lại apply")
            _item("state['fix_feedback']['fix_instruction']", fb["fix_instruction"][:200], color=yellow)


def _explain_output(node: str, update: dict, state_before: dict) -> None:
    _block("GHI VÀO STATE", green)

    if node == "architecture":
        plan = update.get("infrastructure_plan") or {}
        resources = plan.get("resources", [])
        if resources:
            _note(f"→ A1 ghi infrastructure_plan: {len(resources)} resources, "
                  f"{len(plan.get('data_sources', []))} data_sources")
            _note("→ mỗi resource có: type (AWS resource type), name, attributes, blocks")
            print()
            for r in resources:
                attrs  = list(r.get("attributes", {}).keys())
                blocks = list(r.get("blocks", {}).keys())
                all_k  = attrs + [f"[block]{k}" for k in blocks]
                keys_s = ", ".join(all_k[:6]) + ("…" if len(all_k) > 6 else "")
                print(f"    {blue(bold(r.get('type', '')))}.{r.get('name', '')}")
                if keys_s:
                    print(f"      {dim('attributes/blocks: ' + keys_s)}")
            if _rc(update, "eng") == 0 and _rc(state_before, "eng") > 0:
                print()
                _note("→ A1 cũng reset retry counters (eng/sec) về 0")
                _note("   lý do: plan mới hoàn toàn → A3 cần full budget, không bị cắt sớm vì lỗi plan cũ")
        else:
            fb = update.get("fix_feedback") or {}
            _note("→ A1 FAILED — LLM call lỗi hoặc response không parse được")
            _item("fix_feedback['error_type']", fb.get("error_type", "?"), color=red)
            _item("fix_feedback['error_stage']", fb.get("error_stage", "?"), color=yellow)
            _item("fix_feedback['fix_instruction']", (fb.get("fix_instruction") or "")[:250], color=red)
            if fb.get("raw_error"):
                _item("raw_error (truncated)", fb["raw_error"][:250], color=dim)
        _note("→ A1 clear fix_feedback={} khi success (báo hiệu 'ok, không phải retry')")

    elif node == "security":
        prof = update.get("security_profile") or {}
        _note(f"→ A2 ghi security_profile: CKV check IDs cho {len(prof)} resources")
        _note("→ LLM chọn dựa trên intent + menu catalog (chỉ IDs hợp lệ cho đúng resource type)")
        sec_status = update.get("security_status", "ok")
        if sec_status != "ok":
            _note(f"→ security_status={sec_status}: A2 degraded, downstream sẽ hiển thị best-effort rõ ràng")
        if prof:
            print()
            for label, info in prof.items():
                checks = info.get("checks", [])
                print(f"    {magenta('•')} {label}")
                print(f"      checks = {cyan(str(checks)) if checks else dim('[]  (no enforcement)')}")

    elif node == "engineering":
        code = update.get("generated_code", "")
        if code.strip():
            res_count = len(re.findall(r'resource\s+"[^"]+"\s+"[^"]+"', code))
            _note(f"→ A3 ghi generated_code: {len(code)} chars, {res_count} resource blocks")
            _note("→ A3 clear fix_feedback={} khi success")
            _note("   lý do: route_after_engineering đọc fix_feedback — nếu error_type=None → validation")
            _hcl(code)
        else:
            fb = update.get("fix_feedback") or {}
            _note("→ A3 FAILED — không sinh được resource block hợp lệ sau 2 lần thử")
            _item("fix_feedback['error_type']",    fb.get("error_type", "?"),  color=red)
            _item("fix_feedback['root_cause']",    fb.get("root_cause", "?"),  color=yellow)
            _item("fix_feedback['error_label']",    fb.get("error_label", "?"),  color=yellow)
            _item("fix_feedback['fix_instruction']",
                  (fb.get("fix_instruction") or "")[:250], color=red)

    elif node == "validation":
        fb     = update.get("fix_feedback") or {}
        passed = fb.get("overall_passed", False)
        applicable_failed = fb.get("applicable_failed_checks") or []

        if passed:
            _note("→ A4 ghi overall_passed=True vào fix_feedback")
            if applicable_failed:
                _note("→ có applicable_failed_checks nhưng không block (best-effort: hết sec retry budget)")
            if fb.get("security_degraded"):
                _note("→ security_degraded=True: A2 từng fail, nên security gate đã được best-effort bypass")
        else:
            _note("→ A4 ghi overall_passed=False + thông tin lỗi để agent retry biết cần sửa gì")

        print()
        _check_mark(fb.get("validate_passed"), "terraform validate",
                    "cú pháp HCL hợp lệ")
        _check_mark(fb.get("plan_passed"), "terraform plan",
                    "AWS provider chấp nhận config")
        ck = fb.get("checkov") or {}
        if ck:
            f_ = ck.get("failed_count", 0)
            p_ = ck.get("passed_count", 0)
            ids = ck.get("failed_ckv_ids", [])
            _check_mark(f_ == 0, f"checkov gate: {p_} passed, {f_} failed",
                        "enforce CKV IDs mà A2 đã chọn, scan trên plan JSON")
            if ids:
                _note(f"   failed_ckv_ids: {ids}")

        if applicable_failed:
            ids = [u.get("ckv_id") for u in applicable_failed]
            print()
            _note(f"→ applicable_failed_checks {ids}: hết sec retry budget → best-effort accept, deploy tiếp")

        not_applicable = fb.get("not_applicable_checks") or []
        if not_applicable:
            _note(f"→ not_applicable_checks {not_applicable}: check ID không áp dụng được cho plan này")

        if not passed:
            print()
            et = fb.get("error_type", "?")
            rc = fb.get("root_cause", "?")
            _note(f"→ error_type={yellow(et)}  root_cause={yellow(rc)}")
            _note("   root_cause xác định agent nào sẽ nhận fix_instruction để retry")
            if fb.get("fix_instruction"):
                print()
                _item("fix_feedback['fix_instruction']", fb["fix_instruction"][:400], color=yellow)
            if fb.get("raw_error"):
                _item("raw_error (truncated)", fb["raw_error"][:250], color=dim)

        # counters — đọc từ update nếu có, fallback về state_before
        merged_for_cnt = {**state_before, **(update or {})}
        new_total = _rc(merged_for_cnt, "total")
        new_eng   = _rc(merged_for_cnt, "eng")
        new_sec   = _rc(merged_for_cnt, "sec")
        new_arch  = _rc(merged_for_cnt, "arch")
        print()
        col = yellow if new_total > 0 else dim
        _note("→ A4 cập nhật retry counters (dùng để quyết định còn budget retry không)")
        _item("counters",
              f"total={new_total}/5  eng={new_eng}/3  sec={new_sec}/2  arch={new_arch}/2",
              color=col if new_total > 0 else dim)

    elif node == "deployment":
        dr  = update.get("deployment_result") or {}
        ok_ = dr.get("success", False)
        if ok_:
            created = dr.get("resources_created", [])
            managed = [r for r in created if not str(r).startswith("data.")]
            data_sources = [r for r in created if str(r).startswith("data.")]
            _note(f"→ terraform apply thành công: {len(managed)} managed resources, "
                  f"{len(data_sources)} data sources trong state")
            print()
            for r in created:
                print(f"    {green('✓')} {white(r)}")
            destroyed = dr.get("destroyed")
            print()
            if destroyed:
                _note("→ cleanup: terraform destroy đã chạy thành công sau apply")
            else:
                _note("→ cleanup chưa xác nhận thành công; kiểm tra destroy_error/state nếu cần")
            if dr.get("destroy_error"):
                _item("destroy_error", dr["destroy_error"][:200], color=red)
        else:
            _note("→ terraform apply thất bại")
            _item("deployment_result['error_type']", dr.get("error_type", "?"), color=red)
            _item("deployment_result['error_label']", dr.get("error_label", "?"), color=yellow)
            if dr.get("cleanup_error_label"):
                _item("deployment_result['cleanup_error_label']", dr.get("cleanup_error_label"), color=yellow)
            if dr.get("apply_raw_error"):
                _item("apply_raw_error (truncated)", dr["apply_raw_error"][:300], color=dim)
            if dr.get("fix_instruction"):
                print()
                _note("→ A5 đã phân loại lỗi và sinh fix_instruction:")
                _item("fix_instruction", dr["fix_instruction"][:300], color=yellow)

    elif node == "requires_human":
        fb = update.get("fix_feedback") or state_before.get("fix_feedback") or {}
        dr = update.get("deployment_result") or state_before.get("deployment_result") or {}
        reason = fb.get("fix_instruction") or dr.get("error_type") or "unknown"
        _note("→ node này không thay đổi state — chỉ log lý do dừng")
        _item("lý do dừng", str(reason)[:300], color=red)


def _explain_routing(node: str, update: dict, merged: dict) -> None:
    _block("ROUTING — ĐI TIẾP THEO HƯỚNG NÀO?", yellow)

    fb = update.get("fix_feedback") or merged.get("fix_feedback") or {}
    et = fb.get("error_type")
    rc = fb.get("root_cause")

    _note("→ LangGraph đọc fix_feedback từ state để quyết định edge nào kích hoạt")
    print()

    if node == "architecture":
        if et == "INFRASTRUCTURE":
            _note("→ fix_feedback['error_type'] == 'INFRASTRUCTURE'  (LLM call thất bại hoàn toàn)")
            _note("   không có ích gì khi retry A1 ngay — cần người kiểm tra cấu hình LLM")
            _arrow_next(node, "requires_human", "error_type=INFRASTRUCTURE, LLM không phản hồi")
        else:
            _note("→ fix_feedback cleared (error_type=None)  → edge tĩnh architecture→security")
            _arrow_next(node, "security", "A1 thành công, plan sẵn sàng để A2 đánh giá bảo mật")

    elif node == "security":
        _note("→ A2 KHÔNG có conditional edge — luôn đi tới engineering")
        _arrow_next(node, "engineering", "edge tĩnh, A2 fail cũng không dừng pipeline")

    elif node == "engineering":
        if not et:
            _note("→ fix_feedback cleared (error_type=None) = A3 thành công")
            _arrow_next(node, "validation", "HCL đã sinh, gửi sang A4 để kiểm tra")
        elif et == "MISSING_RESOURCE":
            arch_cnt = _rc(merged, "arch")
            _note(f"→ error_type=MISSING_RESOURCE: A3 nhận infrastructure_plan rỗng từ A1")
            _note(f"   giải pháp: quay về A1 để re-plan (arch_retry={arch_cnt}/{_MAX_ARCH_RETRY})")
            if arch_cnt < _MAX_ARCH_RETRY:
                _arrow_next(node, "architecture",
                            f"plan rỗng → re-plan, arch_retry={arch_cnt}/{_MAX_ARCH_RETRY}")
            else:
                _note("   hết budget arch_retry → dừng, không thể tự sửa")
                _arrow_next(node, "requires_human",
                            f"hết budget arch_retry ({arch_cnt}/{_MAX_ARCH_RETRY})")
        else:
            _note(f"→ error_type={et}: A3 không sinh được HCL hợp lệ sau retry nội bộ")
            _arrow_next(node, "requires_human",
                        f"error_type={et} (INFRASTRUCTURE=LLM timeout / SYNTAX=không sinh được resource block)")

    elif node == "validation":
        passed  = fb.get("overall_passed", False)
        total_r = _rc(merged, "total")
        eng_r   = _rc(merged, "eng")
        arch_r  = _rc(merged, "arch")
        sec_r   = _rc(merged, "sec")

        if passed:
            applicable_failed = fb.get("applicable_failed_checks") or []
            if applicable_failed:
                _note("→ overall_passed=True dù có applicable_failed_checks")
                _note("   hết sec retry budget → A4 chấp nhận best-effort, không block deploy")
            else:
                _note("→ overall_passed=True: cả 4 bước kiểm tra đều pass")
            _arrow_next(node, "deployment", "HCL hợp lệ và an toàn → deploy thật lên AWS")
        else:
            _note(f"→ overall_passed=False, A4 cần route về agent phù hợp để sửa")
            _note(f"   error_type={yellow(et or '?')}  root_cause={yellow(rc or '?')}")
            _note(f"   budget: total={total_r}/{_MAX_VAL_TOTAL_RETRY}  eng={eng_r}/{_MAX_ENG_RETRY}  sec={sec_r}/{_MAX_SEC_RETRY}  arch={arch_r}/{_MAX_ARCH_RETRY}")
            print()
            if total_r >= _MAX_VAL_TOTAL_RETRY:
                _note(f"→ total_val_attempts >= {_MAX_VAL_TOTAL_RETRY}: backstop global — dừng để không loop vô hạn")
                _arrow_next(node, "requires_human", f"backstop total_retry={total_r}/{_MAX_VAL_TOTAL_RETRY}")
            elif et == "MISSING_RESOURCE":
                if arch_r < _MAX_ARCH_RETRY:
                    _note("→ MISSING_RESOURCE: plan thiếu resource → A1 phải re-plan")
                    _note("   (không phải lỗi code A3, không ích gì khi gửi fix về A3)")
                    _arrow_next(node, "architecture",
                                f"plan thiếu resource, arch_retry={arch_r}/{_MAX_ARCH_RETRY}")
                else:
                    _arrow_next(node, "requires_human", f"hết budget arch_retry ({arch_r}/{_MAX_ARCH_RETRY})")
            elif et == "SECURITY":
                if sec_r < _MAX_SEC_RETRY:
                    _note("→ SECURITY: Checkov fail → A3 sửa code theo fix_instruction")
                    _note("   root_cause='engineering' vì sửa attribute trên resource sẵn có")
                    _arrow_next(node, "engineering",
                                f"Checkov fail, A3 thêm/sửa security attributes, sec_retry={sec_r}/{_MAX_SEC_RETRY}")
                else:
                    _note("→ hết budget sec_retry → best-effort accept, deploy tiếp")
                    _note("   applicable_failed_checks sẽ được ghi lại để tracking, không block")
                    _arrow_next(node, "deployment", f"best-effort security, sec_retry hết ({sec_r}/{_MAX_SEC_RETRY})")
            elif et in ("SYNTAX", "LOGIC"):
                if eng_r < _MAX_ENG_RETRY:
                    _note(f"→ {et}: lỗi trong HCL A3 sinh → gửi fix_instruction về A3")
                    _arrow_next(node, "engineering",
                                f"lỗi {et} trong HCL, eng_retry={eng_r}/{_MAX_ENG_RETRY}")
                else:
                    _arrow_next(node, "requires_human", f"hết budget eng_retry ({eng_r}/{_MAX_ENG_RETRY})")
            elif et == "INFRASTRUCTURE":
                _note("→ INFRASTRUCTURE: terraform init/plan timeout — không phải lỗi code")
                _arrow_next(node, "requires_human", "terraform infra timeout, không tự sửa được")
            else:
                _arrow_next(node, "requires_human", f"error_type={et} không có handler")

    elif node == "deployment":
        dr       = update.get("deployment_result") or {}
        ok_      = dr.get("success", False)
        deploy_r = _rc(merged, "deploy")
        deploy_total = _rc(merged, "deploy_total")
        if ok_:
            _note("→ deployment_result['success']=True")
            _arrow_next(node, "END", "tất cả resource đã được tạo trên AWS thành công")
        elif deploy_total >= _MAX_DEPLOY_TOTAL_RETRY:
            et_d = dr.get("error_type", "")
            _note(f"→ deployment_result['success']=False, error_type={red(et_d)}")
            _note(f"→ total_deploy_attempts >= {_MAX_DEPLOY_TOTAL_RETRY}: deploy-phase backstop "
                  f"(ĐỘC LẬP total_val_attempts của A4) — dừng để không loop")
            _arrow_next(node, "requires_human",
                        f"backstop total_deploy_attempts={deploy_total}/{_MAX_DEPLOY_TOTAL_RETRY}")
        else:
            et_d = dr.get("error_type", "")
            _note(f"→ deployment_result['success']=False, error_type={red(et_d)}")
            _note(f"   deploy retry count={deploy_r}  total_deploy_attempts={deploy_total}/{_MAX_DEPLOY_TOTAL_RETRY}")
            print()
            if et_d == "TRANSIENT":
                _note("→ TRANSIENT: network timeout / connection refused / AWS rate limit")
                _note("   không phải lỗi code → retry A5 nguyên bản (không cần sửa gì)")
                _arrow_next(node, "deployment",
                            f"retry A5, lỗi mạng tạm thời, deploy_retry={deploy_r}/2")
            elif et_d in ("FIXABLE", "LOGIC"):
                _note("→ FIXABLE/LOGIC: lỗi apply do code HCL — A3 cần sửa rồi apply lại")
                _note("   fix_instruction đã được set vào state để A3 đọc")
                _arrow_next(node, "engineering",
                            f"lỗi code trong apply, A3 sửa, deploy_retry={deploy_r}/2")
            elif et_d == "MISSING_RESOURCE":
                _note("→ MISSING_RESOURCE: AWS báo resource phụ thuộc chưa tồn tại")
                _note("   cần re-plan ở A1 để thêm resource còn thiếu vào plan")
                _arrow_next(node, "architecture",
                            f"thiếu resource phụ thuộc, re-plan, deploy_retry={deploy_r}/2")
            else:
                _note("→ UNKNOWN hoặc hết budget deploy_retry")
                _arrow_next(node, "requires_human",
                            f"không xử lý được hoặc hết budget ({deploy_r})")


# ── Main trace ────────────────────────────────────────────────────────────────

def trace(prompt: str, no_deploy: bool = False) -> dict:
    """Chạy pipeline cho một prompt và in trace từng bước."""
    g = _select_graph(no_deploy)

    run_dir = ROOT / "tmp" / "trace"
    run_dir.mkdir(parents=True, exist_ok=True)
    _clean_trace_tf_dir(run_dir)
    state: dict = build_initial_state(prompt)
    state["run_dir"] = str(run_dir)

    _real_stdout = sys.stdout
    _writer = None
    try:
        # ── Header ────────────────────────────────────────────────────────────
        print(f"\n{BOLD}{white('█' * _W)}{R}")
        print(f"{BOLD}{white('  PIPELINE TRACE — Multi-Agent Terraform Generation')}{R}")
        print(f"{BOLD}{white('  LangGraph StateGraph: A1 → A2 → A3 → A4 → A5')}{R}")
        print(f"{BOLD}{white('█' * _W)}{R}")
        print(f"\n  {dim('prompt:')} {white(bold(prompt))}")
        if no_deploy:
            print(f"  {dim('flags:')}  {yellow('--no-deploy (dừng sau A4)')}")

        # ── Initial state ─────────────────────────────────────────────────────
        print(f"\n  {dim('─' * _W)}")
        print(f"  {bold('AGENTSTATE KHỞI TẠO')}")
        print(f"  {dim('LangGraph truyền state này qua mọi node. Mỗi agent chỉ đọc')}")
        print(f"  {dim('những field nó cần và ghi lại kết quả vào field của mình.')}")
        print()
        _item("prompt", prompt)
        print(f"  {dim('(tất cả counters, plans, code, feedback đều = 0 / empty)')}")
        print(f"  {dim('─' * _W)}")

        node_counts: dict[str, int] = {}
        current_state = dict(state)
        step = 0

        for chunk in g.stream(
            state,
            config={"recursion_limit": RECURSION_LIMIT},
            stream_mode="updates",
        ):
            for node_name, update in chunk.items():
                step += 1
                node_counts[node_name] = node_counts.get(node_name, 0) + 1
                repeat = node_counts[node_name]

                _agent_header(node_name, step, repeat)

                role = _AGENT_ROLE.get(node_name, "")
                if role:
                    print(f"\n  {cyan(bold('VAI TRÒ'))}")
                    for line in role.splitlines():
                        print(f"  {cyan(line)}")

                _explain_input(node_name, current_state)
                _explain_output(node_name, update or {}, current_state)
                if update:
                    current_state.update(update)
                _explain_routing(node_name, update or {}, current_state)

        # ── Kết quả cuối ──────────────────────────────────────────────────────
        fb   = current_state.get("fix_feedback") or {}
        dr   = current_state.get("deployment_result") or {}
        plan_s = current_state.get("infrastructure_plan") or {}
        code = current_state.get("generated_code", "")

        print(f"\n{BOLD}{white('═' * _W)}{R}")
        print(f"{BOLD}{white('  KẾT QUẢ CUỐI CÙNG')}{R}")
        print(f"{BOLD}{white('═' * _W)}{R}\n")

        def _final(label, val, good=None):
            col = green if good is True else (red if good is False else white)
            mark = green("✓") if good is True else (red("✗") if good is False else dim("·"))
            print(f"  {mark}  {dim(label + ':')} {col(str(val))}")

        _final("infrastructure_plan", f"{len(plan_s.get('resources', []))} resources")
        _final("generated_code",      f"{len(code)} chars")
        _final("terraform validate",  fb.get("validate_passed"), good=fb.get("validate_passed"))
        _final("terraform plan",      fb.get("plan_passed"),     good=fb.get("plan_passed"))
        _final("overall_passed (A4)", fb.get("overall_passed"),  good=fb.get("overall_passed"))
        if not no_deploy:
            _final("deployment.success", dr.get("success"), good=dr.get("success"))
        _final("total_val_attempts (val phase)", _rc(current_state, "total"))
        if not no_deploy:
            _final("total_deploy_attempts (deploy phase)", _rc(current_state, "deploy_total"))
            _final("deploy_retry", _rc(current_state, "deploy"))

        rl = current_state.get("routing_log") or []
        if rl:
            print(f"\n  {dim('routing_log (' + str(len(rl)) + ' entries):')}")
            for entry in rl:
                print(f"    {dim(str(entry))}")
        print()
    finally:
        sys.stdout = _real_stdout
        if _writer:
            _writer.close()

    return current_state


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Trace one prompt through the multi-agent Terraform pipeline.",
    )
    parser.add_argument(
        "prompt",
        nargs="?",
        default=None,
        help="IaC prompt to trace",
    )
    parser.add_argument(
        "--csv",
        type=Path,
        default=None,
        help="Dataset CSV path (default: dataset/data-dev.csv)",
    )
    parser.add_argument(
        "--case",
        type=int,
        default=0,
        help="Zero-based row index when using --csv",
    )
    parser.add_argument(
        "--no-deploy",
        action="store_true",
        help="Stop after A4 Validation",
    )
    parser.add_argument(
        "--logs",
        type=str,
        default=None,
        help="Log file path (ghi tất cả WARNING/INFO)",
    )
    args = parser.parse_args()

    # Setup logging to file
    if args.logs:
        log_path = Path(args.logs)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_formatter = logging.Formatter(
            "[%(levelname)s] %(name)s: %(message)s"
        )
        file_handler.setFormatter(file_formatter)
        logging.getLogger().addHandler(file_handler)

        # Suppress console output (chỉ ghi file)
        root_logger = logging.getLogger()
        for handler in root_logger.handlers[:]:
            if isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler):
                root_logger.removeHandler(handler)

        print(cyan(f"📝 Logs → {log_path.absolute()}"))
        print()

    if args.csv:
        csv_path = args.csv
        if not csv_path.is_absolute():
            csv_path = ROOT / csv_path
        prompt, difficulty = load_trace_prompt(csv_path, args.case)
        print(f"Trace CSV row {args.case} | difficulty={difficulty} | csv={csv_path}")
    else:
        prompt = args.prompt or "Create an S3 bucket with versioning and server-side encryption."

    from core.terraform import check_required_tools
    check_required_tools()

    trace(
        prompt,
        no_deploy=args.no_deploy,
    )
