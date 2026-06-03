from typing import TypedDict


class RetryTracker(TypedDict):
    """Tracker retry cho một agent."""
    count: int
    last_error_type: str
    last_error_details: str
    error_history: list  # list[str] — giữ 5 lỗi gần nhất


class AgentState(TypedDict):
    # Input — không thay đổi trong suốt pipeline
    prompt: str

    # Cấu hình môi trường — đọc từ env vars khi khởi tạo graph
    terraform_plan_timeout: int
    auto_destroy: bool          # True trong eval/batch — destroy ngay sau apply thành công

    # Agent 1 — {"resources": [...], "data_sources": [...]}
    # attributes: primitive → flat attr, nested dict → HCL block, "REF:..." → reference
    infrastructure_plan: dict

    # Agent 2 — {"type.name": {"posture": "minimal|standard|strict", "description": ...}}
    # LLM phán posture theo intent; description text đưa A3 apply best practices.
    # Security grading độc lập ở score.py (full Checkov, không qua A2).
    security_profile: dict

    # Agent 3
    generated_code: str

    # Agent 4
    fix_feedback: dict

    # ✅ Retry tracking — 1 dict tập trung thay vì 7 counter độc lập
    # Keys: "arch" (A1), "eng" (A3), "sec" (A2), "deploy" (A5)
    # Thay thế: arch_retry_count, eng_retry_count, sec_retry_count, total_retry_count,
    #          deploy_retry_count, deploy_eng_retry_count, deploy_arch_retry_count
    retries: dict[str, RetryTracker]
    total_attempts: int         # Global safety: max 20 attempts toàn pipeline

    # Agent 5
    deployment_result: dict

    # Oscillation prevention — lịch sử fix_instruction A1/A3 đã nhận từ A4/A5
    arch_error_history: list
    eng_error_history: list

    # Audit log
    routing_log: list

    # Per-run working directory — set by evaluate.py (tmp/row_<idx>), dùng bởi A4/A5
    # cho structured dirs. Optional: nếu không set, A4/A5 fallback về temp dir.
    run_dir: str
