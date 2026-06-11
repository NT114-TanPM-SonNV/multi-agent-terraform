"""Architecture agent: prompt to JSON infrastructure plan."""
import logging

from core.errors import build_fail_result, recent_fix_instructions
from core.llm import call_llm
from core.parsers import parse_llm_json
from core.retry_control import new_tracker
from core.state import AgentState
from prompts.architecture import (
    FIX_HEADER,
    PREV_ATTEMPTS_HEADER,
    DEFECT_RETRY,
    SYSTEM_PROMPT,
)

logger = logging.getLogger(__name__)

_MAX_ATTEMPTS = 3


def _normalize_plan_entry(section: str, index: int, obj: object) -> dict:
    """Validate and normalize one resource or data-source entry."""
    if not isinstance(obj, dict):
        raise TypeError(
            f"{section}[{index}] must be a JSON object, got {type(obj).__name__}"
        )

    rtype = obj.get("type")
    name = obj.get("name")
    if not isinstance(rtype, str) or not rtype.strip():
        raise TypeError(f"{section}[{index}].type must be a non-empty string")
    if not isinstance(name, str) or not name.strip():
        raise TypeError(f"{section}[{index}].name must be a non-empty string")

    attributes = obj.get("attributes", {})
    blocks = obj.get("blocks", {})
    if not isinstance(attributes, dict):
        raise TypeError(
            f"{section}[{index}].attributes must be a JSON object, got {type(attributes).__name__}"
        )
    if not isinstance(blocks, dict):
        raise TypeError(
            f"{section}[{index}].blocks must be a JSON object, got {type(blocks).__name__}"
        )

    return {
        **obj,
        "type": rtype.strip(),
        "name": name.strip(),
        "attributes": attributes,
        "blocks": blocks,
    }


def _parse_plan(raw: str) -> dict:
    """Parse the model output into a normalized plan."""
    plan = parse_llm_json(raw, {"resources": list})
    data_sources = plan.get("data_sources", [])
    if not isinstance(data_sources, list):
        raise TypeError(
            f"Field 'data_sources' must be a list, got {type(data_sources).__name__}"
        )

    return {
        **plan,
        "resources": [
            _normalize_plan_entry("resources", i, obj)
            for i, obj in enumerate(plan["resources"])
        ],
        "data_sources": [
            _normalize_plan_entry("data_sources", i, obj)
            for i, obj in enumerate(data_sources)
        ],
    }


def _plan_defects(plan: dict) -> list[str]:
    """Return structural defects that A1 can repair directly."""
    defects: list[str] = []
    resources = plan["resources"]
    data_sources = plan["data_sources"]

    if not resources:
        defects.append("'resources' is empty — no infrastructure to generate")
        return defects

    for section, items, prefix in (
        ("resources", resources, ""),
        ("data_sources", data_sources, "data."),
    ):
        seen: set[str] = set()
        for obj in items:
            label = f"{prefix}{obj['type']}.{obj['name']}"
            if label in seen:
                defects.append(f"{section} declares '{label}' more than once")
            seen.add(label)
    return defects


def _architecture_fix_message(state: AgentState, fix_instruction: str) -> str:
    """Build the repair prompt for architecture feedback."""
    fix_msg = FIX_HEADER.format(fix_instruction=fix_instruction)
    past = recent_fix_instructions(
        state["arch_error_history"],
        max_chars=400,
        exclude=fix_instruction,
    )
    if past:
        fix_msg += PREV_ATTEMPTS_HEADER + "\n".join(f"- {p}" for p in past)
    return fix_msg


def _build_architecture_messages(state: AgentState) -> tuple[list[dict], str, dict]:
    """Build the base prompt for A1 and append repair context if needed."""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": state["prompt"]},
    ]

    fix_feedback = state["fix_feedback"]
    fix_instruction = fix_feedback.get("fix_instruction", "")
    if fix_instruction and fix_feedback.get("root_cause") == "architecture":
        messages.append({
            "role": "user",
            "content": _architecture_fix_message(state, fix_instruction),
        })
    elif fix_instruction:
        logger.debug(
            "Archi: fix_instruction ignored (root_cause=%s)",
            fix_feedback.get("root_cause"),
        )
    return messages, fix_instruction, fix_feedback


def _make_architecture_failure(
    fix_instruction: str,
    error_stage: str,
    raw_error: str,
) -> dict:
    """Build the standard failure payload for A1."""
    logger.warning("Archi agent: FAIL INFRASTRUCTURE [%s]", error_stage)
    result = build_fail_result("INFRASTRUCTURE", None, fix_instruction)
    feedback = result["fix_feedback"]
    feedback["error_stage"] = error_stage
    feedback["raw_error"] = raw_error[:2000]
    return result


def _run_architecture_attempt(messages: list[dict]) -> tuple[str, dict, list[str]]:
    """Run one architecture attempt."""
    raw = call_llm(messages, agent="architecture")
    plan = _parse_plan(raw)
    defects = _plan_defects(plan)
    return raw, plan, defects


def architecture_node(state: AgentState) -> dict:
    """Run initial and repair attempts until the plan is usable."""
    base_messages, fix_instruction, fix_feedback = _build_architecture_messages(state)

    current_messages = base_messages
    raw = ""
    plan: dict = {}
    defects: list[str] = []

    for attempt_index in range(_MAX_ATTEMPTS):
        is_repair = attempt_index > 0
        stage = "repair" if is_repair else "initial"
        try:
            raw, plan, defects = _run_architecture_attempt(current_messages)
        except Exception as e:
            prefix = "Archi agent retry error" if is_repair else "Archi agent error"
            return _make_architecture_failure(f"{prefix}: {e}", stage, str(e))

        if not defects:
            break

        if attempt_index >= _MAX_ATTEMPTS - 1:
            return _make_architecture_failure(
                f"Plan still has defects after repair attempts: {defects[:3]}",
                "repair",
                "; ".join(defects[:5]),
            )

        logger.warning(
            "Archi agent: %d defect — re-prompt: %s",
            len(defects),
            defects[:5],
        )
        current_messages = base_messages + [
            {"role": "assistant", "content": raw},
            {
                "role": "user",
                "content": DEFECT_RETRY.format(
                    defects="\n".join(f"- {d}" for d in defects)
                ),
            },
        ]

    logger.info(
        "Archi agent: %d resources, %d data_sources",
        len(plan["resources"]),
        len(plan["data_sources"]),
    )

    out: dict = {
        "infrastructure_plan": plan,
        "fix_feedback": {},
        "retries": {
            **state["retries"],
            "val_eng": new_tracker(),
            "deploy_eng": new_tracker(),
            "sec": new_tracker(),
        },
    }
    if fix_instruction and fix_feedback.get("root_cause") == "architecture":
        out["arch_error_history"] = (
            state["arch_error_history"] + [{"fix_instruction": fix_instruction}]
        )[-5:]
    return out
