"""Dataset enricher — augments raw CSV rows with derived fields."""
from .filter import parse_resources, is_floci_compatible

_DIFFICULTY_LABELS = {
    1: "trivial",
    2: "easy",
    3: "medium",
    4: "hard",
    5: "very_hard",
    6: "expert",
}


def map_difficulty(level: int) -> str:
    """Map integer difficulty (1-6) to a human-readable label."""
    return _DIFFICULTY_LABELS.get(level, "unknown")


def enrich_sample(row: dict) -> dict:
    """Add derived fields to a raw dataset row.

    Adds:
      - resource_types: list[str] — parsed from 'resource' field
      - floci_compatible: bool
      - difficulty_label: str
    """
    enriched = dict(row)
    enriched["resource_types"] = parse_resources(row.get("resource", ""))
    enriched["floci_compatible"] = is_floci_compatible(row.get("resource", ""))
    enriched["difficulty_label"] = map_difficulty(row.get("difficulty", 0))
    return enriched
