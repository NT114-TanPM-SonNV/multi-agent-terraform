"""core/check_tiers.py — phân tầng graph check theo cấu trúc Checkov.

Tier suy từ CÁCH CHECKOV MÔ HÌNH HOÁ check (connected_types), không từ keyword tên check.
Dùng bởi validation.py để classify graph checks (CKV2_AWS_*) — không dùng cho posture.

  TIER 0  single-resource   không có connected_types — không model quan hệ.
  TIER 1  config-wrapper    connected_types toàn sub-config free (public_access_block, versioning…).
  TIER 2  functional        connected_types có companion service (logging bucket, DNSSEC key…).
"""
from __future__ import annotations

import re

_CONFIG_COMPANION_RE = re.compile(
    r"_(public_access_block|versioning|server_side_encryption_configuration|"
    r"ownership_controls|acl|object_lock_configuration)$"
)


def classify_tier(name: str, connected_types: list[str] | None) -> int:
    if not connected_types:
        return 0
    return 1 if all(bool(_CONFIG_COMPANION_RE.search(c or "")) for c in connected_types) else 2
