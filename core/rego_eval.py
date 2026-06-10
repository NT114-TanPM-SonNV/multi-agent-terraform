"""Semantic correctness evaluation using OPA/Rego policies."""
import json
import shutil
import subprocess
from pathlib import Path

from core.terraform import run_terraform, write_terraform_dir, terraform_workdir

_INIT_TIMEOUT = 300
_PLAN_TIMEOUT = 180
_OPA_TIMEOUT = 60


def _iter_leaves(value):
    """Yield every leaf in a nested dict/list structure."""
    if isinstance(value, dict):
        for v in value.values():
            yield from _iter_leaves(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _iter_leaves(v)
    else:
        yield value


def opa_available() -> bool:
    return shutil.which("opa") is not None


def _opa_eval(plan_json_path: Path, policy_path: Path) -> tuple[bool, str]:
    """Run ``opa eval`` on ``plan.json`` and ``policy.rego``."""
    rego_text = policy_path.read_text(encoding="utf-8", errors="ignore")
    cmd = ["opa", "eval"]
    # OPA 1.x defaults to Rego v1; older policies need --v0-compatible.
    if "import rego.v1" not in rego_text:
        cmd.append("--v0-compatible")
    cmd += ["-i", str(plan_json_path), "-d", str(policy_path), "data"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=_OPA_TIMEOUT)
    except subprocess.TimeoutExpired:
        return False, f"opa eval timed out (>{_OPA_TIMEOUT}s)"
    except FileNotFoundError:
        return False, "opa binary not found"

    try:
        out = json.loads(proc.stdout)
        value = out["result"][0]["expressions"][0]["value"]
    except (json.JSONDecodeError, KeyError, IndexError):
        return False, f"opa output unparseable: {(proc.stdout or proc.stderr)[:300]}"

    leaves = list(_iter_leaves(value))
    # No leaves means no matching rule, so treat it as failure.
    if not leaves:
        return False, "opa produced no decision leaves"
    passed = False not in leaves
    return passed, ("" if passed else f"rule violation; decision={value}")


def semantic_correct(code: str, rego_policy: str,
                     plan_timeout: int = _PLAN_TIMEOUT) -> dict:
    """Score one HCL config against a Rego policy."""
    if not (code or "").strip():
        return {"plan_ok": False, "rego_pass": False, "correct": False,
                "stage": "plan", "error": "empty code"}
    if not (rego_policy or "").strip():
        return {"plan_ok": False, "rego_pass": False, "correct": False,
                "stage": "opa", "error": "no rego policy in dataset row"}

    with terraform_workdir(None, "rego") as d:
        d = Path(d)
        write_terraform_dir(d, code)

        try:
            init = run_terraform(["terraform", "init", "-no-color"], d, _INIT_TIMEOUT)
        except subprocess.TimeoutExpired:
            return {"plan_ok": False, "rego_pass": False, "correct": False,
                    "stage": "plan", "error": "terraform init timed out"}
        if init.returncode != 0:
            return {"plan_ok": False, "rego_pass": False, "correct": False,
                    "stage": "plan", "error": f"init failed: {(init.stderr or '')[:300]}"}

        plan_file = d / "plan.out"
        try:
            plan = run_terraform(
                ["terraform", "plan", "-out", str(plan_file), "-no-color"], d, plan_timeout)
        except subprocess.TimeoutExpired:
            return {"plan_ok": False, "rego_pass": False, "correct": False,
                    "stage": "plan", "error": f"plan timed out (>{plan_timeout}s)"}
        if plan.returncode != 0:
            return {"plan_ok": False, "rego_pass": False, "correct": False,
                    "stage": "plan", "error": (plan.stderr or plan.stdout or "")[:500]}

        # terraform show -json → plan.json
        plan_json = d / "plan.json"
        try:
            show = run_terraform(["terraform", "show", "-json", str(plan_file)], d, 60)
        except subprocess.TimeoutExpired:
            return {"plan_ok": True, "rego_pass": False, "correct": False,
                    "stage": "opa", "error": "terraform show timed out"}
        if show.returncode != 0:
            return {"plan_ok": True, "rego_pass": False, "correct": False,
                    "stage": "opa", "error": f"show failed: {(show.stderr or '')[:300]}"}
        plan_json.write_text(show.stdout, encoding="utf-8")

        policy_path = d / "policy.rego"
        policy_path.write_text(rego_policy, encoding="utf-8")

        rego_pass, err = _opa_eval(plan_json, policy_path)
        return {"plan_ok": True, "rego_pass": rego_pass, "correct": rego_pass,
                "stage": "ok" if rego_pass else "opa", "error": err}
