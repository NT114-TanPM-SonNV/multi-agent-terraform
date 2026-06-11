import logging
import re
import subprocess
import time
from pathlib import Path

from core.terraform import run_terraform
from core.errors import matches_any, TRANSIENT_PATTERNS

logger = logging.getLogger(__name__)

# Destroy timeouts, retry budget, and deletion-protection patches.

_DESTROY_TIMEOUT = 600   # ElastiCache/RDS cần 5-10 phút để xóa
_MAX_DESTROY_TRANSIENT_RETRY = 1  # Retry destroy nếu transient (network/throttle)
_DESTROY_RETRY_BACKOFF = 5  # giây chờ giữa các lần retry
_DESTROY_OVERRIDE_NAME = "destroy_override.tf"

# Patch HCL before destroy to disable delete blockers.
_DESTROY_PATCHES = [
    (r'(deletion_protection_enabled\s*=\s*)true',    r'\g<1>false'),  # DynamoDB
    (r'(deletion_protection\s*=\s*)true',            r'\g<1>false'),  # RDS/ALB
    (r'(skip_final_snapshot\s*=\s*)false',           r'\g<1>true'),   # RDS
    (r'\n[ \t]*final_snapshot_identifier\s*=\s*[^\n]+', ''),          # RDS (conflicts với skip)
    (r'(apply_immediately\s*=\s*)false',             r'\g<1>true'),   # RDS
    (r'(automatic_failover_enabled\s*=\s*)true',     r'\g<1>false'),  # ElastiCache
    (r'(multi_az_enabled\s*=\s*)true',               r'\g<1>false'),  # ElastiCache
]

# Patch and destroy helpers.

def patch_for_destroy(code: str) -> str:
    """Patch HCL to disable deletion protection before destroy."""
    for pattern, replacement in _DESTROY_PATCHES:
        code = re.sub(pattern, replacement, code)
    return code


def write_destroy_override(workdir: str | Path, code: str) -> Path | None:
    """Write ``destroy_override.tf`` when the code needs patching."""
    patched = patch_for_destroy(code)
    if patched == code:
        return None
    override_path = Path(workdir) / _DESTROY_OVERRIDE_NAME
    override_path.write_text(patched, encoding="utf-8")
    return override_path


def cleanup_destroy_override(workdir: str | Path) -> None:
    """Remove ``destroy_override.tf`` if it exists."""
    override_path = Path(workdir) / _DESTROY_OVERRIDE_NAME
    try:
        override_path.unlink()
    except FileNotFoundError:
        pass


def destroy_with_override(
    workdir: str | Path,
    code: str,
    timeout: int = _DESTROY_TIMEOUT,
    max_retries: int = _MAX_DESTROY_TRANSIENT_RETRY,
    backoff: int = _DESTROY_RETRY_BACKOFF,
) -> tuple[bool, str | None]:
    """Apply destroy overrides, run destroy, then clean up the override file."""
    override_path = write_destroy_override(workdir, code)
    try:
        if override_path is not None:
            try:
                run_terraform(
                    ["terraform", "apply", "-auto-approve", "-no-color", "-parallelism=4"],
                    workdir, timeout,
                )
            except subprocess.TimeoutExpired:
                logger.warning("Destroy override apply timed out — continue to destroy")
            except Exception as e:
                logger.warning("Destroy override apply failed — continue to destroy: %s", e)
        return destroy_resources(workdir, timeout=timeout, max_retries=max_retries, backoff=backoff)
    finally:
        cleanup_destroy_override(workdir)


def destroy_resources(
    tmpdir: str,
    timeout: int = _DESTROY_TIMEOUT,
    max_retries: int = _MAX_DESTROY_TRANSIENT_RETRY,
    backoff: int = _DESTROY_RETRY_BACKOFF,
) -> tuple[bool, str | None]:
    """Run ``terraform destroy`` with a small transient retry loop."""
    for attempt in range(max_retries + 1):
        if attempt > 0:
            time.sleep(backoff * attempt)

        try:
            destroy = run_terraform(
                ["terraform", "destroy", "-auto-approve", "-no-color", "-parallelism=4"],
                tmpdir, timeout,
            )
        except subprocess.TimeoutExpired:
            if attempt < max_retries:
                logger.warning("Destroy timeout (attempt %d/%d) — retry", attempt + 1, max_retries + 1)
                continue
            timeout_msg = f"terraform destroy timed out (>{timeout}s)"
            logger.warning("Destroy FAILED: %s", timeout_msg)
            return False, timeout_msg

        if destroy.returncode == 0:
            logger.info("Destroy OK")
            return True, None

        destroy_err = (destroy.stderr or destroy.stdout or "").strip()

        if attempt < max_retries and matches_any(destroy_err, TRANSIENT_PATTERNS):
            logger.warning("Destroy transient (attempt %d/%d) — retry: %s",
                          attempt + 1, max_retries + 1, destroy_err[:100])
            continue

        error_msg = destroy_err[:500]
        logger.warning("Destroy FAILED: %s", error_msg)
        return False, error_msg

    return True, None  # Shouldn't reach here, but fallback
