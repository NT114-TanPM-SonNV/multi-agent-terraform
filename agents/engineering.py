"""Agent 3: turn the plan and security profile into Terraform HCL."""
import json
import logging
import re

from core.state import AgentState
from core.llm import call_llm
from core.errors import build_fail_result, recent_fix_instructions
from core.parsers import strip_code_block, RESOURCE_DECL_RE as _RESOURCE_DECL_RE
from core.catalog import get_check_names
from prompts.engineering import (
    SYSTEM_PROMPT as _SYSTEM_PROMPT, USER_TEMPLATE as _USER_TEMPLATE,
    PATCH_HEADER, PREV_CODE_HEADER, PREV_ERRORS_HEADER, BOUNDARY_RETRY,
    NO_RESOURCE_RETRY,
)

logger = logging.getLogger(__name__)

_CKV_NAME: dict[str, str] = get_check_names()

_PLAN_TAG = re.compile(r"<plan>.*?</plan>", re.DOTALL | re.IGNORECASE)

# First valid HCL block in the model output.
_HCL_BLOCK_START = re.compile(r'(?:terraform\s*\{|provider\s+"|resource\s+"|data\s+"|variable\s+"|output\s+"|module\s+")')
_BACKEND_RE = re.compile(r'\bbackend\s+"[^"]+"\s*\{')
_MODULE_RE = re.compile(r'\bmodule\s+"([^"]+)"\s*\{')


def _strip_preamble(hcl: str) -> str:
    """Drop text before the first HCL block."""
    m = _HCL_BLOCK_START.search(hcl)
    return hcl[m.start():] if m else hcl


def _clean_hcl(raw: str) -> str:
    """Strip plan tags, fences, and preamble text."""
    cleaned = _PLAN_TAG.sub("", raw).strip()
    return _strip_preamble(strip_code_block(cleaned).strip())


def _planned_resource_pairs(plan: dict) -> set[tuple[str, str]]:
    """Return the planned resource pairs."""
    return {
        (r.get("type", ""), r.get("name", ""))
        for r in plan.get("resources", [])
        if r.get("type") and r.get("name")
    }


def _planned_data_pairs(plan: dict) -> set[tuple[str, str]]:
    """Return the planned data-source pairs."""
    return {
        (d.get("type", ""), d.get("name", ""))
        for d in plan.get("data_sources", [])
        if d.get("type") and d.get("name")
    }


_DATA_DECL_RE = re.compile(r'data\s+"([^"]+)"\s+"([^"]+)"')


def _boundary_defects(plan: dict, hcl: str) -> list[str]:
    """Return plan-boundary defects in generated HCL."""
    planned = _planned_resource_pairs(plan)
    generated_list = _RESOURCE_DECL_RE.findall(hcl)
    generated = set(generated_list)
    planned_data = _planned_data_pairs(plan)
    generated_data_list = _DATA_DECL_RE.findall(hcl)
    generated_data = set(generated_data_list)
    defects: list[str] = []

    extra = sorted(f"{t}.{n}" for t, n in generated - planned)
    missing = sorted(f"{t}.{n}" for t, n in planned - generated)
    extra_data = sorted(f"data.{t}.{n}" for t, n in generated_data - planned_data)
    missing_data = sorted(f"data.{t}.{n}" for t, n in planned_data - generated_data)
    dup = sorted(f"{t}.{n}" for t, n in {pair for pair in generated_list if generated_list.count(pair) > 1})
    dup_data = sorted(f"data.{t}.{n}" for t, n in {pair for pair in generated_data_list if generated_data_list.count(pair) > 1})
    if extra:
        defects.append("extra managed resources not in plan: " + ", ".join(extra))
    if missing:
        defects.append("missing managed resources from plan: " + ", ".join(missing))
    if dup:
        defects.append("duplicate managed resource declarations: " + ", ".join(dup))
    if extra_data:
        defects.append("extra data sources not in plan: " + ", ".join(extra_data))
    if missing_data:
        defects.append("missing data sources from plan: " + ", ".join(missing_data))
    if dup_data:
        defects.append("duplicate data source declarations: " + ", ".join(dup_data))
    if _BACKEND_RE.search(hcl):
        defects.append("terraform backend blocks are not allowed")
    modules = sorted(set(_MODULE_RE.findall(hcl)))
    if modules:
        defects.append("module blocks are not allowed: " + ", ".join(modules))
    return defects


def _security_context(security_profile: dict[str, dict]) -> str:
    """Render selected checks as prompt text."""
    lines: list[str] = []
    for label, info in security_profile.items():
        checks = info.get("checks", [])
        if not checks:
            continue
        lines.append(f"  {label}:")
        for cid in checks:
            name = _CKV_NAME.get(cid, cid)
            lines.append(f"    - {cid}: {name}")
    return "\n".join(lines) or "  (no security checks selected)"


def _engineering_fix_message(state: AgentState, fix_instruction: str) -> str:
    """Build the repair prompt for engineering feedback."""
    fix_msg = PATCH_HEADER + fix_instruction
    if state["generated_code"]:
        fix_msg += PREV_CODE_HEADER + state["generated_code"]
    past = recent_fix_instructions(
        state["eng_error_history"],
        max_chars=200,
        exclude=fix_instruction,
    )
    if past:
        fix_msg += PREV_ERRORS_HEADER + "\n".join(f"- {p}" for p in past)
    return fix_msg


def _build_engineering_messages(state: AgentState) -> list[dict]:
    """Build the base prompt for A3 and append repair context if needed."""
    user_content = _USER_TEMPLATE.format(
        PLAN=json.dumps(state["infrastructure_plan"]),
        SECURITY_CONTEXT=_security_context(state["security_profile"]),
    )

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]

    fix_feedback = state["fix_feedback"]
    fix_instruction = fix_feedback.get("fix_instruction", "")
    if fix_instruction and fix_feedback.get("root_cause") == "engineering":
        messages.append({
            "role": "user",
            "content": _engineering_fix_message(state, fix_instruction),
        })
    elif fix_instruction:
        logger.debug(
            "Engi: fix_instruction ignored (root_cause=%s)",
            fix_feedback.get("root_cause"),
        )
    return messages


def _make_engineering_failure(
    fix_instruction: str,
    error_label: str,
    error_stage: str,
    *,
    raw_error: str | None = None,
) -> dict:
    """Build the standard failure payload for A3."""
    logger.warning("Engineering agent: FAIL INFRASTRUCTURE [%s/%s]", error_stage, error_label)
    result = build_fail_result("INFRASTRUCTURE", None, fix_instruction)
    fb = result["fix_feedback"]
    fb["error_label"] = error_label
    fb["error_stage"] = error_stage
    if raw_error:
        fb["raw_error"] = raw_error[:2000]
    return result


def _run_engineering_attempt(messages: list[dict]) -> tuple[str, str]:
    """Run one engineering attempt and return raw plus cleaned HCL."""
    raw = call_llm(messages, agent="engineering")
    return raw, _clean_hcl(raw)


def engineering_node(state: AgentState) -> dict:
    """Generate Terraform HCL from the plan and security context."""
    messages = _build_engineering_messages(state)

    fix_feedback = state["fix_feedback"]
    fix_instruction = fix_feedback.get("fix_instruction", "")

    try:
        raw, body = _run_engineering_attempt(messages)
    except TimeoutError as e:
        logger.error("Engineering agent timeout: %s", e)
        return _make_engineering_failure(f"Engineering agent LLM timeout: {e}", "LLM_TIMEOUT", "initial", raw_error=str(e))
    except Exception as e:
        logger.error("Engineering agent error: %s", e)
        return _make_engineering_failure(f"Engineering agent error: {e}", "LLM_ERROR", "initial", raw_error=str(e))

    if 'resource "' not in body:
        logger.warning("Engineering agent: no resource block — retry")
        retry_msgs = messages + [
            {"role": "assistant", "content": raw},
            {"role": "user", "content": NO_RESOURCE_RETRY},
        ]
        try:
            raw, body = _run_engineering_attempt(retry_msgs)
        except Exception as e:
            return _make_engineering_failure(f"Engineering agent retry error: {e}", "RETRY_LLM_ERROR", "no_resource_retry", raw_error=str(e))
        if 'resource "' not in body:
            return _make_engineering_failure(
                f"Engineering agent did not produce a resource block after retry. Raw: {raw[:300]}",
                "NO_RESOURCE_BLOCK",
                "no_resource_retry",
                raw_error=raw[:3000],
            )

    defects = _boundary_defects(state["infrastructure_plan"], body)
    if defects:
        logger.warning("Engineering agent: boundary defect — retry: %s", defects[:5])
        retry_msgs = messages + [
            {"role": "assistant", "content": raw},
            {"role": "user", "content": BOUNDARY_RETRY.format(
                defects="\n".join(f"- {d}" for d in defects)
            )},
        ]
        try:
            raw, body = _run_engineering_attempt(retry_msgs)
        except Exception as e:
            return _make_engineering_failure(f"Engineering agent boundary retry error: {e}", "BOUNDARY_RETRY_LLM_ERROR", "boundary_retry", raw_error=str(e))
        defects = _boundary_defects(state["infrastructure_plan"], body)
        if defects:
            return _make_engineering_failure(
                "Engineering agent kept violating the plan boundary after retry: "
                + "; ".join(defects[:5]),
                "BOUNDARY_VIOLATION",
                "boundary_retry",
                raw_error="; ".join(defects[:5]),
            )

    generated_code = f"{body}\n"
    gen_pairs = set(_RESOURCE_DECL_RE.findall(body))
    logger.info("Engineering agent: %d chars, %d resources", len(generated_code), len(gen_pairs))

    out: dict = {"generated_code": generated_code, "fix_feedback": {}}
    if fix_instruction and fix_feedback.get("root_cause") == "engineering":
        out["eng_error_history"] = (state["eng_error_history"] + [{"fix_instruction": fix_instruction}])[-5:]
    return out
