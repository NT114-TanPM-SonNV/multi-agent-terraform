"""Wrapper cho Terraform CLI, Checkov, và Floci — dùng chung cho toàn pipeline.

_TF_ENV đảm bảo mọi subprocess đều dùng plugin cache — tránh download
provider hàng trăm lần khi chạy benchmark.
"""
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from contextlib import contextmanager
from pathlib import Path
# Cache provider giữa các lần gọi terraform — đặt ngoài thư mục tmp
# để tồn tại xuyên suốt toàn bộ benchmark
_TF_CACHE_DIR = Path(__file__).parent.parent / ".tf_plugin_cache"
_TF_CACHE_DIR.mkdir(exist_ok=True)

# Env dùng chung cho mọi subprocess terraform — inject cache dir.
# MAY_BREAK_DEPENDENCY_LOCK_FILE: cho phép dùng plugin cache khi init trong
# thư mục chưa có .terraform.lock.hcl (tránh lỗi checksum mismatch ngẫu nhiên).
_TF_ENV = {
    **os.environ,
    "TF_PLUGIN_CACHE_DIR": str(_TF_CACHE_DIR),
    "TF_PLUGIN_CACHE_MAY_BREAK_DEPENDENCY_LOCK_FILE": "true",
}

# Công cụ bắt buộc cho PIPELINE (generate→validate→deploy). `opa` KHÔNG ở đây vì
# pipeline không dùng OPA — nó chỉ cần cho semantic eval (core/rego_eval.py),
# nơi tự kiểm tra opa riêng.
_REQUIRED_TOOLS = ("checkov", "terraform")


def check_required_tools() -> None:
    """Kiểm tra các công cụ bắt buộc cho pipeline có trong PATH không.

    Gọi một lần lúc startup để fail fast thay vì crash giữa benchmark.
    """
    missing = [t for t in _REQUIRED_TOOLS if not shutil.which(t)]
    if missing:
        raise RuntimeError(f"Công cụ chưa được cài: {', '.join(missing)}")


_STUBS_DIR = Path(__file__).parent / "stubs"

# Các pattern HCL có thể reference file local — capture group 1 là path
_LOCAL_FILE_PATTERNS = re.compile(
    r'(?:'
    r'filename\s*=\s*"([^"]+)"'                  # filename = "..."
    r'|source_file\s*=\s*"([^"]+)"'              # source_file = "..."
    r'|source_dir\s*=\s*"([^"]+)"'               # source_dir = "..." (archive_file dir)
    r'|source\s*=\s*"(\.{1,2}/[^"]+)"'           # source = "./..." or "../..." (local only)
    r'|(?:template|config)file?\s*=\s*"([^"]+)"' # template/config = "..."
    r'|file\s*\(\s*"([^"]+)"\s*\)'               # file("...")
    r'|templatefile\s*\(\s*"([^"]+)"'            # templatefile("...", ...)
    r')'
)

_STUB_CONTENT: dict[str, bytes | str] = {
    ".zip": None,   # generated dynamically
    ".py":  "def handler(event, context):\n    return {'statusCode': 200}\n",
    ".js":  "exports.handler = async (event) => ({ statusCode: 200 });\n",
    ".sh":  "#!/bin/bash\necho stub\n",
    ".json": "{}\n",
    ".yaml": "",
    ".yml":  "",
    ".env":  "",
    ".conf": "",
    ".tpl":  "",
    ".txt":  "",
    ".pem":  "",
    ".pdf":  b"%PDF-1.4 stub",
    ".csv":  "",
    ".xml":  "",
    ".html": "",
    ".htm":  "",
}

_STUB_ZIP_HANDLER = (
    "def handler(event, context):\n"
    "    return {'statusCode': 200, 'body': 'stub'}\n"
)


def _make_stub_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("handler.py", _STUB_ZIP_HANDLER)
    return buf.getvalue()


def _create_stub_file(path: Path, stub_zip: bytes) -> bytes | None:
    """Tạo stub file phù hợp với extension. Trả về stub_zip bytes nếu vừa tạo."""
    ext = path.suffix.lower()
    if ext not in _STUB_CONTENT and ext not in (".zip",):
        return stub_zip  # extension không biết — bỏ qua
    path.parent.mkdir(parents=True, exist_ok=True)
    if ext == ".zip":
        if stub_zip is None:
            stub_zip = _make_stub_zip()
        path.write_bytes(stub_zip)
    else:
        content = _STUB_CONTENT.get(ext, "")
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
    return stub_zip


def write_terraform_dir(tmpdir: str | Path, code: str,
                        files_dir: str | Path | None = None) -> None:
    """Write main.tf + copy stubs + create stub files for any local path reference.

    Scan HCL cho tất cả pattern reference file local (filename, source_file,
    file(), templatefile(), v.v.). Nếu file chưa tồn tại → tạo stub phù hợp
    theo extension để terraform validate/plan/apply không fail vì thiếu file.

    files_dir: thư mục cache chung giữa các agent trong cùng 1 run.
               Lần đầu tạo stub → copy vào files_dir.
               Lần sau → copy từ files_dir thay vì tạo lại.
    """
    d = Path(tmpdir)
    (d / "main.tf").write_text(code, encoding="utf-8")
    if _STUBS_DIR.exists():
        for stub in _STUBS_DIR.iterdir():
            if stub.is_file():
                shutil.copy2(stub, d / stub.name)

    fd = Path(files_dir) if files_dir else None
    if fd:
        fd.mkdir(parents=True, exist_ok=True)

    stub_zip: bytes | None = None
    seen: set[str] = set()
    for m in _LOCAL_FILE_PATTERNS.finditer(code):
        raw = next(g for g in m.groups() if g)  # lấy group đầu tiên không None
        if raw in seen or raw.startswith("${") or raw.startswith("http"):
            continue  # bỏ qua Terraform interpolation và URL
        seen.add(raw)
        file_path = d / raw
        if file_path.exists():
            continue
        # Copy từ cache nếu đã tạo trước đó (vd: A4 đã tạo, A5 copy lại)
        if fd:
            cached = fd / raw
            if cached.exists():
                file_path.parent.mkdir(parents=True, exist_ok=True)
                if cached.is_dir():
                    if not file_path.exists():
                        shutil.copytree(cached, file_path)
                else:
                    shutil.copy2(cached, file_path)
                continue
        # Tạo stub mới
        if not file_path.suffix:
            # path không có extension → là directory (vd: source_dir = "./lambda")
            file_path.mkdir(parents=True, exist_ok=True)
            stub_entry = file_path / "index.js"
            stub_entry.write_text(
                "exports.handler = async (event) => ({ statusCode: 200 });\n",
                encoding="utf-8",
            )
            if fd:
                cached = fd / raw
                cached.mkdir(parents=True, exist_ok=True)
                shutil.copy2(stub_entry, cached / "index.js")
            continue
        stub_zip = _create_stub_file(file_path, stub_zip)
        # Lưu vào cache để agent tiếp theo dùng lại
        if fd and file_path.exists():
            cached = fd / raw
            cached.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(file_path, cached)


@contextmanager
def terraform_workdir(run_dir: str | Path | None, subdir: str):
    """Context manager trả về thư mục làm việc cho terraform.

    Nếu run_dir được cung cấp: dùng run_dir/subdir (persistent, không xóa khi exit).
    Nếu không: tạo tempdir tạm thời (xóa khi exit).

    Luôn xóa .terraform/ và .terraform.lock.hcl trước khi yield — đảm bảo terraform init
    chạy fresh, tránh lock file từ run trước conflict với version constraint mới.
    Plugin cache (_TF_CACHE_DIR) vẫn giữ nguyên nên init không cần re-download.
    """
    if run_dir:
        d = Path(run_dir) / subdir
        d.mkdir(parents=True, exist_ok=True)
    else:
        d = None

    def _clean(p: Path) -> None:
        lock = p / ".terraform.lock.hcl"
        dot_tf = p / ".terraform"
        if lock.exists():
            lock.unlink()
        if dot_tf.exists():
            import shutil as _shutil
            _shutil.rmtree(dot_tf, ignore_errors=True)

    if d:
        _clean(d)
        yield d
    else:
        with tempfile.TemporaryDirectory(prefix=f"tf_{subdir}_") as tmp:
            yield Path(tmp)


def run_terraform(cmd: list[str], cwd: str | Path, timeout: int) -> subprocess.CompletedProcess:
    """Chạy lệnh terraform với plugin cache và timeout tường minh.

    Không bắt TimeoutExpired ở đây — để agent gọi tự xử lý
    vì mỗi agent có cách route khác nhau khi timeout.
    """
    # Timeout với Popen + wait để ensure cleanup (subprocess.run timeout sometimes hangs)
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_TF_ENV,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=proc.returncode,
            stdout=stdout,
            stderr=stderr,
        )
    except subprocess.TimeoutExpired:
        proc.kill()  # Forcefully terminate if timeout
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass  # Already dead
        raise



def _checkov_bin() -> str:
    b = os.environ.get("CHECKOV_BIN") or shutil.which("checkov")
    if not b:
        raise RuntimeError("checkov not found — set CHECKOV_BIN in .env or add to PATH")
    return b


def _parse_checkov_json(stdout: str, elapsed: float = 0.0) -> dict:
    """Parse Checkov --output json stdout → dict thống nhất.

    Checkov có thể trả single dict hoặc list (nhiều framework).
    Trường hợp parse fail → raise RuntimeError (caller quyết định fallback).
    """
    # Strip ANSI và tìm JSON object/array đầu tiên (banner in ra stderr nhưng đôi khi lẫn)
    clean = re.sub(r"\x1b\[[0-9;]*m", "", stdout)
    m = re.search(r"(\{|\[)", clean)
    if not m:
        raise RuntimeError("Checkov output không chứa JSON")
    data = json.loads(clean[m.start():])

    # Chuẩn hoá thành list để xử lý đồng nhất
    items = data if isinstance(data, list) else [data]

    passed_ids: set[str] = set()
    failed_ids: set[str] = set()
    failed_pairs: list[tuple[str, str]] = []
    total_passed = total_failed = 0

    for item in items:
        if not isinstance(item, dict):
            continue
        summary = item.get("summary") or {}
        total_passed += summary.get("passed", 0)
        total_failed += summary.get("failed", 0)
        results = item.get("results") or {}
        for c in results.get("passed_checks", []):
            cid = c.get("check_id", "")
            if cid:
                passed_ids.add(cid)
        for c in results.get("failed_checks", []):
            cid = c.get("check_id", "")
            addr = c.get("resource") or c.get("resource_address") or ""
            if cid:
                failed_ids.add(cid)
                if addr:
                    failed_pairs.append((addr, cid))

    return {
        "failed_ckv_ids":      sorted(failed_ids),
        "passed_ckv_ids":      sorted(passed_ids),
        "failed_per_resource": failed_pairs,
        "passed_count":        total_passed,
        "failed_count":        total_failed,
        "total_checks":        total_passed + total_failed,
        "scan_seconds":        elapsed,
    }


def run_checkov_on_hcl(hcl: str, timeout: int = 60,
                       check_ids: list[str] | None = None) -> dict:
    """Chạy Checkov trên HCL string (source scan).

    Dùng cho score.py (full scan không có plan file).
    A4 dùng run_checkov_on_plan() khi có plan JSON.

    check_ids: None = scan tất cả (--quiet, chỉ lấy fail).
               list  = scan tập hạn chế (không --quiet để lấy cả passed).
    """
    bin_ = _checkov_bin()
    cmd = [bin_, "-d", ".", "--framework", "terraform", "--output", "json"]
    if check_ids:
        cmd += ["--check", ",".join(sorted(set(check_ids)))]
    else:
        cmd += ["--quiet"]

    t0 = time.time()
    with tempfile.TemporaryDirectory(prefix="checkov_") as tmpdir:
        (Path(tmpdir) / "main.tf").write_text(hcl)
        try:
            proc = subprocess.run(cmd, cwd=tmpdir,
                                  capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"Checkov timeout after {timeout}s")
    return _parse_checkov_json(proc.stdout, round(time.time() - t0, 2))


def run_checkov_on_plan(plan_json_str: str, timeout: int = 60,
                        check_ids: list[str] | None = None) -> dict:
    """Chạy Checkov trên Terraform plan JSON (terraform show -json output).

    Chính xác hơn source scan: resolved computed values, for_each expansion,
    graph checks dùng connection graph đầy đủ từ plan.
    Fallback: nếu terraform_plan framework trả rỗng (check không support),
    caller nên gọi lại run_checkov_on_hcl.
    """
    bin_ = _checkov_bin()
    cmd = [bin_, "-f", "plan.json", "--framework", "terraform_plan",
           "--output", "json"]
    if check_ids:
        cmd += ["--check", ",".join(sorted(set(check_ids)))]

    t0 = time.time()
    with tempfile.TemporaryDirectory(prefix="checkov_plan_") as tmpdir:
        (Path(tmpdir) / "plan.json").write_text(plan_json_str)
        try:
            proc = subprocess.run(cmd, cwd=tmpdir,
                                  capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"Checkov plan scan timeout after {timeout}s")
    return _parse_checkov_json(proc.stdout, round(time.time() - t0, 2))
