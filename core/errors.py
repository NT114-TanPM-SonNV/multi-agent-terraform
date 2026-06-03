"""Hàm tạo error dict chuẩn để ghi vào LangGraph State."""


def make_fail(error_type: str, root_cause: str | None, fix_instruction: str) -> dict:
    """Tạo error dict cho fix_feedback với error_type và root_cause cụ thể.

    error_type: INFRASTRUCTURE | MISSING_RESOURCE | WRONG_CONSTRAINT | SYNTAX | LOGIC | SECURITY
    root_cause: "architecture" | "security" | "engineering" | None (khi INFRASTRUCTURE)
    """
    return {
        "fix_feedback": {
            "overall_passed": False,
            "error_type": error_type,
            "root_cause": root_cause,
            "fix_instruction": fix_instruction,
            "checkov": {"passed_count": 0, "failed": []},
            "validate_passed": False,
            "plan_passed": False,
        }
    }
