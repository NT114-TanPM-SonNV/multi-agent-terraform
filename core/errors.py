"""Shared error taxonomy and failure payload helpers."""

# Credential / permission failures.
AUTH_CREDENTIAL_PATTERNS = (
    "no valid credential",
    "nocredentialproviders",
    "could not load credentials",
    "expired token",
    "invalidclienttokenid",
    "authfailure",
    "unauthorizedoperation",
    "accessdenied",
    "access denied",
    "not authorized",
    "operationnotpermitted",
    "requesterror",
)

# Provider / plugin setup failures.
PROVIDER_SETUP_PATTERNS = (
    "failed to instantiate provider",
    "could not load plugin",
    "failed to load plugin",
)

# Resource type / data source / dependency lookup failures.
MISSING_RESOURCE_PATTERNS = (
    "invalid resource type",
    "unsupported resource type",
    "does not support resource type",
    "does not support data source",
    "unknown resource type",
    "type not defined",
    "no such resource",
    "resource cannot be found",
)

# terraform init / backend / required_providers failures.
INIT_CONFIG_ERROR_PATTERNS = (
    "backend initialization required",
    "invalid backend configuration",
    "unsupported block type",
    "invalid or missing required argument",
    "terraform required_providers",
    "invalid provider configuration",
)

# Retryable network / throttling / timeout failures.
TRANSIENT_PATTERNS = (
    "connection refused", "connection reset", "could not connect",
    "i/o timeout", "timed out", "context deadline exceeded",
    "tls handshake timeout", "no such host", "dial tcp",
    "reset by peer", "unexpected eof", "requesttimeout",
    "requestlimitexceeded", "throttling", "rate exceeded",
    "vpcquotaexceeded", "limitexceeded",
    "failed to query available provider packages", "registry error",  # init-specific
    "validating provider credentials", "retrieving caller identity",
    "request send failed", "statuscode: 0",
)


def matches_any(text: str, patterns: tuple[str, ...]) -> bool:
    """Return ``True`` if any pattern matches ``text`` case-insensitively."""
    low = (text or "").lower()
    return any(p in low for p in patterns)


def recent_fix_instructions(
    history: list[dict] | None,
    *,
    limit: int = 2,
    max_chars: int = 300,
    exclude: str | None = None,
) -> list[str]:
    """Return recent ``fix_instruction`` values from an error history."""
    out: list[str] = []
    for e in (history or [])[-limit:]:
        raw = e.get("fix_instruction") or ""
        if raw == exclude:
            continue
        fix = raw[:max_chars].strip()
        if fix:
            out.append(fix)
    return out


def build_fix_feedback(
    error_type: str,
    root_cause: str | None,
    fix_instruction: str,
    *,
    validate_passed: bool = False,
    plan_passed: bool = False,
    error_stage: str | None = None,
    raw_error: str | None = None,
    checkov_failed: list | None = None,
) -> dict:
    """Build the canonical ``fix_feedback`` payload."""
    feedback = {
        "overall_passed": False,
        "error_type": error_type,
        "root_cause": root_cause,
        "fix_instruction": fix_instruction,
        "checkov": {"passed_count": 0, "failed": checkov_failed or []},
        "validate_passed": validate_passed,
        "plan_passed": plan_passed,
    }
    if error_stage is not None:
        feedback["error_stage"] = error_stage
    if raw_error is not None:
        feedback["raw_error"] = raw_error
    return feedback


def build_fail_result(error_type: str, root_cause: str | None, fix_instruction: str) -> dict:
    """Return the standard failure payload for early node exits."""
    return {
        "fix_feedback": build_fix_feedback(error_type, root_cause, fix_instruction),
    }
