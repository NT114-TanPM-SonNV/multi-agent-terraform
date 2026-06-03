"""run.py — chạy pipeline và in kết quả từng bước ngắn gọn.

    python run.py "Create an S3 bucket with versioning"
    python run.py "Create an RDS instance" --no-deploy
    python run.py "Create a Lambda function" --no-secu --no-deploy
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

import logging
logging.basicConfig(level=logging.WARNING, format="%(message)s", force=True)

from graph import build_initial_state, RECURSION_LIMIT
from evaluate import _select_graph


def run(prompt: str, no_secu: bool = False, no_deploy: bool = False,
        auto_destroy: bool = False) -> dict:
    g = _select_graph(no_secu, no_deploy)
    state = build_initial_state(prompt, auto_destroy=auto_destroy)

    print(f"\nprompt: {prompt}\n")

    for chunk in g.stream(state, config={"recursion_limit": RECURSION_LIMIT},
                          stream_mode="updates"):
        for node, update in chunk.items():
            if not update:
                continue

            if node == "architecture":
                plan = update.get("infrastructure_plan") or {}
                res = plan.get("resources", [])
                print(f"[A1] architecture → {len(res)} resources: "
                      f"{', '.join(r.get('type','') + '.' + r.get('name','') for r in res)}")

            elif node == "security":
                prof = update.get("security_profile") or {}
                for lbl, info in prof.items():
                    checks = info.get("checks", [])
                    if checks:
                        print(f"[A2] security   → {lbl}: {checks}")

            elif node == "engineering":
                code = update.get("generated_code", "")
                import re
                n = len(re.findall(r'resource\s+"[^"]+"\s+"[^"]+"', code))
                fb = update.get("fix_feedback") or {}
                if code.strip():
                    print(f"[A3] engineering → {n} resource blocks ({len(code)} chars)")
                else:
                    print(f"[A3] engineering → FAIL: {fb.get('error_type')} — {(fb.get('fix_instruction') or '')[:80]}")

            elif node == "validation":
                fb = update.get("fix_feedback") or {}
                ck = fb.get("checkov") or {}
                passed = fb.get("overall_passed")
                val = "✓" if fb.get("validate_passed") else "✗"
                plan_ = "✓" if fb.get("plan_passed") else "✗"
                ck_p, ck_f = ck.get("passed_count", 0), ck.get("failed_count", 0)
                status = "PASS" if passed else f"FAIL({fb.get('error_type')})"
                print(f"[A4] validation  → {status}  validate={val} plan={plan_} "
                      f"checkov={ck_p}p/{ck_f}f")
                if not passed and fb.get("fix_instruction"):
                    print(f"         fix: {fb['fix_instruction'][:100]}")

            elif node == "deployment":
                dr = update.get("deployment_result") or {}
                if dr.get("success"):
                    created = dr.get("resources_created", [])
                    destroyed = "→ destroyed" if dr.get("auto_destroyed") else ""
                    print(f"[A5] deployment → OK  {created} {destroyed}")
                else:
                    print(f"[A5] deployment → FAIL({dr.get('error_type')}): "
                          f"{(dr.get('apply_raw_error') or '')[:80]}")

            elif node == "requires_human":
                fb = update.get("fix_feedback") or state.get("fix_feedback") or {}
                print(f"[!!] requires_human: {(fb.get('fix_instruction') or '')[:120]}")

    return state


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run pipeline — compact output")
    parser.add_argument("prompt", nargs="?",
                        default="Create an S3 bucket with versioning and SSE enabled.")
    parser.add_argument("--no-secu",    action="store_true")
    parser.add_argument("--no-deploy",  action="store_true")
    parser.add_argument("--no-destroy", action="store_true")
    args = parser.parse_args()

    final = run(args.prompt, no_secu=args.no_secu, no_deploy=args.no_deploy,
                auto_destroy=not args.no_destroy)

    fb = final.get("fix_feedback") or {}
    dr = final.get("deployment_result") or {}
    print(f"\n{'─'*50}")
    print(f"validate={fb.get('validate_passed')}  plan={fb.get('plan_passed')}  "
          f"passed={fb.get('overall_passed')}  deploy={dr.get('success')}")
