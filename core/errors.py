"""Các hàm tạo error dict chuẩn để ghi vào LangGraph State.

Dùng chung cho tất cả agent — tránh định nghĩa lại cùng một cấu trúc dict
ở nhiều nơi khác nhau.
"""


def make_infra_error(fix_instruction: str) -> dict:
    """Tạo error dict với error_type=INFRA để route về requires_human.

    Dùng khi lỗi không phải do code (ví dụ: Floci down, timeout,
    LLM parse error, retry guard bắt được plan không thay đổi).
    """
    return {
        "validation_result": {
            "overall_passed": False,
            "error_type": "INFRA",
            "root_cause": None,
            "fix_instruction": fix_instruction,
            "checkov": {"passed_count": 0, "failed": []},
            "validate_passed": False,
            "plan_passed": False,
        }
    }
