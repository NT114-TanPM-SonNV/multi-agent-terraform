"""Security Agent — Agent 2 trong pipeline.

Nhận infrastructure_plan từ Agent 1, thêm các security attribute cơ bản
cho từng resource. Chỉ xử lý flat HCL attrs (không nested block).

Output: security_constraints dict ghi vào LangGraph State.
  {"aws_db_instance.main": {"storage_encrypted": true, "deletion_protection": true}, ...}
"""
import json
import logging
import re

from core.state import AgentState
from core.llm import call_llm_with_parse_retry
from core.errors import make_infra_error
from core.parsers import parse_llm_json
from prompts.security_v2 import SYSTEM_PROMPT as _SYSTEM_PROMPT
from prompts.security_v2 import TOP_PROMPT as _TOP, BOTTOM_PROMPT as _BOTTOM

logger = logging.getLogger(__name__)

_PLAN_TAG = re.compile(r"<plan>.*?</plan>", re.DOTALL | re.IGNORECASE)


def _parse_security_response(text: str) -> dict:
    return parse_llm_json(text, {"security_constraints": dict})


def _split_constraints(raw_constraints: dict) -> tuple[dict, dict]:
    """Tách LLM output thành 2 dict riêng biệt.

    Input:  {"type.name": {"attr": {"value": <val>, "ckv_id": "CKV_AWS_17"|null}}}
    Output: (flat_attrs, ckv_ids)
      flat_attrs = {"type.name": {"attr": <val>}}
                   ← A3 dùng để inject vào HCL (flat primitive)
      ckv_ids    = {"type.name": {"attr": "CKV_AWS_17"}}
                   ← A4 dùng để filter Checkov (chỉ chứa attrs có ckv_id non-null)

    Backward-compatible: nếu LLM sinh flat value thay vì {"value":...,"ckv_id":...},
    vẫn accept và coi ckv_id = null (attr sẽ fallback sang text check trong A4).
    """
    flat_attrs: dict = {}
    ckv_ids: dict = {}
    for resource_label, attrs in raw_constraints.items():
        if not isinstance(attrs, dict):
            continue
        flat: dict = {}
        per_attr_ckv: dict = {}
        for attr, spec in attrs.items():
            if isinstance(spec, dict) and "value" in spec:
                flat[attr] = spec["value"]
                ckv = spec.get("ckv_id")
                if ckv and isinstance(ckv, str):
                    per_attr_ckv[attr] = ckv
            else:
                # backward-compat: LLM sinh flat value trực tiếp → ckv_id = null
                flat[attr] = spec
        if flat:
            flat_attrs[resource_label] = flat
        if per_attr_ckv:
            ckv_ids[resource_label] = per_attr_ckv
    return flat_attrs, ckv_ids


def _constraints_unchanged(old: dict, new: dict) -> bool:
    """Kiểm tra LLM có thực sự thay đổi constraints sau fix instruction không."""
    if not old:
        return False
    return old == new


def security_node(state: AgentState) -> dict:
    """LangGraph node function cho Security Agent."""
    _validation = state.get("validation_result") or {}
    fix         = _validation.get("fix_instruction")
    _root_cause = _validation.get("root_cause")
    plan        = state["infrastructure_plan"]
    old_constraints = state.get("security_constraints") or {}

    plan_json = json.dumps(plan, indent=2)

    if fix and _root_cause == "security" and state["sec_retry_count"] > 0:
        user_content = (
            _TOP + plan_json + "\n\n"
            f"PREVIOUS CONSTRAINTS WERE WRONG.\n"
            f"Previous constraints:\n{json.dumps(old_constraints, indent=2)}\n\n"
            f"Fix instruction:\n{fix}\n\n"
            "Patch ONLY what is indicated. Keep all others unchanged."
            + _BOTTOM
        )
    else:
        user_content = _TOP + plan_json + _BOTTOM

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user",   "content": user_content},
    ]

    raw = ""
    try:
        raw, parsed = call_llm_with_parse_retry(messages, _parse_security_response)
        raw_constraints = parsed["security_constraints"]
    except TimeoutError as e:
        logger.error("Security agent timeout: %s", e)
        return make_infra_error(f"Security agent LLM timeout: {e}")
    except (ValueError, KeyError, TypeError) as e:
        stripped = _PLAN_TAG.sub("", raw).strip() if raw else ""
        if not stripped:
            logger.info("Security agent: no JSON output (no constraints needed)")
            return {"security_constraints": {}, "security_ckv_ids": {}}
        logger.error("Security agent parse error: %s | raw: %.300s", e, raw)
        return make_infra_error(f"Security agent parse error: {e}. Raw: {raw[:300]}")
    except Exception as e:
        logger.error("Security agent unexpected error: %s", e)
        return make_infra_error(f"Security agent unexpected error: {e}")

    # Tách thành flat_attrs (cho A3) và ckv_ids (cho A4)
    new_constraints, new_ckv_ids = _split_constraints(raw_constraints)

    # Retry guard
    if fix and _root_cause == "security" and state["sec_retry_count"] > 0:
        if _constraints_unchanged(old_constraints, new_constraints):
            return make_infra_error(
                "Security agent returned identical constraints after fix instruction. "
                f"Expected change based on: {fix}"
            )

    # Drop constraints trỏ tới resource không tồn tại trong plan
    plan_keys = {f"{r['type']}.{r['name']}" for r in plan.get("resources", [])}
    valid_constraints = {k: v for k, v in new_constraints.items() if k in plan_keys and v}
    valid_ckv_ids     = {k: v for k, v in new_ckv_ids.items()     if k in plan_keys and v}
    dropped = len(new_constraints) - len(valid_constraints)
    if dropped:
        logger.warning("Security agent: dropped %d constraints (not in plan or empty)", dropped)

    all_ckv = [ckv for ids in valid_ckv_ids.values() for ckv in ids]
    logger.info("Security agent: %d resources, %d CKV IDs", len(valid_constraints), len(all_ckv))
    return {"security_constraints": valid_constraints, "security_ckv_ids": valid_ckv_ids}
