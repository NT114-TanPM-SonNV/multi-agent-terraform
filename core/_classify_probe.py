"""Prototype taxonomy classifier — chạy trên .check_targets.json hiện có để audit.
KHÔNG phải file production; xoá sau khi port vào gen_check_targets.py."""
import json, collections
from pathlib import Path

m = json.loads((Path(__file__).parent / ".check_targets.json").read_text())
uniq = {}
for t, es in m.items():
    for e in es:
        uniq.setdefault(e["id"], (e["name"], tuple(e.get("cat") or ["UNCAT"])))


def H(name, *subs):
    low = name.lower()
    return any(s in low for s in subs)


# (predicate, control_type, keep) — first match wins. KEEP = baseline confidentiality/access,
# đơn giản (inline-satisfiable, không companion phức tạp). DROP = operational / availability /
# recovery / over-secure / phức tạp.
RULES = [
    # -- service-breaking egress ----------------------------------------------------------
    (lambda n, c: H(n, "egress") and H(n, "0.0.0.0", "port -1"), "egress_restriction", False),
    # -- IMDSv2 / instance metadata = access hardening (đặt trước patch để 'version' không nuốt)
    (lambda n, c: H(n, "metadata service version", "instance metadata", "imdsv"),
     "workload_isolation", True),
    # -- healthcheck = availability (đặt trước in-transit để 'https' không nuốt CKV_AWS_261) ---
    (lambda n, c: H(n, "healthcheck", "health check", "health reporting"), "availability_reliability", False),
    # -- public exposure / reachability = ACCESS CONTROL (giữ) -----------------------------
    (lambda n, c: H(n, "public", "publicly", "internet access", "not exposed", "0.0.0.0",
                    "open access", "global view acl"), "public_exposure", True),
    # -- operational / governance / companion phức tạp (DROP) -----------------------------
    (lambda n, c: H(n, "scanning", "approval", "rotat", "managed platform", "platform update",
                    "copy tags", "default root object", "lifecycle", "failed uploads",
                    "caching", "guardduty", "guardrail", "code-signing", "code signing",
                    "waf", "distribution is enabled", "detector is enabled", "validate code",
                    "config is enabled", "x-ray", "associated rules", "any actions"),
     "operational_governance", False),
    # -- encryption với CUSTOMER-MANAGED KMS (CMK) → over-secure + cần companion kms_key (DROP)
    (lambda n, c: H(n, "customer mana", "cmk"), "encryption_cmk", False),
    # -- encryption-at-rest (giữ) ----------------------------------------------------------
    (lambda n, c: "ENCRYPTION" in c and H(n, "encrypt", "at rest", " kms"), "encryption_at_rest", True),
    # -- encryption-in-transit / TLS (giữ). KHÔNG dùng bare 'latest'/'modern' (va chạm patch) -
    (lambda n, c: H(n, "tls", "ssl", "https", "secure transport", "in transit", "strict transport",
                    "secure protocol", "viewerprotocol", "security policy"), "encryption_in_transit", True),
    # -- deletion-protection / immutability / create-before-destroy → operational (DROP) ---
    (lambda n, c: H(n, "deletion protection", "object lock", "lock configuration",
                    "create before destroy", "transfer lock", "immutab"), "deletion_protection", False),
    # -- patch / version management → operational (DROP) ----------------------------------
    (lambda n, c: H(n, "non vulnerable", "deprecated", "cacert", "minor version", "minor upgrade",
                    "version is current", "version upgrade", "supported kubernetes",
                    "latest fargate", "modern ca", "up to date"), "patch_version", False),
    # -- backup / recovery / multi-az (DROP) ----------------------------------------------
    (lambda n, c: H(n, "backup", "snapshot", "multi-az", "backtrack", "retention", "failover",
                    "point in time"), "backup_recovery", False),
    # -- availability / reliability / capacity (DROP) -------------------------------------
    (lambda n, c: H(n, "concurren", "reserved", "scaling", "quota", "timeout", "cross-zone",
                    "capacity", "dedicated master", "auto accept", "auto minor", "dead letter",
                    "hop limit", "ebs optimized", "geo restriction", "launch template",
                    "launch configurations"), "availability_reliability", False),
    # -- access policy / IAM least-privilege (giữ) ----------------------------------------
    (lambda n, c: H(n, "wildcard", "principal", "least privilege", "administr", "lockout",
                    "all (*)", "*-*", "\"*\"", "iam authentication", "authoriz", "authtype",
                    "auth type", "permission", "password policy", "cross-account", "cross account",
                    "privilege escalation", "credentials exposure", "data exfiltration",
                    "unauthenticated", "assume role", "access keys", "sso"), "access_policy", True),
    # -- secrets (giữ) --------------------------------------------------------------------
    (lambda n, c: "SECRETS" in c or H(n, "hard-coded", "hardcoded", "secret"), "secret_management", True),
    # -- logging / observability (DROP) ---------------------------------------------------
    (lambda n, c: "LOGGING" in c or H(n, "logging", "log file", "monitor", "alarm", "insights",
                    "tracing", "audit log", "flow log", "notification", "event notif"),
     "logging_observability", False),
    # -- workload isolation / hardening (giữ) ---------------------------------------------
    (lambda n, c: H(n, "privileged", "read-only", "root access", "network isolation", "kerberos",
                    "non-priv", " isolation", "inside a vpc", "outside of a vpc", "launched into",
                    "default security group", "default subnet", "default vpc", "default database",
                    "host's process", "user identity", "root directory", "node group"),
     "workload_isolation", True),
]


def classify(name, cats):
    for pred, ct, keep in RULES:
        if pred(name, cats):
            return ct, keep
    if "LOGGING" in cats or "BACKUP_AND_RECOVERY" in cats:
        return "fallback_operational", False
    return "fallback_keep", True


by_ct = collections.defaultdict(list)
for cid, (name, cats) in uniq.items():
    ct, keep = classify(name, cats)
    by_ct[(keep, ct)].append((cid, name, cats))

kept = sum(len(v) for (k, _), v in by_ct.items() if k)
dropped = sum(len(v) for (k, _), v in by_ct.items() if not k)
print(f"UNIQUE={len(uniq)}  KEEP={kept}  DROP={dropped}\n")
for keep in (True, False):
    print("================ KEEP ================" if keep else "================ DROP ================")
    for (k, ct), rows in sorted(by_ct.items()):
        if k != keep:
            continue
        print(f"\n--- {ct} ({len(rows)}) ---")
        for cid, name, cats in sorted(rows):
            print(f"  {cid:14s} [{'/'.join(cats)}] {name[:64]}")
