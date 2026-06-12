"""Baseline B0/B1/B2 — ablation so sánh với multi-agent pipeline.

B0 (--retry 0): Direct pass@1, prompt gốc, không security prompt.
B1 (--retry N): Direct + Terraform plan repair, không security prompt.
B2 (--retry N --security-prompt): Direct + security prompt + Terraform plan repair.

Retry N nên match MAX_VAL_ENG_RETRY của framework để so plan-level fair.

Schema output tương thích score.py → chấm bằng cùng thước đo.

Chạy:
  python baseline.py --csv dataset/data-test.csv --retry 0 --out results/b0_direct.json
  python baseline.py --csv dataset/data-test.csv --retry 2 --out results/b1_direct_retry.json
  python baseline.py --csv dataset/data-test.csv --retry 2 --security-prompt --out results/b2_security_retry.json
"""
import argparse
import csv
import json
import logging
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dotenv import load_dotenv

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")

from core.llm import call_llm
from core.parsers import strip_code_block, RESOURCE_DECL_RE as _RESOURCE_DECL_RE
from core.terraform import run_terraform, write_terraform_dir, terraform_workdir
from agents.engineering import _strip_preamble

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

_BASE_SYSTEM = """\
You are TerraformAI, an expert that writes Terraform configurations for AWS.
Given a request, output a single complete, valid, plannable Terraform HCL configuration
using the AWS provider ~> 5.0. Include the terraform{} and provider "aws" blocks.

Output ONLY raw HCL — no markdown fences, no explanation."""

_SECURITY_GUIDANCE = """\
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


def _system_prompt(security_prompt: bool) -> str:
    if security_prompt:
        return f"{_BASE_SYSTEM}\n\n{_SECURITY_GUIDANCE}"
    return _BASE_SYSTEM


def _generate_hcl(prompt: str, security_prompt: bool) -> str:
    raw = call_llm([{"role": "system", "content": _system_prompt(security_prompt)},
                    {"role": "user", "content": prompt}], agent="engineering")
    body = _strip_preamble(strip_code_block(raw).strip())
    return f"{body}\n" if 'resource "' in body else ""


def _repair_hcl(prompt: str, code: str, error: str, security_prompt: bool) -> str:
    """Feed lỗi tf vào cùng single agent để sửa."""
    messages = [
        {"role": "system", "content": _system_prompt(security_prompt)},
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


def run_row(prompt: str, gt_types: list[str], plan_timeout: int,
            max_retry: int, security_prompt: bool) -> dict:
    """Chạy 1 row với retry loop. max_retry=0 -> pass@1."""
    t0 = time.time()

    try:
        code = _generate_hcl(prompt, security_prompt)
    except Exception as e:
        code = ""

    retries = 0
    validate_ok, plan_ok, err = _run_tf(code, plan_timeout)

    # Snapshot lần-sinh-đầu (= pass@1 / B0) TRƯỚC khi repair ghi đè code.
    a1 = (code, validate_ok, plan_ok, err, round(time.time() - t0, 1))

    while not plan_ok and retries < max_retry:
        if not code.strip():
            break
        try:
            code = _repair_hcl(prompt, code, err, security_prompt)
        except Exception:
            break
        retries += 1
        validate_ok, plan_ok, err = _run_tf(code, plan_timeout)

    elapsed = round(time.time() - t0, 1)

    def _view(c, vok, pok, e, el):
        return {
            "validate_ok": vok, "plan_ok": pok, "err": e, "code": c,
            "n_res": len(_RESOURCE_DECL_RE.findall(c)),
            "elapsed": el,
            "resource_match": _resource_comparison(gt_types, _declared_types(c)) if gt_types else {},
        }

    return {
        "final":    _view(code, validate_ok, plan_ok, err, elapsed),
        "attempt1": _view(*a1),
        "retries":  retries,
    }


def main():
    ap = argparse.ArgumentParser(description="Baseline B0/B1/B2 cho ablation")
    ap.add_argument("--csv",          required=True)
    ap.add_argument("--limit",        type=int, default=None)
    ap.add_argument("--cases",        nargs="+", default=None,
                    help="Row indices, e.g. --cases 0 1 2 5 7-10")
    ap.add_argument("--out",          default="reviews/baseline_b0.json")
    ap.add_argument("--plan-timeout", type=int, default=120)
    ap.add_argument("--retry",        type=int, default=0,
                    help="Số lần repair tối đa: 0=B0; B1/B2 nên match MAX_VAL_ENG_RETRY")
    ap.add_argument("--security-prompt", action="store_true",
                    help="Bật security best-practice guidance cho strong baseline B2")
    ap.add_argument("--workers",      type=int, default=1,
                    help="Số worker song song (mặc định 1)")
    ap.add_argument("--b0-out",       default=None,
                    help="Ghi THÊM file pass@1 (B0) lấy từ snapshot lần-sinh-đầu của cùng run "
                         "→ chạy --retry 2 1 lần là có cả B0 lẫn B1, không tốn LLM thêm.")
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

    if args.retry == 0 and not args.security_prompt:
        label = "B0-direct-pass@1"
    elif args.security_prompt:
        label = f"B2-security+plan-retry≤{args.retry}"
    else:
        label = f"B1-direct+plan-retry≤{args.retry}"
    print(f"{label}  |  csv={Path(args.csv).name}  |  n={len(rows)}  |  workers={args.workers}")

    # B0 derived: nếu run KHÔNG security → đúng B0; nếu có security → "security pass@1".
    b0_label = ("B0-direct-pass@1 (from attempt1)" if not args.security_prompt
                else "B2-security-pass@1 (from attempt1)")
    print_lock = threading.Lock()

    def _record(orig_idx, diff, prompt, view, mode, sec_prompt, retry_budget, retries):
        return {
            "row": orig_idx, "difficulty": diff, "prompt": prompt,
            "baseline": {"mode": mode, "security_prompt": sec_prompt,
                         "retry_budget": retry_budget},
            "engineering": {"ok": bool(view["code"].strip()), "generated_code": view["code"]},
            "validation":  {"ok": view["plan_ok"], "plan_ok": view["plan_ok"],
                            "validate_ok": view["validate_ok"]},
            "deployment":  None,
            "total_retry_count": retries,
            "total_elapsed_s":   view["elapsed"],
            "resource_match":    view["resource_match"],
        }

    def _run_one(item: tuple) -> tuple:
        orig_idx, row = item
        prompt   = row["Prompt"]
        diff     = (row.get("Difficulty") or "?").strip() or "?"
        gt_raw   = row.get("Resource") or ""
        gt_types = [t.strip() for t in gt_raw.split(",") if t.strip()]

        try:
            r = run_row(prompt, gt_types, args.plan_timeout, args.retry, args.security_prompt)
        except Exception as e:  # 1 row lỗi KHÔNG được kéo chết cả batch (ex.map raise sớm)
            ev = {"validate_ok": False, "plan_ok": False, "err": str(e)[:200],
                  "code": "", "n_res": 0, "elapsed": 0.0, "resource_match": {}}
            r = {"final": ev, "attempt1": ev, "retries": 0}
        fin, a1 = r["final"], r["attempt1"]

        with print_lock:
            print(f"row {orig_idx:3d} [{diff:<6}] plan_valid={fin['plan_ok']} "
                  f"({fin['n_res']} res, retry={r['retries']}, {fin['elapsed']}s)"
                  + (f"  err={fin['err'][:60]}" if not fin['plan_ok'] else ""))

        final_rec = _record(orig_idx, diff, prompt, fin, label,
                            args.security_prompt, args.retry, r["retries"])
        b0_rec = (_record(orig_idx, diff, prompt, a1, b0_label, args.security_prompt, 0, 0)
                  if args.b0_out else None)
        return final_rec, b0_rec

    if args.workers <= 1:
        pairs = [_run_one(item) for item in rows]
    else:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            pairs = list(ex.map(_run_one, rows))
    pairs.sort(key=lambda p: p[0]["row"])
    results = [p[0] for p in pairs]

    def _save(path_str, recs, lbl):
        p = Path(path_str)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(recs, indent=2, ensure_ascii=False), encoding="utf-8")
        n_ok = sum(1 for r in recs if r["validation"]["plan_ok"])
        print(f"{lbl} plan_valid: {n_ok}/{len(recs)}  →  saved {p}")

    print()
    _save(args.out, results, label)
    if args.b0_out:
        _save(args.b0_out, [p[1] for p in pairs], b0_label)


if __name__ == "__main__":
    main()
