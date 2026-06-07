#!/usr/bin/env python3
"""Pre-populate Terraform provider cache với tất cả provider cần cho dataset.

Chạy 1 lần trước benchmark:
  python scripts/populate_provider_cache.py

Script:
  1. Tạo dummy main.tf với required_providers (aws, random)
  2. Chạy terraform init (download providers vào cache)
  3. Cleanup dummy files
  4. Cache sẵn sàng cho pipeline
"""
import subprocess
import sys
import tempfile
from pathlib import Path

# UTF-8 output on Windows
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

# Provider cần cho dataset. `local` được code sinh ra dùng nhiều (lambda zip, file
# rendering) nhưng trước đây thiếu → terraform init fail "provider not found".
# archive/tls/null: provider tiện ích LLM hay emit (lambda archive, TLS key, null_resource).
PROVIDERS = {
    "aws": "~> 5.0",
    "random": "~> 3.0",
    "local": "~> 2.0",
    "archive": "~> 2.0",
    "tls": "~> 4.0",
    "null": "~> 3.0",
}

# Cache path (match core/terraform.py)
CACHE_DIR = Path(__file__).parent.parent / ".tf_plugin_cache"
CACHE_DIR.mkdir(exist_ok=True)

def main():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        # 1. Tạo dummy main.tf
        main_tf = tmpdir / "main.tf"
        providers_block = ""
        for name, version in PROVIDERS.items():
            providers_block += f"""  {name} = {{
    source  = "hashicorp/{name}"
    version = "{version}"
  }}
"""
        main_tf.write_text(f"""terraform {{
  required_providers {{
{providers_block}  }}
}}
""")
        print(f"✓ Tạo {main_tf}")

        # 2. Chạy terraform init (download providers)
        print(f"⏳ Downloading providers vào {CACHE_DIR}...")
        env = {
            **__import__('os').environ,
            "TF_PLUGIN_CACHE_DIR": str(CACHE_DIR),
        }
        result = subprocess.run(
            ["terraform", "init", "-no-color"],
            cwd=tmpdir,
            env=env,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"✗ terraform init failed:")
            print(result.stderr or result.stdout)
            return 1
        print("✓ terraform init success")

        # 3. Verify cache populated
        provider_count = len(list(CACHE_DIR.glob("**/terraform-provider-*")))
        if provider_count == 0:
            print("⚠ Cache không có provider binary — TF_PLUGIN_CACHE_DIR không được dùng")
            print("  Retry: chạy terraform init với -plugin-dir flag sẽ tạo providers/")
            return 1
        print(f"✓ Cache có {provider_count} provider binary(s)")

    print(f"\n✅ Cache sẵn sàng: {CACHE_DIR}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
