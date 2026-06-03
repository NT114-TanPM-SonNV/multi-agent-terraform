"""Single-LLM baseline — ablation để chứng minh giá trị của multi-agent.

Mỗi prompt → MỘT lần gọi LLM sinh thẳng HCL (không phân rã agent, không retry,
không security agent). Chạy validate + plan để lấy plan_valid. Ghi results JSON
theo CÙNG schema mà score.py đọc → chấm bằng CÙNG thước đo (kể cả Rego, Checkov)
→ so sánh 1-1 với pipeline multi-agent.

Chạy:
  python baseline.py --csv dataset/data-test.csv --out reviews/baseline.json
  python baseline.py --csv dataset/data-test.csv --limit 5
"""
import argparse
import csv
import logging
import subprocess
import time
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")

from core.llm import call_llm
from core.parsers import strip_code_block
from core.terraform import run_terraform, write_terraform_dir, terraform_workdir
from agents.engineering import _strip_preamble, _RESOURCE_DECL_RE

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

# System prompt tối giản, trung lập — vai trò "TerraformAI" giống IaC-Eval baseline,
# KHÔNG có hướng dẫn security/decomposition (đó là phần multi-agent đóng góp).
_SYSTEM = """\
You are TerraformAI, an expert that writes Terraform configurations for AWS.
Given a request, output a single complete, valid, deployable Terraform HCL configuration
using the AWS provider ~> 5.0. Include the terraform{} and provider "aws" blocks.
Output ONLY raw HCL — no markdown fences, no explanation."""

_INIT_TIMEOUT = 300
_VALIDATE_TIMEOUT = 60


def generate_hcl(prompt: str) -> str:
    raw = call_llm([{"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": prompt}], agent="engineering")
    body = _strip_preamble(strip_code_block(raw).strip())
    return f"{body}\n" if 'resource "' in body else ""


def plan_valid(code: str, plan_timeout: int) -> tuple[bool, bool, str]:
    """Trả (validate_ok, plan_ok, error)."""
    if not code.strip():
        return False, False, "empty code"
    with terraform_workdir(None, "baseline") as d:
        write_terraform_dir(d, code)
        try:
            init = run_terraform(["terraform", "init", "-no-color"], d, _INIT_TIMEOUT)
        except subprocess.TimeoutExpired:
            return False, False, "init timed out"
        if init.returncode != 0:
            return False, False, f"init: {(init.stderr or '')[:200]}"
        try:
            val = run_terraform(["terraform", "validate", "-no-color"], d, _VALIDATE_TIMEOUT)
        except subprocess.TimeoutExpired:
            return False, False, "validate timed out"
        if val.returncode != 0:
            return False, False, f"validate: {(val.stderr or val.stdout or '')[:200]}"
        try:
            plan = run_terraform(["terraform", "plan", "-no-color"], d, plan_timeout)
        except subprocess.TimeoutExpired:
            return True, False, "plan timed out"
        ok = plan.returncode == 0
        return True, ok, ("" if ok else (plan.stderr or plan.stdout or "")[:300])


def main():
    ap = argparse.ArgumentParser(description="Single-LLM baseline cho ablation")
    ap.add_argument("--csv", required=True)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--out", default="reviews/baseline.json")
    ap.add_argument("--plan-timeout", type=int, default=120)
    args = ap.parse_args()

    rows = list(csv.DictReader(open(args.csv, encoding="utf-8")))
    if args.limit:
        rows = rows[:args.limit]

    import json
    results = []
    for i, row in enumerate(rows):
        prompt = row["Prompt"]
        diff = (row.get("Difficulty") or "?").strip() or "?"
        t0 = time.time()
        try:
            code = generate_hcl(prompt)
        except Exception as e:
            code = ""
            print(f"row {i}: LLM error {e}")
        validate_ok, plan_ok, err = plan_valid(code, args.plan_timeout)
        elapsed = round(time.time() - t0, 1)
        n_res = len(_RESOURCE_DECL_RE.findall(code))
        print(f"row {i:3d} [{diff:<6}] plan_valid={plan_ok} ({n_res} res, {elapsed}s)"
              + (f"  err={err[:60]}" if not plan_ok else ""))
        # Schema tương thích score.py
        results.append({
            "row": i,
            "difficulty": diff,
            "prompt": prompt,
            "engi": {"ok": bool(code.strip()), "generated_code": code,
                     "resource_count": n_res},
            "val": {"ok": plan_ok, "validate_ok": validate_ok, "plan_ok": plan_ok},
            "deploy": None,           # baseline không deploy
            "total_retry_count": 0,   # single-shot, không retry
        })

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2, ensure_ascii=False))
    n_ok = sum(1 for r in results if r["val"]["plan_ok"])
    print(f"\nbaseline plan_valid: {n_ok}/{len(results)}  →  saved {out}")


if __name__ == "__main__":
    main()
