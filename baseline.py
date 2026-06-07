"""Baseline B0/B1 — ablation so sánh với multi-agent pipeline.

B0 (--retry 0): 1 LLM call, không retry — single-shot baseline.
B1 (--retry 3): tối đa 3 repair iteration với tf error feedback — Multi-turn baseline.
  → Budget retry bằng val_eng của pipeline (3) để so sánh fair.

Schema output tương thích score.py → chấm bằng cùng thước đo.

Chạy:
  python baseline.py --csv dataset/data-test.csv --out reviews/b0.json
  python baseline.py --csv dataset/data-test.csv --retry 3 --out reviews/b1.json
  python baseline.py --csv dataset/data-test.csv --retry 3 --limit 5
"""
import argparse
import csv
import json
import logging
import os
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")

from core.llm import call_llm
from core.parsers import strip_code_block, RESOURCE_DECL_RE as _RESOURCE_DECL_RE
from core.terraform import run_terraform, write_terraform_dir, terraform_workdir
from agents.engineering import _strip_preamble

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

_SYSTEM = """\
You are TerraformAI, an expert that writes Terraform configurations for AWS.
Given a request, output a single complete, valid, deployable Terraform HCL configuration
using the AWS provider ~> 5.0. Include the terraform{} and provider "aws" blocks.

Apply AWS security best practices where applicable:
- Encrypt data at rest (KMS or provider-managed encryption)
- Encrypt data in transit (TLS, enforce HTTPS)
- IAM least-privilege (no wildcards in actions or principals)
- Block public access on storage and databases unless the request requires it
- Use IMDSv2 on EC2 instances
- Enable monitoring/logging on resources that support it

Output ONLY raw HCL — no markdown fences, no explanation."""

_INIT_TIMEOUT = 300
_VALIDATE_TIMEOUT = 60
_DECLARED_RE = re.compile(r'(?:resource|data)\s+"([^"]+)"\s+"[^"]+"')


def _generate_hcl(prompt: str) -> str:
    raw = call_llm([{"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": prompt}], agent="engineering")
    body = _strip_preamble(strip_code_block(raw).strip())
    return f"{body}\n" if 'resource "' in body else ""


def _repair_hcl(prompt: str, code: str, error: str) -> str:
    """Feed lỗi tf vào LLM để sửa — kiểu Multi-turn của MACog."""
    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": prompt},
        {"role": "assistant", "content": code},
        {"role": "user", "content": (
            f"The Terraform configuration has an error:\n{error}\n\n"
            "Fix it and output the corrected HCL only."
        )},
    ]
    raw = call_llm(messages, agent="engineering")
    body = _strip_preamble(strip_code_block(raw).strip())
    return f"{body}\n" if 'resource "' in body else code


def _run_tf(code: str, plan_timeout: int) -> tuple[bool, bool, str]:
    """Trả (validate_ok, plan_ok, error_message)."""
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


def _declared_types(code: str) -> list[str]:
    return _DECLARED_RE.findall(code)


def _resource_comparison(gt_types: list[str], gen_types: list[str]) -> dict:
    gt_set  = set(gt_types)
    gen_set = set(gen_types)
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


def run_row(prompt: str, gt_types: list[str], plan_timeout: int, max_retry: int) -> dict:
    """Chạy 1 row với retry loop. max_retry=0 → B0, max_retry>0 → B1."""
    t0 = time.time()

    try:
        code = _generate_hcl(prompt)
    except Exception as e:
        code = ""

    retries = 0
    validate_ok, plan_ok, err = _run_tf(code, plan_timeout)

    while not plan_ok and retries < max_retry:
        if not code.strip():
            break
        try:
            code = _repair_hcl(prompt, code, err)
        except Exception:
            break
        retries += 1
        validate_ok, plan_ok, err = _run_tf(code, plan_timeout)

    elapsed = round(time.time() - t0, 1)
    gen_types = _declared_types(code)

    return {
        "validate_ok":    validate_ok,
        "plan_ok":        plan_ok,
        "err":            err,
        "code":           code,
        "n_res":          len(_RESOURCE_DECL_RE.findall(code)),
        "retries":        retries,
        "elapsed":        elapsed,
        "resource_match": _resource_comparison(gt_types, gen_types) if gt_types else {},
    }


def main():
    ap = argparse.ArgumentParser(description="Baseline B0 / B1 cho ablation")
    ap.add_argument("--csv",          required=True)
    ap.add_argument("--limit",        type=int, default=None)
    ap.add_argument("--cases",        nargs="+", default=None,
                    help="Row indices, e.g. --cases 0 1 2 5 7-10")
    ap.add_argument("--out",          default="reviews/baseline_b0.json")
    ap.add_argument("--plan-timeout", type=int,
                    default=int(os.getenv("TF_PLAN_TIMEOUT", "120")),
                    help="Mặc định = TF_PLAN_TIMEOUT trong .env (khớp pipeline graph.py:131)")
    ap.add_argument("--retry",        type=int, default=0,
                    help="Số lần repair tối đa: 0=B0 (no retry), 3=B1 (with retry)")
    ap.add_argument("--workers",      type=int, default=1,
                    help="Số worker song song (mặc định 1)")
    args = ap.parse_args()

    rows = list(csv.DictReader(open(args.csv, encoding="utf-8")))
    if args.limit:
        rows = rows[:args.limit]
    if args.cases:
        selected = set()
        for part in args.cases:
            if "-" in part:
                lo, hi = part.split("-", 1)
                selected.update(range(int(lo), int(hi) + 1))
            else:
                selected.add(int(part))
        rows = [(i, r) for i, r in enumerate(rows) if i in selected]
        print(f"--cases filter: {len(rows)} rows")
    else:
        rows = list(enumerate(rows))

    label = "B0" if args.retry == 0 else f"B1(retry≤{args.retry})"
    print(f"{label}  |  csv={Path(args.csv).name}  |  n={len(rows)}  |  workers={args.workers}")

    print_lock = threading.Lock()

    def _run_one(item: tuple) -> dict:
        orig_idx, row = item
        prompt   = row["Prompt"]
        diff     = (row.get("Difficulty") or "?").strip() or "?"
        gt_raw   = row.get("Resource") or ""
        gt_types = [t.strip() for t in gt_raw.split(",") if t.strip()]

        r = run_row(prompt, gt_types, args.plan_timeout, args.retry)

        with print_lock:
            print(f"row {orig_idx:3d} [{diff:<6}] plan_valid={r['plan_ok']} "
                  f"({r['n_res']} res, retry={r['retries']}, {r['elapsed']}s)"
                  + (f"  err={r['err'][:60]}" if not r['plan_ok'] else ""))

        return {
            "row":        orig_idx,
            "difficulty": diff,
            "prompt":     prompt,
            "archi": {"ok": r["n_res"] > 0, "resource_count": r["n_res"]},
            "secu":  {"ok": True, "skipped": True, "security_profile": {}},
            "engi":  {
                "ok":             bool(r["code"].strip()),
                "generated_code": r["code"],
                "resource_count": r["n_res"],
            },
            "val": {
                "ok":          r["plan_ok"],
                "validate_ok": r["validate_ok"],
                "plan_ok":     r["plan_ok"],
            },
            "deploy":             None,
            "total_retry_count":  r["retries"],
            "total_elapsed_s":    r["elapsed"],
            "resource_match":     r["resource_match"],
        }

    if args.workers <= 1:
        results = [_run_one(item) for item in rows]
    else:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            results = list(ex.map(_run_one, rows))
        results.sort(key=lambda r: r["row"])

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    n_ok = sum(1 for r in results if r["val"]["plan_ok"])
    print(f"\n{label} plan_valid: {n_ok}/{len(results)}  →  saved {out}")


if __name__ == "__main__":
    main()
