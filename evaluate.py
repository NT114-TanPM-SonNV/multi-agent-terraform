"""Batch evaluation script — chạy full pipeline A1→A2→A3→A4→A5 dùng LangGraph StateGraph.

Routing do LangGraph quản lý qua conditional edges. Dùng graph.stream(stream_mode="updates")
để log per-step và đo timing từng agent.

Chạy:
    python evaluate.py --csv dataset/data-test.csv
    python evaluate.py --csv dataset/data-dev.csv --limit 5
    python evaluate.py --csv dataset/data-test.csv --cases 0 3 7-10
    python evaluate.py --no-secu --csv dataset/data-test.csv
    python evaluate.py --no-deploy --csv dataset/data-test.csv
    python evaluate.py --no-destroy --csv dataset/data-test.csv
    python evaluate.py --workers 3 --csv dataset/data-test.csv
"""
import argparse
import csv
import json
import logging
import os
import re
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from pathlib import Path

# Suppress noisy HTTP/SDK logs (httpx, httpcore, openai, botocore)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)
logging.getLogger("botocore").setLevel(logging.WARNING)
logging.getLogger("boto3").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

from langgraph.graph import StateGraph, START, END

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from graph import (
    graph as _default_graph,
    build_initial_state,
    requires_human_node,
    RECURSION_LIMIT,
)
from cleanup import cleanup_row as _cleanup_row
from agents.architecture import architecture_node
from agents.security import security_node
from agents.engineering import engineering_node
from agents.validation import validation_node, route_after_validation
from agents.deployment import deployment_node, route_after_deployment
from core.state import AgentState

# force=True: reset handler mà thư viện import-time (checkov/litellm/langgraph) đã gắn
# vào root logger — nếu không, basicConfig thành no-op và INFO của agent vẫn lọt ra.
logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s",
                    force=True)

CSV_PATH = ROOT / "dataset" / "data-dev.csv"  # overridden by --csv arg
_RESOURCE_RE = re.compile(r'resource\s+"[^"]+"\s+"[^"]+"')
_PRINT_LOCK = threading.Lock()


# ─── Variant graphs ───────────────────────────────────────────────────────────

def _shared_deploy_edges(g: StateGraph) -> None:
    """Thêm deployment node + conditional edges chung cho mọi variant có deploy."""
    g.add_node("deployment", deployment_node)
    g.add_conditional_edges("deployment", route_after_deployment, {
        "end":            END,
        "deployment":     "deployment",
        "engineering":    "engineering",
        "architecture":   "architecture",
        "requires_human": "requires_human",
    })


def build_no_secu_graph():
    """Graph bỏ security node: architecture → engineering trực tiếp.
    route_after_validation có thể trả "security" → map về "engineering".
    """
    g = StateGraph(AgentState)
    g.add_node("architecture",    architecture_node)
    g.add_node("engineering",     engineering_node)
    g.add_node("validation",      validation_node)
    g.add_node("requires_human",  requires_human_node)
    _shared_deploy_edges(g)

    g.add_edge(START, "architecture")
    g.add_edge("architecture", "engineering")
    g.add_edge("engineering",  "validation")
    g.add_conditional_edges("validation", route_after_validation, {
        "deployment":     "deployment",
        "architecture":   "architecture",
        "security":       "engineering",   # secu absent → fall back to engi
        "engineering":    "engineering",
        "requires_human": "requires_human",
    })
    g.add_edge("requires_human", END)
    return g.compile()


def build_no_deploy_graph():
    """Graph dừng sau validation (không có deployment node)."""
    g = StateGraph(AgentState)
    g.add_node("architecture",   architecture_node)
    g.add_node("security",       security_node)
    g.add_node("engineering",    engineering_node)
    g.add_node("validation",     validation_node)
    g.add_node("requires_human", requires_human_node)

    g.add_edge(START, "architecture")
    g.add_edge("architecture", "security")
    g.add_edge("security",     "engineering")
    g.add_edge("engineering",  "validation")
    g.add_conditional_edges("validation", route_after_validation, {
        "deployment":     END,             # validation pass → kết thúc (không deploy)
        "architecture":   "architecture",
        "security":       "security",
        "engineering":    "engineering",
        "requires_human": "requires_human",
    })
    g.add_edge("requires_human", END)
    return g.compile()


def build_no_secu_no_deploy_graph():
    """Kết hợp: bỏ security + dừng sau validation."""
    g = StateGraph(AgentState)
    g.add_node("architecture",   architecture_node)
    g.add_node("engineering",    engineering_node)
    g.add_node("validation",     validation_node)
    g.add_node("requires_human", requires_human_node)

    g.add_edge(START, "architecture")
    g.add_edge("architecture", "engineering")
    g.add_edge("engineering",  "validation")
    g.add_conditional_edges("validation", route_after_validation, {
        "deployment":     END,
        "architecture":   "architecture",
        "security":       "engineering",
        "engineering":    "engineering",
        "requires_human": "requires_human",
    })
    g.add_edge("requires_human", END)
    return g.compile()


def _select_graph(no_secu: bool, no_deploy: bool):
    if no_secu and no_deploy:
        return build_no_secu_no_deploy_graph()
    if no_secu:
        return build_no_secu_graph()
    if no_deploy:
        return build_no_deploy_graph()
    return _default_graph


# ─── CSV helpers (same as test_pipeline.py) ──────────────────────────────────

def load_csv(limit: int | None) -> list[tuple[int, str, str, list[str]]]:
    rows = list(csv.DictReader(open(CSV_PATH, encoding="utf-8")))
    if limit:
        rows = rows[:limit]
    result = []
    for i, row in enumerate(rows):
        gt_raw = row.get("Resource") or ""
        gt_types = [t.strip() for t in gt_raw.split(",") if t.strip()]
        result.append((i, row.get("Difficulty", ""), row["Prompt"], gt_types))
    return result


def _parse_cases(tokens: list[str]) -> set[int]:
    result = set()
    for part in tokens:
        if "-" in part:
            lo, hi = part.split("-", 1)
            result.update(range(int(lo), int(hi) + 1))
        else:
            result.add(int(part))
    return result


def _n_resources(code: str) -> int:
    return len(_RESOURCE_RE.findall(code))


def _resource_comparison(gt_types: list[str], created: list[str]) -> dict:
    def _normalize(items: list[str]) -> set[str]:
        out = set()
        for item in items:
            parts = item.split(".")
            if parts[0] == "data" and len(parts) >= 2:
                out.add(parts[1])
            elif len(parts) >= 1:
                out.add(parts[0])
        return out

    gt_set  = set(gt_types)
    gen_set = _normalize(created)
    tp = gt_set & gen_set
    fp = gen_set - gt_set
    fn = gt_set - gen_set
    precision = len(tp) / len(gen_set) if gen_set else 0.0
    recall    = len(tp) / len(gt_set)  if gt_set  else 1.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    return {
        "gt": sorted(gt_set), "generated": sorted(gen_set),
        "tp": sorted(tp), "fp": sorted(fp), "fn": sorted(fn),
        "precision": round(precision, 3),
        "recall":    round(recall, 3),
        "f1":        round(f1, 3),
    }


_DECLARED_RE = re.compile(r'(?:resource|data)\s+"([^"]+)"\s+"[^"]+"')


def _declared_types(code: str) -> list[str]:
    """Type của MỌI block khai báo trong HCL (cả resource lẫn data source).

    Dùng cho resource_match khi --no-deploy: không có apply nên không có
    resources_created → trích type trực tiếp từ code A3 sinh (gold gồm cả data-source
    như aws_iam_policy_document)."""
    return _DECLARED_RE.findall(code)


def _timeout_sentinel(row_args: tuple, *, timeout: bool = False, error: str | None = None) -> dict:
    """Kết quả tối thiểu cho row timeout/exception → score.py ĐẾM nó như fail thay vì để
    biến mất khỏi denominator (tránh phồng pass-rate). val={} → plan_valid=False."""
    idx, difficulty, prompt = row_args[0], row_args[1], row_args[2]
    rec = {
        "row": idx, "difficulty": difficulty, "prompt": prompt,
        "timeout": timeout,
        "archi": {"ok": False}, "secu": {}, "engi": {}, "val": {}, "deploy": None,
        "total_retry_count": 0, "deploy_retry_count": 0,
        "routing_log": [], "iterations": 0, "resource_match": {},
    }
    if error:
        rec["error"] = error[:300]
    return rec


# ─── Result extraction from final state ──────────────────────────────────────

def _extract_results(
    final_state: dict,
    timings: dict[str, float],
    node_counts: dict[str, int],
    no_secu: bool,
    no_deploy: bool,
    val_feedback: dict | None = None,
) -> tuple[dict | None, dict | None, dict | None, dict | None, dict | None]:
    """Trích archi/secu/engi/val/deploy result từ final state + timings."""

    # archi
    plan = final_state.get("infrastructure_plan") or {}
    archi_result = None
    if "architecture" in timings:
        ok = bool(plan.get("resources"))
        archi_result = {
            "ok": ok,
            "elapsed_s": timings["architecture"],
            "resource_count": len(plan.get("resources", [])),
            "plan": plan,
        }
        if not ok:
            fb = final_state.get("fix_feedback") or {}
            archi_result["error"] = fb.get("fix_instruction", "unknown")

    # secu
    secu_result = None
    if no_secu:
        secu_result = {
            "ok": True, "elapsed_s": 0, "skipped": True,
            "ckv_resource_count": 0, "ckv_total": 0, "ckv_ids": {},
        }
    elif "security" in timings:
        secu_result = {
            "ok": True,
            "elapsed_s": timings["security"],
            "security_profile": final_state.get("security_profile") or {},
        }

    # engi
    engi_result = None
    if "engineering" in timings:
        code = final_state.get("generated_code", "")
        ok = bool(code.strip())
        engi_result = {
            "ok": ok,
            "elapsed_s": timings["engineering"],
            "resource_count": _n_resources(code),
            "line_count": code.count("\n"),
            "generated_code": code,
        }
        if not ok:
            fb = final_state.get("fix_feedback") or {}
            engi_result["error"] = fb.get("fix_instruction", "unknown")

    # val — dùng val_feedback (captured lúc validation chạy) để tránh bị A5 ghi đè fix_feedback.
    val_result = None
    if "validation" in timings:
        fb = val_feedback if val_feedback is not None else (final_state.get("fix_feedback") or {})
        ck = fb.get("checkov") or {}
        val_result = {
            "ok": bool(fb.get("overall_passed")),
            "elapsed_s": timings["validation"],
            "error_type": fb.get("error_type", ""),
            "checkov_passed": ck.get("passed_count", 0),
            "checkov_failed": ck.get("failed_count", 0),
            "checkov_failed_ids": ck.get("failed_ckv_ids", []),
            "validate_ok": fb.get("validate_passed"),
            "plan_ok": fb.get("plan_passed"),
            "security_incomplete": bool(fb.get("unmet_checks")),
            "unmet_checks": fb.get("unmet_checks", []),
            "phantom_checks": fb.get("phantom_checks", []),
            "raw_error": (fb.get("raw_error") or "")[:2000],
            "fix_instruction": (fb.get("fix_instruction") or "")[:500],
            "attempts": node_counts.get("validation", 0),
        }

    # deploy
    deploy_result = None
    if not no_deploy and "deployment" in timings:
        dr = final_state.get("deployment_result") or {}
        ok = bool(dr.get("success"))
        deploy_result = {
            "ok": ok,
            "elapsed_s": timings["deployment"],
            "error_type": dr.get("error_type") if not ok else None,
            "resources_created": dr.get("resources_created", []),
            "auto_destroyed": dr.get("auto_destroyed", False),
            "auto_destroy_error": dr.get("auto_destroy_error"),
            "apply_raw_error": (dr.get("apply_raw_error") or "")[:2000],
            "fix_instruction": (dr.get("fix_instruction") or "")[:500],
            "attempts": node_counts.get("deployment", 0),
        }

    return archi_result, secu_result, engi_result, val_result, deploy_result


# ─── Row runner ───────────────────────────────────────────────────────────────

def run_row_lg(
    idx: int,
    difficulty: str,
    prompt: str,
    g,
    auto_destroy: bool,
    no_secu: bool,
    no_deploy: bool,
    gt_types: list[str] | None = None,
    live: bool = False,
) -> tuple[dict, str]:
    """Chạy 1 row qua LangGraph graph. Trả về (result_dict, output_str)."""
    lines: list[str] = []

    def log(msg: str = "") -> None:
        lines.append(msg)
        if live:
            print(msg)

    sep = "=" * 72
    log(f"\n{sep}")
    log(f"ROW {idx:4d}  difficulty={difficulty or '?'}")
    log(f"  {prompt[:100]}")
    log(sep)

    # Build initial state — patch run_dir (build_initial_state không có field này)
    run_dir = ROOT / "tmp" / f"row_{idx}"
    run_dir.mkdir(parents=True, exist_ok=True)
    state: dict = build_initial_state(prompt, auto_destroy=auto_destroy)
    state["run_dir"] = str(run_dir)

    # Accumulate stream updates
    timings: dict[str, float] = {}       # node → cumulative elapsed (giây)
    node_counts: dict[str, int] = {}     # node → số lần chạy
    last_val_fb: dict = {}               # fix_feedback lần validation cuối (tránh A5 ghi đè)
    final_state = dict(state)
    iterations = 0
    t_prev = time.time()

    try:
        for chunk in g.stream(
            state,
            config={"recursion_limit": RECURSION_LIMIT},
            stream_mode="updates",
        ):
            t_now = time.time()
            elapsed = round(t_now - t_prev, 2)
            t_prev = t_now
            iterations += 1

            for node_name, update in chunk.items():
                timings[node_name]     = round(timings.get(node_name, 0) + elapsed, 2)
                node_counts[node_name] = node_counts.get(node_name, 0) + 1
                if update is not None:
                    final_state.update(update)

                # ── per-node log ──────────────────────────────────────────────
                if node_name == "architecture":
                    plan = update.get("infrastructure_plan") or {}
                    n = len(plan.get("resources", []))
                    ok = bool(n)
                    if ok:
                        log(f"  [archi] {n} resources ({elapsed}s)")
                    else:
                        fb = update.get("fix_feedback") or {}
                        log(f"  [archi] FAILED ({elapsed}s): "
                            f"{(fb.get('fix_instruction') or '')[:200]}")
                    if no_secu:
                        log(f"  [secu]  skipped (--no-secu)")

                elif node_name == "security":
                    prof = update.get("security_profile") or {}
                    checks_summary = {k: v.get("checks", []) for k, v in prof.items() if v.get("checks")}
                    log(f"  [secu]  {len(prof)} resources ({elapsed}s)")
                    if checks_summary:
                        log(f"    security checks: {checks_summary}")

                elif node_name == "engineering":
                    code = update.get("generated_code", "")
                    ok = bool(code.strip())
                    if ok:
                        log(f"  [engi]  {_n_resources(code)} resources, "
                            f"{code.count(chr(10))} lines ({elapsed}s)")
                    else:
                        fb = update.get("fix_feedback") or {}
                        log(f"  [engi]  FAILED ({elapsed}s): "
                            f"{(fb.get('fix_instruction') or '')[:80]}")

                elif node_name == "validation":
                    fb = update.get("fix_feedback") or {}
                    last_val_fb = fb
                    passed = bool(fb.get("overall_passed"))
                    et = fb.get("error_type", "")
                    ck = fb.get("checkov") or {}
                    unmet = fb.get("unmet_checks") or []
                    status = "PASS" if passed else f"FAIL [{et}]"
                    if passed and unmet:
                        unmet_ids = sorted({u.get("ckv_id") for u in unmet})
                        status = f"PASS (security best-effort, unmet {unmet_ids})"
                    phantom = fb.get("phantom_checks") or []
                    ck_str = (f"ckv pass={ck.get('passed_count',0)} "
                              f"fail={ck.get('failed_count',0)} "
                              f"fail_ids={ck.get('failed_ckv_ids',[])}"
                              + (f" phantom={phantom}" if phantom else "")) if ck else ""
                    attempt = node_counts.get("validation", 0)
                    total_r = final_state.get("total_attempts", 0)
                    log(f"  [val]   {status} ({elapsed}s) {ck_str}"
                        f" attempt={attempt} total_retry={total_r}")
                    if not passed and fb.get("fix_instruction"):
                        log(f"  [val]   fix: {fb['fix_instruction'][:100]}")

                elif node_name == "deployment":
                    dr = update.get("deployment_result") or {}
                    ok = bool(dr.get("success"))
                    attempt = node_counts.get("deployment", 0)
                    if ok:
                        n_created = len(dr.get("resources_created", []))
                        d_str = "(destroyed)" if dr.get("auto_destroyed") else "(resources kept)"
                        log(f"  [deploy] OK ({elapsed}s) {n_created} resources {d_str}")
                    else:
                        et = dr.get("error_type", "")
                        log(f"  [deploy] FAIL [{et}] ({elapsed}s) attempt={attempt}")
                        if dr.get("fix_instruction"):
                            log(f"  [deploy] fix: {dr['fix_instruction'][:100]}")

                elif node_name == "requires_human":
                    fb = final_state.get("fix_feedback") or {}
                    dr = final_state.get("deployment_result") or {}
                    reason = fb.get("fix_instruction") or dr.get("error_type") or "unknown"
                    log(f"  [→] REQUIRES_HUMAN: {str(reason)[:120]}")

    except Exception as e:
        import traceback
        log(f"  [ERROR] graph.stream exception: {e}")
        log(traceback.format_exc())

    # Post-row AWS cleanup — safety net sau auto_destroy A5.
    # Chỉ chạy khi có deploy thật (no_deploy=False); idempotent (NotFound bị bỏ qua).
    if not no_deploy:
        _cleanup_row(
            final_state.get("generated_code", ""),
            region=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
            row_idx=idx,
        )

    # Cleanup per-run dir
    if run_dir.exists():
        shutil.rmtree(run_dir, ignore_errors=True)

    # Extract structured results
    archi_result, secu_result, engi_result, val_result, deploy_result = _extract_results(
        final_state, timings, node_counts, no_secu, no_deploy,
        val_feedback=last_val_fb or None,
    )

    # Resource comparison vs ground truth
    cmp: dict = {}
    if gt_types is not None:
        # Deploy → resource apply thật; --no-deploy (hoặc chưa apply) → type khai báo trong HCL.
        created = (deploy_result or {}).get("resources_created", [])
        if not created:
            created = _declared_types((engi_result or {}).get("generated_code", ""))
        cmp = _resource_comparison(gt_types, created)
        log(f"  [match] P={cmp['precision']:.2f} R={cmp['recall']:.2f} F1={cmp['f1']:.2f}"
            f"  TP={len(cmp['tp'])} FP={len(cmp['fp'])} FN={len(cmp['fn'])}")
        if cmp["fp"]:
            log(f"  [match]   extra  : {', '.join(cmp['fp'])}")
        if cmp["fn"]:
            log(f"  [match]   missing: {', '.join(cmp['fn'])}")

    result = {
        "row": idx,
        "difficulty": difficulty,
        "prompt": prompt,
        "archi":  archi_result,
        "secu":   secu_result,
        "engi":   engi_result,
        "val":    val_result,
        "deploy": deploy_result,
        "total_retry_count":  final_state.get("total_attempts", 0),
        "deploy_retry_count": (final_state.get("retries") or {}).get("deploy", {}).get("count", 0),
        "routing_log": final_state.get("routing_log", []),
        "iterations":  iterations,
        "resource_match": cmp,
    }
    return result, "\n".join(lines)


# ─── Counters (same as test_pipeline.py) ─────────────────────────────────────

def _update_counters(counters: dict, r: dict, no_deploy: bool,
                     lock: threading.Lock) -> None:
    def _ok(key): return r.get(key) and r[key].get("ok")
    with lock:
        if _ok("archi"):   counters["ok1"] += 1
        else:              counters["fail1"] += 1
        if r["archi"]:
            if _ok("secu"):  counters["ok2"] += 1
            elif r["secu"]:  counters["fail2"] += 1
        if r["secu"]:
            if _ok("engi"):  counters["ok3"] += 1
            elif r["engi"]:  counters["fail3"] += 1
        if r["engi"]:
            if _ok("val"):   counters["ok4"] += 1
            elif r["val"]:   counters["fail4"] += 1
        if r["val"] and not no_deploy:
            if _ok("deploy"):  counters["ok5"] += 1
            elif r["deploy"]:  counters["fail5"] += 1


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Test full pipeline A1→A2→A3→A4→A5 dùng LangGraph"
    )
    parser.add_argument("--csv",    type=str, default=None, help="CSV path (mặc định data-dev.csv)")
    parser.add_argument("--limit",    type=int, default=None, help="Số row tối đa")
    parser.add_argument("--out",      type=str, default=None, help="Output JSON path")
    parser.add_argument("--cases",    nargs="+", default=None,
                        help="Row indices, e.g. --cases 0 3 7-10 15")
    parser.add_argument("--no-secu",    action="store_true", help="Bỏ qua A2")
    parser.add_argument("--no-deploy",  action="store_true", help="Dừng sau A4")
    parser.add_argument("--no-destroy", action="store_true", help="Giữ resources sau apply")
    parser.add_argument("--workers",  type=int, default=1,
                        help="Số worker song song (mặc định 1)")
    parser.add_argument("--row-timeout", type=int, default=1800,
                        help="Timeout mỗi row tính bằng giây (mặc định 1800; nên >= vài lần "
                             "LLM_TIMEOUT vì 1 row gồm nhiều call, nhất là model reasoning)")
    args = parser.parse_args()

    # Fail-fast: thiếu terraform/checkov thì báo ngay thay vì crash giữa batch.
    from core.terraform import check_required_tools
    check_required_tools()

    # Cảnh báo config mâu thuẫn: 1 call LLM (<= LLM_TIMEOUT) không được dài hơn ngân sách
    # cả row, nếu không case khó sẽ bị cắt giữa chừng = "timeout giả".
    _llm_timeout = int(os.environ.get("LLM_TIMEOUT", "120"))
    if args.row_timeout < _llm_timeout:
        print(f"  [warn] --row-timeout ({args.row_timeout}s) < LLM_TIMEOUT ({_llm_timeout}s): "
              f"1 call LLM có thể vượt ngân sách row → timeout giả. Nên tăng --row-timeout.")

    global CSV_PATH
    if args.csv:
        CSV_PATH = Path(args.csv)

    provider = os.getenv("LLM_PROVIDER", "deepseek").lower()
    model = (os.getenv("DEEPSEEK_MODEL", "deepseek-chat") if provider == "deepseek"
             else os.getenv("NVIDIA_MODEL", "meta/llama-3.3-70b-instruct"))
    deploy_str = ("no-deploy" if args.no_deploy
                  else ("no-destroy" if args.no_destroy else "auto-destroy"))
    print(f"Full pipeline A1→A2→A3→A4→A5 [LangGraph]  |  model={model}  "
          f"|  csv={CSV_PATH.name}  |  deploy={deploy_str}  |  workers={args.workers}")

    g = _select_graph(args.no_secu, args.no_deploy)

    rows = load_csv(args.limit)
    if args.cases:
        selected = _parse_cases(args.cases)
        rows = [(i, d, p, gt) for i, d, p, gt in rows if i in selected]
        print(f"--cases filter: {len(rows)} rows")
    print(f"Loaded {len(rows)} rows\n")

    results: list[dict] = []
    counters = {k: 0 for k in ("ok1", "ok2", "ok3", "ok4", "ok5",
                                "fail1", "fail2", "fail3", "fail4", "fail5")}
    counter_lock = threading.Lock()

    def _run_one(row_args: tuple, live: bool = False) -> tuple[dict, str]:
        idx, difficulty, prompt, gt_types = row_args
        return run_row_lg(
            idx, difficulty, prompt, g,
            auto_destroy=not args.no_destroy,
            no_secu=args.no_secu,
            no_deploy=args.no_deploy,
            gt_types=gt_types,
            live=live,
        )

    interrupted = False

    if args.workers <= 1:
        for row_args in rows:
            idx = row_args[0]
            _result: list = []
            _exc:    list = []

            def _single_target(ra=row_args):
                try:
                    _result.append(_run_one(ra, live=True))
                except BaseException as e:
                    _exc.append(e)

            t = threading.Thread(target=_single_target, daemon=True)
            t.start()
            t.join(timeout=args.row_timeout)

            if t.is_alive():
                print(f"\n  [timeout] row={idx}: vượt {args.row_timeout}s — "
                      f"thread vẫn chạy nền (không kill được; kiểm tra resource AWS).")
                results.append(_timeout_sentinel(row_args, timeout=True))
                with counter_lock:
                    counters["fail1"] += 1
                continue

            if _exc:
                e = _exc[0]
                if isinstance(e, KeyboardInterrupt):
                    print("\n[interrupted]")
                    interrupted = True
                    break
                print(f"  [error] row={idx}: {e}")
                import traceback; traceback.print_exc()
                results.append(_timeout_sentinel(row_args, error=str(e)))
                with counter_lock:
                    counters["fail1"] += 1
            elif _result:
                r, _ = _result[0]
                results.append(r)
                _update_counters(counters, r, args.no_deploy, counter_lock)
    else:
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            future_to_args = {
                executor.submit(_run_one, row_args): row_args
                for row_args in rows
            }
            try:
                # Iterate theo submission order + result(timeout) để ÉP row-timeout per-row.
                # (as_completed chỉ yield future ĐÃ xong → result() không bao giờ timeout.)
                for future, ra in future_to_args.items():
                    idx = ra[0]
                    try:
                        r, output = future.result(timeout=args.row_timeout)
                        with _PRINT_LOCK:
                            print(output)
                        results.append(r)
                        _update_counters(counters, r, args.no_deploy, counter_lock)
                    except FuturesTimeoutError:
                        with _PRINT_LOCK:
                            print(f"  [timeout] row={idx}: vượt {args.row_timeout}s — bỏ qua. "
                                  f"LƯU Ý: terraform/LLM nền có thể VẪN chạy (không kill được); "
                                  f"nếu deploy → KIỂM TRA & xóa resource sót trên AWS thủ công.")
                        # Ghi sentinel để score.py ĐẾM row này (fail), không để nó biến mất khỏi
                        # denominator → tránh phồng pass-rate (đặc biệt case khó timeout trên v4-pro).
                        results.append(_timeout_sentinel(ra, timeout=True))
                        with counter_lock:
                            counters["fail1"] += 1
                    except Exception as e:
                        with _PRINT_LOCK:
                            print(f"  [error] row={idx}: {e}")
                            import traceback; traceback.print_exc()
                        results.append(_timeout_sentinel(ra, error=str(e)))
                        with counter_lock:
                            counters["fail1"] += 1
            except KeyboardInterrupt:
                print("\n[interrupted — cancelling remaining futures]")
                for f in future_to_args:
                    f.cancel()
                interrupted = True

        results.sort(key=lambda r: r["row"])

    total = counters["ok1"] + counters["fail1"]
    print(f"\n{'='*72}")
    print(f"SUMMARY [LangGraph]  total={total}" + (" [interrupted]" if interrupted else ""))
    print(f"  A1 archi:  {counters['ok1']}/{total}  ok")
    if counters["ok1"]:
        print(f"  A2 secu:   {counters['ok2']}/{counters['ok1']}  ok")
        print(f"  A3 engi:   {counters['ok3']}/{counters['ok1']}  ok")
    if counters["ok3"]:
        print(f"  A4 val:    {counters['ok4']}/{counters['ok3']}  ok")
    if counters["ok4"] and not args.no_deploy:
        print(f"  A5 deploy: {counters['ok5']}/{counters['ok4']}  ok")

    out_path = (Path(args.out) if args.out
                else ROOT / "reviews" / "pipeline_lg_results.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(results, indent=2, ensure_ascii=False))
    print(f"\nSaved {len(results)} results → {out_path}")


if __name__ == "__main__":
    main()
