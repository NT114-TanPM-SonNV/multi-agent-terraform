
import json
from pathlib import Path

_CATALOG_FILE = "../catalog.json"


def get_check_names() -> dict[str, str]:
    """Return a flat ``{check_id: check_name}`` map from ``catalog.json``."""
    try:
        data = json.loads(Path(_CATALOG_FILE).read_text(encoding="utf-8"))
    except Exception:
        return {}
    names: dict[str, str] = {}
    for checks in data.values():
        for c in checks:
            cid = c.get("id", "")
            if cid and cid not in names:
                names[cid] = c.get("name", cid)
    return names
