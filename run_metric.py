"""Multi-run metric harness — chạy evaluate.py k lần rồi gộp bằng score.py.

Vì model reasoning (v4-pro) non-deterministic cross-run, 1 run KHÔNG đủ; harness này
chạy lặp rồi score.py tính pass@k + mean±std (xem reference_macog_multiagent: mean over
tasks + bootstrap CI, project_metrics). Model lấy từ .env (DEEPSEEK_MODEL) — KHÔNG đổi ở đây.

Ví dụ:
  python run_metric.py --csv dataset/data-dev.csv --cases 0 7 33 --runs 3
  python run_metric.py --csv dataset/data-dev.csv --runs 3 --no-deploy
"""
import argparse
import subprocess
import sys
from pathlib import Path

REVIEWS = Path("reviews")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", required=True, help="Dataset CSV")
    ap.add_argument("--cases", nargs="+", default=None, help="Row indices (mặc định: tất cả)")
    ap.add_argument("--runs", type=int, default=3, help="Số lần chạy mỗi case (mặc định 3)")
    ap.add_argument("--workers", type=int, default=3, help="Song song trong 1 run")
    ap.add_argument("--no-deploy", action="store_true", help="Bỏ A5 deploy")
    ap.add_argument("--prefix", default="metric", help="Tiền tố file output trong reviews/")
    ap.add_argument("--no-rego", action="store_true", help="Bỏ chấm semantic_correct (opa)")
    ap.add_argument("--no-checkov", action="store_true", help="Bỏ chấm security_score (checkov)")
    ap.add_argument("--resume", action="store_true", help="Bỏ qua run đã có file")
    args = ap.parse_args()

    REVIEWS.mkdir(exist_ok=True)
    run_files = []
    for i in range(1, args.runs + 1):
        out = REVIEWS / f"{args.prefix}_run{i}.json"
        run_files.append(str(out))
        if args.resume and out.exists():
            print(f"[run {i}/{args.runs}] skip — đã có {out}")
            continue
        cmd = ["uv", "run", "python3", "evaluate.py", "--csv", args.csv,
               "--out", str(out), "--workers", str(args.workers)]
        if args.cases:
            cmd += ["--cases", *args.cases]
        if args.no_deploy:
            cmd.append("--no-deploy")
        print(f"\n{'='*70}\n[run {i}/{args.runs}] {' '.join(cmd)}\n{'='*70}")
        r = subprocess.run(cmd)
        if r.returncode != 0:
            print(f"⚠️  run {i} exit {r.returncode} — vẫn tiếp tục, score sẽ dùng file có sẵn")

    existing = [f for f in run_files if Path(f).exists()]
    if not existing:
        print("❌ Không có run nào thành công — dừng.")
        sys.exit(1)

    report = REVIEWS / f"{args.prefix}_report.json"
    score = ["uv", "run", "python3", "score.py", *existing,
             "--csv", args.csv, "--out", str(report)]
    if not args.no_rego:
        score.append("--rego")
    if not args.no_checkov:
        score.append("--checkov")
    print(f"\n{'='*70}\n[score] {' '.join(score)}\n{'='*70}")
    subprocess.run(score)
    print(f"\n✅ Report: {report}  |  runs: {existing}")


if __name__ == "__main__":
    main()
