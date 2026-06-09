"""Tập trung quản lý retry budget cho toàn pipeline.

A4 (Validation) và A5 (Deployment) là HAI PHA có backstop ĐỘC LẬP:
  - validation phase: fail của A1/A3/A4 đếm vào total_val_attempts (cap MAX_TOTAL_RETRY).
  - deploy phase:     fail của A5 đếm vào total_deploy_attempts (cap MAX_DEPLOY_TOTAL_RETRY).
Hai counter tách biệt → A4 đốt hết budget của nó KHÔNG starve A5 (lỗi apply-time là
lớp mới, không liên quan lỗi A4 đã xử lý). Nếu dùng chung 1 backstop, A4 cạn budget
sẽ chặn A5 oan dù deploy_eng/deploy_arch còn nguyên.

Vì sao cần backstop (không chỉ dựa per-agent cap)? architecture_node RESET
val_eng/deploy_eng/sec sau mỗi re-plan → per-agent cap không bound được vòng lặp
xuyên pha A1↔A3↔A4. total_val_attempts/total_deploy_attempts (không reset) mới là chốt thật.

Keys trong retries dict:
  val_eng     — A4 → A3 (SYNTAX/LOGIC/SECURITY)    → bump total_val_attempts
  val_arch    — A4 → A1 (MISSING_RESOURCE)         → bump total_val_attempts
  deploy_eng  — A5 → A3 (LOGIC_DEPLOY)             → bump total_deploy_attempts
  deploy_arch — A5 → A1 (MISSING_RESOURCE_DEPLOY)  → bump total_deploy_attempts
  sec         — security gate trong A4 (best-effort khi hết) → bump total_val_attempts
"""
from core.state import AgentState, RetryTracker

# ── Retry budgets — single source of truth ────────────────────────────────────
MAX_TOTAL_RETRY        = 5  # validation-phase backstop — dừng sau 5 fail của A1/A3/A4
MAX_VAL_ENG_RETRY      = 2  # A4 → A3: nhiều hơn A5 vì validate rẻ hơn apply
MAX_VAL_ARCH_RETRY     = 2  # A4 → A1: re-plan ít thôi, thường fix trong 1-2 lần
MAX_VAL_SEC_RETRY      = 1  # security gate — cho A3 sửa 1 lần, sau đó best-effort deploy

MAX_DEPLOY_ENG_RETRY   = 2  # A5 → A3: ít hơn A4 vì mỗi lần apply tốn tiền AWS
MAX_DEPLOY_ARCH_RETRY  = 2  # A5 → A1: độc lập val_arch
MAX_DEPLOY_TOTAL_RETRY = 4  # deploy-phase backstop — dừng sau 4 fail của A5, ĐỘC LẬP total_val_attempts
                            # (= deploy_eng2 + deploy_arch2: per-agent cap là ràng buộc chính)

def new_tracker() -> RetryTracker:
    """Tạo tracker rỗng MỚI (mutable) — single source of truth cho schema RetryTracker.

    Dùng khi khởi tạo state (graph.build_initial_state) và khi reset counter
    (architecture_node sau re-plan). Mỗi lần gọi trả dict mới → không chia sẻ
    reference (tránh mutate nhầm tracker khác).
    Không dùng RetryTracker() vì TypedDict() trả {} không có keys → KeyError khi đọc.
    """
    return {
        "count": 0,
        "last_error_type": "",
        "last_error_details": "",
        "error_history": [],
    }


# Fallback read-only khi agent chưa có entry trong retries dict (chỉ đọc, không mutate).
_BLANK_TRACKER: RetryTracker = new_tracker()


def increment_retry(
    state: AgentState,
    agent: str,
    error_type: str,
    error_details: str = "",
) -> None:
    """Tăng retry counter cho agent, tạo dict mới thay vì mutate in-place.

    Tại sao không mutate?
    LangGraph dựa vào node trả update dict để merge vào state. Nếu mutate nested
    dict trực tiếp, LangGraph structural sharing (checkpointing) có thể đọc
    giá trị cũ. Tạo dict mới đảm bảo node trả giá trị đúng khi return state.
    """
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
    # Phase-scoped backstop: fail của A5 (deploy_eng/deploy_arch) đếm vào total_deploy_attempts
    # riêng để A4 không starve A5; mọi fail khác (A1/A3/A4) đếm vào total_val_attempts.
    if agent in ("deploy_eng", "deploy_arch"):
        state["total_deploy_attempts"] = state["total_deploy_attempts"] + 1
    else:
        state["total_val_attempts"] = state["total_val_attempts"] + 1


def check_retry_budget(
    state: AgentState,
    agent: str,
    max_retries: int = 3,
) -> tuple[bool, str]:
    """Kiểm tra agent còn retry budget không.

    Luôn trả True nếu agent chưa có entry (chưa retry lần nào).

    Returns:
        (can_retry, reason) — reason mô tả lý do khi can_retry=False
    """
    tracker = state["retries"].get(agent)
    if not tracker:
        return True, ""

    count = tracker["count"]
    if count >= max_retries:
        return False, f"{agent} đã retry {count}/{max_retries} lần"

    return True, ""
