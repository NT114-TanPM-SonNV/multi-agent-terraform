"""Agent 1 — Architecture Planning (architecture_node)

Sinh kế hoạch infrastructure từ user request. LLM trả JSON chứa resources + data_sources.
Workflow:
  1. LLM sinh plan initial (hoặc fix plan nếu A4/A5 route back với fix_instruction)
  2. Validate structure (defects check) — nếu có → re-prompt LLM tự sửa (giữ intent)
  3. Trả infrastructure_plan để A2/A3 dùng

Input: state["prompt"] (user request), optional state["fix_feedback"] (fix instruction từ A4/A5)
Output: state["infrastructure_plan"] (dict resources + data_sources) hoặc error

Retry logic:
  - retries["arch"] max 2 (từ A4 MISSING_RESOURCE + A5 MISSING_RESOURCE)
  - Khi hết budget → requires_human
"""
import logging

from core.state import AgentState
from core.llm import call_llm
from core.errors import make_fail
from core.parsers import parse_llm_json
from prompts.architecture import SYSTEM_PROMPT, DEFECT_FIX

logger = logging.getLogger(__name__)


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
    """Phát hiện defect CẤU TRÚC trong plan LLM trả — KHÔNG sửa, chỉ báo.

    Triết lý: Python KHÔNG được đoán hộ LLM. Nếu LLM trả item thiếu 'name',
    có 2 khả năng: (a) LLM quên, (b) LLM định đặt tên đặc biệt. Drop âm thầm
    mất resource → hạ downstream (A2 không có entry, A3 thiếu code, A4 MISSING).
    → Đúng hơn: trả lỗi cho LLM TỰ sửa (re-prompt in-node), giữ intent.

    5 loại defect được kiểm:
      0. resources rỗng — LLM không sinh được resource nào
      1. Item không phải JSON object (LLM trả primitive/string thay vì dict)
      2. Thiếu 'type' (resource type, vd "aws_s3_bucket")
      3. Thiếu 'name' (logical name, vd "main")
      4. Trùng type.name trong cùng section (Terraform không cho phép 2 resource cùng addr)

    Message viết bằng English vì sẽ được đút vào prompt LLM.
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


def architecture_node(state: AgentState) -> dict:
    """LangGraph node — generate infrastructure plan from user request.

    Main steps:
      1. Build LLM context: user prompt + optional fix_instruction (khi root_cause="architecture")
      2. Call LLM to generate plan (JSON với resources + data_sources)
      3. Validate structure — nếu có defect, re-prompt LLM tự sửa (1 lần)
      4. Return infrastructure_plan hoặc error
    """
    # ── Step 1: Build LLM messages ──────────────────────────────────────────────
    # Fresh start mỗi lần — không inject plan cũ vì plan cũ đã sai.
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user",   "content": state["prompt"]},
    ]

    # Inject fix_instruction nếu A4/A5 route ngược về với root_cause="architecture".
    # Gate root_cause tránh nhận nhầm fix dành cho A3 (root_cause="engineering").
    fix_feedback = state["fix_feedback"]
    fix_instruction = fix_feedback.get("fix_instruction", "")
    if fix_instruction and fix_feedback.get("root_cause") == "architecture":
        fix_msg = f"REQUIRED CHANGE:\n{fix_instruction}"
        # Gắn 2 attempt gần nhất vào prompt để LLM không lặp lại sai lầm cũ.
        past = [e.get("fix_instruction", "")[:400]
                for e in state["arch_error_history"][-2:]
                if e.get("fix_instruction") and e.get("fix_instruction") != fix_instruction]
        if past:
            fix_msg += "\n\nPREVIOUS ATTEMPTS (do NOT repeat):\n" + "\n".join(f"- {p}" for p in past)
        messages.append({"role": "user", "content": fix_msg})
    elif fix_instruction:
        logger.debug("Archi: fix_instruction ignored (root_cause=%s)", fix_feedback.get("root_cause"))

    # ── Step 2: Call LLM ────────────────────────────────────────────────────────
    # Lỗi LLM/network/parse → INFRASTRUCTURE → requires_human (không retry vì lỗi hạ tầng).
    try:
        raw = call_llm(messages, agent="architecture")
        plan = _parse_plan(raw)
    except Exception as e:
        return make_fail("INFRASTRUCTURE", None, f"Archi agent error: {e}")

    # ── Step 3: Validate + re-prompt nếu có defect ────────────────────────────
    # Re-prompt 1 lần: LLM thấy lại output + danh sách defect cụ thể để tự sửa.
    defects = _plan_defects(plan)
    if defects:
        logger.warning("Archi agent: %d defect — re-prompt: %s", len(defects), defects[:5])
        retry_msgs = messages + [
            {"role": "assistant", "content": raw},
            {"role": "user", "content": DEFECT_FIX.format(
                defects="\n".join(f"- {d}" for d in defects))},
        ]
        try:
            raw = call_llm(retry_msgs, agent="architecture")
            plan = _parse_plan(raw)
        except Exception as e:
            return make_fail("INFRASTRUCTURE", None, f"Archi agent retry error: {e}")
        defects = _plan_defects(plan)
        if defects:
            return make_fail("INFRASTRUCTURE", None, f"Plan still has defects after retry: {defects[:3]}")

    logger.info("Archi agent: %d resources, %d data_sources",
                len(plan["resources"]), len(plan["data_sources"]))

    # ── Step 4: Cập nhật state ──────────────────────────────────────────────────
    # Reset eng/sec: re-plan = code mới → lỗi cũ không còn liên quan, tránh cạn budget oan.
    # Không reset "arch" (budget vòng re-plan) và "deploy" (vòng A5 riêng).
    blank = {"count": 0, "last_error_type": "", "last_error_details": "", "error_history": []}
    out: dict = {
        "infrastructure_plan": plan,
        "fix_feedback": {},
        "retries": {**state["retries"], "eng": {**blank}, "sec": {**blank}},
    }
    if fix_instruction and fix_feedback.get("root_cause") == "architecture":
        out["arch_error_history"] = state["arch_error_history"] + [{"fix_instruction": fix_instruction}]
    return out
