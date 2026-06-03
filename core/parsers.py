"""Hàm parse và validate JSON response từ LLM.

Tách riêng khỏi errors.py vì parse logic không phải error handling —
đây là bước tiền xử lý output của LLM trước khi agent dùng.
"""
import json
import re


def strip_code_block(raw: str) -> str:
    """Xóa markdown code fence mà LLM hay bọc quanh JSON/HCL dù đã dặn không làm vậy.

    Xử lý các dạng: ```json ... ```, ```hcl ... ```, ``` ... ```
    """
    raw = raw.strip()
    # Dùng search thay fullmatch để handle trường hợp LLM có text trước/sau fence
    match = re.search(r"```(?:\w+)?\n(.*?)\n?```", raw, re.DOTALL)
    if match:
        return match.group(1).strip()
    return raw


def _fix_outer_escaped_quotes(s: str) -> str:
    """Sửa \"value\" dùng làm outer string delimiters thành "value".

    LLM đôi khi escape outer quotes của một string value (\"node_type\" thay vì
    "node_type"). Standard JSON parser reject vì \" chỉ hợp lệ bên trong string.

    Dùng state machine thay vì regex vì cần phân biệt:
    - \"tcp\" ở VỊ TRÍ OUTSIDE STRING → outer delimiter cần fix
    - \"tcp\" bên trong "protocol = \"tcp\"" → escape hợp lệ, không touch

    States:
      normal_string  — đang trong "..." string bình thường
      escaped_string — đang trong \"...\" string (outer quotes bị escape)
      outside        — ngoài mọi string
    """
    result = []
    i = 0
    in_normal = False
    in_escaped = False

    while i < len(s):
        ch = s[i]

        if in_escaped:
            # Trong \"...\": chỉ \" là closing delimiter, mọi thứ khác là nội dung
            if ch == '\\' and i + 1 < len(s) and s[i + 1] == '"':
                result.append('"')   # đóng string bằng dấu " thông thường
                in_escaped = False
                i += 2
            else:
                result.append(ch)
                i += 1

        elif in_normal:
            # Trong "...": \" là escape sequence, " là closing
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
            # Outside: " mở normal string, \" mở escaped string
            if ch == '"':
                in_normal = True
                result.append(ch)
                i += 1
            elif ch == '\\' and i + 1 < len(s) and s[i + 1] == '"':
                result.append('"')   # thay \" → " để mở string đúng chuẩn
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
    """Parse JSON từ LLM response và validate các field bắt buộc.

    Args:
        raw: chuỗi raw trả về từ LLM
        required_fields: dict ánh xạ tên field → kiểu dữ liệu mong đợi.
                         Dùng None để chỉ kiểm tra field tồn tại, bỏ qua kiểm tra kiểu.

    Raises:
        ValueError: LLM trả về chuỗi không phải JSON hợp lệ
        KeyError: thiếu field bắt buộc trong JSON
        TypeError: field tồn tại nhưng sai kiểu (ví dụ resources=null thay vì list)
    """
    cleaned = strip_code_block(raw)
    cleaned = _fix_outer_escaped_quotes(cleaned)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        # Fallback: dùng raw_decode để parse JSON đầu tiên hợp lệ, bỏ qua
        # text thừa phía sau (e.g. LLM thêm trailing "}}" hoặc giải thích).
        start = cleaned.find("{")
        if start == -1:
            raise ValueError("LLM response không chứa JSON object")
        try:
            data, _ = json.JSONDecoder().raw_decode(cleaned, start)
        except json.JSONDecodeError as e2:
            # Fallback cuối: json-repair xử lý các LLM quirk mà standard parser không handle được
            # (escaped outer quotes, truncated JSON, trailing commas, v.v.)
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
