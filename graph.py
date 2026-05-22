"""LangGraph pipeline — ráp 5 agent thành StateGraph với các vòng retry.

Topology:
    START → architecture → floci_check ─(route)─→ security → engineering → validation
                                  └─ unsupported ─→ requires_human

    validation ─(route_after_validation)─→ deployment        (pass)
                                         → architecture       (MISSING_RESOURCE)
                                         → security           (WRONG_CONSTRAINT)
                                         → engineering        (SECURITY/SYNTAX/LOGIC)
                                         → requires_human      (INFRA / hết budget / oscillation)

    deployment ─(route_after_deployment)─→ END                (success)
                                         → deployment          (TRANSIENT retry)
                                         → engineering         (FIXABLE)
                                         → requires_human       (FLOCI_LIMIT/UNKNOWN/dirty)

Các edge tĩnh (architecture→floci_check, security→engineering, engineering→validation)
khiến mọi vòng retry tự chảy xuôi tới validation rồi mới route tiếp.
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
from dataset.filter import FLOCI_SUPPORTED

logger = logging.getLogger(__name__)

# Cao hơn default 25 vì các vòng retry (mỗi cycle 2-5 node) có thể vượt 25 trước khi
# chạm cap total_retry_count=5 / deploy_retry_count.
RECURSION_LIMIT = 60


def _plan_unsupported_types(state: AgentState) -> list:
    plan = state.get("infrastructure_plan") or {}
    types = (
        [r["type"] for r in plan.get("resources", [])]
        + [d["type"] for d in plan.get("data_sources", [])]
    )
    return sorted({t for t in types if t not in FLOCI_SUPPORTED})


def floci_check_node(state: AgentState) -> dict:
    """Gate non-LLM: nếu Agent 1 sinh resource type Floci không hỗ trợ → ghi INFRA
    record để terminal state giải thích lý do. Pass → không ghi gì."""
    unsupported = _plan_unsupported_types(state)
    if unsupported:
        logger.info("Floci check: FAIL — unsupported %s", unsupported)
        return {"validation_result": {
            "overall_passed": False, "error_type": "INFRA", "root_cause": None,
            "fix_instruction": f"Floci không hỗ trợ resource type: {unsupported}",
            "checkov": {"passed_count": 0, "failed": []},
            "validate_passed": False, "plan_passed": False,
        }}
    return {}


def route_after_floci_check(state: AgentState) -> str:
    """Recompute từ plan (không đọc validation_result — tránh nhiễu state ở vòng retry)."""
    return "requires_human" if _plan_unsupported_types(state) else "security"


def requires_human_node(state: AgentState) -> dict:
    """Terminal: pipeline cần can thiệp người. Lý do nằm trong validation_result/
    deployment_result. Không đổi state."""
    vr = state.get("validation_result") or {}
    dr = state.get("deployment_result") or {}
    logger.info("REQUIRES_HUMAN — validation=%s deployment=%s",
                vr.get("fix_instruction"), dr.get("error_type"))
    return {}


def build_graph():
    """Dựng và compile LangGraph StateGraph cho toàn pipeline."""
    g = StateGraph(AgentState)

    g.add_node("architecture", architecture_node)
    g.add_node("floci_check", floci_check_node)
    g.add_node("security", security_node)
    g.add_node("engineering", engineering_node)
    g.add_node("validation", validation_node)
    g.add_node("deployment", deployment_node)
    g.add_node("requires_human", requires_human_node)

    g.add_edge(START, "architecture")
    g.add_edge("architecture", "floci_check")
    g.add_conditional_edges("floci_check", route_after_floci_check, {
        "security": "security",
        "requires_human": "requires_human",
    })
    g.add_edge("security", "engineering")
    g.add_edge("engineering", "validation")
    g.add_conditional_edges("validation", route_after_validation, {
        "agent5": "deployment",
        "architecture": "architecture",
        "security": "security",
        "engineering": "engineering",
        "requires_human": "requires_human",
    })
    g.add_conditional_edges("deployment", route_after_deployment, {
        "end": END,
        "agent5": "deployment",
        "engineering": "engineering",
        "requires_human": "requires_human",
    })
    g.add_edge("requires_human", END)

    return g.compile()


def build_initial_state(prompt: str, floci_endpoint: str | None = None,
                        terraform_plan_timeout: int | None = None) -> AgentState:
    """Khởi tạo đầy đủ AgentState — TypedDict không có default, thiếu field → KeyError."""
    return {
        "prompt": prompt,
        "floci_endpoint": floci_endpoint if floci_endpoint is not None
            else os.environ.get("FLOCI_ENDPOINT", "http://localhost:4566"),
        "terraform_plan_timeout": terraform_plan_timeout if terraform_plan_timeout is not None
            else int(os.environ.get("TF_PLAN_TIMEOUT", "120")),
        "infrastructure_plan": {},
        "security_constraints": {},
        "security_ckv_ids": {},
        "generated_code": "",
        "validation_result": {},
        "deployment_result": {},
        "arch_retry_count": 0,
        "sec_retry_count": 0,
        "eng_retry_count": 0,
        "total_retry_count": 0,
        "deploy_retry_count": 0,
        "error_history": [],
        "routing_log": [],
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
    print(f"constraints: {len(final['security_constraints'])}")
    print(f"code chars: {len(final['generated_code'])}")
    print(f"validation: {final['validation_result'].get('overall_passed')} "
          f"({final['validation_result'].get('error_type')})")
    print(f"deployment: {final['deployment_result'].get('success')} "
          f"({final['deployment_result'].get('error_type')})")
    print(f"total_retry: {final['total_retry_count']}  deploy_retry: {final['deploy_retry_count']}")
    print(f"routing_log: {len(final['routing_log'])} entries")
