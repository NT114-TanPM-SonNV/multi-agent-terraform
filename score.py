"""Scorer độc lập — tính bộ metric đầy đủ từ output pipeline + dataset gold.

Tách rời khỏi việc CHẠY pipeline (evaluate.py) để: (a) chấm lại không tốn LLM,
(b) chấm baseline single-LLM bằng CÙNG thước đo → so sánh 1-1 (ablation),
(c) gộp nhiều run → mean ± std + pass@k chuẩn.

Đầu vào: ≥1 file results JSON (mỗi file = 1 run của evaluate.py / baseline.py),
mỗi file là list các row dict có: row, difficulty, engi.generated_code,
val.plan_ok, deploy.ok, total_retry_count.

Thước đo (xem core/metrics.METRIC_DEFS):
  • plan_valid       — tất định (val.plan_ok)
  • semantic_correct — tất định, Rego gold (recompute, cần --rego + opa + AWS creds)
  • security_score   — tất định, full Checkov scan (recompute, cần --checkov)
  • deploy_success   — phụ thuộc môi trường (deploy.ok)
  • resolved@≤k      — nội bộ: giải quyết trong ≤ k vòng iteration
  • pass@k           — chuẩn: ước lượng trên các run độc lập (≥2 file)

Chạy:
  python score.py reviews/run1.json --csv dataset/data-test.csv --rego --checkov
  python score.py reviews/run1.json reviews/run2.json --csv dataset/data-test.csv --rego
"""
import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()  # nạp AWS_ACCESS_KEY_ID/SECRET từ .env trước khi terraform subprocess khởi động

from core.metrics import pass_at_k, aggregate, rate, bootstrap_ci, METRIC_DEFS

_MAX_K = 5


def _load_dataset(csv_path: Path) -> dict[int, dict]:
    rows = list(csv.DictReader(open(csv_path, encoding="utf-8")))
    return {i: row for i, row in enumerate(rows)}


def _code_of(row: dict) -> str:
    engi = row.get("engi") or {}
    return engi.get("generated_code") or ""


def _success_signal(scored: dict, has_deploy: bool, has_rego: bool) -> bool:
    """Tín hiệu 'thành công' mạnh nhất hiện có cho resolved@≤k."""
    if has_rego and scored.get("semantic_correct") is not None:
        return bool(scored["semantic_correct"])
    if has_deploy and scored.get("deploy_success") is not None:
        return bool(scored["deploy_success"])
    return bool(scored.get("plan_valid"))


def _score_row(run_row: dict, gold: dict, do_rego: bool, do_checkov: bool,
               do_judge: bool = False) -> dict:
    """Chấm 1 row của 1 run. Recompute rego/checkov/judge nếu được bật."""
    val = run_row.get("val") or {}
    deploy = run_row.get("deploy")
    code = _code_of(run_row)

    scored = {
        "row": run_row.get("row"),
        "difficulty": (run_row.get("difficulty") or gold.get("Difficulty") or "?").strip() or "?",
        "attempts": int(run_row.get("total_retry_count", 0)) + 1,
        "plan_valid": bool(val.get("plan_ok")),
        "deploy_success": (bool(deploy.get("ok")) if isinstance(deploy, dict) else None),
        "semantic_correct": None,
        "llm_judge": None,
        "security_score": None,
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
            scored["security_score"] = rate(ck["passed_count"], total) if total else None
        except Exception as e:
            scored["_checkov_error"] = str(e)[:120]

    # Posture A2 phán + số check enforced (để phân tích, KHÔNG phải metric chấm điểm).
    secu = run_row.get("secu") or {}
    scored["security_selected"] = secu.get("ckv_total", 0)

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

    resolved = {}
    for k in range(1, _MAX_K + 1):
        ok = sum(1 for r in scored_rows
                 if _success_signal(r, has_deploy, has_rego) and r["attempts"] <= k)
        resolved[f"resolved@<={k}"] = rate(ok, n)

    return {
        "n": n,
        "plan_valid": rate(plan_valid, n),
        "semantic_correct": rate(sem_ok, len(sem_rows)) if sem_rows else None,
        "semantic_n": len(sem_rows),
        "llm_judge": rate(judge_ok, len(judge_rows)) if judge_rows else None,
        "llm_judge_n": len(judge_rows),
        "deploy_success": rate(deploy_ok, n) if has_deploy else None,
        "security_score_mean": round(sum(sec_vals) / len(sec_vals), 4) if sec_vals else None,
        "security_n": len(sec_vals),
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
        run = json.loads(Path(path).read_text(encoding="utf-8"))
        has_deploy = any(isinstance(r.get("deploy"), dict) for r in run)
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
        if s["security_score_mean"] is not None:
            print(f"  security_score   : {s['security_score_mean']:.3f}   "
                  f"[full Checkov pass-rate trên code deploy-được — HEADLINE, n={s['security_n']}]")
        if s["deploy_success"] is not None:
            print(f"  deploy_success   : {s['deploy_success']:.3f}   [env-dependent]")
        print("  resolved@<=k     : " +
              "  ".join(f"k={k}:{s[f'resolved@<={k}']:.3f}" for k in range(1, _MAX_K + 1)))
        print("  by difficulty:")
        for diff, d in s["by_difficulty"].items():
            sem = f" sem={d['semantic_correct']:.2f}" if d["semantic_correct"] is not None else ""
            print(f"    {diff:<8} n={d['n']:<3} plan_valid={d['plan_valid']:.2f}{sem}")
    bci = cross_run.get("success_bootstrap_ci95")
    if bci:
        print(f"\n▶ Success rate (task-level bootstrap): {bci['mean']:.3f} "
              f"[{bci['ci95_lo']:.3f}, {bci['ci95_hi']:.3f}] 95% CI (n_tasks={bci['n']})")
    if cross_run.get("pass_at_k"):
        print("▶ Cross-run (independent samples):")
        print("  " + "  ".join(f"{k}={v:.3f}" for k, v in cross_run["pass_at_k"].items()))
        r1 = cross_run.get("resolved@<=1_across_runs")
        if r1:
            print(f"  resolved@<=1: {r1['mean']:.3f} ± {r1['ci95']:.3f} (t-CI95, n_runs={r1['n']})")

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(report, indent=2, ensure_ascii=False))
        print(f"\nSaved report → {args.out}")


if __name__ == "__main__":
    main()
