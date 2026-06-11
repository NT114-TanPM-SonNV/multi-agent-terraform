"""Terraform, Checkov, and Floci helpers shared by the pipeline."""
import base64
import io
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from contextlib import contextmanager
from pathlib import Path
# Provider cache shared across runs.
_TF_CACHE_DIR = Path(__file__).parent.parent / ".tf_plugin_cache"
_TF_CACHE_DIR.mkdir(exist_ok=True)

# Serialize concurrent terraform init calls on Windows.
_TF_INIT_LOCK = threading.Lock()

# Shared subprocess environment with offline plugin cache.
_TF_ENV = {**os.environ}
for _proxy_key in (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "all_proxy",
):
    _TF_ENV.pop(_proxy_key, None)
_TF_ENV["NO_PROXY"] = ",".join(
    p for p in (
        _TF_ENV.get("NO_PROXY", ""),
        "amazonaws.com",
        ".amazonaws.com",
        "169.254.169.254",
    )
    if p
)

# Required tools for the main pipeline. OPA is checked separately for semantic eval.
_REQUIRED_TOOLS = ("checkov", "terraform")


def _safe_rmtree(path: str | Path) -> None:
    """Remove a directory safely on Windows, including junctions."""
    p = Path(path)
    if not p.exists():
        return
    if sys.platform == "win32":
        subprocess.run(
            ["cmd", "/c", "rmdir", "/s", "/q", str(p)],
            capture_output=True, timeout=30,
        )
    else:
        shutil.rmtree(p, ignore_errors=True)


# Provider name is the prefix before the first underscore in a resource/data type.
_DECL_TYPE_RE = re.compile(r'(?:resource|data)\s+"([^"]+)"')


def required_provider_names(code: str) -> set[str]:
    """Return provider names required by the HCL."""
    return {m.group(1).split("_", 1)[0] for m in _DECL_TYPE_RE.finditer(code)}


def installed_provider_names(dot_tf: Path) -> set[str]:
    """Return installed provider names from ``.terraform/providers``."""
    prov = dot_tf / "providers"
    if not prov.exists():
        return set()
    return {p.name for p in prov.glob("*/*/*") if p.is_dir()}


def tf_init_cmd() -> list[str]:
    """Return the offline ``terraform init`` command."""
    return ["terraform", "init", "-plugin-dir", str(_TF_CACHE_DIR), "-no-color"]


def check_required_tools() -> None:
    """Fail fast when required tools are missing from ``PATH``."""
    missing = [t for t in _REQUIRED_TOOLS if not shutil.which(t)]
    if missing:
        raise RuntimeError(f"Công cụ chưa được cài: {', '.join(missing)}")


# HCL patterns that may reference local files.
_LOCAL_FILE_PATTERNS = re.compile(
    r'(?:'
    r'filename\s*=\s*"([^"]+)"'                   # filename = "..."  (archive_file, lambda)
    r'|source_file\s*=\s*"([^"]+)"'               # source_file = "..."  (archive_file)
    r'|source_dir\s*=\s*"([^"]+)"'                # source_dir = "..."  (archive_file dir)
    r'|source\s*=\s*"([^"${}:][^"]*\.[A-Za-z0-9][A-Za-z0-9._-]*)"'  # local source file path
    r'|(?:template|config)file?\s*=\s*"([^"]+)"'  # templatefile/configfile = "..."
    r'|templatefile\s*\(\s*"([^"]+)"'             # templatefile("...", vars)
    r'|filebase64sha256\s*\(\s*"([^"]+)"\s*\)'    # filebase64sha256("...")
    r'|filebase64\s*\(\s*"([^"]+)"\s*\)'          # filebase64("...")
    r'|filesha(?:256|512)\s*\(\s*"([^"]+)"\s*\)'  # filesha256/filesha512("...")
    r'|filemd5\s*\(\s*"([^"]+)"\s*\)'             # filemd5("...")
    r'|file\s*\(\s*"([^"]+)"\s*\)'                # file("...")  — phải sau các file* cụ thể
    r')'
)


def _make_stub_pub_key() -> str:
    """Sinh OpenSSH RSA-2048 public key với wire format hợp lệ.

    Vấn đề với key giả kiểu "ssh-rsa AAAA...stub": base64 không decode ra đúng SSH wire
    format → AWS ImportKeyPair báo InvalidKey.Format dù terraform plan đã pass.
    Fix: xây wire format chuẩn (length-prefixed fields) rồi base64-encode.

    Key này KHÔNG an toàn mặt toán học (modulus random, không phải RSA prime product)
    nhưng đủ để qua format validation của AWS API. Dùng key thật khi deploy production.
    """
    key_type = b"ssh-rsa"
    e_bytes = b'\x01\x00\x01'            # e=65537, MSB=0x01 → không cần sign byte
    raw = os.urandom(256)
    # MSB byte phải ≥ 0x80 để modulus đủ 2048-bit; thêm \x00 sign byte vì MSB set
    n_bytes = b'\x00' + bytes([raw[0] | 0x80]) + raw[1:]   # 257 bytes total
    wire = (
        struct.pack('>I', len(key_type)) + key_type +
        struct.pack('>I', len(e_bytes)) + e_bytes +
        struct.pack('>I', len(n_bytes)) + n_bytes
    )
    return "ssh-rsa " + base64.b64encode(wire).decode() + " stub-key\n"


# Content mặc định cho stub theo extension.
# Extension KHÔNG có trong dict vẫn được tạo file rỗng (_create_stub_file fallback).
# Chỉ thêm vào đây khi stub cần content cụ thể (script, key format, binary header).
_STUB_CONTENT: dict[str, bytes | str] = {
    # Lambda / serverless function handlers
    ".zip":   None,   # generated dynamically by _make_stub_zip()
    ".py":    "def handler(event, context):\n    return {'statusCode': 200}\n",
    ".js":    "exports.handler = async (event) => ({ statusCode: 200 });\n",
    ".ts":    "export const handler = async (event: any) => ({ statusCode: 200 });\n",
    ".go":    "package main\nfunc main() {}\n",
    ".rb":    "def handler(event:, context:)\n  { statusCode: 200 }\nend\n",
    ".java":  "public class Handler {}\n",
    # Scripts
    ".sh":    "#!/bin/bash\necho stub\n",
    ".bash":  "#!/bin/bash\necho stub\n",
    ".ps1":   "Write-Output 'stub'\n",
    ".bat":   "@echo off\necho stub\n",
    ".cmd":   "@echo off\necho stub\n",
    # SSH / TLS keys & certs
    ".pub":   None,   # generated dynamically by _make_stub_pub_key() — cần SSH wire format đúng
    ".pem":   "-----BEGIN CERTIFICATE-----\nstub\n-----END CERTIFICATE-----\n",
    ".crt":   "-----BEGIN CERTIFICATE-----\nstub\n-----END CERTIFICATE-----\n",
    ".cert":  "-----BEGIN CERTIFICATE-----\nstub\n-----END CERTIFICATE-----\n",
    ".cer":   "-----BEGIN CERTIFICATE-----\nstub\n-----END CERTIFICATE-----\n",
    ".key":   "-----BEGIN PRIVATE KEY-----\nstub\n-----END PRIVATE KEY-----\n",
    ".ca":    "-----BEGIN CERTIFICATE-----\nstub\n-----END CERTIFICATE-----\n",
    # Data / config — file rỗng/minimal đủ để terraform plan đọc
    ".json":  "{}\n",
    ".yaml":  "",
    ".yml":   "",
    ".toml":  "",
    ".env":   "",
    ".conf":  "",
    ".cfg":   "",
    ".ini":   "",
    ".properties": "",
    # Templates
    ".tpl":   "",
    ".tmpl":  "",
    ".j2":    "",
    ".jinja": "",
    ".jinja2": "",
    # Text / document
    ".txt":   "",
    ".csv":   "",
    ".xml":   "",
    ".html":  "",
    ".htm":   "",
    ".sql":   "",
    ".pdf":   b"%PDF-1.4 stub",
}

_STUB_ZIP_HANDLER = (
    "def handler(event, context):\n"
    "    return {'statusCode': 200, 'body': 'stub'}\n"
)

_STUB_ZIP_MAIN = (
    "def lambda_handler(event, context):\n"
    "    return {'statusCode': 200, 'body': 'stub'}\n"
)

_STUB_ZIP_INDEX_JS = (
    "exports.handler = async (event) => ({ statusCode: 200, body: 'stub' });\n"
)


def _make_stub_zip() -> bytes:
    """Create a deployable Lambda stub package with common handler entry points."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # Covers handler = "handler.handler"
        zf.writestr("handler.py", _STUB_ZIP_HANDLER)
        # Covers handler = "main.lambda_handler"
        zf.writestr("main.py", _STUB_ZIP_MAIN)
        # Covers handler = "index.handler" for Node.js runtimes
        zf.writestr("index.js", _STUB_ZIP_INDEX_JS)
    return buf.getvalue()


_STUB_BUILDSPEC = """version: 0.2
phases:
  build:
    commands:
      - echo stub build
artifacts:
  files:
    - '**/*'
"""


def _stub_content_for_path(path: Path) -> bytes | str | None:
    """Return path-aware stub content for files whose name matters."""
    name = path.name.lower()
    ext = path.suffix.lower()
    if name in {"buildspec.yml", "buildspec.yaml"} or name.startswith("buildspec."):
        return _STUB_BUILDSPEC
    if name in {"package.json"}:
        return '{"name":"stub","version":"1.0.0","main":"index.js"}\n'
    return _STUB_CONTENT.get(ext, "")


def _create_stub_file(path: Path, stub_zip: bytes) -> bytes | None:
    """Tạo stub file phù hợp với extension/name. Trả về stub_zip bytes nếu vừa tạo.

    Extension không có trong _STUB_CONTENT → tạo file rỗng (fallback).
    Terraform cần file TỒN TẠI để file()/filebase64()/... không throw; content
    chỉ quan trọng ở apply-time khi AWS validate (key format, buildspec, zip, v.v.).
    """
    ext = path.suffix.lower()
    path.parent.mkdir(parents=True, exist_ok=True)
    if ext == ".zip":
        if stub_zip is None:
            stub_zip = _make_stub_zip()
        path.write_bytes(stub_zip)
    elif ext == ".pub":
        # SSH public key cần wire format chuẩn — tạo mới mỗi lần (os.urandom modulus)
        path.write_text(_make_stub_pub_key(), encoding="utf-8")
    else:
        content = _stub_content_for_path(path)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content or "", encoding="utf-8")
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
    fd = Path(files_dir) if files_dir else None
    if fd:
        fd.mkdir(parents=True, exist_ok=True)

    stub_zip: bytes | None = None
    seen: set[str] = set()
    for m in _LOCAL_FILE_PATTERNS.finditer(code):
        raw = next(g for g in m.groups() if g)  # lấy group đầu tiên không None
        if raw in seen:
            continue
        raw_l = raw.lower()
        if (
            raw.startswith("${")
            or raw.startswith("/")
            or raw_l.startswith(("http://", "https://", "s3://", "arn:"))
        ):
            continue  # bỏ qua interpolation, absolute paths, URLs, S3 URIs, and ARNs
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
def terraform_workdir(run_dir: str | Path | None, subdir: str, reuse: bool = False):
    """Context manager trả về thư mục làm việc cho terraform.

    Nếu run_dir được cung cấp: dùng run_dir/subdir (persistent, không xóa khi exit).
    Nếu không: tạo tempdir tạm thời (xóa khi exit).

    reuse=False (default): xóa .terraform/ và .terraform.lock.hcl trước khi yield —
      đảm bảo terraform init chạy fresh, tránh lock file cũ conflict.
    reuse=True: giữ nguyên .terraform/ và lock file — dùng khi A5 tái sử dụng thư mục
      mà A4 đã init để skip re-download provider.
    Plugin cache (_TF_CACHE_DIR) vẫn giữ nguyên nên init không cần re-download.
    """
    if run_dir:
        d = Path(run_dir) / subdir
        d.mkdir(parents=True, exist_ok=True)
    else:
        d = None

    def _clean(p: Path) -> None:
        lock = p / ".terraform.lock.hcl"
        if lock.exists():
            lock.unlink()
        # _safe_rmtree dùng rmdir /s /q trên Windows: xóa junction entry trong
        # .terraform/providers/ mà không follow target (plugin cache) → init fresh.
        _safe_rmtree(p / ".terraform")

    if d:
        if not reuse:
            _clean(d)
        yield d
    else:
        # Tự tạo + dọn bằng _safe_rmtree (junction-safe, không raise). KHÔNG dùng
        # TemporaryDirectory vì cleanup của nó (shutil.rmtree) FOLLOW directory junction
        # trong .terraform/providers/ → crash WinError 5/267 trên Windows khi gặp
        # provider .exe bị lock (đặc biệt khi chạy nhiều worker song song).
        tmp = tempfile.mkdtemp(prefix=f"tf_{subdir}_")
        try:
            yield Path(tmp)
        finally:
            _safe_rmtree(tmp)


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


def run_terraform_init(cwd: str | Path, timeout: int) -> subprocess.CompletedProcess:
    """terraform init với global lock để tránh file lock trên Windows.

    Gọi thay cho run_terraform(tf_init_cmd(), cwd, timeout) khi chạy --workers > 1.
    Lock chỉ bao quanh init (vài giây); validate/plan/apply chạy song song bình thường.
    """
    with _TF_INIT_LOCK:
        return run_terraform(tf_init_cmd(), cwd, timeout)


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
