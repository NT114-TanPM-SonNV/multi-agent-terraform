"""Agent 3 — Engineering / Code Generation (engineering_node)

Sinh Terraform HCL từ infrastructure_plan (A1) + security_profile (A2).
Serialize resource declarations theo AWS provider ~> 5.0 schema, sau đó implement
các security checks (CKV IDs) mà A2 đã chọn cho từng resource.
Có thể nhận fix_instruction từ A4 SYNTAX/LOGIC/SECURITY hoặc A5 LOGIC để incremental patch.

Workflow:
  1. Build security context từ A2 (CKV IDs kèm tên check)
  2. Build LLM context (plan JSON + security context)
  3. Inject fix_instruction nếu là engineering fix (incremental patch)
  4. Call LLM sinh HCL
  5. Clean output (strip tags + preamble + code block)
  6. Validate có ít nhất 1 resource block (retry 1 lần nếu không)
  7. Return generated HCL hoặc error

Input: state["infrastructure_plan"], state["security_profile"], optional state["fix_feedback"]
Output: state["generated_code"] (HCL string) hoặc error

Retry logic:
  - retries["eng"] max 3 (từ A4 SYNTAX/LOGIC/SECURITY + A5 LOGIC)
  - Khi hết budget → requires_human
  - Oscillation prevention: eng_error_history giữ 2 fix gần nhất để LLM không lặp lỗi cũ

Incremental patching (khi nhận fix_instruction từ A4/A5):
  - Gửi HCL code cũ + fix yêu cầu → LLM patch minimal (không rewrite từ đầu)
  - Lý do: rewrite từ plan có thể mất các edit A3 đã làm ở lần trước (provider block,
    security companion resources, etc.)
  - Điều kiện: root_cause PHẢI là "engineering" — nếu là "architecture" thì fix cho A1, không A3
"""
import json
import logging
import re
from pathlib import Path

from core.state import AgentState
from core.llm import call_llm
from core.errors import make_fail
from core.parsers import strip_code_block
from prompts.engineering import SYSTEM_PROMPT as _SYSTEM_PROMPT, USER_TEMPLATE as _USER_TEMPLATE

logger = logging.getLogger(__name__)

_CATALOG_FILE = Path(__file__).parent.parent / "core" / "catalog.json"


def _load_check_names() -> dict[str, str]:
    try:
        data = json.loads(_CATALOG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    names: dict[str, str] = {}
    for checks in data.values():
        for c in checks:
            cid = c.get("id", "")
            if cid and cid not in names:
                names[cid] = c.get("name", "")
    return names


_CKV_NAME: dict[str, str] = _load_check_names()

# Xóa <plan>...</plan> tags từ LLM output.
# Reasoning model (deepseek-v4-pro) đôi khi wrap chain-of-thought trong <plan> tags
# trước khi trả HCL — những tags này không phải HCL hợp lệ, phải strip ra.
_PLAN_TAG = re.compile(r"<plan>.*?</plan>", re.DOTALL | re.IGNORECASE)

# Pattern match tên resource trong HCL: `resource "aws_s3_bucket" "main"`
# Group 1 = type, Group 2 = name — dùng để log và đếm resource count.
_RESOURCE_DECL_RE = re.compile(r'resource\s+"([^"]+)"\s+"([^"]+)"')

# Các keyword bắt đầu một block HCL hợp lệ.
# Dùng để tìm điểm đầu tiên cần giữ trong output LLM (strip preamble text trước đó).
_HCL_BLOCK_START = re.compile(r'(?:terraform\s*\{|provider\s+"|resource\s+"|data\s+"|variable\s+"|output\s+"|module\s+")')


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


def engineering_node(state: AgentState) -> dict:
    """LangGraph node — serialize A1 plan sang HCL, implement security checks từ A2.

    Main steps:
      1. Build security context từ A2 (CKV IDs + tên check per resource)
      2. Build LLM context (plan JSON + security context)
      3. Inject fix_instruction nếu là engineering fix (incremental patch)
      4. Call LLM sinh HCL
      5. Clean output (strip tags + preamble + code block)
      6. Validate có ít nhất 1 resource block (retry 1 lần nếu không)
      7. Return generated HCL hoặc error
    """
    # ── Step 1: Build security context từ A2 ─────────────────────────────────
    # Render "label: - CKV_ID: tên check" để A3 biết cần implement gì.
    # Tên check lấy từ catalog (ví dụ: "Ensure S3 bucket is encrypted at rest")
    # giúp A3 hiểu ý nghĩa mà không cần nhớ ID.
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

    # ── Step 2: Build LLM context ────────────────────────────────────────────
    # Plan ở dạng JSON vì A3 cần đọc type/name/attributes/blocks để sinh đúng HCL syntax.
    user_content = _USER_TEMPLATE.format(
        PLAN=json.dumps(state["infrastructure_plan"], indent=2),
        SECURITY_CONTEXT=ctx_lines,
    )

    # ── Step 3: Inject fix_instruction nếu là engineering fix ─────────────────
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user",   "content": user_content},
    ]

    fix_feedback = state["fix_feedback"]
    fix_instruction = fix_feedback.get("fix_instruction", "")
    # Incremental patching: chỉ áp khi fix nhắm tới engineering.
    # Gate root_cause tránh apply fix dành cho A1 (root_cause="architecture").
    if fix_instruction and fix_feedback.get("root_cause") == "engineering":
        fix_msg = (
            f"Your previous HCL had an error. "
            f"Make ONLY the fix below — do not change anything else:\n\n"
            f"FIX:\n{fix_instruction}"
        )
        if state["generated_code"]:
            fix_msg += f"\n\nPREVIOUS CODE (keep everything except the fix):\n{state['generated_code']}"
        # Gắn 2 lần thử gần nhất để LLM không lặp lại sai lầm cũ.
        past = [
            e.get("fix_instruction", "")[:200]
            for e in state["eng_error_history"][-2:]
            if e.get("fix_instruction") and e.get("fix_instruction") != fix_instruction
        ]
        if past:
            fix_msg += "\n\nPREVIOUS ERRORS (do NOT reintroduce these):\n" + "\n".join(f"- {p}" for p in past)
        messages.append({"role": "user", "content": fix_msg})

    # ── Step 4: Call LLM ─────────────────────────────────────────────────────
    raw = ""
    try:
        raw = call_llm(messages, agent="engineering")
    except TimeoutError as e:
        logger.error("Engineering agent timeout: %s", e)
        return make_fail("INFRASTRUCTURE", None, f"Engineering agent LLM timeout: {e}")
    except Exception as e:
        logger.error("Engineering agent error: %s", e)
        return make_fail("INFRASTRUCTURE", None, f"Engineering agent error: {e}")

    # ── Step 5: Clean + validate có resource block ───────────────────────────
    body = _clean_hcl(raw)
    if 'resource "' not in body:
        logger.warning("Engineering agent: không có resource block — retry")
        retry_msgs = messages + [
            {"role": "assistant", "content": raw},
            {"role": "user", "content": (
                "Your response did not contain any `resource \"` blocks. "
                "Output the complete Terraform HCL with ALL resource blocks "
                "from the plan. Do not omit any resource."
            )},
        ]
        try:
            raw = call_llm(retry_msgs, agent="engineering")
        except Exception as e:
            return make_fail("INFRASTRUCTURE", None, f"Engineering agent retry error: {e}")
        body = _clean_hcl(raw)
        if 'resource "' not in body:
            return make_fail(
                "SYNTAX", "engineering",
                f"Engineering agent không sinh được resource block (sau retry). Raw: {raw[:300]}",
            )

    generated_code = f"{body}\n"
    gen_pairs = set(_RESOURCE_DECL_RE.findall(body))
    logger.info("Engineering agent: %d chars, %d resources", len(generated_code), len(gen_pairs))

    # ── Step 6 (cont): Return ─────────────────────────────────────────────────
    # fix_feedback={}: báo hiệu success cho route_after_engineering.
    out: dict = {"generated_code": generated_code, "fix_feedback": {}}
    if fix_instruction and fix_feedback.get("root_cause") == "engineering":
        out["eng_error_history"] = state["eng_error_history"] + [{"fix_instruction": fix_instruction}]
    return out
