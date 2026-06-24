"""trace.py - annotated walkthrough for first-time readers of the framework.
Each step prints:
  - what the current agent does in the pipeline
  - which state fields it reads
  - which state fields it writes
  - why the graph routes to the next node

Usage:
    python trace.py "Create a Lambda function with SQS trigger"
    python trace.py --no-deploy "Create a VPC with public and private subnets"
    python trace.py --csv dataset/data-dev.csv --case 17
"""
import argparse
import json
import re
import sys
from pathlib import Path

# Force UTF-8 stdout/stderr on Windows to avoid UnicodeEncodeError.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))


from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

import logging
logging.basicConfig(level=logging.WARNING, force=True)
for _n in ("httpx", "httpcore", "openai", "botocore", "boto3", "urllib3",
           "agents.architecture", "agents.security", "agents.engineering",
           "agents.validation", "agents.deployment", "checkov", "litellm",
           "langgraph", "langchain"):
    logging.getLogger(_n).setLevel(logging.ERROR)

from graph import build_initial_state, RECURSION_LIMIT, graph as _full_graph
from core.terraform import _safe_rmtree
from core.eval_utils import (
    BOLD,
    DIM,
    R,
    _c,
    blue,
    bold,
    cyan,
    dim,
    green,
    load_trace_prompt,
    magenta,
    red,
    white,
    yellow,
)
from core.retry_control import (
    MAX_VAL_ARCH_RETRY, MAX_DEPLOY_ARCH_RETRY,
    MAX_VAL_ENG_RETRY,  MAX_DEPLOY_ENG_RETRY,
    MAX_VAL_SEC_RETRY,  MAX_DEPLOY_TOTAL_RETRY,
    MAX_VAL_TOTAL_RETRY,
)
_MAX_ARCH_RETRY = MAX_VAL_ARCH_RETRY + MAX_DEPLOY_ARCH_RETRY  # 4 across validation/deploy phases
_MAX_ENG_RETRY  = MAX_VAL_ENG_RETRY  + MAX_DEPLOY_ENG_RETRY   # 5 across validation/deploy phases
_MAX_SEC_RETRY  = MAX_VAL_SEC_RETRY                            # 2
_MAX_DEPLOY_TOTAL_RETRY = MAX_DEPLOY_TOTAL_RETRY               # 4 deploy-phase backstop
_MAX_VAL_TOTAL_RETRY = MAX_VAL_TOTAL_RETRY                     # 5 val-phase backstop
from eval import _build_no_deploy_graph


def _mono(s: str) -> str:
    return str(s)


BOLD = DIM = R = ""
bold = dim = blue = cyan = green = magenta = red = white = yellow = _mono


def _select_graph(no_deploy: bool):
    return _build_no_deploy_graph() if no_deploy else _full_graph


def _clean_trace_tf_dir(run_dir: Path) -> None:
    """Remove stale Terraform workdir before a trace case starts.

    Trace reuses tmp/trace/tf across runs so old terraform.tfstate can make the
    next row refresh unrelated resources. Clean only the local workdir before a
    case; A5 still keeps state during the run if destroy fails.
    """
    _safe_rmtree(run_dir / "tf")


# -- Display style -------------------------------------------------------------

_AGENT_COLORS = {
    "architecture":  ("", "A1"),
    "security":      ("", "A2"),
    "engineering":   ("", "A3"),
    "validation":    ("", "A4"),
    "deployment":    ("", "A5"),
    "requires_human":("", "!!"),
}

_AGENT_NAMES = {
    "architecture":  "Architecture Agent",
    "security":      "Security Agent",
    "engineering":   "Engineering Agent",
    "validation":    "Validation Agent",
    "deployment":    "Deployment Agent",
    "requires_human":"Requires Human",
}

_AGENT_ROLE = {
    "architecture": """Reads the user prompt and asks the LLM to produce an infrastructure plan.
  Output is a JSON plan listing every AWS resource to create: type, name, attributes, and blocks.
  Later agents implement this plan; they should not invent extra resources.""",
    "security": """Reads infrastructure_plan from A1 and the user prompt, then selects Checkov check IDs
  to enforce for each resource based on the request intent.
  The LLM chooses from the local catalog menu, limited to valid IDs for each resource type.
  Output: CKV IDs per resource for A3 to implement and A4 to verify.""",
    "engineering": """Reads infrastructure_plan from A1 and security_profile from A2, then generates Terraform HCL.
  The LLM writes complete HCL: terraform{}, provider{}, and resource{} blocks for the plan.
  On retry, fix_instruction tells A3 which part to change while keeping the rest stable.""",
    "validation": """Checks A3's HCL through four ordered gates; later gates run only if earlier gates pass:
  1. terraform init     - loads the AWS provider plugin
  2. terraform validate - checks HCL syntax
  3. terraform plan     - checks whether the provider accepts the configuration
  4. Checkov gate       - verifies the CKV IDs selected by A2
     (scan is based on plan JSON from terraform show -json, which is more precise than source scan)
  On failure, A4 classifies the error, writes fix_instruction, and routes to the right agent.""",
    "deployment": """Runs terraform init and terraform apply to create real AWS resources.
  If apply fails, A5 checks for partial state, destroys dirty state when needed,
  classifies the error, and routes it:
    TRANSIENT        -> retry A5 for temporary network or throttling issues
    LOGIC            -> A3 fixes code, then A5 applies again
    MISSING_RESOURCE -> A1 re-plans missing dependencies
    OTHER            -> requires_human for unknown or exhausted cases
  After a successful apply, A5 runs terraform destroy to avoid AWS cost.""",
    "requires_human": """The pipeline cannot resolve this case automatically. Retry budget is exhausted,
  or the error is not safely fixable by the graph. Details remain in fix_feedback or deployment_result.""",
}

_W = 72


# -- Layout helpers ------------------------------------------------------------

def _divider(char="-", color=DIM):
    print(f"{color}{char * _W}{R}")

def _agent_header(node: str, step: int, repeat: int) -> None:
    _, tag = _AGENT_COLORS.get(node, ("", "??"))
    name = _AGENT_NAMES.get(node, node)
    rep = f"  retry #{repeat}" if repeat > 1 else ""
    print()
    print(dim("-" * _W))
    print(f"  {bold(f'[{tag}] STEP {step} - {name}{rep}')}")
    print(dim("-" * _W))

def _block(title: str, color_fn=cyan) -> None:
    print()
    print(f"  {bold(title)}")
    print(f"  {dim('-' * (_W - 2))}")

def _item(label: str, value, *, color=white, indent=4) -> None:
    pad = " " * indent
    s = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
    if len(s) > 100 or "\n" in s:
        print(f"{pad}{dim(label + ':')}")
        for line in s.splitlines():
            print(f"{pad}  {color(line)}")
    else:
        print(f"{pad}{dim(label + ':')} {color(s)}")

def _note(text: str, indent: int = 4) -> None:
    pad = " " * indent
    print(f"{pad}{dim(text)}")

def _check_mark(ok: bool | None, label: str, detail: str = "") -> None:
    marker = "[OK]" if ok is True else ("[FAIL]" if ok is False else "[--]")
    d = f"  {dim(detail)}" if detail else ""
    print(f"    {marker:<6} {label}{d}")

def _hcl(code: str) -> None:
    print()
    print(f"  {dim('+' + '-' * (_W - 4) + '+')}")
    for line in code.splitlines():
        print(f"  {dim('|')} {line}")
    print(f"  {dim('+' + '-' * (_W - 4) + '+')}")

def _arrow_next(src: str, dst: str, explanation: str) -> None:
    _, src_tag = _AGENT_COLORS.get(src, ("", "??"))
    _, dst_tag = _AGENT_COLORS.get(dst, ("", "??"))
    dst_name = _AGENT_NAMES.get(dst, dst)
    if dst == "END":
        dst_tag, dst_name = "OK", "END"
    print()
    print(f"    [{src_tag}] -> {bold(f'[{dst_tag}] {dst_name}')}")
    print(f"    {dim('reason: ' + explanation)}")

# -- Retry counter helper -----------------------------------------------------

def _rc(state: dict, key: str) -> int:
    """Read retry counters from state.
    key: 'total' | 'deploy_total' | 'eng' | 'arch' | 'sec' | 'deploy'
    'total' is the validation-phase backstop; 'deploy_total' is the deploy-phase backstop.
    eng/arch aggregate both val_ and deploy_ phases.
    """
    if key == "total":
        return state.get("total_val_attempts", 0)
    if key == "deploy_total":
        return state.get("total_deploy_attempts", 0)
    r = state.get("retries") or {}
    if key == "eng":
        return r.get("val_eng", {}).get("count", 0) + r.get("deploy_eng", {}).get("count", 0)
    if key == "arch":
        return r.get("val_arch", {}).get("count", 0) + r.get("deploy_arch", {}).get("count", 0)
    if key == "deploy":
        return r.get("deploy_eng", {}).get("count", 0) + r.get("deploy_arch", {}).get("count", 0)
    return r.get(key, {}).get("count", 0)


# -- Per-agent commentary ------------------------------------------------------

def _explain_input(node: str, state: dict) -> None:
    _block("READ FROM STATE", cyan)

    if node == "architecture":
        _item("state['prompt']", state.get("prompt", "")[:200])
        fb = state.get("fix_feedback") or {}
        if fb.get("root_cause") == "architecture" and fb.get("fix_instruction"):
            print()
            _note("-> retry run: A1 receives fix_instruction so it knows how to re-plan")
            _item("state['fix_feedback']['fix_instruction']", fb["fix_instruction"][:300], color=yellow)
            _item("retries[val_arch+deploy_arch]['count']", _rc(state, "arch"), color=yellow)
        else:
            _note("-> first run; no fix_instruction is present yet")

    elif node == "security":
        plan = state.get("infrastructure_plan") or {}
        resources = plan.get("resources", [])
        _note("-> A2 reads both the prompt and infrastructure_plan to understand intent and resource types")
        _item("state['prompt']", state.get("prompt", "")[:120], color=dim)
        print()
        _item("state['infrastructure_plan']['resources']",
              f"{len(resources)} resources: " + ", ".join(f"{r.get('type')}.{r.get('name')}" for r in resources))

    elif node == "engineering":
        plan = state.get("infrastructure_plan") or {}
        resources = plan.get("resources", [])
        prof = state.get("security_profile") or {}

        _note("-> A3 receives two inputs: A1 plan (what to create) and A2 security_profile (how to secure it)")
        print()
        _item("state['infrastructure_plan']['resources']",
              f"{len(resources)} resources")
        for r in resources:
            attrs = list(r.get("attributes", {}).keys())
            blocks = list(r.get("blocks", {}).keys())
            all_k = attrs + [f"[block]{k}" for k in blocks]
            short = ", ".join(all_k[:5]) + ("..." if len(all_k) > 5 else "")
            print(f"      {cyan('-')} {bold(r.get('type', ''))}.{r.get('name', '')}  "
                  f"{dim(short)}")

        if prof:
            print()
            _note("-> security_profile: A3 enforces the CKV check IDs selected by A2 for each resource")
            for label, info in prof.items():
                checks = info.get("checks", [])
                print(f"      {cyan('-')} {label}  checks={cyan(str(checks)) if checks else dim('[]')}")
        else:
            _note("-> security_profile is empty (A2 was skipped or degraded); no checks are enforced")

        fb = state.get("fix_feedback") or {}
        if fb.get("root_cause") == "engineering" and fb.get("fix_instruction"):
            print()
            _note("-> retry run: A3 receives fix_instruction and changes only the requested part")
            _item("state['fix_feedback']['fix_instruction']", fb["fix_instruction"][:300], color=yellow)
            _item("retries[val_eng+deploy_eng]['count']", _rc(state, "eng"), color=yellow)

    elif node == "validation":
        code = state.get("generated_code", "")
        res_count = len(re.findall(r'resource\s+"[^"]+"\s+"[^"]+"', code))
        _note("-> A4 only reads generated_code from A3; it does not need prompt or plan")
        _item("state['generated_code']",
              f"{len(code)} chars, {res_count} resource blocks")
        total_r = _rc(state, "total")
        eng_r   = _rc(state, "eng")
        sec_r   = _rc(state, "sec")
        print()
        _note("-> A4 also checks retry counters to decide whether retry budget remains")
        col = yellow if total_r > 0 else dim
        _item("retry budget used",
              f"total={total_r}/5  eng={eng_r}/3  sec={sec_r}/2",
              color=col if total_r > 0 else dim)

    elif node == "deployment":
        code = state.get("generated_code", "")
        _note("-> A5 receives generated_code that passed A4 and runs terraform apply")
        _item("state['generated_code']", f"{len(code)} chars")
        _note("-> after a successful apply, A5 destroys resources to avoid AWS cost")
        deploy_r = _rc(state, "deploy")
        if deploy_r > 0:
            _item("retries[deploy_eng+deploy_arch]['count']", deploy_r, color=yellow)
        fb = state.get("fix_feedback") or {}
        if fb.get("root_cause") == "engineering" and fb.get("fix_instruction"):
            print()
            _note("-> route from A5 FIXABLE: A3 changed the code, then A5 applies again")
            _item("state['fix_feedback']['fix_instruction']", fb["fix_instruction"][:200], color=yellow)


def _explain_output(node: str, update: dict, state_before: dict) -> None:
    _block("WRITE TO STATE", green)

    if node == "architecture":
        plan = update.get("infrastructure_plan") or {}
        resources = plan.get("resources", [])
        if resources:
            _note(f"-> A1 writes infrastructure_plan: {len(resources)} resources, "
                  f"{len(plan.get('data_sources', []))} data_sources")
            _note("-> each resource has type (AWS resource type), name, attributes, and blocks")
            print()
            for r in resources:
                attrs  = list(r.get("attributes", {}).keys())
                blocks = list(r.get("blocks", {}).keys())
                all_k  = attrs + [f"[block]{k}" for k in blocks]
                keys_s = ", ".join(all_k[:6]) + ("..." if len(all_k) > 6 else "")
                print(f"    {blue(bold(r.get('type', '')))}.{r.get('name', '')}")
                if keys_s:
                    print(f"      {dim('attributes/blocks: ' + keys_s)}")
            if _rc(update, "eng") == 0 and _rc(state_before, "eng") > 0:
                print()
                _note("-> A1 also resets eng/sec retry counters to 0")
                _note("   reason: the plan is new, so A3 gets a fresh budget instead of being limited by old-plan failures")
        else:
            fb = update.get("fix_feedback") or {}
            _note("-> A1 FAILED - LLM call failed or the response could not be parsed")
            _item("fix_feedback['error_type']", fb.get("error_type", "?"), color=red)
            _item("fix_feedback['error_stage']", fb.get("error_stage", "?"), color=yellow)
            _item("fix_feedback['fix_instruction']", (fb.get("fix_instruction") or "")[:250], color=red)
            if fb.get("raw_error"):
                _item("raw_error (truncated)", fb["raw_error"][:250], color=dim)
        _note("-> A1 clears fix_feedback={} on success to signal that this is not a retry failure")

    elif node == "security":
        prof = update.get("security_profile") or {}
        _note(f"-> A2 writes security_profile: CKV check IDs for {len(prof)} resources")
        _note("-> the LLM selects checks from intent plus the catalog menu, limited to valid IDs for each resource type")
        sec_status = update.get("security_status", "ok")
        if sec_status != "ok":
            _note(f"-> security_status={sec_status}: A2 degraded, downstream nodes will show best-effort mode clearly")
        if prof:
            print()
            for label, info in prof.items():
                checks = info.get("checks", [])
                print(f"    {magenta('-')} {label}")
                print(f"      checks = {cyan(str(checks)) if checks else dim('[]  (no enforcement)')}")

    elif node == "engineering":
        code = update.get("generated_code", "")
        if code.strip():
            res_count = len(re.findall(r'resource\s+"[^"]+"\s+"[^"]+"', code))
            _note(f"-> A3 writes generated_code: {len(code)} chars, {res_count} resource blocks")
            _note("-> A3 clears fix_feedback={} on success")
            _note("   reason: route_after_engineering reads fix_feedback; if error_type=None, it routes to validation")
            _hcl(code)
        else:
            fb = update.get("fix_feedback") or {}
            _note("-> A3 FAILED - no valid resource block was generated after two attempts")
            _item("fix_feedback['error_type']",    fb.get("error_type", "?"),  color=red)
            _item("fix_feedback['root_cause']",    fb.get("root_cause", "?"),  color=yellow)
            _item("fix_feedback['error_label']",    fb.get("error_label", "?"),  color=yellow)
            _item("fix_feedback['fix_instruction']",
                  (fb.get("fix_instruction") or "")[:250], color=red)

    elif node == "validation":
        fb     = update.get("fix_feedback") or {}
        passed = fb.get("overall_passed", False)
        applicable_failed = fb.get("applicable_failed_checks") or []

        if passed:
            _note("-> A4 writes overall_passed=True into fix_feedback")
            if applicable_failed:
                _note("-> applicable_failed_checks exist, but do not block deploy because security retry budget is exhausted")
            if fb.get("security_degraded"):
                _note("-> security_degraded=True: A2 failed earlier, so the security gate is bypassed as best-effort")
        else:
            _note("-> A4 writes overall_passed=False plus error details for the retry target")

        print()
        _check_mark(fb.get("validate_passed"), "terraform validate",
                    "HCL syntax is valid")
        _check_mark(fb.get("plan_passed"), "terraform plan",
                    "AWS provider accepts the configuration")
        ck = fb.get("checkov") or {}
        if ck:
            f_ = ck.get("failed_count", 0)
            p_ = ck.get("passed_count", 0)
            ids = ck.get("failed_ckv_ids", [])
            _check_mark(f_ == 0, f"checkov gate: {p_} passed, {f_} failed",
                        "enforces the CKV IDs selected by A2, scanned on plan JSON")
            if ids:
                _note(f"   failed_ckv_ids: {ids}")

        if applicable_failed:
            ids = [u.get("ckv_id") for u in applicable_failed]
            print()
            _note(f"-> applicable_failed_checks {ids}: security retry budget is exhausted -> best-effort accept and continue to deploy")

        not_applicable = fb.get("not_applicable_checks") or []
        if not_applicable:
            _note(f"-> not_applicable_checks {not_applicable}: check ID does not apply to this plan")

        if not passed:
            print()
            et = fb.get("error_type", "?")
            rc = fb.get("root_cause", "?")
            _note(f"-> error_type={yellow(et)}  root_cause={yellow(rc)}")
            _note("   root_cause decides which agent receives fix_instruction for retry")
            if fb.get("fix_instruction"):
                print()
                _item("fix_feedback['fix_instruction']", fb["fix_instruction"][:400], color=yellow)
            if fb.get("raw_error"):
                _item("raw_error (truncated)", fb["raw_error"][:250], color=dim)

        # Counters: read from update when available, otherwise fall back to state_before.
        merged_for_cnt = {**state_before, **(update or {})}
        new_total = _rc(merged_for_cnt, "total")
        new_eng   = _rc(merged_for_cnt, "eng")
        new_sec   = _rc(merged_for_cnt, "sec")
        new_arch  = _rc(merged_for_cnt, "arch")
        print()
        col = yellow if new_total > 0 else dim
        _note("-> A4 updates retry counters, which control whether retry budget remains")
        _item("counters",
              f"total={new_total}/5  eng={new_eng}/3  sec={new_sec}/2  arch={new_arch}/2",
              color=col if new_total > 0 else dim)

    elif node == "deployment":
        dr  = update.get("deployment_result") or {}
        ok_ = dr.get("success", False)
        if ok_:
            created = dr.get("resources_created", [])
            managed = [r for r in created if not str(r).startswith("data.")]
            data_sources = [r for r in created if str(r).startswith("data.")]
            _note(f"-> terraform apply succeeded: {len(managed)} managed resources, "
                  f"{len(data_sources)} data sources in state")
            print()
            for r in created:
                print(f"    {green('OK')} {white(r)}")
            destroyed = dr.get("destroyed")
            print()
            if destroyed:
                _note("-> cleanup: terraform destroy completed after apply")
            else:
                _note("-> cleanup is not confirmed; inspect destroy_error/state if needed")
            if dr.get("destroy_error"):
                _item("destroy_error", dr["destroy_error"][:200], color=red)
        else:
            _note("-> terraform apply failed")
            _item("deployment_result['error_type']", dr.get("error_type", "?"), color=red)
            _item("deployment_result['error_label']", dr.get("error_label", "?"), color=yellow)
            if dr.get("cleanup_error_label"):
                _item("deployment_result['cleanup_error_label']", dr.get("cleanup_error_label"), color=yellow)
            if dr.get("apply_raw_error"):
                _item("apply_raw_error (truncated)", dr["apply_raw_error"][:300], color=dim)
            if dr.get("fix_instruction"):
                print()
                _note("-> A5 classified the error and wrote fix_instruction:")
                _item("fix_instruction", dr["fix_instruction"][:300], color=yellow)

    elif node == "requires_human":
        fb = update.get("fix_feedback") or state_before.get("fix_feedback") or {}
        dr = update.get("deployment_result") or state_before.get("deployment_result") or {}
        reason = fb.get("fix_instruction") or dr.get("error_type") or "unknown"
        _note("-> this node does not modify state; it only reports why the graph stopped")
        _item("stop reason", str(reason)[:300], color=red)


def _explain_routing(node: str, update: dict, merged: dict) -> None:
    _block("ROUTING - NEXT EDGE", yellow)

    fb = update.get("fix_feedback") or merged.get("fix_feedback") or {}
    et = fb.get("error_type")
    rc = fb.get("root_cause")

    _note("-> LangGraph reads fix_feedback from state to decide which edge activates")
    print()

    if node == "architecture":
        if et == "INFRASTRUCTURE":
            _note("-> fix_feedback['error_type'] == 'INFRASTRUCTURE'  (LLM call failed completely)")
            _note("   retrying A1 immediately is not useful; a human should inspect LLM configuration")
            _arrow_next(node, "requires_human", "error_type=INFRASTRUCTURE, LLM did not respond")
        else:
            _note("-> fix_feedback cleared (error_type=None)  -> static architecture->security edge")
            _arrow_next(node, "security", "A1 succeeded; the plan is ready for A2 security selection")

    elif node == "security":
        _note("-> A2 has no conditional edge; it always routes to engineering")
        _arrow_next(node, "engineering", "static edge; A2 degradation does not stop the pipeline")

    elif node == "engineering":
        if not et:
            _note("-> fix_feedback cleared (error_type=None) = A3 succeeded")
            _arrow_next(node, "validation", "HCL is generated and sent to A4 for validation")
        elif et == "MISSING_RESOURCE":
            arch_cnt = _rc(merged, "arch")
            _note(f"-> error_type=MISSING_RESOURCE: A3 received an empty infrastructure_plan from A1")
            _note(f"   solution: route back to A1 for re-planning (arch_retry={arch_cnt}/{_MAX_ARCH_RETRY})")
            if arch_cnt < _MAX_ARCH_RETRY:
                _arrow_next(node, "architecture",
                            f"empty plan -> re-plan, arch_retry={arch_cnt}/{_MAX_ARCH_RETRY}")
            else:
                _note("   arch_retry budget exhausted -> stop; cannot self-repair")
                _arrow_next(node, "requires_human",
                            f"arch_retry budget exhausted ({arch_cnt}/{_MAX_ARCH_RETRY})")
        else:
            _note(f"-> error_type={et}: A3 could not generate valid HCL after internal retries")
            _arrow_next(node, "requires_human",
                        f"error_type={et} (INFRASTRUCTURE=LLM timeout / SYNTAX=no resource block was generated)")

    elif node == "validation":
        passed  = fb.get("overall_passed", False)
        total_r = _rc(merged, "total")
        eng_r   = _rc(merged, "eng")
        arch_r  = _rc(merged, "arch")
        sec_r   = _rc(merged, "sec")

        if passed:
            applicable_failed = fb.get("applicable_failed_checks") or []
            if applicable_failed:
                _note("-> overall_passed=True even though applicable_failed_checks exists")
                _note("   security retry budget is exhausted -> A4 accepts best-effort and does not block deploy")
            else:
                _note("-> overall_passed=True: all four validation gates passed")
            _arrow_next(node, "deployment", "HCL is valid and acceptable -> deploy to AWS")
        else:
            _note(f"-> overall_passed=False, so A4 routes to the agent that can fix it")
            _note(f"   error_type={yellow(et or '?')}  root_cause={yellow(rc or '?')}")
            _note(f"   budget: total={total_r}/{_MAX_VAL_TOTAL_RETRY}  eng={eng_r}/{_MAX_ENG_RETRY}  sec={sec_r}/{_MAX_SEC_RETRY}  arch={arch_r}/{_MAX_ARCH_RETRY}")
            print()
            if total_r >= _MAX_VAL_TOTAL_RETRY:
                _note(f"-> total_val_attempts >= {_MAX_VAL_TOTAL_RETRY}: global backstop reached; stop to avoid an infinite loop")
                _arrow_next(node, "requires_human", f"backstop total_retry={total_r}/{_MAX_VAL_TOTAL_RETRY}")
            elif et == "MISSING_RESOURCE":
                if arch_r < _MAX_ARCH_RETRY:
                    _note("-> MISSING_RESOURCE: plan is missing a resource -> A1 must re-plan")
                    _note("   (this is not an A3 code error; routing the fix to A3 is not useful)")
                    _arrow_next(node, "architecture",
                                f"plan is missing a resource, arch_retry={arch_r}/{_MAX_ARCH_RETRY}")
                else:
                    _arrow_next(node, "requires_human", f"arch_retry budget exhausted ({arch_r}/{_MAX_ARCH_RETRY})")
            elif et == "SECURITY":
                if sec_r < _MAX_SEC_RETRY:
                    _note("-> SECURITY: Checkov failed -> A3 updates code from fix_instruction")
                    _note("   root_cause='engineering' because the fix changes attributes on existing resources")
                    _arrow_next(node, "engineering",
                                f"Checkov fail, A3 adds or updates security attributes, sec_retry={sec_r}/{_MAX_SEC_RETRY}")
                else:
                    _note("-> security retry budget exhausted -> best-effort accept and continue to deploy")
                    _note("   applicable_failed_checks are kept for tracking but do not block")
                    _arrow_next(node, "deployment", f"best-effort security, sec_retry exhausted ({sec_r}/{_MAX_SEC_RETRY})")
            elif et in ("SYNTAX", "LOGIC"):
                if eng_r < _MAX_ENG_RETRY:
                    _note(f"-> {et}: error in A3-generated HCL -> send fix_instruction back to A3")
                    _arrow_next(node, "engineering",
                                f"{et} error in HCL, eng_retry={eng_r}/{_MAX_ENG_RETRY}")
                else:
                    _arrow_next(node, "requires_human", f"eng_retry budget exhausted ({eng_r}/{_MAX_ENG_RETRY})")
            elif et == "INFRASTRUCTURE":
                _note("-> INFRASTRUCTURE: terraform init/plan timeout; not a code error")
                _arrow_next(node, "requires_human", "terraform infrastructure timeout; cannot self-repair")
            else:
                _arrow_next(node, "requires_human", f"error_type={et} has no handler")

    elif node == "deployment":
        dr       = update.get("deployment_result") or {}
        ok_      = dr.get("success", False)
        deploy_r = _rc(merged, "deploy")
        deploy_total = _rc(merged, "deploy_total")
        if ok_:
            _note("-> deployment_result['success']=True")
            _arrow_next(node, "END", "all resources were created successfully on AWS")
        elif deploy_total >= _MAX_DEPLOY_TOTAL_RETRY:
            et_d = dr.get("error_type", "")
            _note(f"-> deployment_result['success']=False, error_type={red(et_d)}")
            _note(f"-> total_deploy_attempts >= {_MAX_DEPLOY_TOTAL_RETRY}: deploy-phase backstop "
                  f"independent from A4 total_val_attempts; stop to avoid looping")
            _arrow_next(node, "requires_human",
                        f"backstop total_deploy_attempts={deploy_total}/{_MAX_DEPLOY_TOTAL_RETRY}")
        else:
            et_d = dr.get("error_type", "")
            _note(f"-> deployment_result['success']=False, error_type={red(et_d)}")
            _note(f"   deploy retry count={deploy_r}  total_deploy_attempts={deploy_total}/{_MAX_DEPLOY_TOTAL_RETRY}")
            print()
            if et_d == "TRANSIENT":
                _note("-> TRANSIENT: network timeout / connection refused / AWS rate limit")
                _note("   not a code error -> retry A5 without changing code")
                _arrow_next(node, "deployment",
                            f"retry A5, temporary network error, deploy_retry={deploy_r}/2")
            elif et_d in ("FIXABLE", "LOGIC"):
                _note("-> FIXABLE/LOGIC: apply failed because of HCL code -> A3 must fix it before another apply")
                _note("   fix_instruction is set in state for A3 to read")
                _arrow_next(node, "engineering",
                            f"apply-time code error, A3 fixes it, deploy_retry={deploy_r}/2")
            elif et_d == "MISSING_RESOURCE":
                _note("-> MISSING_RESOURCE: AWS reports that a dependent resource does not exist")
                _note("   A1 must re-plan to add the missing resource")
                _arrow_next(node, "architecture",
                            f"missing dependent resource, re-plan, deploy_retry={deploy_r}/2")
            else:
                _note("-> UNKNOWN error or deploy_retry budget exhausted")
                _arrow_next(node, "requires_human",
                            f"unhandled error or exhausted budget ({deploy_r})")


# -- Main trace ----------------------------------------------------------------

def trace(prompt: str, no_deploy: bool = False) -> dict:
    """Run the pipeline for one prompt and print the step-by-step trace."""
    g = _select_graph(no_deploy)

    run_dir = ROOT / "tmp" / "trace"
    run_dir.mkdir(parents=True, exist_ok=True)
    _clean_trace_tf_dir(run_dir)
    state: dict = build_initial_state(prompt)
    state["run_dir"] = str(run_dir)

    _real_stdout = sys.stdout
    _writer = None
    try:
        # Header
        print(f"\n{bold('=' * _W)}")
        print(f"  {bold('PIPELINE TRACE - Multi-Agent Terraform Generation')}")
        print(f"  {dim('LangGraph StateGraph: A1 -> A2 -> A3 -> A4 -> A5')}")
        print(f"{bold('=' * _W)}")
        print(f"\n  {dim('prompt:')} {white(bold(prompt))}")
        if no_deploy:
            print(f"  {dim('flags:')}  {yellow('--no-deploy (stop after A4)')}")

        # Initial state
        print(f"\n  {dim('-' * _W)}")
        print(f"  {bold('INITIAL AGENTSTATE')}")
        print(f"  {dim('LangGraph passes this state through every node. Each agent reads')}")
        print(f"  {dim('the fields it needs and writes its own result fields back.')}")
        print()
        _item("prompt", prompt)
        print(f"  {dim('(all counters, plans, code, and feedback start as 0 / empty)')}")
        print(f"  {dim('-' * _W)}")

        node_counts: dict[str, int] = {}
        current_state = dict(state)
        step = 0

        for chunk in g.stream(
            state,
            config={"recursion_limit": RECURSION_LIMIT},
            stream_mode="updates",
        ):
            for node_name, update in chunk.items():
                step += 1
                node_counts[node_name] = node_counts.get(node_name, 0) + 1
                repeat = node_counts[node_name]

                _agent_header(node_name, step, repeat)

                role = _AGENT_ROLE.get(node_name, "")
                if role:
                    print(f"\n  {bold('ROLE')}")
                    for line in role.splitlines():
                        print(f"  {line}")

                _explain_input(node_name, current_state)
                _explain_output(node_name, update or {}, current_state)
                if update:
                    current_state.update(update)
                _explain_routing(node_name, update or {}, current_state)

        # Final result
        fb   = current_state.get("fix_feedback") or {}
        dr   = current_state.get("deployment_result") or {}
        plan_s = current_state.get("infrastructure_plan") or {}
        code = current_state.get("generated_code", "")

        print(f"\n{bold('=' * _W)}")
        print(f"  {bold('FINAL RESULT')}")
        print(f"{bold('=' * _W)}\n")

        def _final(label, val, good=None):
            marker = "[OK]" if good is True else ("[FAIL]" if good is False else "[--]")
            print(f"  {marker:<6} {dim(label + ':')} {val}")

        _final("infrastructure_plan", f"{len(plan_s.get('resources', []))} resources")
        _final("generated_code",      f"{len(code)} chars")
        _final("terraform validate",  fb.get("validate_passed"), good=fb.get("validate_passed"))
        _final("terraform plan",      fb.get("plan_passed"),     good=fb.get("plan_passed"))
        _final("overall_passed (A4)", fb.get("overall_passed"),  good=fb.get("overall_passed"))
        if not no_deploy:
            _final("deployment.success", dr.get("success"), good=dr.get("success"))
        _final("total_val_attempts (val phase)", _rc(current_state, "total"))
        if not no_deploy:
            _final("total_deploy_attempts (deploy phase)", _rc(current_state, "deploy_total"))
            _final("deploy_retry", _rc(current_state, "deploy"))

        rl = current_state.get("routing_log") or []
        if rl:
            print(f"\n  {dim('routing_log (' + str(len(rl)) + ' entries):')}")
            for entry in rl:
                print(f"    {dim(str(entry))}")
        print()
    finally:
        sys.stdout = _real_stdout
        if _writer:
            _writer.close()

    return current_state


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Trace one prompt through the multi-agent Terraform pipeline.",
    )
    parser.add_argument(
        "prompt",
        nargs="?",
        default=None,
        help="IaC prompt to trace",
    )
    parser.add_argument(
        "--csv",
        type=Path,
        default=None,
        help="Dataset CSV path (default: dataset/data-dev.csv)",
    )
    parser.add_argument(
        "--case",
        type=int,
        default=0,
        help="Zero-based row index when using --csv",
    )
    parser.add_argument(
        "--no-deploy",
        action="store_true",
        help="Stop after A4 Validation",
    )
    parser.add_argument(
        "--logs",
        type=str,
        default=None,
        help="Log file path (writes all WARNING/INFO records)",
    )
    args = parser.parse_args()

    # Setup logging to file
    if args.logs:
        log_path = Path(args.logs)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_formatter = logging.Formatter(
            "[%(levelname)s] %(name)s: %(message)s"
        )
        file_handler.setFormatter(file_formatter)
        logging.getLogger().addHandler(file_handler)

        # Suppress console logging; write logs only to the file.
        root_logger = logging.getLogger()
        for handler in root_logger.handlers[:]:
            if isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler):
                root_logger.removeHandler(handler)

        print(f"Logs -> {log_path.absolute()}")
        print()

    if args.csv:
        csv_path = args.csv
        if not csv_path.is_absolute():
            csv_path = ROOT / csv_path
        prompt, difficulty = load_trace_prompt(csv_path, args.case)
        print(f"Trace CSV row {args.case} | difficulty={difficulty} | csv={csv_path}")
    else:
        prompt = args.prompt or "Create an S3 bucket with versioning and server-side encryption."

    from core.terraform import check_required_tools
    check_required_tools()

    trace(
        prompt,
        no_deploy=args.no_deploy,
    )

