"""Engineering Agent — Agent 3 trong pipeline.

Nhận infrastructure_plan + security_constraints, sinh Terraform HCL hoàn chỉnh.
Security constraints từ Agent 2 được merge trực tiếp vào resource blocks tương ứng.

Khối `terraform {}` + `provider "aws" {}` (với Floci endpoints) là FILE TĨNH
core/provider.tf — Agent 3 chỉ prepend nguyên trạng, KHÔNG để LLM sinh. URL dùng
localhost:4566; dataset/evaluator._substitute_endpoint() tự thay bằng FLOCI_ENDPOINT
thật lúc eval, nên không cần inject endpoint động ở đây.

Output: generated_code (chuỗi HCL) ghi vào LangGraph State.
"""
import json
import logging
import re
from pathlib import Path

from core.state import AgentState
from core.llm import call_llm
from core.errors import make_infra_error
from core.parsers import strip_code_block
from prompts.engineering import SYSTEM_PROMPT as _SYSTEM_PROMPT
from prompts.engineering import TOP_PROMPT as _TOP, BOTTOM_PROMPT as _BOTTOM

logger = logging.getLogger(__name__)

# Strip CoT reasoning block mà LLM sinh trước HCL (giống Agent 2).
_PLAN_TAG = re.compile(r"<plan>.*?</plan>", re.DOTALL | re.IGNORECASE)

# Provider block tĩnh — endpoints đã verify bằng terraform validate (xem core/provider.tf).
_PROVIDER_BLOCK = (Path(__file__).parent.parent / "core" / "provider.tf").read_text(encoding="utf-8").strip()

# Khớp khối top-level `terraform {` hoặc `provider "..." {` mà LLM có thể lỡ sinh.
_BLOCK_HEADER = re.compile(r'(?m)^[ \t]*(terraform|provider)\b[^\n{]*\{')

_RESOURCE_DECL_RE = re.compile(r'resource\s+"([^"]+)"\s+"([^"]+)"')
_ATTR_RE = re.compile(r'^\s*(\w+)\s*=\s*(.+)', re.MULTILINE)


def _strip_injected_blocks(hcl: str) -> str:
    """Xóa mọi khối terraform{} / provider{} do LLM sinh để tránh trùng lặp.

    Code tự prepend provider block riêng; nếu LLM cũng sinh thì terraform validate
    sẽ fail vì duplicate provider/required_providers. Dùng đếm ngoặc để xóa đúng
    phạm vi khối, không dùng regex một lần (block lồng có ngoặc bên trong).
    """
    while True:
        m = _BLOCK_HEADER.search(hcl)
        if not m:
            return hcl
        # Vị trí dấu { mở khối (ký tự cuối của match)
        open_idx = m.end() - 1
        depth = 0
        end_idx = None
        for i in range(open_idx, len(hcl)):
            if hcl[i] == "{":
                depth += 1
            elif hcl[i] == "}":
                depth -= 1
                if depth == 0:
                    end_idx = i + 1
                    break
        if end_idx is None:
            # Khối không đóng (HCL hỏng) — xóa từ header tới hết để guard bắt empty
            return hcl[: m.start()].strip()
        hcl = (hcl[: m.start()] + hcl[end_idx:]).strip()


def _code_signature(hcl: str) -> frozenset:
    """Normalized signature: resource declarations + attribute assignments.

    Bắt được cả thay đổi cấu trúc (thêm/bớt resource) lẫn thay đổi attribute
    (encrypted = false → encrypted = true) do fix_instruction yêu cầu.
    """
    sig = set()
    for m in _RESOURCE_DECL_RE.finditer(hcl):
        sig.add(("res", m.group(1), m.group(2)))
    for m in _ATTR_RE.finditer(hcl):
        key = m.group(1).strip()
        val = m.group(2).strip().rstrip(",").strip('"')
        sig.add(("attr", key, val))
    return frozenset(sig)


def _code_unchanged(old: str, new: str) -> bool:
    """Kiểm tra LLM có thực sự thay đổi code sau fix_instruction không."""
    if not old:
        return False
    return _code_signature(old) == _code_signature(new)


def engineering_node(state: AgentState) -> dict:
    """LangGraph node function cho Engineering Agent.

    Đọc plan + constraints (+ fix_instruction nếu retry), gọi LLM sinh HCL body,
    strip block provider/terraform thừa, prepend provider block tĩnh.
    """
    plan = state["infrastructure_plan"]
    if not plan.get("resources"):
        return make_infra_error(
            "Engineering agent nhận infrastructure_plan rỗng — Agent 1 phải chạy trước."
        )

    constraints = state.get("security_constraints") or {}
    _validation = state.get("validation_result") or {}
    fix = _validation.get("fix_instruction")
    _root_cause = _validation.get("root_cause")

    plan_json = json.dumps(plan, indent=2)
    constraints_json = json.dumps(constraints, indent=2)

    # Retry prompt chỉ khi fix này thực sự dành cho Engineering Agent
    if fix and _root_cause == "engineering" and state["eng_retry_count"] > 0:
        old_code = state.get("generated_code") or "N/A"
        user_content = (
            _TOP
            + f"Infrastructure plan:\n{plan_json}\n\n"
            + f"Security constraints:\n{constraints_json}\n"
            + _BOTTOM
            + f"\n\nPREVIOUS CODE FAILED VALIDATION.\n"
            + f"Previous code:\n{old_code}\n\n"
            + f"Fix instruction:\n{fix}\n\n"
            + "Apply the fix. fix_instruction takes priority over security_constraints if they conflict. "
            + "Keep correct parts of the previous code unchanged."
        )
    else:
        user_content = (
            _TOP
            + f"Infrastructure plan:\n{plan_json}\n\n"
            + f"Security constraints:\n{constraints_json}\n"
            + _BOTTOM
        )

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]

    raw = ""
    try:
        raw = call_llm(messages)
    except TimeoutError as e:
        logger.error("Engineering agent timeout: %s", e)
        return make_infra_error(f"Engineering agent LLM timeout: {e}")
    except Exception as e:
        logger.error("Engineering agent unexpected error: %s", e)
        return make_infra_error(f"Engineering agent unexpected error: {e}")

    body = _strip_injected_blocks(_PLAN_TAG.sub("", strip_code_block(raw)).strip())

    # Guard: phải có ít nhất một resource block — retry một lần nếu thiếu
    if 'resource "' not in body:
        logger.warning("Engineering agent: không có resource block — retry với explicit note")
        retry_msgs = messages + [
            {"role": "assistant", "content": raw},
            {"role": "user", "content":
             "Your response did not contain any `resource \"` blocks. "
             "Please write the complete Terraform HCL with ALL resource blocks "
             "required by the infrastructure plan. Do not omit any resource."},
        ]
        try:
            raw = call_llm(retry_msgs)
        except TimeoutError as e:
            logger.error("Engineering agent retry timeout: %s", e)
            return make_infra_error(f"Engineering agent LLM timeout on retry: {e}")
        except Exception as e:
            logger.error("Engineering agent retry error: %s", e)
            return make_infra_error(f"Engineering agent retry error: {e}")
        body = _strip_injected_blocks(strip_code_block(_PLAN_TAG.sub("", raw)).strip())
        if 'resource "' not in body:
            return make_infra_error(
                f"Engineering agent không sinh được resource block nào (sau retry). Raw: {raw[:300]}"
            )

    # Retry guard: nếu LLM bỏ qua fix_instruction và trả về code giống hệt,
    # escalate ngay thay vì để Agent 4 tốn thêm một vòng validate.
    old_code = state.get("generated_code") or ""
    if fix and _root_cause == "engineering" and state["eng_retry_count"] > 0 and old_code:
        old_body = _strip_injected_blocks(strip_code_block(old_code))
        if _code_unchanged(old_body, body):
            logger.warning("Engineering agent trả về code giống hệt sau fix_instruction")
            return make_infra_error(
                "Engineering agent returned identical HCL after fix instruction. "
                f"Expected changes based on: {fix}"
            )

    # Coverage check: log warning cho resource trong plan nhưng vắng mặt trong HCL
    plan_pairs = {(r["type"], r["name"]) for r in plan.get("resources", [])}
    gen_pairs = set(_RESOURCE_DECL_RE.findall(body))
    missing = plan_pairs - gen_pairs
    if missing:
        logger.warning(
            "Engineering agent HCL thiếu %d resource từ plan: %s",
            len(missing), sorted(missing),
        )

    generated_code = f"{_PROVIDER_BLOCK}\n\n{body}\n"

    logger.info("Engineering agent: %d ký tự HCL", len(generated_code))
    return {"generated_code": generated_code}
