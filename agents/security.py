"""Agent 2: choose Checkov checks for each planned resource."""
import json
import logging
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

from core.state import AgentState
from core.llm import call_llm
from core.parsers import parse_llm_json
from prompts.security import SYSTEM_PROMPT, USER_TEMPLATE, RETRY_MSG

logger = logging.getLogger(__name__)

_CATALOG_FILE = Path(__file__).parent.parent / "core" / "catalog.json"

def _load_catalog() -> dict[str, dict[str, list[dict]]]:
    """Load ``catalog.json`` into ``{resource_type: {category: [checks]}}``."""
    result: dict[str, dict[str, list[dict]]] = {}
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
            connected = list(c.get("connected_types") or [])
            mode, companions = ("requires_companion", connected) if connected else ("candidate_in_place", [])
            item = {
                "id": cid,
                "name": name,
                "mode": mode,
                "companions": companions,
            }
            for cat in c.get("cat", []):
                by_cat.setdefault(cat, []).append(item)
    return result


_CATALOG: dict[str, dict[str, list[dict]]] = _load_catalog()


@lru_cache(maxsize=None)
def _valid_ids(rtype: str) -> frozenset[str]:
    by_cat = _CATALOG.get(rtype, {})
    return frozenset(c["id"] for entries in by_cat.values() for c in entries)


def _has_any_type(plan_types: frozenset[str], types: tuple[str, ...] | list[str]) -> bool:
    return any(t in plan_types for t in types)


def _is_safe_in_place_check(check: dict, plan_types: frozenset[str]) -> bool:
    """Keep only checks whose companion resource types are present in the plan."""
    if check["mode"] == "requires_companion":
        return _has_any_type(plan_types, check.get("companions", []))
    return True


@lru_cache(maxsize=None)
def _build_menu(rtype: str, plan_types: frozenset[str]) -> tuple[str, frozenset[str]]:
    """Render prompt menu text for one resource type."""
    by_cat = _CATALOG.get(rtype, {})
    if not by_cat:
        return "    (no applicable security checks for this resource type)", frozenset()
    lines = []
    allowed: set[str] = set()
    for cat in sorted(by_cat):
        cat_lines = []
        for c in sorted(by_cat[cat], key=lambda x: x["id"]):
            if not _is_safe_in_place_check(c, plan_types):
                continue
            if c["mode"] == "requires_companion":
                comp = ", ".join(c["companions"]) or "external companion resource"
                tag = f"requires_companion: {comp}"
            else:
                tag = "candidate_in_place"
            cat_lines.append(f"      {c['id']} [{tag}]: {c['name']}")
            allowed.add(c["id"])
        if cat_lines:
            lines.append(f"    {cat}:")
            lines.extend(cat_lines)
    if not lines:
        return "    (no safe in-place checks for this resource within this plan)", frozenset()
    return "\n".join(lines), frozenset(allowed)


def _compact_value(value, max_string: int = 300):
    """Trim very large literals so the prompt stays readable."""
    if isinstance(value, str):
        return value if len(value) <= max_string else value[:max_string] + "...(truncated)"
    if isinstance(value, list):
        return [_compact_value(v, max_string) for v in value]
    if isinstance(value, dict):
        return {k: _compact_value(v, max_string) for k, v in value.items()}
    return value


def _security_plan(plan: dict) -> dict:
    """Return a compact but useful view of the infrastructure plan."""
    resources = []
    for r in plan.get("resources", []):
        rtype = r.get("type", "")
        name = r.get("name", "")
        resources.append({
            "label": f"{rtype}.{name}",
            "type": rtype,
            "name": name,
            "attributes": _compact_value(r.get("attributes", {})),
            "blocks": _compact_value(r.get("blocks", {})),
        })
    data_sources = []
    for d in plan.get("data_sources", []):
        dtype = d.get("type", "")
        name = d.get("name", "")
        data_sources.append({
            "label": f"data.{dtype}.{name}",
            "type": dtype,
            "name": name,
            "attributes": _compact_value(d.get("attributes", {})),
            "blocks": _compact_value(d.get("blocks", {})),
        })
    return {"resources": resources, "data_sources": data_sources}


def _clean_profile(parsed: dict, resources: list[dict],
                   allowed_by_type: dict[str, frozenset[str]] | None = None) -> dict[str, dict]:
    """Normalize LLM output and drop IDs not present in the menu."""
    out: dict[str, dict] = {}
    for r in resources:
        label = f"{r.get('type')}.{r.get('name')}"
        rtype = r.get("type", "")

        prof = parsed.get(label, {})
        raw_checks = prof.get("checks", []) if isinstance(prof, dict) else []

        valid = allowed_by_type.get(rtype, frozenset()) if allowed_by_type is not None else _valid_ids(rtype)
        checks = sorted(c for c in raw_checks if isinstance(c, str) and c in valid)

        out[label] = {"type": rtype, "checks": checks}
    return out


def _count_raw_checks(parsed: dict) -> int:
    """Count raw check IDs before menu filtering."""
    total = 0
    for prof in parsed.values():
        raw_checks = prof.get("checks", []) if isinstance(prof, dict) else []
        total += sum(1 for c in raw_checks if isinstance(c, str))
    return total


def security_node(state: AgentState) -> dict:
    """LangGraph node that selects security checks for each resource."""
    resources = state["infrastructure_plan"].get("resources", [])
    if not resources:
        return {"security_profile": {}, "security_status": "ok"}

    plan_types = frozenset(
        obj.get("type", "")
        for section in ("resources", "data_sources")
        for obj in state["infrastructure_plan"].get(section, [])
        if obj.get("type")
    )

    by_type: dict[str, list[str]] = defaultdict(list)
    for r in resources:
        rtype = r.get("type", "")
        by_type[rtype].append(f"{rtype}.{r.get('name')}")
    menu_blocks = []
    allowed_by_type: dict[str, frozenset[str]] = {}
    for rtype, labels in by_type.items():
        menu, allowed = _build_menu(rtype, plan_types)
        allowed_by_type[rtype] = allowed
        menu_blocks.append(f"  {', '.join(labels)}:\n{menu}")
    menu_str = "\n".join(menu_blocks)

    security_plan = _security_plan(state["infrastructure_plan"])

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": USER_TEMPLATE.format(
            PROMPT=state["prompt"],
            PLAN=json.dumps(security_plan, ensure_ascii=False),
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
                    {"role": "user", "content": RETRY_MSG},
                ]
            else:
                logger.warning("Security agent failed: %s — returning empty checks", e)
                profile = _clean_profile({}, resources, allowed_by_type)
                return {"security_profile": profile, "security_status": "degraded"}

    if not isinstance(parsed, dict):
        parsed = {}

    profile = _clean_profile(parsed, resources, allowed_by_type)
    raw_count = _count_raw_checks(parsed)
    kept_count = sum(len(info.get("checks", [])) for info in profile.values())
    if raw_count and kept_count == 0:
        logger.warning(
            "Security agent: parsed %d check(s) but all were filtered out by menu — checks=[] for every resource",
            raw_count,
        )
    elif raw_count > kept_count:
        logger.warning(
            "Security agent: parsed %d check(s), kept %d after menu filter",
            raw_count,
            kept_count,
        )
    checks_by_res = {lbl: p["checks"] for lbl, p in profile.items()}
    logger.info("Security agent: %d resources | checks=%s", len(profile), checks_by_res)
    return {"security_profile": profile, "security_status": "ok"}
