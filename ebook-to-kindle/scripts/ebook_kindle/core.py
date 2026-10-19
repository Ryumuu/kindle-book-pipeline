"""Shared contracts, privacy controls, and filesystem helpers."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlparse


SUPPORTED_INPUTS = frozenset({".docx", ".pdf", ".html", ".htm", ".txt", ".md", ".markdown", ".epub"})
SUPPORTED_OUTPUTS = ("azw3", "mobi", "epub")
OUTPUT_ALIASES = {"mobi6": "mobi", "mobi-old": "mobi"}
SENSITIVE_KEY = re.compile(r"(?:token|secret|password|cookie|authorization|api[_-]?key)", re.I)
SAFE_ENV_KEYS = frozenset({"HOME", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TERM"})
NETWORK_SANDBOX = Path("/usr/bin/sandbox-exec")
NETWORK_DENY_PROFILE = "(version 1)(allow default)(deny network*)"
CALIBRE_USER_STATE_RELATIVE = {
    "preferences": Path("Library/Preferences/calibre"),
    "application_support": Path("Library/Application Support/calibre"),
    "cache": Path("Library/Caches/calibre"),
    "config": Path(".config/calibre"),
    "dot_cache": Path(".cache/calibre"),
    "saved_state": Path("Library/Saved Application State/net.kovidgoyal.calibre.savedState"),
}


class SkillError(RuntimeError):
    """A stable, user-facing failure with a machine-readable code."""

    def __init__(self, code: str, message: str, details: Optional[Mapping[str, Any]] = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})


def looks_like_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme.lower() in {"http", "https", "ftp", "s3", "gs"} and bool(parsed.netloc)


def is_supported_input(path: Path) -> bool:
    return path.suffix.lower() in SUPPORTED_INPUTS


def parse_formats(value: Optional[str]) -> Tuple[str, ...]:
    if not value:
        return ("azw3",)
    result = []
    for raw in value.split(","):
        normalized = OUTPUT_ALIASES.get(raw.strip().lower(), raw.strip().lower())
        if not normalized:
            continue
        if normalized not in SUPPORTED_OUTPUTS:
            raise SkillError("UNSUPPORTED_OUTPUT", "输出格式仅支持 azw3、mobi6/mobi 和 epub。")
        if normalized not in result:
            result.append(normalized)
    if not result:
        raise SkillError("UNSUPPORTED_OUTPUT", "至少需要一个输出格式。")
    return tuple(result)


def ensure_private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def make_private_job_dir(parent: Path) -> Path:
    ensure_private_dir(parent)
    job = Path(tempfile.mkdtemp(prefix="job-", dir=str(parent)))
    try:
        job.chmod(0o700)
    except OSError as exc:
        try:
            shutil.rmtree(job)
        except OSError as cleanup_exc:
            raise SkillError(
                "ISOLATION_FAILED",
                "私有任务目录权限设置失败且未能完整清理。",
                {"recovery_entries": [job.name]},
            ) from cleanup_exc
        raise SkillError("ISOLATION_FAILED", "无法设置私有任务目录权限。") from exc
    return job


def minimal_subprocess_env(base: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """Pass only non-credential process context to third-party tools."""
    source = base if base is not None else os.environ
    env = {key: str(source[key]) for key in SAFE_ENV_KEYS if key in source}
    env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
    return env


def offline_subprocess_command(command: Sequence[str]) -> list[str]:
    """Wrap a conversion-time command in the macOS kernel network sandbox."""
    if not NETWORK_SANDBOX.is_file() or not os.access(NETWORK_SANDBOX, os.X_OK):
        raise SkillError("ISOLATION_FAILED", "系统网络隔离工具不可用；离线转换已拒绝。")
    return [str(NETWORK_SANDBOX), "-p", NETWORK_DENY_PROFILE, *command]


def build_job_env(job_dir: Path, runtime_root: Path, base: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    env = minimal_subprocess_env(base)
    job_home = job_dir / "home"
    paths = {
        "HOME": job_home,
        "CFFIXED_USER_HOME": job_home,
        "CALIBRE_CONFIG_DIRECTORY": job_dir / "calibre-config",
        "CALIBRE_CACHE_DIRECTORY": job_dir / "calibre-cache",
        "CALIBRE_TEMP_DIR": job_dir / "calibre-temp",
        "TMPDIR": job_dir / "tmp",
        "XDG_CACHE_HOME": job_dir / "xdg-cache",
        "XDG_CONFIG_HOME": job_dir / "xdg-config",
        "XDG_DATA_HOME": job_dir / "xdg-data",
        "DOCLING_CACHE_DIR": job_dir / "docling-cache",
        "HF_HOME": job_dir / "hf-cache",
        "TORCH_HOME": job_dir / "torch-cache",
    }
    for key, target in paths.items():
        ensure_private_dir(target)
        env[key] = str(target)
    env.update(
        {
            "DOCLING_ARTIFACTS_PATH": str(runtime_root / "pdf" / "models"),
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_HUB_DISABLE_TELEMETRY": "1",
            "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1",
            "DO_NOT_TRACK": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONUTF8": "1",
        }
    )
    return env


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _tree_state_signature(path: Path) -> str:
    digest = hashlib.sha256()

    def visit(current: Path, relative: str) -> None:
        metadata = os.lstat(current)
        mode = metadata.st_mode
        digest.update(relative.encode("utf-8", errors="surrogateescape"))
        digest.update(
            f"\0{stat.S_IFMT(mode)}:{stat.S_IMODE(mode)}:{metadata.st_uid}:{metadata.st_gid}:"
            f"{metadata.st_ino}:{metadata.st_size}:{metadata.st_mtime_ns}:{metadata.st_ctime_ns}\0".encode("ascii")
        )
        if stat.S_ISLNK(mode):
            digest.update(os.readlink(current).encode("utf-8", errors="surrogateescape"))
        elif stat.S_ISDIR(mode):
            with os.scandir(current) as entries:
                names = sorted(entry.name for entry in entries)
            for name in names:
                visit(current / name, f"{relative}/{name}")

    if not os.path.lexists(path):
        return "absent"
    visit(path, ".")
    return digest.hexdigest()


def snapshot_calibre_user_state(home: Optional[Path] = None) -> Dict[str, str]:
    """Fingerprint only metadata for known Calibre user-state trees."""
    root = home if home is not None else Path.home()
    try:
        return {
            label: _tree_state_signature(root / relative)
            for label, relative in CALIBRE_USER_STATE_RELATIVE.items()
        }
    except OSError as exc:
        raise SkillError("ISOLATION_FAILED", "无法核验 Calibre 用户状态；运行已停止。") from exc


def atomic_write_json(path: Path, payload: Mapping[str, Any], *, private_parent: bool = True) -> None:
    if path.parent.exists():
        if not path.parent.is_dir():
            raise SkillError("UNSUPPORTED_OUTPUT", "JSON 输出父路径不是目录。")
        if private_parent:
            path.parent.chmod(0o700)
    else:
        path.parent.mkdir(parents=True, mode=0o700)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(str(temporary), str(path))
    finally:
        if temporary.exists():
            temporary.unlink()


def _sanitize(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _sanitize(item) for key, item in value.items() if not SENSITIVE_KEY.search(str(key))}
    if isinstance(value, (list, tuple)):
        return [_sanitize(item) for item in value]
    if isinstance(value, Path):
        return value.name
    if isinstance(value, str):
        if value.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", value):
            return Path(value).name
        return value
    return value


def safe_report(
    *,
    status: str,
    input_path: Path,
    output_dir: Path,
    outputs: Sequence[Path],
    route: str,
    details: Optional[Mapping[str, Any]] = None,
    error_code: Optional[str] = None,
) -> Dict[str, Any]:
    report: Dict[str, Any] = {
        "schema_version": 1,
        "status": status,
        "input": input_path.name,
        "output_directory": output_dir.name or ".",
        "outputs": [item.name for item in outputs],
        "route": route,
        "details": _sanitize(dict(details or {})),
    }
    if error_code:
        report["error_code"] = error_code
    return report


def validate_local_input(raw: str) -> Path:
    if looks_like_url(raw):
        raise SkillError("UNSUPPORTED_INPUT", "只接受本地文件，不下载 URL。")
    path = Path(raw).expanduser().resolve()
    if not path.exists() or not path.is_file():
        raise SkillError("UNSUPPORTED_INPUT", "输入文件不存在或不是普通文件。")
    if not is_supported_input(path):
        raise SkillError("UNSUPPORTED_INPUT", "输入仅支持 PDF、DOCX、HTML、TXT、Markdown 和 EPUB。")
    return path


def file_mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def unique_output_names(stem: str, formats: Iterable[str]) -> Dict[str, str]:
    cleaned = re.sub(r"[^\w\-. ]+", "_", stem, flags=re.UNICODE).strip(" .") or "book"
    return {fmt: f"{cleaned}.{fmt}" for fmt in formats}
