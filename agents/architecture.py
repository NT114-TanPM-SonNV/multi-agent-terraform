"""Agent 1 — Architecture: prompt → JSON plan (resources + data_sources).

Re-prompt in-node nếu plan có defect cấu trúc. Reset val_eng/deploy_eng/sec sau
mỗi re-plan vì code cũ không còn liên quan với lỗi mới.
"""
import logging
import json
import re
import time

from core.state import AgentState
from core.llm import call_llm
from core.errors import make_fail, recent_fix_instructions
from core.parsers import parse_llm_json
from core.retry_control import new_tracker
from prompts.architecture import SYSTEM_PROMPT, DEFECT_FIX, ARCH_FIX_HEADER, ARCH_PREV_ATTEMPTS

logger = logging.getLogger(__name__)

_MAX_LLM_TRANSIENT_RETRY = 1
_LLM_TRANSIENT_BACKOFF = 2
_MAX_PLAN_REPAIR_RETRY = 2
_LLM_TRANSIENT_HINTS = (
    "connection error",
    "apiconnectionerror",
    "timeout",
    "timed out",
    "proxyerror",
    "rate limit",
    "too many requests",
    "temporarily unavailable",
    "service unavailable",
    "502",
    "503",
    "504",
)

# ──────────────────────────────────────────────────────────────────────────────
# HELPERS: Parse & Validate plan JSON
# ──────────────────────────────────────────────────────────────────────────────


def _parse_plan(raw: str) -> dict:
    """Parse JSON từ LLM response thành plan dict.

    Chỉ require 'resources' là list. 'data_sources' default [] nếu LLM bỏ qua.
    setdefault attributes/blocks ở đây vì A2/A3 subscript 2 key này trực tiếp.
    """
    plan = parse_llm_json(raw, {"resources": list})
    if not isinstance(plan.get("data_sources"), list):
        plan["data_sources"] = []
    for section in ("resources", "data_sources"):
        for obj in plan.get(section, []):
            if isinstance(obj, dict):
                obj.setdefault("attributes", {})
                obj.setdefault("blocks", {})
    return plan


def _plan_defects(plan: dict) -> list[str]:
    """Kiểm tra structure của plan — báo lỗi để LLM tự sửa, không drop âm thầm.

    Messages viết bằng English vì được đút vào prompt LLM.
    """
    defects: list[str] = []
    if not plan.get("resources"):
        defects.append("'resources' is empty — no infrastructure to generate")
        return defects
    for section in ("resources", "data_sources"):
        seen: set[str] = set()
        for i, obj in enumerate(plan.get(section, [])):
            if not isinstance(obj, dict):
                defects.append(f"{section}[{i}] is not a JSON object")
                continue
            t, n = obj.get("type"), obj.get("name")
            if not t or not n:
                missing = "type" if not t else "name"
                defects.append(f"{section}[{i}] is missing '{missing}'")
                continue
            label = f"{t}.{n}"
            if label in seen:
                defects.append(f"{section} declares '{label}' more than once")
            seen.add(label)
    return defects


_REF_RE = re.compile(r"^REF:(data\.)?([A-Za-z0-9_]+)\.([A-Za-z0-9_]+)\.[A-Za-z0-9_]+$")
_PLACEHOLDER_RE = re.compile(r"\b(todo|tbd|replace[_ -]?me|your[_ -]?(?:value|id|arn|name))\b", re.I)

def _walk_values(value):
    if isinstance(value, dict):
        for v in value.values():
            yield from _walk_values(v)
    elif isinstance(value, list):
        for v in value:
            yield from _walk_values(v)
    else:
        yield value


def _semantic_plan_defects(plan: dict) -> list[str]:
    """Lightweight consistency checks. LLM still performs the correction."""
    defects: list[str] = []
    res_labels = {f"{r.get('type')}.{r.get('name')}" for r in plan.get("resources", [])}
    data_labels = {f"data.{d.get('type')}.{d.get('name')}" for d in plan.get("data_sources", [])}

    for section in ("resources", "data_sources"):
        for obj in plan.get(section, []):
            if not isinstance(obj, dict):
                continue
            label = f"{'data.' if section == 'data_sources' else ''}{obj.get('type')}.{obj.get('name')}"
            for value in _walk_values({"attributes": obj.get("attributes", {}), "blocks": obj.get("blocks", {})}):
                if value is None:
                    defects.append(f"{label} contains null; use a concrete deployable value or omit the argument")
                    continue
                if not isinstance(value, str):
                    continue
                if _PLACEHOLDER_RE.search(value):
                    defects.append(f"{label} contains placeholder value '{value}'")
                m = _REF_RE.match(value)
                if m:
                    target = f"data.{m.group(2)}.{m.group(3)}" if m.group(1) else f"{m.group(2)}.{m.group(3)}"
                    if target not in data_labels and target not in res_labels:
                        defects.append(f"{label} has unresolved reference {value}")

    return defects


def _is_transient_llm_error(error: Exception) -> bool:
    text = f"{type(error).__name__}: {error}".lower()
    return any(hint in text for hint in _LLM_TRANSIENT_HINTS)


def _call_architecture_llm(messages: list[dict]) -> str:
    """Call the architecture LLM with one extra transient retry outside call_llm()."""
    last_error: Exception | None = None
    for attempt in range(_MAX_LLM_TRANSIENT_RETRY + 1):
        try:
            return call_llm(messages, agent="architecture")
        except Exception as e:
            last_error = e
            if attempt >= _MAX_LLM_TRANSIENT_RETRY or not _is_transient_llm_error(e):
                raise
            time.sleep(_LLM_TRANSIENT_BACKOFF * (attempt + 1))
    assert last_error is not None
    raise last_error


def _fail_architecture(fix_instruction: str, error_label: str, error_stage: str,
                       *, error_type: str = "INFRASTRUCTURE", raw_error: str | None = None) -> dict:
    logger.warning("Archi agent: FAIL %s [%s/%s]", error_type, error_stage, error_label)
    result = make_fail(error_type, None, fix_instruction)
    fb = result["fix_feedback"]
    fb["error_label"] = error_label
    fb["error_stage"] = error_stage
    if raw_error:
        fb["raw_error"] = raw_error[:2000]
    return result


# ──────────────────────────────────────────────────────────────────────────────
# MAIN LOGIC: LLM prompt + retry
# ──────────────────────────────────────────────────────────────────────────────

def architecture_node(state: AgentState) -> dict:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user",   "content": state["prompt"]},
    ]

    fix_feedback = state["fix_feedback"]
    fix_instruction = fix_feedback.get("fix_instruction", "")
    if fix_instruction and fix_feedback.get("root_cause") == "architecture":
        fix_msg = ARCH_FIX_HEADER.format(fix_instruction=fix_instruction)
        past = recent_fix_instructions(state["arch_error_history"], max_chars=400,
                                       exclude=fix_instruction)
        if past:
            fix_msg += ARCH_PREV_ATTEMPTS + "\n".join(f"- {p}" for p in past)
        messages.append({"role": "user", "content": fix_msg})
    elif fix_instruction:
        logger.debug("Archi: fix_instruction ignored (root_cause=%s)", fix_feedback.get("root_cause"))

    try:
        raw = _call_architecture_llm(messages)
        plan = _parse_plan(raw)
    except Exception as e:
        msg = str(e).lower()
        label = "LLM_TRANSIENT" if _is_transient_llm_error(e) else ("INVALID_JSON" if "json" in msg or "parse" in msg else "LLM_ERROR")
        return _fail_architecture(f"Archi agent error: {e}", label, "initial", raw_error=str(e))

    defects = _plan_defects(plan) + _semantic_plan_defects(plan)
    for repair_round in range(_MAX_PLAN_REPAIR_RETRY):
        if not defects:
            break
        logger.warning("Archi agent: %d defect — re-prompt: %s", len(defects), defects[:5])
        retry_msgs = messages + [
            {"role": "assistant", "content": raw},
            {"role": "user", "content": DEFECT_FIX.format(
                defects="\n".join(f"- {d}" for d in defects))},
        ]
        try:
            raw = _call_architecture_llm(retry_msgs)
            plan = _parse_plan(raw)
        except Exception as e:
            msg = str(e).lower()
            label = "REPAIR_LLM_TRANSIENT" if _is_transient_llm_error(e) else ("INVALID_JSON" if "json" in msg or "parse" in msg else "REPAIR_LLM_ERROR")
            return _fail_architecture(f"Archi agent retry error: {e}", label, "repair", raw_error=str(e))
        defects = _plan_defects(plan) + _semantic_plan_defects(plan)
    if defects:
        return _fail_architecture(
            f"Plan still has defects after retry: {defects[:3]}",
            "PLAN_REPAIR_EXHAUSTED",
            "repair",
            raw_error="; ".join(defects[:5]),
        )

    logger.info("Archi agent: %d resources, %d data_sources",
                len(plan["resources"]), len(plan["data_sources"]))

    # Reset val_eng/deploy_eng/sec — lỗi cũ không còn liên quan sau re-plan.
    # val_arch/deploy_arch không reset (budget vòng re-plan).
    out: dict = {
        "infrastructure_plan": plan,
        "fix_feedback": {},
        "retries": {
            **state["retries"],
            "val_eng":    new_tracker(),
            "deploy_eng": new_tracker(),
            "sec":        new_tracker(),
        },
    }
    if fix_instruction and fix_feedback.get("root_cause") == "architecture":
        out["arch_error_history"] = (state["arch_error_history"] + [{"fix_instruction": fix_instruction}])[-5:]
    return out
