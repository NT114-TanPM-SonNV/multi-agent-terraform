"""Build ``core/catalog.json`` from Checkov AWS checks."""
from __future__ import annotations

import glob
import json
import os
import re
from pathlib import Path

import yaml
from checkov.terraform.checks.resource.registry import resource_registry
import checkov

_OUT = Path(__file__).parent / "catalog.json"
_GRAPH_DIR = os.path.join(os.path.dirname(checkov.__file__), "terraform", "checks", "graph_checks", "aws")

_CKV_AWS_RE = re.compile(r"^CKV_AWS_\d+$")

_DROP_CATS: frozenset[str] = frozenset({
    "LOGGING",
    "BACKUP_AND_RECOVERY",
    "KUBERNETES",
    "SUPPLY_CHAIN",
    "AI_AND_ML",
})


# Single-resource checks from the Python registry.

def _build_single() -> dict[str, list[dict]]:
    """Return ``{resource_type: [entry]}`` for ``CKV_AWS_*`` checks."""
    out: dict[str, list[dict]] = {}
    for resource_type, checks in resource_registry.checks.items():
        seen: set[str] = set()
        entries: list[dict] = []
        for c in checks:
            cid = getattr(c, "id", "")
            if not _CKV_AWS_RE.match(cid) or cid in seen:
                continue
            seen.add(cid)
            name = getattr(c, "name", "")
            cats = [getattr(cat, "name", str(cat))
                    for cat in (getattr(c, "categories", None) or [])]
            if _DROP_CATS.intersection(cats):
                continue
            entries.append({"id": cid, "name": name, "cat": cats})
        if entries:
            out[resource_type] = sorted(entries, key=lambda e: e["id"])
    return out


# Graph checks from YAML/JSON files.

def _collect(defn, key: str, acc: set) -> None:
    """Collect nested ``key`` values from a graph-check definition."""
    if isinstance(defn, dict):
        v = defn.get(key)
        if isinstance(v, str):
            acc.add(v)
        elif isinstance(v, list):
            acc.update(x for x in v if isinstance(x, str))
        for vv in defn.values():
            _collect(vv, key, acc)
    elif isinstance(defn, list):
        for item in defn:
            _collect(item, key, acc)


def _build_graph() -> dict[str, list[dict]]:
    """Return graph checks as ``{primary_resource_type: [entry]}``."""
    files = (glob.glob(os.path.join(_GRAPH_DIR, "*.yaml"))
             + glob.glob(os.path.join(_GRAPH_DIR, "*.json")))
    by_type: dict[str, list[dict]] = {}
    for f in files:
        try:
            with open(f, encoding="utf-8") as fp:
                doc = yaml.safe_load(fp)
        except Exception:
            continue
        if not isinstance(doc, dict):
            continue
        meta = doc.get("metadata", {}) or {}
        cid = meta.get("id", "")
        if not (cid.startswith("CKV_AWS") or cid.startswith("CKV2_AWS")):
            continue
        raw_cat = (meta.get("category", "") or "").upper().replace(" ", "_")
        cats = [raw_cat] if raw_cat else []
        if _DROP_CATS.intersection(cats):
            continue

        defn = doc.get("definition", {})
        prim, conn = set(), set()
        _collect(defn, "resource_types", prim)
        _collect(defn, "connected_resource_types", conn)

        primary = sorted(prim - conn) or sorted(prim) or sorted(conn) or ["__unkeyed__"]
        entry: dict = {"id": cid, "name": meta.get("name", ""), "cat": cats}
        connected = sorted(conn)
        if connected:
            entry["connected_types"] = connected

        for rt in primary:
            by_type.setdefault(rt, []).append(entry)

    for rt in by_type:
        by_type[rt] = sorted(by_type[rt], key=lambda e: e["id"])
    return by_type


# Merge and write output.

def build_catalog() -> dict[str, list[dict]]:
    """Merge the two sources into a sorted catalog."""
    catalog = _build_single()
    for rtype, entries in _build_graph().items():
        existing_ids = {e["id"] for e in catalog.get(rtype, [])}
        new = [e for e in entries if e["id"] not in existing_ids]
        if new:
            catalog.setdefault(rtype, [])
            catalog[rtype] = sorted(catalog[rtype] + new, key=lambda e: e["id"])
    return dict(sorted(catalog.items()))


def main() -> None:
    catalog = build_catalog()
    _OUT.write_text(json.dumps(catalog, indent=2, sort_keys=False), encoding="utf-8")
    total = sum(len(v) for v in catalog.values())
    single = sum(1 for v in catalog.values() for e in v if "connected_types" not in e)
    graph  = total - single
    print(f"Wrote {_OUT} — {len(catalog)} resource types, {total} checks "
          f"({single} single-resource + {graph} graph)")


if __name__ == "__main__":
    main()
