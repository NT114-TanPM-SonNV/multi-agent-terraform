"""Central retry-budget tracking for the whole pipeline."""
from core.state import AgentState, RetryTracker

MAX_TOTAL_RETRY = 5
MAX_VAL_ENG_RETRY = 2
MAX_VAL_ARCH_RETRY = 2
MAX_VAL_SEC_RETRY = 1

MAX_DEPLOY_ENG_RETRY = 2
MAX_DEPLOY_ARCH_RETRY = 2
MAX_DEPLOY_TOTAL_RETRY = 4

def new_tracker() -> RetryTracker:
    """Return a fresh mutable retry tracker."""
    return {
        "count": 0,
        "last_error_type": "",
        "last_error_details": "",
        "error_history": [],
    }


# Read-only fallback when a retry entry does not exist yet.
_BLANK_TRACKER: RetryTracker = new_tracker()


def increment_retry(
    state: AgentState,
    agent: str,
    error_type: str,
    error_details: str = "",
) -> None:
    """Increment one agent's retry counter by writing a new nested dict."""
    old = state["retries"].get(agent) or _BLANK_TRACKER
    history = list(old["error_history"])  # copy để tránh mutate list cũ
    history.append(error_type)
    if len(history) > 5:
        history.pop(0)  # giữ 5 lỗi gần nhất cho LLM context (tránh lặp lỗi cũ) + debug
    state["retries"] = {
        **state["retries"],
        agent: {
            "count": old["count"] + 1,
            "last_error_type": error_type,
            "last_error_details": error_details,
            "error_history": history,
        },
    }
    # A5 failures count toward the deploy backstop; everything else counts toward validation.
    if agent in ("deploy_eng", "deploy_arch"):
        state["total_deploy_attempts"] = state["total_deploy_attempts"] + 1
    else:
        state["total_val_attempts"] = state["total_val_attempts"] + 1


def check_retry_budget(
    state: AgentState,
    agent: str,
    max_retries: int = 3,
) -> tuple[bool, str]:
    """Return whether the agent still has retry budget left."""
    tracker = state["retries"].get(agent)
    if not tracker:
        return True, ""

    count = tracker["count"]
    if count >= max_retries:
        return False, f"{agent} đã retry {count}/{max_retries} lần"

    return True, ""
