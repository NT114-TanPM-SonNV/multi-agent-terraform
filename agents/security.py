"""Agent 2 — Security Policy (security_node)

Chọn Checkov rules cần enforce cho từng resource, dựa trên user intent.
KHÔNG chạy Checkov → shift-left (early classification, không enforcement).

Workflow 2 bước:
  1. Chọn category phù hợp với resource/intent (ENCRYPTION, IAM, NETWORKING, ...)
  2. Trong mỗi category đó, chọn specific rules (CKV IDs) phù hợp
  → Output: danh sách CKV IDs per resource để A3 implement + A4 enforce.

Input: state["prompt"], state["infrastructure_plan"]
Output: state["security_profile"] ({label: {type, checks}} per resource)

Profile schema:
  {"aws_s3_bucket.main": {"type": "aws_s3_bucket", "checks": ["CKV_AWS_19", "CKV_AWS_70"]}}

Cơ chế grounding:
  - catalog.json map resource_type → {category → [(id, name)]}
  - Menu inject per resource: LLM chỉ thấy và chọn rules THỰC SỰ áp dụng cho type đó
  - _clean_profile validate: drop bất kỳ ID không có trong menu (hallucinate)

Tại sao bỏ posture (minimal/standard/strict)?
  - Posture scalar là proxy mờ cho intent thật → A2 gán sai category vì bị đánh lừa bởi văn phong
  - Category + rule selection: A2 quyết định trực tiếp "resource này cần enforce rule nào"
  - A4 không cần level/tier trung gian, chỉ cần `check_ids = set(checks)` per resource

Tại sao A2 fail không chặn pipeline?
  - Fail → profile với checks=[] cho mọi resource, security_agent_failed=True
  - A4 đọc security_agent_failed → skip gate → best-effort deploy
"""
import json
import logging
from pathlib import Path

from core.state import AgentState
from core.llm import call_llm
from core.parsers import parse_llm_json
from prompts.security import SYSTEM_PROMPT, USER_TEMPLATE

logger = logging.getLogger(__name__)

_CATALOG_FILE = Path(__file__).parent.parent / "core" / "catalog.json"

def _load_catalog() -> dict[str, dict[str, list[tuple[str, str]]]]:
    """Nạp catalog.json → {resource_type → {category → [(id, name)]}}.

    catalog.json gộp single-resource (CKV_AWS_*) + graph checks (CKV2_AWS_*)
    với cùng format: {"id", "name", "cat": [...], "connected_types": [...]}.
    Sinh bởi core/build_catalog.py — chạy lại khi nâng Checkov.
    """
    result: dict[str, dict[str, list[tuple[str, str]]]] = {}
    try:
        data = json.loads(_CATALOG_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("Không nạp được catalog.json: %s — A2 menu rỗng", e)
        return result
    for rtype, checks in data.items():
        by_cat = result.setdefault(rtype, {})
        for c in checks:
            cid = c.get("id", "")
            name = c.get("name", "")
            for cat in c.get("cat", []):
                by_cat.setdefault(cat, []).append((cid, name))
    return result


# Nạp 1 lần khi import.
_CATALOG: dict[str, dict[str, list[tuple[str, str]]]] = _load_catalog()




def _valid_ids(rtype: str) -> frozenset[str]:
    """Tập hợp tất cả IDs hợp lệ trong menu của resource type này."""
    by_cat = _CATALOG.get(rtype, {})
    return frozenset(cid for entries in by_cat.values() for cid, _ in entries)


def _build_menu(rtype: str) -> str:
    """Render menu dạng text để inject vào prompt.

    Format:
      ENCRYPTION:
        CKV_AWS_19: Ensure all data stored in the S3 bucket is securely encrypted at rest
        CKV_AWS_145: Ensure that S3 buckets are encrypted with KMS by default
      IAM:
        CKV_AWS_70: Ensure S3 bucket does not allow an action with any Principal
    """
    by_cat = _CATALOG.get(rtype, {})
    if not by_cat:
        return "    (no applicable security checks for this resource type)"
    lines = []
    for cat in sorted(by_cat):
        lines.append(f"    {cat}:")
        for cid, name in sorted(by_cat[cat]):
            lines.append(f"      {cid}: {name}")
    return "\n".join(lines)


_RETRY_MSG = (
    "Response could not be parsed as JSON. Return ONLY a raw JSON object: "
    '{"type.name": {"checks": ["CKV_AWS_NNN", ...]}}. '
    "Empty list [] is valid. Empty object {} is valid."
)


def _clean_profile(parsed: dict, resources: list[dict]) -> dict[str, dict]:
    """Chuẩn hoá LLM output thành profile dict đồng nhất.

    Input format từ LLM: {"aws_s3_bucket.main": {"checks": ["CKV_AWS_19", "CKV_AWS_70"]}}
    Output format:       {"aws_s3_bucket.main": {"type": "aws_s3_bucket",
                                                  "checks": ["CKV_AWS_19", "CKV_AWS_70"]}}

    Logic:
      - Validate: drop IDs không có trong menu của resource type (hallucinate hoặc sai type)
      - LLM bỏ qua resource → checks=[] (không enforce gì, A2 đã phán không cần)
      - Sort để output ổn định.
    """
    out: dict[str, dict] = {}
    for r in resources:
        label = f"{r.get('type')}.{r.get('name')}"
        rtype = r.get("type", "")

        prof = parsed.get(label, {})
        raw_checks = prof.get("checks", []) if isinstance(prof, dict) else []

        valid = _valid_ids(rtype)
        checks = sorted(c for c in raw_checks if isinstance(c, str) and c in valid)

        out[label] = {"type": rtype, "checks": checks}
    return out


def security_node(state: AgentState) -> dict:
    """LangGraph node — chọn security rules cho từng resource trong plan A1."""
    resources = state["infrastructure_plan"].get("resources", [])
    if not resources:
        return {"security_profile": {}}

    # Dựng menu per resource để inject vào prompt
    menu_blocks = []
    for r in resources:
        label = f"{r.get('type')}.{r.get('name')}"
        menu_blocks.append(f"  {label}:\n{_build_menu(r.get('type', ''))}")
    menu_str = "\n".join(menu_blocks)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": USER_TEMPLATE.format(
            PROMPT=state["prompt"],
            PLAN=json.dumps(state["infrastructure_plan"], indent=2),
            MENU=menu_str,
        )},
    ]

    raw = ""
    parsed: dict = {}
    for attempt in range(2):
        try:
            raw = call_llm(messages, agent="security")
            parsed = parse_llm_json(raw, {})
            break
        except Exception as e:
            if attempt == 0:
                logger.warning("Security agent retry: %s", e)
                messages = messages + [
                    {"role": "assistant", "content": raw or ""},
                    {"role": "user", "content": _RETRY_MSG},
                ]
            else:
                logger.warning("Security agent failed: %s — checks=[] cho mọi resource", e)
                profile = _clean_profile({}, resources)
                return {"security_profile": profile, "security_agent_failed": True}

    if not isinstance(parsed, dict):
        parsed = {}

    profile = _clean_profile(parsed, resources)
    checks_by_res = {lbl: p["checks"] for lbl, p in profile.items()}
    logger.info("Security agent: %d resources | checks=%s", len(profile), checks_by_res)
    return {"security_profile": profile}
