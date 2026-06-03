"""Tập trung quản lý retry budget cho toàn pipeline.

Thay thế 7 counter độc lập (arch_retry_count, eng_retry_count, sec_retry_count,
total_retry_count, deploy_retry_count, deploy_eng_retry_count, deploy_arch_retry_count)
bằng 1 dict duy nhất: retries[agent] → RetryTracker.

Lợi ích:
  - 1 source of truth (không double-count)
  - Dễ maintain (thay max budget 1 chỗ)
  - Dễ thêm agent mới
  - Rõ ràng error history per agent
"""
from dataclasses import dataclass, field

from core.state import AgentState, RetryTracker


def increment_retry(
    state: AgentState,
    agent: str,  # "arch", "eng", "sec", "deploy"
    error_type: str,
    error_details: str = "",
) -> None:
    """Tăng retry counter cho một agent.

    Args:
        state: AgentState
        agent: "arch" (A1), "eng" (A3), "sec" (A2), "deploy" (A5)
        error_type: loại lỗi (MISSING_RESOURCE, SYNTAX, LOGIC, SECURITY, TRANSIENT, etc.)
        error_details: mô tả lỗi (debug)
    """
    if agent not in state["retries"]:
        state["retries"][agent] = RetryTracker()

    tracker = state["retries"][agent]
    tracker["count"] += 1
    tracker["last_error_type"] = error_type
    tracker["last_error_details"] = error_details
    tracker["error_history"].append(error_type)

    # Giữ 5 lỗi gần nhất (để detect oscillation)
    if len(tracker["error_history"]) > 5:
        tracker["error_history"].pop(0)

    state["total_attempts"] += 1


def check_retry_budget(
    state: AgentState,
    agent: str,
    max_retries: int = 3,
) -> tuple[bool, str]:
    """Kiểm tra agent còn retry budget không.

    Returns:
        (can_retry: bool, reason: str)
        - Nếu can_retry=False, reason mô tả tại sao hết budget
    """
    if agent not in state["retries"]:
        return True, ""

    tracker = state["retries"][agent]
    count = tracker["count"]

    # Agent budget: "arch" max 2, "eng" max 3, "sec" max 2, "deploy" max 2
    if count >= max_retries:
        return False, f"{agent} đã retry {count}/{max_retries} lần"

    # Global safety: tối đa 20 attempts toàn pipeline
    if state["total_attempts"] >= 20:
        return False, f"Toàn pipeline đã attempt {state['total_attempts']}/20"

    return True, ""


def detect_oscillation(
    state: AgentState,
    agent: str,
    current_error_type: str,
) -> bool:
    """Phát hiện oscillation (lặp lại lỗi) → nên dừng.

    Kiểm tra các pattern:
      - 3 lỗi cùng loại liên tiếp → oscillation
      - Xoay vòng 2 loại (A→B→A→B) → oscillation
      - Xoay vòng 3 loại (A→B→C→A→B) → oscillation
    """
    tracker = state["retries"][agent]
    history = tracker["error_history"]

    if len(history) < 3:
        return False

    # Pattern 1: Cùng loại lỗi 3 lần liên tiếp
    if history[-3:] == [current_error_type] * 3:
        return True

    # Pattern 2: Xoay vòng 2 loại (A→B→A→B)
    if len(history) >= 4:
        if (history[-4] == history[-2] == current_error_type and
            history[-3] != current_error_type and history[-1] != current_error_type):
            return True

    # Pattern 3: Xoay vòng 3 loại (A→B→C→A→B)
    if len(history) >= 5:
        if (history[-5] == history[-2] == current_error_type and
            len(set(history[-5:])) == 3):
            return True

    return False


def get_retry_summary(state: AgentState) -> dict:
    """Trả summary retry state để log/debug."""
    return {
        agent: {
            "count": tracker["count"],
            "last_error": tracker["last_error_type"],
            "history": tracker["error_history"],
        }
        for agent, tracker in state["retries"].items()
    }
