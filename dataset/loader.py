"""Dataset loader — reads data-success.csv and exposes it in various split formats."""
import csv
import json
import pickle
from pathlib import Path
from typing import Any

_CSV_PATH = Path(__file__).parent / "data-success.csv"
_CACHE_PATH = Path(__file__).parent / ".dataset_cache.pkl"

# Column name → internal key mapping
_COL_MAP = {
    "Resource":       "resource",
    "Prompt":         "prompt",
    "Rego intent":    "rego_intent",
    "Difficulty":     "difficulty",
    "Reference output": "reference_output",
    "Intent":         "intent",
}


def _parse_row(row: dict) -> dict:
    out: dict[str, Any] = {}
    for csv_col, key in _COL_MAP.items():
        out[key] = row.get(csv_col, "").strip()
    out["difficulty"] = int(out["difficulty"]) if out["difficulty"].isdigit() else 0
    return out


def load_and_process() -> list[dict]:
    """Load and parse all 88 rows from CSV. Saves a pickle cache."""
    rows = _read_csv()
    _CACHE_PATH.write_bytes(pickle.dumps(rows))
    return rows


def load_from_cache() -> list[dict]:
    """Load from pickle cache if present, else fall back to CSV."""
    if _CACHE_PATH.exists():
        return pickle.loads(_CACHE_PATH.read_bytes())
    return load_and_process()


def _read_csv() -> list[dict]:
    rows = []
    with open(_CSV_PATH, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader):
            parsed = _parse_row(row)
            parsed["id"] = i
            rows.append(parsed)
    return rows


def load_dev(n: int = 10) -> list[dict]:
    """Return first n rows for quick dev testing."""
    return load_from_cache()[:n]


def load_test() -> list[dict]:
    """Return all 88 rows (our full evaluation set)."""
    return load_from_cache()


def get_by_difficulty(level: int) -> list[dict]:
    """Return rows matching a specific difficulty level (1-6)."""
    return [r for r in load_from_cache() if r["difficulty"] == level]


def load_original_format() -> list[dict]:
    """Return rows with original CSV column names (for compatibility with iac-eval tooling)."""
    rows = []
    with open(_CSV_PATH, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(dict(row))
    return rows
