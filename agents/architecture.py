"""Architecture Agent — Agent 1 trong pipeline.

Nhận prompt ngôn ngữ tự nhiên, dùng LLM xác định các AWS resource
cần thiết và dependency giữa chúng. Không sinh HCL code.

Output: infrastructure_plan ghi vào LangGraph State.
"""
import json
import logging
import re

from core.state import AgentState
from core.llm import call_llm_with_parse_retry
from core.errors import make_infra_error
from core.parsers import parse_llm_json
from prompts.architecture_v2 import SYSTEM_PROMPT as _SYSTEM_PROMPT
from prompts.architecture_v2 import TOP_PROMPT as _TOP, INSTRUCTIONS_PROMPT as _INSTRUCTIONS

logger = logging.getLogger(__name__)

_PLAN_TAG = re.compile(r"<plan>.*?</plan>", re.DOTALL | re.IGNORECASE)


def _strip_plan_tag(text: str) -> str:
    return _PLAN_TAG.sub("", text).strip()


def _parse_arch_response(text: str) -> dict:
    return parse_llm_json(_strip_plan_tag(text), {
        "resources": list,
        "data_sources": list,
        "dependencies": dict,
    })


def _plan_signature(plan: dict) -> frozenset:
    """Tính signature của plan để so sánh order-insensitive.

    Dùng frozenset thay vì == trực tiếp vì LLM có thể trả về cùng resources
    nhưng khác thứ tự — frozenset loại bỏ order, chỉ so sánh nội dung.

    Chỉ so sánh cấu trúc (keys), không so sánh values — đủ để detect
    trường hợp LLM bỏ qua fix instruction hoàn toàn (thêm/bớt resource,
    thêm/bớt attr). Value thay đổi nhỏ vẫn cho retry tiếp — chấp nhận được.
    """
    sig = []
    for r in plan.get("resources", []):
        sig.append((
            "res", r["type"], r["name"],
            tuple(sorted(r.get("attrs", {}).keys())),
            tuple(sorted(r.get("refs", {}).keys())),
        ))
    for d in plan.get("data_sources", []):
        sig.append((
            "ds", d["type"], d["name"],
            tuple(sorted((d.get("filters") or {}).keys())),
        ))
    for src, dests in sorted(plan.get("dependencies", {}).items()):
        for dst in sorted(set(dests)):
            sig.append(("dep", src, dst))
    return frozenset(sig)


def _plan_unchanged(old_plan: dict, new_plan: dict) -> bool:
    """Kiểm tra LLM có thực sự thay đổi plan sau fix instruction không.

    Trả về True (unchanged) → pipeline dừng sớm thay vì retry vô ích.
    Trả về False khi old_plan rỗng (lần chạy đầu, chưa có plan để so sánh).
    """
    if not old_plan:
        return False
    return _plan_signature(old_plan) == _plan_signature(new_plan)


def architecture_node(state: AgentState) -> dict:
    """LangGraph node function cho Architecture Agent.

    Chọn prompt phù hợp (initial hoặc retry), gọi LLM, parse kết quả,
    kiểm tra retry guard rồi ghi infrastructure_plan vào state.
    """
    _validation = state.get("validation_result") or {}
    fix = _validation.get("fix_instruction")
    _root_cause = _validation.get("root_cause")
    old_plan = state.get("infrastructure_plan") or {}

    if fix and _root_cause == "architecture" and state["arch_retry_count"] > 0:
        old_plan_json = json.dumps(old_plan, indent=2) if old_plan else "N/A"
        user_content = (
            _TOP + state["prompt"] + "</request>\n\n"
            f"PREVIOUS PLAN WAS REJECTED.\n"
            f"Previous plan:\n{old_plan_json}\n\n"
            f"Fix instruction:\n{fix}\n\n"
            "Incorporate the fix — add or modify resources as instructed. Keep valid resources unchanged.\n\n"
            + _INSTRUCTIONS
        )
    else:
        user_content = _TOP + state["prompt"] + "</request>\n\n" + _INSTRUCTIONS

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]

    raw = ""
    try:
        raw, new_plan = call_llm_with_parse_retry(messages, _parse_arch_response)
    except TimeoutError as e:
        logger.error("Architecture agent timeout: %s", e)
        return make_infra_error(f"Architecture agent LLM timeout: {e}")
    except (ValueError, KeyError, TypeError) as e:
        logger.error("Architecture agent parse error after retries: %s | raw: %.300s", e, raw)
        return make_infra_error(f"Architecture agent LLM parse error (after retries): {e}. Raw: {raw[:300]}")
    except Exception as e:
        logger.error("Architecture agent unexpected error: %s", e)
        return make_infra_error(f"Architecture agent unexpected error: {e}")

    if not new_plan.get("resources"):
        return make_infra_error(
            f"Architecture agent produced empty resource list. Prompt: {state['prompt'][:100]}"
        )

    for r in new_plan["resources"]:
        if "type" not in r or "name" not in r:
            return make_infra_error(f"Resource item thiếu 'type' hoặc 'name': {r}")
        r.setdefault("attrs", {})
        r.setdefault("refs", {})

    for d in new_plan.get("data_sources", []):
        if "type" not in d or "name" not in d:
            return make_infra_error(f"Data source item thiếu 'type' hoặc 'name': {d}")
        if isinstance(d.get("filters"), list):
            d["filters"] = {}

    if fix and _root_cause == "architecture" and state["arch_retry_count"] > 0 and _plan_unchanged(old_plan, new_plan):
        logger.warning("Architecture agent trả về plan giống hệt sau fix instruction")
        return make_infra_error(
            "Architecture agent returned identical resource types after fix instruction. "
            f"Expected new resources based on: {fix}"
        )

    logger.info("Architecture agent: %d resources", len(new_plan.get("resources", [])))
    return {"infrastructure_plan": new_plan}
