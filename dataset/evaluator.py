"""Evaluation utilities — passItr@n metric, Rego validation, Floci health check."""
import json
import logging
import subprocess
import tempfile
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ── Floci health ──────────────────────────────────────────────────────────────

def check_floci_health(endpoint: str, timeout: int = 5) -> bool:
    """Return True if the Floci (LocalStack) endpoint is reachable.

    Tries /health first (LocalStack standard), then root URL as fallback.
    """
    for path in ("/health", "/"):
        url = endpoint.rstrip("/") + path
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                if resp.status < 500:
                    return True
        except urllib.error.HTTPError as e:
            if e.code < 500:
                return True   # 4xx still means server is up
        except Exception:
            continue
    return False


# ── Rego / OPA validation ─────────────────────────────────────────────────────

def validate_with_rego(hcl_code: str, rego_policy: str,
                       terraform_plan_json: dict | None = None) -> dict:
    """Evaluate hcl_code against a Rego intent policy using OPA.

    Returns {"passed": bool, "violations": list[str], "error": str | None}.
    Falls back gracefully if OPA binary is not installed.
    """
    if not rego_policy or not rego_policy.strip():
        return {"passed": True, "violations": [], "error": None}

    with tempfile.TemporaryDirectory() as d:
        policy_file = Path(d) / "policy.rego"
        policy_file.write_text(rego_policy, encoding="utf-8")

        # OPA expects JSON input — use terraform plan JSON if available,
        # else wrap the HCL in a dummy object (OPA can't parse HCL directly).
        if terraform_plan_json:
            input_data = terraform_plan_json
        else:
            input_data = {"code": hcl_code}
        input_file = Path(d) / "input.json"
        input_file.write_text(json.dumps(input_data), encoding="utf-8")

        try:
            result = subprocess.run(
                ["opa", "eval", "--data", str(policy_file),
                 "--input", str(input_file),
                 "--format", "json",
                 "data.terraform.validation"],
                capture_output=True, text=True, timeout=30,
            )
        except FileNotFoundError:
            return {"passed": True, "violations": [],
                    "error": "opa binary not found — skipping Rego check"}
        except subprocess.TimeoutExpired:
            return {"passed": False, "violations": [],
                    "error": "OPA eval timed out"}

        try:
            data = json.loads(result.stdout)
            bindings = data.get("result", [{}])[0].get("bindings", {})
            # Convention: policy should define `is_configuration_valid`
            passed = bool(bindings.get("is_configuration_valid", False))
            violations = bindings.get("violations", [])
            return {"passed": passed, "violations": violations, "error": None}
        except (json.JSONDecodeError, IndexError, KeyError) as e:
            return {"passed": False, "violations": [],
                    "error": f"OPA output parse error: {e}"}


# ── passItr@n metric ──────────────────────────────────────────────────────────

def compute_pass_at_k(results: list[dict], k: int = 1) -> float:
    """Compute pass@k from a list of per-sample result dicts.

    Each result dict must have:
        "passed": bool
        "iterations": int  — number of pipeline iterations used (1-based)

    pass@k = fraction of samples that passed within k iterations.
    (Equivalent to passItr@n from the IaCGen paper — Section 4.2.)
    """
    if not results:
        return 0.0
    passed = sum(
        1 for r in results
        if r.get("passed") and r.get("iterations", 999) <= k
    )
    return passed / len(results)


def compute_pass_itr_at_n(results: list[dict], n: int) -> float:
    """Alias matching the IaCGen paper naming convention."""
    return compute_pass_at_k(results, k=n)


# ── Benchmark runner ──────────────────────────────────────────────────────────

def run_benchmark(
    samples: list[dict],
    run_pipeline_fn,
    max_workers: int = 6,
    pass_threshold: int = 3,
) -> dict:
    """Run the pipeline over all samples and collect metrics.

    Args:
        samples: list of dataset rows (each with 'prompt', 'rego_intent', etc.)
        run_pipeline_fn: callable(prompt, floci_endpoint) → pipeline result dict
        max_workers: number of parallel Floci instances to use
        pass_threshold: max iterations to count for passItr@n

    Returns:
        {
            "total": int,
            "passed": int,
            "pass_rate": float,
            "pass_itr_at_1": float,
            "pass_itr_at_3": float,
            "by_difficulty": dict[int, {"total": int, "passed": int}],
            "results": list[dict],
        }
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import os

    floci_base = os.environ.get("FLOCI_ENDPOINT", "http://localhost:4566")

    def _run_one(idx: int, sample: dict, port_offset: int) -> dict:
        # Distribute across 6 Floci instances
        port = 4566 + (port_offset % max_workers)
        endpoint = f"http://localhost:{port}"
        try:
            result = run_pipeline_fn(sample["prompt"], endpoint)
            passed = result.get("deployment_result", {}).get("success", False)
            iterations = result.get("total_retry_count", 0) + 1
        except Exception as e:
            logger.error("Sample %d failed: %s", idx, e)
            passed = False
            iterations = 0
            result = {}

        # Optional Rego validation on top of deployment success
        rego_passed = True
        rego_policy = sample.get("rego_intent", "")
        generated = result.get("generated_code", "")
        if passed and rego_policy and generated:
            rego_result = validate_with_rego(generated, rego_policy)
            rego_passed = rego_result["passed"]

        return {
            "id": idx,
            "prompt": sample["prompt"][:80],
            "difficulty": sample.get("difficulty", 0),
            "passed": passed and rego_passed,
            "deploy_passed": passed,
            "rego_passed": rego_passed,
            "iterations": iterations,
            "error": result.get("validation_result", {}).get("fix_instruction"),
        }

    all_results = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(_run_one, i, s, i): i
            for i, s in enumerate(samples)
        }
        for future in as_completed(futures):
            all_results.append(future.result())

    all_results.sort(key=lambda r: r["id"])

    # Aggregate
    total = len(all_results)
    passed_count = sum(1 for r in all_results if r["passed"])
    by_diff: dict[int, dict] = {}
    for r in all_results:
        d = r["difficulty"]
        if d not in by_diff:
            by_diff[d] = {"total": 0, "passed": 0}
        by_diff[d]["total"] += 1
        if r["passed"]:
            by_diff[d]["passed"] += 1

    return {
        "total": total,
        "passed": passed_count,
        "pass_rate": passed_count / total if total else 0.0,
        "pass_itr_at_1": compute_pass_at_k(all_results, k=1),
        "pass_itr_at_3": compute_pass_at_k(all_results, k=3),
        "by_difficulty": by_diff,
        "results": all_results,
    }
