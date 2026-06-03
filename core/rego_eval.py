"""Semantic correctness eval — chấm HCL bằng OPA/Rego policy của IaC-Eval.

Đây là thước đo CORRECTNESS *ngữ nghĩa* (không chỉ "có resource đúng type"):
nó kiểm tra cấu hình resource có thật sự thỏa intent của prompt hay không, bằng
chính gold Rego policy đi kèm dataset (cột "Rego intent").

Quy trình (giống hệt IaC-Eval để số liệu so sánh được 1-1):
    terraform init → terraform plan -out plan.out
    terraform show -json plan.out → plan.json
    opa eval -i plan.json -d policy.rego data
    → duyệt mọi leaf trong value; nếu CÓ bất kỳ False → Failure, ngược lại Success.

Tham chiếu: ref/sources/iac-eval/evaluation/eval.py::OPA_Rego_evaluation
"""
import json
import shutil
import subprocess
from pathlib import Path

from core.terraform import run_terraform, write_terraform_dir, terraform_workdir

_INIT_TIMEOUT = 300
_PLAN_TIMEOUT = 180
_OPA_TIMEOUT = 60


def _iter_leaves(value):
    """Duyệt mọi leaf của dict/list lồng nhau (giống dict_generator của IaC-Eval)."""
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
    """Chạy `opa eval` trên plan.json + policy.rego. Trả (passed, error)."""
    rego_text = policy_path.read_text(encoding="utf-8", errors="ignore")
    cmd = ["opa", "eval"]
    # OPA >= 1.0 mặc định Rego v1 (bắt buộc `if`/`contains`). Gold policy của
    # IaC-Eval viết bằng Rego v0 (không `if`, không `import rego.v1`) nên cần
    # opt-in v0. Chỉ policy khai báo `import rego.v1` mới chạy ở chế độ v1.
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
    # Không có leaf nào (policy không định nghĩa rule nào match) → coi là Failure
    # để tránh false-positive, giống tinh thần "default allow = false".
    if not leaves:
        return False, "opa produced no decision leaves"
    passed = False not in leaves
    return passed, ("" if passed else f"rule violation; decision={value}")


def semantic_correct(code: str, rego_policy: str,
                     plan_timeout: int = _PLAN_TIMEOUT) -> dict:
    """Chấm 1 HCL config bằng gold Rego policy.

    Returns:
        {
          "plan_ok":    bool,   # terraform plan thành công?
          "rego_pass":  bool,   # OPA policy thỏa? (chỉ ý nghĩa khi plan_ok)
          "correct":    bool,   # plan_ok AND rego_pass — định nghĩa correctness cuối
          "stage":      "plan" | "opa" | "ok",
          "error":      str,
        }
    """
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
