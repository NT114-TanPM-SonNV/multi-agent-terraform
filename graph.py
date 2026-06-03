"""LangGraph pipeline — ráp 5 agent thành StateGraph với các vòng retry.

Topology:
    START → architecture → security → engineering → validation

    validation ─(route_after_validation)─→ deployment        (pass)
                                         → architecture       (MISSING_RESOURCE)
                                         → engineering        (SECURITY/SYNTAX/LOGIC)
                                         → requires_human      (INFRASTRUCTURE / hết budget / oscillation)

    deployment ─(route_after_deployment)─→ END                (success)
                                         → deployment          (TRANSIENT retry)
                                         → engineering         (FIXABLE — code fix)
                                         → architecture        (MISSING_RESOURCE — re-plan)
                                         → requires_human       (UNKNOWN/dirty/budget)

Edge tĩnh: architecture→security, security→engineering. architecture & engineering có
conditional edge chặn fail (INFRASTRUCTURE/SYNTAX) khỏi chảy xuống làm A4 chấm nhầm code cũ/rỗng;
engineering→validation chỉ khi A3 success.
"""
import logging
import os

from langgraph.graph import StateGraph, START, END

from core.state import AgentState
from agents.architecture import architecture_node
from agents.security import security_node
from agents.engineering import engineering_node
from agents.validation import validation_node, route_after_validation
from agents.deployment import deployment_node, route_after_deployment

logger = logging.getLogger(__name__)

# Cao hơn default 25 vì các vòng retry (mỗi cycle 2-5 node) có thể vượt 25 trước khi
# chạm cap total_retry_count=5 / deploy_retry_count. Worst-case (A4 5 retry + A5
# transient/eng/arch) ~44 node → 100 cho margin; các cap retry thật mới là chốt chặn loop,
# RECURSION_LIMIT chỉ là trần an toàn (không gây loop vô hạn vì cap đã bound).
RECURSION_LIMIT = 100


def route_after_architecture(state: AgentState) -> str:
    """Conditional edge sau Agent 1. KHÔNG ghi state.

    A1 LLM lỗi → architecture_node trả make_fail("INFRASTRUCTURE") (chỉ set fix_feedback, KHÔNG set
    infrastructure_plan). Nếu cứ chảy xuôi A2→A3→A4 thì A4 thấy code rỗng → MISSING_RESOURCE
    → route ngược về A1 → loop đốt cạn total_retry_count mà không sửa được gì. Chặn tại đây.
    architecture_node khi success clear fix_feedback={} nên error_type chỉ còn INFRASTRUCTURE khi đúng là
    A1 vừa fail (không nhầm với stale feedback sau MISSING_RESOURCE re-plan)."""
    fb = state.get("fix_feedback") or {}
    if fb.get("error_type") == "INFRASTRUCTURE":
        return "requires_human"
    return "security"


_MAX_ARCH_RETRY = 2  # đồng bộ agents/validation.py — bound vòng A3→A1 khi plan rỗng


def route_after_engineering(state: AgentState) -> str:
    """Conditional edge sau Agent 3. KHÔNG ghi state.

    engineering_node success → fix_feedback={} → error_type None → validation.
    engineering_node fail (INFRASTRUCTURE/SYNTAX) → requires_human.
    """
    fb = state.get("fix_feedback") or {}
    if not fb.get("error_type"):
        return "validation"
    return "requires_human"


def requires_human_node(state: AgentState) -> dict:
    """Terminal: pipeline cần can thiệp người. Lý do nằm trong fix_feedback/
    deployment_result. Không đổi state."""
    vr = state.get("fix_feedback") or {}
    dr = state.get("deployment_result") or {}
    logger.info("REQUIRES_HUMAN — validation=%s deployment=%s",
                vr.get("fix_instruction"), dr.get("error_type"))
    return {}


def build_graph():
    """Dựng và compile LangGraph StateGraph cho toàn pipeline."""
    g = StateGraph(AgentState)

    g.add_node("architecture", architecture_node)
    g.add_node("security", security_node)
    g.add_node("engineering", engineering_node)
    g.add_node("validation", validation_node)
    g.add_node("deployment", deployment_node)
    g.add_node("requires_human", requires_human_node)

    g.add_edge(START, "architecture")
    g.add_conditional_edges("architecture", route_after_architecture, {
        "security": "security",
        "requires_human": "requires_human",
    })
    g.add_edge("security", "engineering")
    g.add_conditional_edges("engineering", route_after_engineering, {
        "validation": "validation",
        "architecture": "architecture",
        "requires_human": "requires_human",
    })
    g.add_conditional_edges("validation", route_after_validation, {
        "deployment": "deployment",
        "architecture": "architecture",
        "security": "security",
        "engineering": "engineering",
        "requires_human": "requires_human",
    })
    g.add_conditional_edges("deployment", route_after_deployment, {
        "end": END,
        "deployment": "deployment",
        "engineering": "engineering",
        "architecture": "architecture",
        "requires_human": "requires_human",
    })
    g.add_edge("requires_human", END)

    return g.compile()


def build_initial_state(prompt: str,
                        terraform_plan_timeout: int | None = None,
                        auto_destroy: bool = False) -> AgentState:
    """Khởi tạo đầy đủ AgentState — TypedDict không có default, thiếu field → KeyError."""
    def _retry_tracker():
        return {"count": 0, "last_error_type": "", "last_error_details": "", "error_history": []}

    return {
        "prompt": prompt,
        "auto_destroy": auto_destroy,
        "terraform_plan_timeout": terraform_plan_timeout if terraform_plan_timeout is not None
            else int(os.environ.get("TF_PLAN_TIMEOUT", "120")),
        "infrastructure_plan": {},
        "security_profile": {},
        "generated_code": "",
        "fix_feedback": {},
        "deployment_result": {},
        "retries": {
            "arch": _retry_tracker(),
            "eng": _retry_tracker(),
            "sec": _retry_tracker(),
            "deploy": _retry_tracker(),
        },
        "total_attempts": 0,
        "routing_log": [],
        "arch_error_history": [],
        "eng_error_history": [],
    }


# Compile một lần khi import — tái dùng cho mọi lần invoke
graph = build_graph()


def run_pipeline(prompt: str, **kwargs) -> AgentState:
    """Chạy toàn pipeline trên một prompt, trả final state."""
    initial = build_initial_state(prompt, **kwargs)
    return graph.invoke(initial, config={"recursion_limit": RECURSION_LIMIT})


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    p = sys.argv[1] if len(sys.argv) > 1 else \
        "Create an S3 bucket with versioning and server-side encryption enabled."
    final = run_pipeline(p)
    print("\n" + "=" * 60)
    print(f"PROMPT: {p}")
    print(f"resources: {len(final['infrastructure_plan'].get('resources', []))}")
    prof = final.get("security_profile") or {}
    print(f"sec_checks: {sum(len(v.get('checks',[])) for v in prof.values())} total IDs selected")
    print(f"code chars: {len(final['generated_code'])}")
    print(f"validation: {final['fix_feedback'].get('overall_passed')} "
          f"({final['fix_feedback'].get('error_type')})")
    print(f"deployment: {final['deployment_result'].get('success')} "
          f"({final['deployment_result'].get('error_type')})")
    _retries = final.get("retries") or {}
    print(f"total_attempts: {final.get('total_attempts', 0)}  deploy_retry: {_retries.get('deploy', {}).get('count', 0)}")
    print(f"routing_log: {len(final['routing_log'])} entries")
