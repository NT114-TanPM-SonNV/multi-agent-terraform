"""Agent 3 — Engineering: JSON plan + security profile → Terraform HCL.

Khi nhận fix_instruction: incremental patch (gửi code cũ + yêu cầu fix) thay vì
rewrite từ đầu — tránh mất các edit security companion từ vòng trước.
Strip <plan> tags (reasoning model chain-of-thought) và preamble text trước HCL.
"""
import json
import logging
import re
import time

from core.state import AgentState
from core.llm import call_llm
from core.errors import make_fail, recent_fix_instructions
from core.parsers import strip_code_block, RESOURCE_DECL_RE as _RESOURCE_DECL_RE
from core.catalog import get_check_names
from prompts.engineering import (
    SYSTEM_PROMPT as _SYSTEM_PROMPT, USER_TEMPLATE as _USER_TEMPLATE,
    PATCH_HEADER, PREV_CODE_HEADER, PREV_ERRORS_HEADER, NO_RESOURCE_RETRY,
)

logger = logging.getLogger(__name__)

_MAX_LLM_TRANSIENT_RETRY = 1
_LLM_TRANSIENT_BACKOFF = 2
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
# CONSTANTS: Catalog + Regex patterns
# ──────────────────────────────────────────────────────────────────────────────

_CKV_NAME: dict[str, str] = get_check_names()

# Xóa <plan>...</plan> tags từ LLM output.
# Reasoning model (deepseek-v4-pro) đôi khi wrap chain-of-thought trong <plan> tags
# trước khi trả HCL — những tags này không phải HCL hợp lệ, phải strip ra.
_PLAN_TAG = re.compile(r"<plan>.*?</plan>", re.DOTALL | re.IGNORECASE)

# Các keyword bắt đầu một block HCL hợp lệ.
# Dùng để tìm điểm đầu tiên cần giữ trong output LLM (strip preamble text trước đó).
_HCL_BLOCK_START = re.compile(r'(?:terraform\s*\{|provider\s+"|resource\s+"|data\s+"|variable\s+"|output\s+"|module\s+")')
_BACKEND_RE = re.compile(r'\bbackend\s+"[^"]+"\s*\{')
_MODULE_RE = re.compile(r'\bmodule\s+"([^"]+)"\s*\{')

_BOUNDARY_RETRY = """\
Your HCL violates the Architecture plan boundary.

Fix these defects:
{defects}

Rules:
- Do not add managed resources that are not in the plan.
- Do not remove managed resources that are in the plan.
- Do not add data sources that are not in the plan.
- Do not remove data sources that are in the plan.
- Do not add terraform backend or module blocks.
- If a planned resource needs an external object that is not in the plan, keep the
  resource boundary intact and let validation route the missing dependency to Architecture.
- Return the complete corrected Terraform HCL only.\
"""

# ──────────────────────────────────────────────────────────────────────────────
# HELPERS: Clean LLM output (strip tags, preamble, markdown)
# ──────────────────────────────────────────────────────────────────────────────

def _strip_preamble(hcl: str) -> str:
    """Bỏ phần văn bản LLM viết trước block HCL đầu tiên.

    LLM thường viết intro như "Here's the Terraform configuration:" trước block code.
    Những text này không phải HCL → terraform validate sẽ fail.

    Ví dụ:
      Input:  "Sure! Here's the config:\n\nresource \"aws_s3_bucket\" \"main\" { ... }"
      Output: "resource \"aws_s3_bucket\" \"main\" { ... }"
    """
    m = _HCL_BLOCK_START.search(hcl)
    return hcl[m.start():] if m else hcl


def _clean_hcl(raw: str) -> str:
    """Clean LLM output thành HCL thuần.

    3 bước theo thứ tự:
      1. Xóa <plan>...</plan> tags (reasoning model chain-of-thought)
      2. Xóa ```hcl...``` markdown fence
      3. Xóa text giải thích trước block HCL đầu tiên
    """
    cleaned = _PLAN_TAG.sub("", raw).strip()
    return _strip_preamble(strip_code_block(cleaned).strip())


def _planned_resource_pairs(plan: dict) -> set[tuple[str, str]]:
    return {
        (r.get("type", ""), r.get("name", ""))
        for r in plan.get("resources", [])
        if r.get("type") and r.get("name")
    }


def _planned_data_pairs(plan: dict) -> set[tuple[str, str]]:
    return {
        (d.get("type", ""), d.get("name", ""))
        for d in plan.get("data_sources", [])
        if d.get("type") and d.get("name")
    }


def _is_transient_llm_error(error: Exception) -> bool:
    text = f"{type(error).__name__}: {error}".lower()
    return any(hint in text for hint in _LLM_TRANSIENT_HINTS)


def _call_engineering_llm(messages: list[dict]) -> str:
    """Call the engineering LLM with one extra transient retry outside call_llm()."""
    last_error: Exception | None = None
    for attempt in range(_MAX_LLM_TRANSIENT_RETRY + 1):
        try:
            return call_llm(messages, agent="engineering")
        except Exception as e:
            last_error = e
            if attempt >= _MAX_LLM_TRANSIENT_RETRY or not _is_transient_llm_error(e):
                raise
            time.sleep(_LLM_TRANSIENT_BACKOFF * (attempt + 1))
    assert last_error is not None
    raise last_error


def _fail_engineering(fix_instruction: str, error_label: str, error_stage: str,
                      *, raw_error: str | None = None) -> dict:
    logger.warning("Engineering agent: FAIL INFRASTRUCTURE [%s/%s]", error_stage, error_label)
    result = make_fail("INFRASTRUCTURE", None, fix_instruction)
    fb = result["fix_feedback"]
    fb["error_label"] = error_label
    fb["error_stage"] = error_stage
    if raw_error:
        fb["raw_error"] = raw_error[:2000]
    return result


_DATA_DECL_RE = re.compile(r'data\s+"([^"]+)"\s+"([^"]+)"')


def _boundary_defects(plan: dict, hcl: str) -> list[str]:
    """Boundary: managed resources and data sources must match A1 plan."""
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


# ──────────────────────────────────────────────────────────────────────────────
# MAIN LOGIC: LLM prompt + code formatting + validation
# ──────────────────────────────────────────────────────────────────────────────

def engineering_node(state: AgentState) -> dict:
    sec_lines = []
    for label, info in state["security_profile"].items():
        checks = info.get("checks", [])
        if not checks:
            continue
        sec_lines.append(f"  {label}:")
        for cid in checks:
            name = _CKV_NAME.get(cid, cid)
            sec_lines.append(f"    - {cid}: {name}")
    ctx_lines = "\n".join(sec_lines) or "  (no security checks selected)"

    user_content = _USER_TEMPLATE.format(
        PLAN=json.dumps(state["infrastructure_plan"]),
        SECURITY_CONTEXT=ctx_lines,
    )

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user",   "content": user_content},
    ]

    fix_feedback = state["fix_feedback"]
    fix_instruction = fix_feedback.get("fix_instruction", "")
    if fix_instruction and fix_feedback.get("root_cause") == "engineering":
        fix_msg = PATCH_HEADER + fix_instruction
        if state["generated_code"]:
            fix_msg += PREV_CODE_HEADER + state["generated_code"]
        past = recent_fix_instructions(state["eng_error_history"], max_chars=200,
                                       exclude=fix_instruction)
        if past:
            fix_msg += PREV_ERRORS_HEADER + "\n".join(f"- {p}" for p in past)
        messages.append({"role": "user", "content": fix_msg})

    raw = ""
    try:
        raw = _call_engineering_llm(messages)
    except TimeoutError as e:
        logger.error("Engineering agent timeout: %s", e)
        return _fail_engineering(f"Engineering agent LLM timeout: {e}", "LLM_TIMEOUT", "initial", raw_error=str(e))
    except Exception as e:
        logger.error("Engineering agent error: %s", e)
        label = "LLM_TRANSIENT" if _is_transient_llm_error(e) else "LLM_ERROR"
        return _fail_engineering(f"Engineering agent error: {e}", label, "initial", raw_error=str(e))

    body = _clean_hcl(raw)
    if 'resource "' not in body:
        logger.warning("Engineering agent: không có resource block — retry")
        retry_msgs = messages + [
            {"role": "assistant", "content": raw},
            {"role": "user", "content": NO_RESOURCE_RETRY},
        ]
        try:
            raw = _call_engineering_llm(retry_msgs)
        except Exception as e:
            label = "RETRY_LLM_TRANSIENT" if _is_transient_llm_error(e) else "RETRY_LLM_ERROR"
            return _fail_engineering(f"Engineering agent retry error: {e}", label, "no_resource_retry", raw_error=str(e))
        body = _clean_hcl(raw)
        if 'resource "' not in body:
            return _fail_engineering(
                f"Engineering agent không sinh được resource block (sau retry). Raw: {raw[:300]}",
                "NO_RESOURCE_BLOCK",
                "no_resource_retry",
                raw_error=raw[:3000],
            )

    defects = _boundary_defects(state["infrastructure_plan"], body)
    if defects:
        logger.warning("Engineering agent: boundary defect — retry: %s", defects[:5])
        retry_msgs = messages + [
            {"role": "assistant", "content": raw},
            {"role": "user", "content": _BOUNDARY_RETRY.format(
                defects="\n".join(f"- {d}" for d in defects)
            )},
        ]
        try:
            raw = _call_engineering_llm(retry_msgs)
        except Exception as e:
            label = "BOUNDARY_RETRY_LLM_TRANSIENT" if _is_transient_llm_error(e) else "BOUNDARY_RETRY_LLM_ERROR"
            return _fail_engineering(f"Engineering agent boundary retry error: {e}", label, "boundary_retry", raw_error=str(e))
        body = _clean_hcl(raw)
        defects = _boundary_defects(state["infrastructure_plan"], body)
        if defects:
            return _fail_engineering(
                "Engineering agent kept violating plan boundary after retry: "
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
