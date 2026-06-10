"""Parse and validate JSON responses from the LLM."""
import json
import re

# ``resource "type" "name"`` declarations in HCL.
RESOURCE_DECL_RE = re.compile(r'resource\s+"([^"]+)"\s+"([^"]+)"')


def strip_code_block(raw: str) -> str:
    """Remove markdown code fences around JSON or HCL."""
    raw = raw.strip()
    match = re.search(r"```(?:\w+)?\n(.*?)\n?```", raw, re.DOTALL)
    if match:
        return match.group(1).strip()
    return raw


def _fix_outer_escaped_quotes(s: str) -> str:
    """Fix outer escaped quotes like ``\"value\"`` before JSON parsing."""
    result = []
    i = 0
    in_normal = False
    in_escaped = False

    while i < len(s):
        ch = s[i]

        if in_escaped:
            if ch == '\\' and i + 1 < len(s) and s[i + 1] == '"':
                result.append('"')
                in_escaped = False
                i += 2
            else:
                result.append(ch)
                i += 1

        elif in_normal:
            if ch == '\\' and i + 1 < len(s):
                result.append(ch)
                result.append(s[i + 1])
                i += 2
            elif ch == '"':
                in_normal = False
                result.append(ch)
                i += 1
            else:
                result.append(ch)
                i += 1

        else:
            if ch == '"':
                in_normal = True
                result.append(ch)
                i += 1
            elif ch == '\\' and i + 1 < len(s) and s[i + 1] == '"':
                result.append('"')
                in_escaped = True
                i += 2
            else:
                result.append(ch)
                i += 1

    return ''.join(result)


def parse_llm_json(
    raw: str,
    required_fields: dict[str, type | None],
) -> dict:
    """Parse JSON from the LLM and validate required fields."""
    cleaned = strip_code_block(raw)
    cleaned = _fix_outer_escaped_quotes(cleaned)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        if start == -1:
            raise ValueError("LLM response không chứa JSON object")
        try:
            data, _ = json.JSONDecoder().raw_decode(cleaned, start)
        except json.JSONDecodeError as e2:
            try:
                from json_repair import repair_json
                data = repair_json(cleaned, return_objects=True)
                if not isinstance(data, dict):
                    raise ValueError("json-repair không trả về dict")
            except Exception:
                raise ValueError(f"LLM response không phải JSON hợp lệ: {e2}") from e2

    for field, expected_type in required_fields.items():
        if field not in data:
            raise KeyError(f"Thiếu field bắt buộc: '{field}'")
        if expected_type is not None and not isinstance(data[field], expected_type):
            raise TypeError(
                f"Field '{field}' phải là {expected_type.__name__}, "
                f"nhận được {type(data[field]).__name__}"
            )

    return data
