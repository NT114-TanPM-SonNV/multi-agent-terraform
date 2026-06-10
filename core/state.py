"""Shared state schema passed across the LangGraph pipeline."""

from typing import TypedDict


class RetryTracker(TypedDict):
    """Retry bookkeeping for one path."""

    count: int
    last_error_type: str
    last_error_details: str
    error_history: list[str]


class AgentState(TypedDict):
    """Shared memory for the full A1 → A5 pipeline."""

    # Input
    prompt: str

    # Agent outputs
    infrastructure_plan: dict
    security_profile: dict
    security_status: str
    generated_code: str
    fix_feedback: dict
    deployment_result: dict

    # Retry state
    retries: dict[str, RetryTracker]
    total_val_attempts: int
    total_deploy_attempts: int

    # History / anti-oscillation
    arch_error_history: list[dict]
    eng_error_history: list[dict]

    # Audit / runtime
    routing_log: list[dict]
    run_dir: str
