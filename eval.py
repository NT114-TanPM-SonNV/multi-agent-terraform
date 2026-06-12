"""eval.py — runner GỌN, độc lập: chỉ hiện live progress 7 trạng thái/row, KHÔNG ghi log file.

7 trạng thái mỗi row:
    Row N — Architecture processing...   (tương tự Security/Engineering/Validation/Deployment)
    Row N — ✓ PASS (time)
    Row N — ✗ FAIL (time)

Luôn chạy full pipeline A1→A5 (deploy thật, auto-destroy). Chỉ phụ thuộc framework
(graph/core) — KHÔNG import evaluate.py.

Chạy:
    python eval.py --dataset dataset/data-dev.csv --workers 3 --out reviews/eval.json
"""
import argparse
import csv
import json
import logging
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from langgraph.graph import StateGraph, START, END
from graph import (
    graph as _graph,
    build_initial_state,
    requires_human_node,
    route_after_architecture,
    route_after_engineering,
    RECURSION_LIMIT,
)
from agents.architecture import architecture_node
from agents.security import security_node
from agents.engineering import engineering_node
from agents.validation import validation_node, route_after_validation
from core.state import AgentState
from core.terraform import _safe_rmtree
from core.destroy import start_destroy_worker, shutdown_destroy_worker

# Tắt log ồn của lib + chỉ giữ ERROR (runner này không ghi log chi tiết).
for _n in ("httpx", "httpcore", "openai", "botocore", "boto3", "urllib3"):
    logging.getLogger(_n).setLevel(logging.WARNING)
logging.basicConfig(level=logging.ERROR, format="%(levelname)s %(name)s: %(message)s", force=True)

_RESOURCE_RE = re.compile(r'resource\s+"[^"]+"\s+"[^"]+"')
_DECLARED_RE = re.compile(r'(?:resource|data)\s+"([^"]+)"\s+"[^"]+"')


def _build_no_deploy_graph():
    g = StateGraph(AgentState)
    g.add_node("architecture",   architecture_node)
    g.add_node("security",       security_node)
    g.add_node("engineering",    engineering_node)
    g.add_node("validation",     validation_node)
    g.add_node("requires_human", requires_human_node)
    g.add_edge(START, "architecture")
    g.add_conditional_edges("architecture", route_after_architecture,
                            {"security": "security", "requires_human": "requires_human"})
    g.add_edge("security", "engineering")
    g.add_conditional_edges("engineering", route_after_engineering,
                            {"validation": "validation", "requires_human": "requires_human"})
    g.add_conditional_edges("validation", route_after_validation, {
        "deployment":     END,
        "architecture":   "architecture",
        "engineering":    "engineering",
        "requires_human": "requires_human",
    })
    g.add_edge("requires_human", END)
    return g.compile()


# ── Helpers (data only) ───────────────────────────────────────────────────────
def _n_resources(code: str) -> int:
    return len(_RESOURCE_RE.findall(code))


def _declared_types(code: str) -> list[str]:
    """Type của MỌI block khai báo trong HCL (resource + data source) — fallback cho
    resource_match khi chưa có resources_created từ apply."""
    return _DECLARED_RE.findall(code)


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

    gt_set = set(gt_types)
    gen_set = _normalize(created)
    tp = gt_set & gen_set
    fp = gen_set - gt_set
    fn = gt_set - gen_set
    precision = len(tp) / len(gen_set) if gen_set else 0.0
    recall = len(tp) / len(gt_set) if gt_set else 1.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    return {
        "gt": sorted(gt_set), "generated": sorted(gen_set),
        "tp": sorted(tp), "fp": sorted(fp), "fn": sorted(fn),
        "precision": round(precision, 3), "recall": round(recall, 3), "f1": round(f1, 3),
    }


def _check_aws_identity() -> None:
    """Fail-fast preflight credential AWS (retry ngoài qua blip DNS/network)."""
    import boto3
    from botocore.config import Config
    attempts = int(os.environ.get("AWS_PREFLIGHT_RETRIES", "3"))
    cfg = Config(connect_timeout=10, read_timeout=30,
                 retries={"max_attempts": 3, "mode": "standard"}, proxies={})
    last_err: Exception | None = None
    for i in range(attempts):
        try:
            ident = boto3.client("sts", config=cfg).get_caller_identity()
            print(f"AWS identity OK  |  account={ident.get('Account', '')}  |  arn={ident.get('Arn', '')}")
            return
        except Exception as e:
            last_err = e
            if i < attempts - 1:
                wait = 5 * (i + 1)
                print(f"AWS preflight {i+1}/{attempts} failed ({type(e).__name__}) — retry {wait}s",
                      file=sys.stderr)
                time.sleep(wait)
    raise RuntimeError(f"AWS credential preflight failed sau {attempts} lần: "
                       f"{type(last_err).__name__}: {last_err}") from last_err


def _timeout_sentinel(row_args: tuple, *, error: str | None = None,
                      no_deploy: bool = False) -> dict:
    """Kết quả tối thiểu cho row exception → score.py ĐẾM như fail (không biến mất khỏi denominator)."""
    idx, difficulty, prompt = row_args[0], row_args[1], row_args[2]
    rec = {
        "row": idx, "difficulty": difficulty, "prompt": prompt,
        "architecture": {"ok": False}, "security": {}, "engineering": {}, "validation": {}, "deployment": None,
        "run_mode": "no-deploy" if no_deploy else "full",
        "total_retry_count": 0, "deploy_backstop_count": 0, "deploy_retry_count": 0,
        "routing_log": [], "resource_match": {},
        "total_elapsed_s": 0,
    }
    if error:
        rec["error"] = error[:300]
    return rec


def _extract_results(final_state: dict, timings: dict, node_counts: dict, val_feedback: dict | None = None):
    """Trích architecture/security/engineering/validation/deployment result từ final state."""
    # architecture
    plan = final_state.get("infrastructure_plan") or {}
    architecture_result = None
    if "architecture" in timings:
        ok = bool(plan.get("resources"))
        architecture_result = {
            "ok": ok, "elapsed_s": timings["architecture"],
            "resource_count": len(plan.get("resources", [])), "plan": plan,
        }
        if not ok:
            fb = final_state.get("fix_feedback") or {}
            architecture_result["error"] = fb.get("fix_instruction", "unknown")
            architecture_result["error_stage"] = fb.get("error_stage")
            architecture_result["raw_error"] = (fb.get("raw_error") or "")[:2000]

    # security
    security_result = None
    if "security" in timings:
        security_result = {
            "ok": True, "status": final_state.get("security_status", "ok"),
            "elapsed_s": timings["security"],
            "security_profile": final_state.get("security_profile") or {},
        }

    # engineering
    engineering_result = None
    if "engineering" in timings:
        code = final_state.get("generated_code", "")
        ok = bool(code.strip())
        engineering_result = {
            "ok": ok, "elapsed_s": timings["engineering"],
            "resource_count": _n_resources(code), "line_count": code.count("\n"),
            "generated_code": code,
        }
        if not ok:
            fb = final_state.get("fix_feedback") or {}
            engineering_result["error"] = fb.get("fix_instruction", "unknown")
            engineering_result["error_label"] = fb.get("error_label")
            engineering_result["error_stage"] = fb.get("error_stage")

    # validation — dùng val_feedback (tránh bị deployment ghi đè fix_feedback)
    validation_result = None
    if "validation" in timings:
        fb = val_feedback if val_feedback is not None else (final_state.get("fix_feedback") or {})
        ck = fb.get("checkov") or {}
        validation_result = {
            "ok": bool(fb.get("overall_passed")), "elapsed_s": timings["validation"],
            "error_type": fb.get("error_type", ""),
            "checkov_passed": ck.get("passed_count", 0), "checkov_failed": ck.get("failed_count", 0),
            "checkov_failed_ids": ck.get("failed_ckv_ids", []),
            "validate_ok": fb.get("validate_passed"), "plan_ok": fb.get("plan_passed"),
            "security_incomplete": bool(fb.get("applicable_failed_checks")),
            "security_degraded": bool(fb.get("security_degraded")),
            "applicable_failed_checks": fb.get("applicable_failed_checks", []),
            "not_applicable_checks": fb.get("not_applicable_checks", []),
            "raw_error": (fb.get("raw_error") or "")[:2000],
            "fix_instruction": (fb.get("fix_instruction") or "")[:500],
            "attempts": node_counts.get("validation", 0),
        }

    # deployment
    deployment_result = None
    if "deployment" in timings:
        dr = final_state.get("deployment_result") or {}
        ok = bool(dr.get("success"))
        deployment_result = {
            "ok": ok, "elapsed_s": timings["deployment"],
            "error_type": dr.get("error_type") if not ok else None,
            "error_label": dr.get("error_label") if not ok else None,
            "cleanup_error_label": dr.get("cleanup_error_label") if not ok else None,
            "resources_created": dr.get("resources_created", []),
            "destroyed": dr.get("destroyed", False), "destroy_error": dr.get("destroy_error"),
            "apply_raw_error": (dr.get("apply_raw_error") or "")[:2000],
            "fix_instruction": (dr.get("fix_instruction") or "")[:500],
            "attempts": node_counts.get("deployment", 0),
        }

    return architecture_result, security_result, engineering_result, validation_result, deployment_result


# ── Live progress (7 trạng thái) ──────────────────────────────────────────────
_LOCK = threading.Lock()
_PROGRESS: dict[int, dict] = {}  # row → {"current", "status": processing/pass/fail, "elapsed"}

# node vừa xong → agent kế đang chạy (luôn ∈ 5 tên agent). validation xử lý riêng.
_NEXT = {"architecture": "Security", "security": "Engineering",
         "engineering": "Validation", "deployment": "Deployment"}


def _set(row: int, current: str, status: str = "processing", elapsed: float = 0.0):
    with _LOCK:
        _PROGRESS[row] = {"current": current, "status": status, "elapsed": elapsed}


def _render() -> str:
    with _LOCK:
        out = []
        for r in sorted(_PROGRESS):
            p = _PROGRESS[r]
            if p["status"] == "pass":
                out.append(f"  Row {r:<3} — \033[1;32m✓ PASS\033[0m ({p['elapsed']:.1f}s)")
            elif p["status"] == "fail":
                out.append(f"  Row {r:<3} — \033[1;31m✗ FAIL\033[0m ({p['elapsed']:.1f}s)")
            else:
                out.append(f"  Row {r:<3} — {p['current']} processing...")
        return "\n".join(out)


# ── Chạy 1 row ────────────────────────────────────────────────────────────────
def _run_row(idx, difficulty, prompt, gt_types, *, graph=None, no_deploy: bool = False) -> dict:
    _set(idx, "Architecture")
    run_dir = ROOT / "tmp" / f"row_{idx}"
    run_dir.mkdir(parents=True, exist_ok=True)
    state = build_initial_state(prompt)
    state["run_dir"] = str(run_dir)
    if graph is None:
        graph = _graph

    timings: dict[str, float] = {}
    node_counts: dict[str, int] = {}
    last_val_fb: dict = {}
    final_state = dict(state)
    t_prev = time.time()
    err = None
    try:
        for chunk in graph.stream(state, config={"recursion_limit": RECURSION_LIMIT},
                                  stream_mode="updates"):
            now = time.time(); elapsed = round(now - t_prev, 2); t_prev = now
            for node, update in chunk.items():
                timings[node] = round(timings.get(node, 0) + elapsed, 2)
                node_counts[node] = node_counts.get(node, 0) + 1
                if update:
                    final_state.update(update)
                if node == "validation":
                    last_val_fb = update.get("fix_feedback") or last_val_fb
                    _set(idx, "Deployment")
                elif node in _NEXT:
                    _set(idx, _NEXT[node])
    except Exception as e:
        err = str(e)
    finally:
        if run_dir.exists():
            _safe_rmtree(run_dir)

    total_s = round(sum(timings.values()), 1)
    if err:
        _set(idx, "", "fail", total_s)
        return _timeout_sentinel((idx, difficulty, prompt), error=err)

    arch, sec, eng, val, dep = _extract_results(
        final_state, timings, node_counts, val_feedback=last_val_fb or None)

    cmp: dict = {}
    if gt_types is not None:
        created = (dep or {}).get("resources_created", []) or \
            _declared_types((eng or {}).get("generated_code", ""))
        cmp = _resource_comparison(gt_types, created)

    _final_ok = bool((val or {}).get("ok")) if no_deploy else bool((dep or {}).get("ok"))
    _set(idx, "", "pass" if _final_ok else "fail", total_s)

    return {
        "row": idx, "difficulty": difficulty, "prompt": prompt,
        "architecture": arch, "security": sec, "engineering": eng,
        "validation": val, "deployment": dep,
        "run_mode": "no-deploy" if no_deploy else "full",
        "total_retry_count": (final_state.get("total_val_attempts", 0)
                              + final_state.get("total_deploy_attempts", 0)),
        "deploy_backstop_count": final_state.get("total_deploy_attempts", 0),
        "deploy_retry_count": (
            (final_state.get("retries") or {}).get("deploy_eng", {}).get("count", 0)
            + (final_state.get("retries") or {}).get("deploy_arch", {}).get("count", 0)),
        "routing_log": final_state.get("routing_log", []),
        "resource_match": cmp,
        "total_elapsed_s": total_s,
    }


def _load_csv(path: str) -> list[tuple]:
    with open(path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    out = []
    for i, row in enumerate(rows):
        gt = [t.strip() for t in (row.get("Resource") or "").split(",") if t.strip()]
        out.append((i, row.get("Difficulty", ""), row["Prompt"], gt))
    return out


_WRITE_LOCK = threading.Lock()


def _atomic_write(path: Path, results: list[dict]) -> None:
    """Ghi atomic (tmp → replace) → crash giữa chừng không hỏng/mất file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(sorted(results, key=lambda r: r["row"]), indent=2, ensure_ascii=False)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(path)



def _load_existing(path: Path) -> list[dict]:
    """Đọc output cũ cho --resume (chỉ giữ record hợp lệ có 'row' int)."""
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[resume] không đọc được {path}: {type(e).__name__}: {e}")
        return []
    if not isinstance(data, list):
        return []
    return [r for r in data if isinstance(r, dict) and isinstance(r.get("row"), int)]


def main():
    ap = argparse.ArgumentParser(description="Runner gọn — chỉ live progress, không log file")
    ap.add_argument("--dataset", required=True, help="CSV path")
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--out", default="reviews/eval_results.json")
    ap.add_argument("--shard", default=None,
                    help="i/n — chỉ chạy phần i của n (row idx %% n == i). Dùng chia "
                         "dataset cho nhiều process/account. VD: --shard 0/2 và --shard 1/2.")
    ap.add_argument("--resume", action="store_true",
                    help="Bỏ qua row đã có trong --out (tiếp tục run dở sau crash/rate-limit).")
    ap.add_argument("--no-deploy", action="store_true",
                    help="Dừng sau A4 Validation — không terraform apply. "
                         "Kết quả plan-level, so fair với B0/B1/B2 baseline.")
    args = ap.parse_args()

    _check_aws_identity()
    if not args.no_deploy:
        start_destroy_worker()
    selected_graph = _build_no_deploy_graph() if args.no_deploy else _graph
    out_path = Path(args.out)
    rows = _load_csv(args.dataset)
    if args.shard:
        i, ntot = (int(x) for x in args.shard.split("/"))
        rows = [r for r in rows if r[0] % ntot == i]  # r[0] = row idx toàn cục → tmp/out không đụng

    results: list[dict] = []
    if args.resume:
        results = _load_existing(out_path)
        done = {r["row"] for r in results}
        before = len(rows)
        rows = [r for r in rows if r[0] not in done]
        if done:
            print(f"[resume] đã có {len(done)} row trong {out_path} → bỏ qua, còn {len(rows)}/{before}")

    shard_s = f" | shard={args.shard}" if args.shard else ""
    deploy_s = " | no-deploy" if args.no_deploy else ""
    print(f"eval.py | dataset={Path(args.dataset).name} | rows={len(rows)} | "
          f"workers={args.workers}{shard_s}{deploy_s}{' | resume' if args.resume else ''}\n")

    # Progress thread — vẽ in-place mỗi 1s.
    stop = threading.Event()
    pstate = {"last": "", "first": True}

    def _draw():
        o = _render()
        if o and o != pstate["last"]:
            if pstate["first"]:
                print(o, flush=True); pstate["first"] = False
            else:
                print(f"\033[{pstate['last'].count(chr(10)) + 1}A\033[J" + o, flush=True)
            pstate["last"] = o

    def _loop():
        while not stop.is_set():
            if _PROGRESS:
                _draw()
            time.sleep(1.0)

    pt = threading.Thread(target=_loop, daemon=True)
    pt.start()

    def _one(ra):
        return _run_row(ra[0], ra[1], ra[2], ra[3],
                        graph=selected_graph, no_deploy=args.no_deploy)

    def _record(r):
        with _WRITE_LOCK:
            results.append(r)
            _atomic_write(out_path, results)  # ghi sau mỗi row → crash-safe + resume được

    try:
        if args.workers <= 1:
            for ra in rows:
                try:
                    _record(_one(ra))
                except Exception as e:
                    _record(_timeout_sentinel(ra, error=str(e), no_deploy=args.no_deploy))
        else:
            with ThreadPoolExecutor(max_workers=args.workers) as ex:
                futs = {ex.submit(_one, ra): ra for ra in rows}
                for fut, ra in futs.items():
                    try:
                        _record(fut.result())
                    except Exception as e:
                        _record(_timeout_sentinel(ra, error=str(e), no_deploy=args.no_deploy))
    except KeyboardInterrupt:
        print("\n[interrupted]")

    stop.set(); pt.join(timeout=1)
    _draw()  # refresh lần cuối: mọi row hiện trạng thái cuối
    print()

    _atomic_write(out_path, results)  # final (đảm bảo sorted)
    _print_summary(results, out_path, no_deploy=args.no_deploy)

    if not args.no_deploy:
        print("Waiting for pending destroys...")
        shutdown_destroy_worker()
        print("All destroys complete.")


def _print_summary(results: list[dict], out_path: Path, no_deploy: bool = False) -> None:
    n = len(results)
    ok = {a: sum(1 for r in results if (r.get(a) or {}).get("ok"))
          for a in ("architecture", "security", "engineering", "validation", "deployment")}

    def _pct(a, b): return f"{100 * a / b:.0f}%" if b else "—"

    def _row(name, num, den):
        print(f"  {name:<14}{f'{num}/{den}':<13}{_pct(num, den)}")

    print(f"\n{'=' * 50}")
    print("SUMMARY")
    print(f"  {'Agent':<14}{'Pass/Total':<13}Percentage")
    print(f"  {'-' * 36}")
    _row("Architecture", ok["architecture"], n)
    _row("Security",     ok["security"],     ok["architecture"])
    _row("Engineering",  ok["engineering"],  ok["architecture"])
    _row("Validation",   ok["validation"],   ok["engineering"])
    if not no_deploy:
        _row("Deployment",   ok["deployment"],   n)

    tt = [r["total_elapsed_s"] for r in results if r.get("total_elapsed_s")]
    f1v = [r["resource_match"]["f1"] for r in results if (r.get("resource_match") or {}).get("f1") is not None]
    avg_retry = sum(r.get("total_retry_count", 0) for r in results) / n if n else 0
    print(f"  {'-' * 36}")
    if tt:
        print(f"  avg_time        {sum(tt) / len(tt):.1f}s")
    if no_deploy:
        if f1v:
            print(f"  resource_f1     {sum(f1v) / len(f1v):.3f}")
        print(f"  avg_retry       {avg_retry:.2f}")
    print(f"{'=' * 50}")
    final_ok = ok["validation"] if no_deploy else ok["deployment"]
    label = "plan-valid" if no_deploy else "deploy-pass"
    print(f"{final_ok}/{n} {label} → saved {out_path}")



if __name__ == "__main__":
    main()
