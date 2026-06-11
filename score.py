"""Scorer độc lập — tính bộ metric đầy đủ từ output pipeline + dataset gold.

Tách rời khỏi việc CHẠY pipeline (evaluate.py) để: (a) chấm lại không tốn LLM,
(b) chấm baseline single-LLM bằng CÙNG thước đo → so sánh 1-1 (ablation),
(c) gộp nhiều run → mean ± std + pass@k chuẩn.

Đầu vào: ≥1 file results JSON (mỗi file = 1 run của evaluate.py / baseline.py),
mỗi file là list các row dict có: row, difficulty, engi.generated_code,
val.plan_ok, deploy.ok, total_retry_count.

Thước đo:
  • plan_valid        — terraform validate + plan pass
  • resource_f1       — type match với ground truth
  • llm_judge         — semantic adequacy mềm (tuỳ chọn)
  • security_row_mean — mean per-task Checkov pass rate (headline security)
  • deploy_success    — chỉ có nếu results có deploy
  • plan_resolved@≤k  — plan-level resolved trong ≤ k vòng iteration
  • deploy_resolved@≤k — deploy-level resolved trong ≤ k vòng iteration, nếu có deploy

Chạy:
  python score.py reviews/run1.json --csv dataset/data-test.csv --rego --checkov
  python score.py reviews/run1.json reviews/run2.json --csv dataset/data-test.csv --rego
"""
import argparse
import csv
import json
import sys
import io
from collections import defaultdict
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
load_dotenv()  # nạp AWS_ACCESS_KEY_ID/SECRET từ .env trước khi terraform subprocess khởi động

from core.metrics import pass_at_k, aggregate, rate, bootstrap_ci, METRIC_DEFS

_MAX_K = 5


def _load_dataset(csv_path: Path) -> dict[int, dict]:
    with open(csv_path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return {i: row for i, row in enumerate(rows)}


def _code_of(row: dict) -> str:
    engi = row.get("engineering") or row.get("engi") or {}
    return engi.get("generated_code") or ""


def _success_signal(scored: dict, has_deploy: bool, has_rego: bool) -> bool:
    """Tín hiệu success cho cross-run pass@k."""
    if has_rego and scored.get("semantic_correct") is not None:
        return bool(scored["semantic_correct"])
    if has_deploy and scored.get("deploy_success") is not None:
        return bool(scored["deploy_success"])
    return bool(scored.get("plan_valid"))


def _plan_success_signal(scored: dict) -> bool:
    """Resolved@<=k trong bảng chung là plan-level để fair với baseline plan-only."""
    return bool(scored.get("plan_valid"))


def _score_row(run_row: dict, gold: dict, do_rego: bool, do_checkov: bool,
               do_judge: bool = False) -> dict:
    """Chấm 1 row của 1 run. Recompute rego/checkov/judge nếu được bật."""
    val = run_row.get("validation") or run_row.get("val") or {}
    deploy = run_row.get("deployment", run_row.get("deploy"))
    code = _code_of(run_row)

    scored = {
        "row": run_row.get("row"),
        "difficulty": (run_row.get("difficulty") or gold.get("Difficulty") or "?").strip() or "?",
        # attempts = total (val+deploy) → dùng cho deploy_resolved@k.
        "attempts": int(run_row.get("total_retry_count", 0)) + 1,
        # plan_attempts = số vòng tới khi PLAN valid → dùng cho plan_resolved@k.
        # Framework: val_retry_count (pha A1→A4, KHÔNG gồm deploy retry).
        # Baseline: không có val_retry_count → fallback total_retry_count (= plan-repair).
        "plan_attempts": (int(_vrc) if (_vrc := run_row.get("val_retry_count")) is not None
                          else int(run_row.get("total_retry_count", 0))) + 1,
        "val_retry_count": int(run_row.get("val_retry_count", 0)),
        "plan_valid": bool(val.get("plan_ok")),
        "deploy_success": (bool(deploy.get("ok")) if isinstance(deploy, dict) else None),
        "semantic_correct": None,
        "llm_judge": None,
        "security_score": None,
        "security_passed": None,
        "security_failed": None,
        "security_total": None,
        "total_elapsed_s": run_row.get("total_elapsed_s"),
        "first_val_pass_s": run_row.get("first_val_pass_s"),
        "resource_f1": (run_row.get("resource_match") or {}).get("f1"),
    }

    if do_rego and code.strip():
        from core.rego_eval import semantic_correct
        res = semantic_correct(code, gold.get("Rego intent", ""))
        scored["semantic_correct"] = res["correct"]
        scored["_rego_stage"] = res["stage"]

    if do_judge and code.strip():
        from core.llm_judge import llm_judge_single
        prompt = run_row.get("prompt") or gold.get("Prompt", "")
        scored["llm_judge"] = llm_judge_single(prompt, code)

    # security_score = full Checkov pass-rate (HEADLINE; check_ids=None = grader độc lập, held-out
    # với A2 vốn Checkov-free). CHỈ credit khi code DEPLOY ĐƯỢC (≥ plan_valid): TF an toàn mà không
    # apply được = phantom security, không cho điểm trên code vỡ → không plan_valid thì None (loại
    # khỏi mean; plan_valid báo riêng). BỎ security_enforced/phantom_rate cũ — chúng circular (chấm
    # A2 trên chính lựa chọn A2). A2 nay không chọn check, grader là full Checkov ngay đây.
    if do_checkov and code.strip() and scored["plan_valid"]:
        from core.terraform import run_checkov_on_hcl
        try:
            ck = run_checkov_on_hcl(code, timeout=90, check_ids=None)
            total = ck["passed_count"] + ck["failed_count"]
            scored["security_passed"] = ck["passed_count"]
            scored["security_failed"] = ck["failed_count"]
            scored["security_total"] = total
            scored["security_score"] = rate(ck["passed_count"], total) if total else None
        except Exception as e:
            scored["_checkov_error"] = str(e)[:120]

    # Posture A2 phán + số check enforced (để phân tích, KHÔNG phải metric chấm điểm).
    secu = run_row.get("security") or run_row.get("secu") or {}
    prof = secu.get("security_profile") or {}
    scored["security_selected"] = sum(len(v.get("checks", [])) for v in prof.values())

    # degraded: A2 LLM hỏng → 0 check enforce KHÔNG do intent. Tách khỏi run bình thường
    # để security_score_mean không bị nhiễu bởi lỗi hạ tầng-LLM thay vì lỗi mô hình.
    scored["security_degraded"] = bool(val.get("security_degraded"))

    # Khoảng cách intended-vs-verified security — bị best-effort PASS che giấu:
    #   unmet   = A2 target + Checkov fail nhưng hết budget → vẫn overall_passed=True
    #   phantom = A2 target nhưng Checkov không evaluate (thiếu companion) → không pass/fail
    scored["security_unmet"]   = len(val.get("unmet_checks") or [])
    scored["security_phantom"] = len(val.get("phantom_checks") or [])

    return scored


def _summarize_run(scored_rows: list[dict], has_deploy: bool, has_rego: bool) -> dict:
    n = len(scored_rows)
    plan_valid = sum(r["plan_valid"] for r in scored_rows)
    deploy_ok = sum(1 for r in scored_rows if r.get("deploy_success"))
    sem_rows = [r for r in scored_rows if r.get("semantic_correct") is not None]
    sem_ok = sum(1 for r in sem_rows if r["semantic_correct"])
    judge_rows = [r for r in scored_rows if r.get("llm_judge") is not None]
    judge_ok = sum(1 for r in judge_rows if r["llm_judge"] == 1)
    sec_vals = [r["security_score"] for r in scored_rows if r.get("security_score") is not None]
    sec_rows = [r for r in scored_rows if r.get("security_score") is not None]
    sec_passed = sum(r.get("security_passed") or 0 for r in sec_rows)
    sec_total = sum(r.get("security_total") or 0 for r in sec_rows)
    elapsed_vals = [r["total_elapsed_s"] for r in scored_rows if r.get("total_elapsed_s") is not None]
    first_val_vals = [r["first_val_pass_s"] for r in scored_rows if r.get("first_val_pass_s") is not None]
    f1_vals = [r["resource_f1"] for r in scored_rows if r.get("resource_f1") is not None]

    resolved = {}
    for k in range(1, _MAX_K + 1):
        # plan_resolved dùng plan_attempts (pha plan) → cross-comparable framework vs baseline.
        ok = sum(1 for r in scored_rows
                 if _plan_success_signal(r) and r["plan_attempts"] <= k)
        resolved[f"resolved@<={k}"] = rate(ok, n)
        if has_deploy:
            # deploy_resolved dùng attempts tổng (val+deploy).
            dep_ok = sum(1 for r in scored_rows
                         if r.get("deploy_success") and r["attempts"] <= k)
            resolved[f"deploy_resolved@<={k}"] = rate(dep_ok, n)

    return {
        "n": n,
        "plan_valid": rate(plan_valid, n),
        "semantic_correct": rate(sem_ok, len(sem_rows)) if sem_rows else None,
        "semantic_n": len(sem_rows),
        "llm_judge": rate(judge_ok, len(judge_rows)) if judge_rows else None,
        "llm_judge_n": len(judge_rows),
        "deploy_success": rate(deploy_ok, n) if has_deploy else None,
        "security_row_mean": round(sum(sec_vals) / len(sec_vals), 4) if sec_vals else None,
        "security_micro": rate(sec_passed, sec_total) if sec_total else None,
        "security_n": len(sec_vals),
        "security_total_checks": sec_total,
        # Tỷ lệ run mà A2 hỏng (LLM fail) → 0 security enforcement KHÔNG do intent.
        # Cao = kết quả security đang bị nhiễu bởi lỗi hạ tầng, cần điều tra trước khi tin số.
        "security_degraded_rate": rate(sum(1 for r in scored_rows if r.get("security_degraded")), n),
        # Khoảng cách intended-vs-verified: % run "PASS" nhưng thực chưa verify đủ security.
        #   unmet_rate   = có check fail bị best-effort bỏ qua (hết budget)
        #   phantom_rate = có check target mà Checkov không evaluate (thiếu companion)
        #   verified_clean_rate = PASS thật sạch: plan_valid VÀ unmet=0 VÀ phantom=0 VÀ không degraded
        "security_unmet_rate":   rate(sum(1 for r in scored_rows if r.get("security_unmet")),   n),
        "security_phantom_rate": rate(sum(1 for r in scored_rows if r.get("security_phantom")), n),
        "verified_clean_rate":   rate(sum(1 for r in scored_rows
                                          if r.get("plan_valid") and not r.get("security_unmet")
                                          and not r.get("security_phantom")
                                          and not r.get("security_degraded")), n),
        # avg_time_valid = tới khi A4 pass lần đầu (chất lượng sinh code A1→A4);
        # avg_time_total = tổng cả run (gồm deploy + retry).
        "avg_time_valid": round(sum(first_val_vals) / len(first_val_vals), 1) if first_val_vals else None,
        "avg_time_total": round(sum(elapsed_vals) / len(elapsed_vals), 1) if elapsed_vals else None,
        # avg_retry_valid = retry trong pha tạo code valid (A1→A4); avg_retry_total = cả run.
        "avg_retry_valid": round(sum(r.get("val_retry_count") or 0 for r in scored_rows) / n, 2) if n else None,
        "avg_retry_total": round(sum((r.get("attempts") or 1) - 1 for r in scored_rows) / n, 2) if n else None,
        "resource_f1_mean":    round(sum(f1_vals) / len(f1_vals), 4) if f1_vals else None,
        **resolved,
    }


def _by_difficulty(scored_rows: list[dict], has_deploy: bool, has_rego: bool) -> dict:
    buckets: dict[str, list] = defaultdict(list)
    for r in scored_rows:
        buckets[r["difficulty"]].append(r)
    return {diff: _summarize_run(rows, has_deploy, has_rego)
            for diff, rows in sorted(buckets.items())}


def main():
    ap = argparse.ArgumentParser(description="Score pipeline/baseline output với metric đầy đủ")
    ap.add_argument("results", nargs="+", help="≥1 file results JSON (mỗi file = 1 run)")
    ap.add_argument("--csv", required=True, help="Dataset CSV (gold Rego intent, Difficulty)")
    ap.add_argument("--rego", action="store_true", help="Tính semantic_correct (cần opa + AWS creds)")
    ap.add_argument("--checkov", action="store_true", help="Tính security_score (full Checkov scan)")
    ap.add_argument("--llm-judge", action="store_true",
                    help="Tính LLM-judge adequacy (dùng deepseek-chat, tốn API)")
    ap.add_argument("--out", default=None, help="Ghi report JSON")
    args = ap.parse_args()

    gold = _load_dataset(Path(args.csv))

    if args.rego:
        from core.rego_eval import opa_available
        if not opa_available():
            print("⚠️  --rego bật nhưng không tìm thấy 'opa' trong PATH — bỏ qua semantic_correct.")
            args.rego = False

    per_run_summaries = []
    per_run_scored = []
    has_deploy_any = False

    for path in args.results:
        run = json.loads(Path(path).read_text(encoding="utf-8", errors="replace"))
        has_deploy = any(isinstance(r.get("deployment", r.get("deploy")), dict) for r in run)
        has_deploy_any = has_deploy_any or has_deploy
        scored = []
        for run_row in run:
            idx = run_row.get("row")
            g = gold.get(idx, {})
            scored.append(_score_row(run_row, g, args.rego, args.checkov,
                                     do_judge=args.llm_judge))
        per_run_scored.append(scored)
        summary = _summarize_run(scored, has_deploy, args.rego)
        summary["by_difficulty"] = _by_difficulty(scored, has_deploy, args.rego)
        summary["file"] = path
        per_run_summaries.append(summary)

    # ── pass@k chuẩn + mean±std khi có nhiều run ──────────────────────────────
    cross_run = {}
    n_runs = len(per_run_scored)
    if n_runs >= 1:
        # success signal per (row, run)
        by_row: dict[int, list[bool]] = defaultdict(list)
        for scored in per_run_scored:
            for r in scored:
                by_row[r["row"]].append(_success_signal(r, has_deploy_any, args.rego))
        prompts = list(by_row)
        # Bootstrap CI95 ở mức TASK (MACOG-style): resample tasks, mỗi task = tỉ lệ
        # run thành công. Robust hơn t-CI khi phân phối lệch. Có ý nghĩa từ 1 run.
        task_rates = [sum(by_row[p]) / len(by_row[p]) for p in prompts]
        cross_run["success_bootstrap_ci95"] = bootstrap_ci(task_rates)
        # pass@k (chỉ ý nghĩa khi n_runs >= 2)
        if n_runs >= 2:
            passk = {}
            for k in range(1, n_runs + 1):
                vals = [pass_at_k(len(by_row[p]), sum(by_row[p]), k) for p in prompts]
                passk[f"pass@{k}"] = round(sum(vals) / len(vals), 4) if vals else 0.0
            cross_run["pass_at_k"] = passk
            # mean ± std của tỉ lệ resolved@<=1 từng run (proxy độ ổn định)
            cross_run["resolved@<=1_across_runs"] = aggregate(
                [s["resolved@<=1"] for s in per_run_summaries])
            if all(s["semantic_correct"] is not None for s in per_run_summaries):
                cross_run["semantic_correct_across_runs"] = aggregate(
                    [s["semantic_correct"] for s in per_run_summaries])

    report = {
        "n_runs": n_runs,
        "metric_defs": METRIC_DEFS,
        "per_run": per_run_summaries,
        "cross_run": cross_run,
    }

    # ── In gọn ─────────────────────────────────────────────────────────────────
    print("=" * 70)
    print(f"SCORING REPORT — {n_runs} run(s)  |  dataset={Path(args.csv).name}")
    print("=" * 70)
    for s in per_run_summaries:
        print(f"\n▶ {s['file']}  (n={s['n']})")
        print(f"  plan_valid       : {s['plan_valid']:.3f}   [deterministic]")
        if s["semantic_correct"] is not None:
            print(f"  semantic_correct : {s['semantic_correct']:.3f}   "
                  f"[Rego gold, n={s['semantic_n']}]")
        if s.get("llm_judge") is not None:
            print(f"  llm_judge        : {s['llm_judge']:.3f}   "
                  f"[adequacy judge (deepseek-chat), n={s['llm_judge_n']}]")
        if s["security_row_mean"] is not None:
            print(f"  security_row_mean: {s['security_row_mean']:.3f}   "
                  f"[headline: mean per-task Checkov pass-rate, n={s['security_n']}]")
            if s.get("security_micro") is not None:
                print(f"  security_micro   : {s['security_micro']:.3f}   "
                      f"[secondary: total pass/total checks, checks={s['security_total_checks']}]")
        if s["deploy_success"] is not None:
            print(f"  deploy_success   : {s['deploy_success']:.3f}   [env-dependent]")
        if s.get("avg_time_valid") is not None:
            print(f"  avg_time_valid   : {s['avg_time_valid']:.1f}s  [mean tới A4 pass lần đầu]")
        if s.get("avg_time_total") is not None:
            print(f"  avg_time_total   : {s['avg_time_total']:.1f}s  [mean tổng cả run]")
        if s.get("avg_retry_valid") is not None:
            print(f"  avg_retry_valid  : {s['avg_retry_valid']:.2f}   [mean retry pha A1→A4]")
        if s.get("avg_retry_total") is not None:
            print(f"  avg_retry_total  : {s['avg_retry_total']:.2f}   [mean retry cả run]")
        if s.get("resource_f1_mean") is not None:
            print(f"  resource_f1      : {s['resource_f1_mean']:.3f}   [type match vs ground truth]")
        print("  plan_resolved@<=k: " +
              "  ".join(f"k={k}:{s[f'resolved@<={k}']:.3f}" for k in range(1, _MAX_K + 1)))
        if s["deploy_success"] is not None:
            print("  deploy_resolved@<=k: " +
                  "  ".join(f"k={k}:{s[f'deploy_resolved@<={k}']:.3f}" for k in range(1, _MAX_K + 1)))
        print("  by difficulty:")
        for diff, d in s["by_difficulty"].items():
            sem = f" sem={d['semantic_correct']:.2f}" if d["semantic_correct"] is not None else ""
            sec = f" sec={d['security_row_mean']:.2f}" if d["security_row_mean"] is not None else ""
            dep = f" deploy={d['deploy_success']:.2f}" if d["deploy_success"] is not None else ""
            print(f"    {diff:<8} n={d['n']:<3} plan_valid={d['plan_valid']:.2f}{sem}{sec}{dep}")
    bci = cross_run.get("success_bootstrap_ci95")
    if bci:
        print(f"\n▶ Success signal (task-level bootstrap): {bci['mean']:.3f} "
              f"[{bci['ci95_lo']:.3f}, {bci['ci95_hi']:.3f}] 95% CI (n_tasks={bci['n']})")
    if cross_run.get("pass_at_k"):
        print("▶ Cross-run (independent samples):")
        print("  " + "  ".join(f"{k}={v:.3f}" for k, v in cross_run["pass_at_k"].items()))
        r1 = cross_run.get("resolved@<=1_across_runs")
        if r1:
            print(f"  resolved@<=1: {r1['mean']:.3f} ± {r1['ci95']:.3f} (t-CI95, n_runs={r1['n']})")

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nSaved report → {args.out}")


if __name__ == "__main__":
    main()
