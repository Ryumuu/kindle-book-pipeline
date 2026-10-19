"""Pinned runtime layout and readiness checks."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import fcntl
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence

from .contract import (
    CORE_COMPONENT,
    MAX_PERSISTENT_BYTES,
    MODEL_FILES,
    MODEL_HASHES,
    PDF_PYTHON_ASSET,
    PDF_VERSIONS,
    RUNTIME_NAME,
    RUNTIME_SCHEMA_VERSION,
    RUNTIME_VERSION,
)
from .core import (
    SkillError,
    build_job_env,
    make_private_job_dir,
    offline_subprocess_command,
    sha256_file,
    snapshot_calibre_user_state,
)


RUNTIME_LOCK_NAME = ".operation.lock"


def tree_bytes(root: Path) -> int:
    if not root.exists():
        return 0

    def visit(directory: Path) -> int:
        subtotal = 0
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        metadata = entry.stat(follow_symlinks=False)
                    except OSError as exc:
                        raise SkillError("ISOLATION_FAILED", "无法完整计算运行时容量；核验已停止。") from exc
                    if entry.name == RUNTIME_LOCK_NAME and directory == root:
                        continue
                    if stat.S_ISLNK(metadata.st_mode):
                        # The cap is defined over persistent regular-file bytes;
                        # internal venv symlinks are never followed.
                        continue
                    path = Path(entry.path)
                    if stat.S_ISDIR(metadata.st_mode):
                        subtotal += visit(path)
                    elif stat.S_ISREG(metadata.st_mode):
                        subtotal += metadata.st_size
                    else:
                        raise SkillError("ISOLATION_FAILED", "运行时包含不可计量的特殊文件。")
        except OSError as exc:
            raise SkillError("ISOLATION_FAILED", "无法完整计算运行时容量；核验已停止。") from exc
        return subtotal

    return visit(root)


def default_runtime_root() -> Path:
    override = os.environ.get("EBOOK_TO_KINDLE_RUNTIME")
    if override:
        return lexical_absolute(Path(override))
    return lexical_absolute(Path.home() / ".local" / "share" / "ebook-to-kindle")


def lexical_absolute(path: Path) -> Path:
    """Normalize a runtime path without following symbolic links."""
    return Path(os.path.abspath(os.fspath(path.expanduser())))


def contained_regular_file(path: Path, boundary: Path, *, reject_symlinks: bool = True) -> bool:
    """Require a regular file to stay inside a lexical boundary.

    Model and lock files are immutable contract inputs, so unlike an internal
    venv launcher they may not use symlinks at any nested component.
    """
    candidate = lexical_absolute(path)
    root = lexical_absolute(boundary)
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        return False
    if reject_symlinks:
        current = root
        if current.is_symlink():
            return False
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                return False
    try:
        resolved_root = root.resolve(strict=True)
        resolved_candidate = candidate.resolve(strict=True)
    except OSError:
        return False
    return resolved_candidate.is_file() and (
        resolved_candidate == resolved_root or resolved_root in resolved_candidate.parents
    )


def guard_runtime_root(path: Path) -> Path:
    """Reject a runtime path when any existing component is a symlink."""
    value = lexical_absolute(path)
    for component in (value, *value.parents):
        if component.is_symlink():
            raise SkillError("ISOLATION_FAILED", "运行时路径不能包含符号链接。")
    expected_types = {
        "runtime.json": "file",
        "core": "dir",
        "pdf": "dir",
        "jobs": "dir",
        "packages": "dir",
        RUNTIME_LOCK_NAME: "file",
    }
    for name, expected_type in expected_types.items():
        item = value / name
        if item.is_symlink():
            raise SkillError("ISOLATION_FAILED", "运行时顶层条目不能是符号链接。")
        if item.exists() and ((expected_type == "file" and not item.is_file()) or (expected_type == "dir" and not item.is_dir())):
            raise SkillError("ISOLATION_FAILED", "运行时顶层条目类型异常。")
    return value


@contextmanager
def runtime_operation_lock(root: Path) -> Iterator[Path]:
    """Hold a kernel-released single-operation lock on a fixed private file."""
    root = guard_runtime_root(root)
    if not root.is_dir():
        raise SkillError("DEPENDENCY_MISSING", "私有运行时目录不存在。")
    lock_path = root / RUNTIME_LOCK_NAME
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise SkillError("ISOLATION_FAILED", "无法安全打开私有运行时锁。") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
            or metadata.st_size != 0
        ):
            raise SkillError("ISOLATION_FAILED", "私有运行时锁身份或类型异常。")
        os.fchmod(descriptor, 0o600)
    except (OSError, SkillError) as exc:
        try:
            os.close(descriptor)
        except OSError:
            pass
        if isinstance(exc, SkillError):
            raise
        raise SkillError("ISOLATION_FAILED", "无法设置私有运行时锁权限。") from exc
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SkillError("RUNTIME_BUSY", "另一项运行时操作正在进行；请稍后重试。") from exc
        except OSError as exc:
            raise SkillError("ISOLATION_FAILED", "无法取得私有运行时锁。") from exc
        try:
            yield lock_path
        finally:
            # Closing the descriptor below releases the lock even after a crash.
            # The fixed zero-byte lock file remains, so cleanup cannot turn a
            # successfully committed operation into a reported failure.
            pass
    finally:
        try:
            os.close(descriptor)
        except OSError:
            # Process exit is the final kernel backstop. A release error must
            # not rewrite the truth after a book/runtime was committed.
            pass


def runtime_paths(root: Path) -> Dict[str, Path]:
    return {
        "root": root,
        "manifest": root / "runtime.json",
        "ebook_convert": root / "core" / "Calibre.app" / "Contents" / "MacOS" / "ebook-convert",
        "calibre_debug": root / "core" / "Calibre.app" / "Contents" / "MacOS" / "calibre-debug",
        "java": root / "core" / "jre" / "Contents" / "Home" / "bin" / "java",
        "epubcheck_jar": root / "core" / "epubcheck" / "epubcheck.jar",
        "pdf_python": root / "pdf" / "venv" / "bin" / "python",
        "docling": root / "pdf" / "venv" / "bin" / "docling",
        "models": root / "pdf" / "models",
        "jobs": root / "jobs",
        "packages": root / "packages",
    }


def load_manifest(root: Path) -> Dict[str, Any]:
    path = runtime_paths(root)["manifest"]
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def readiness(root: Path) -> Dict[str, Any]:
    root = guard_runtime_root(root)
    paths = runtime_paths(root)
    manifest = load_manifest(root)
    root_resolved = root.resolve()

    def contained(path: Path, kind: str) -> bool:
        try:
            resolved = path.resolve(strict=True)
        except OSError:
            return False
        inside = resolved == root_resolved or root_resolved in resolved.parents
        return inside and ((kind == "file" and resolved.is_file()) or (kind == "dir" and resolved.is_dir()))

    core = all(contained(paths[key], "file") for key in ("ebook_convert", "java", "epubcheck_jar"))
    pdf = core and all(contained(paths[key], "file") for key in ("pdf_python", "docling")) and contained(paths["models"], "dir")
    return {
        "core_ready": core,
        "pdf_ready": pdf,
        "runtime_name": manifest.get("runtime_name"),
        "runtime_schema": manifest.get("schema_version"),
        "runtime_version": manifest.get("runtime_version"),
        "components": manifest.get("components", {}),
        "persistent_bytes": tree_bytes(root),
    }


def _package_lines(path: Path) -> List[str]:
    if not path.is_file():
        return []
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
        if line.strip() and not line.startswith("#") and not line.startswith("Using Python ")
    ]


def _version_check(
    command: Sequence[str],
    expected: str,
    *,
    env: Mapping[str, str],
    cwd: Path,
) -> Dict[str, Any]:
    try:
        result = subprocess.run(
            offline_subprocess_command(list(command)),
            env=dict(env),
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": type(exc).__name__, "expected": expected}
    combined = f"{result.stdout}\n{result.stderr}"
    matched = False
    if expected.startswith("{"):
        try:
            expected_payload = json.loads(expected)
        except json.JSONDecodeError:
            expected_payload = None
        if expected_payload is not None:
            for line in combined.splitlines():
                try:
                    if json.loads(line) == expected_payload:
                        matched = True
                        break
                except json.JSONDecodeError:
                    continue
    else:
        matched = re.search(
            rf"(?<![A-Za-z0-9.+-])v?{re.escape(expected)}(?![A-Za-z0-9.+-])",
            combined,
        ) is not None
    return {"ok": result.returncode == 0 and matched, "return_code": result.returncode, "expected": expected}


def _doctor_job_hygiene(job: Path, jobs_dir: Path, preexisting_jobs: int) -> Dict[str, Any]:
    cleanup_ok = True
    try:
        shutil.rmtree(job)
    except OSError:
        cleanup_ok = False
    remaining_jobs = sum(1 for _ in jobs_dir.iterdir()) if jobs_dir.is_dir() else 0
    return {
        "ok": preexisting_jobs == 0 and cleanup_ok and remaining_jobs == 0,
        "preexisting_entries": preexisting_jobs,
        "remaining_entries": remaining_jobs,
    }


def verify_runtime(
    root: Path,
    skill_root: Optional[Path] = None,
    *,
    require_pdf: bool = True,
) -> Dict[str, Any]:
    """Run the exact runtime contract needed by the current operation.

    Deep doctor keeps ``require_pdf=True``. A non-PDF conversion can request
    the core-only scope, avoiding hundreds of MiB of unrelated model hashes.
    """
    root = guard_runtime_root(root)
    skill_root = skill_root or Path(__file__).resolve().parents[2]
    status = readiness(root)
    paths = runtime_paths(root)
    manifest = load_manifest(root)
    components = manifest.get("components", {}) if isinstance(manifest.get("components"), dict) else {}
    checks: Dict[str, Dict[str, Any]] = {}
    jobs_dir = paths["jobs"]
    preexisting_jobs = sum(1 for _ in jobs_dir.iterdir()) if jobs_dir.is_dir() else 0
    calibre_state_before = snapshot_calibre_user_state()

    checks["manifest"] = {
        "ok": manifest.get("schema_version") == RUNTIME_SCHEMA_VERSION
        and manifest.get("runtime_version") == RUNTIME_VERSION
        and manifest.get("runtime_name") == RUNTIME_NAME,
        "expected_name": RUNTIME_NAME,
        "expected_schema": RUNTIME_SCHEMA_VERSION,
        "expected_version": RUNTIME_VERSION,
    }
    checks["capacity"] = {
        "ok": int(status["persistent_bytes"]) <= MAX_PERSISTENT_BYTES,
        "bytes": int(status["persistent_bytes"]),
        "limit_bytes": MAX_PERSISTENT_BYTES,
    }
    checks["core_manifest"] = {"ok": components.get("core") == CORE_COMPONENT}

    job = None
    if status["core_ready"]:
        job = make_private_job_dir(paths["jobs"])
        try:
            env = build_job_env(job, root)
            checks["calibre_version"] = _version_check(
                [str(paths["ebook_convert"]), "--version"], CORE_COMPONENT["calibre"]["version"], env=env, cwd=job
            )
            checks["java_version"] = _version_check(
                [str(paths["java"]), "-version"], "Temurin-21.0.12+8", env=env, cwd=job
            )
            checks["epubcheck_version"] = _version_check(
                [str(paths["java"]), "-jar", str(paths["epubcheck_jar"]), "--version"],
                CORE_COMPONENT["epubcheck"]["version"],
                env=env,
                cwd=job,
            )

            pdf_present = require_pdf and ((root / "pdf").exists() or "pdf" in components)
            if pdf_present:
                pdf_component = components.get("pdf", {}) if isinstance(components.get("pdf"), dict) else {}
                source_lock = skill_root / "assets" / "docling-requirements.lock"
                runtime_lock = root / "pdf" / "requirements.lock"
                source_lock_safe = contained_regular_file(source_lock, skill_root)
                runtime_lock_safe = contained_regular_file(runtime_lock, root / "pdf")
                source_lock_sha = sha256_file(source_lock) if source_lock_safe else ""
                runtime_lock_sha = sha256_file(runtime_lock) if runtime_lock_safe else ""
                manifest_ok = (
                    pdf_component.get("python") == PDF_PYTHON_ASSET
                    and pdf_component.get("docling") == PDF_VERSIONS["docling"]
                    and pdf_component.get("ocrmac") == PDF_VERSIONS["ocrmac"]
                    and pdf_component.get("requirements_lock_sha256") == source_lock_sha
                    and pdf_component.get("runtime_freeze_sha256") == runtime_lock_sha
                    and pdf_component.get("model_hashes") == MODEL_HASHES
                    and pdf_component.get("model_files") == MODEL_FILES
                    and pdf_component.get("verified_model_files") == MODEL_FILES
                )
                checks["pdf_manifest"] = {"ok": manifest_ok}
                checks["docling_dependency_lock"] = {
                    "ok": source_lock_safe
                    and runtime_lock_safe
                    and bool(_package_lines(source_lock))
                    and _package_lines(source_lock) == _package_lines(runtime_lock),
                    "package_count": len(_package_lines(source_lock)) if source_lock_safe else 0,
                }
                model_results = {}
                for relative, expected_hash in MODEL_FILES.items():
                    model = paths["models"] / relative
                    model_results[relative] = contained_regular_file(model, paths["models"]) and sha256_file(model) == expected_hash
                checks["model_hashes"] = {
                    "ok": len(model_results) == len(MODEL_FILES) and all(model_results.values()),
                    "checked": len(model_results),
                }
                package_probe = (
                    "import importlib.metadata as m,json,platform;"
                    "print(json.dumps({'python':platform.python_version(),'docling':m.version('docling'),"
                    "'ocrmac':m.version('ocrmac'),'pypdfium2':m.version('pypdfium2')},sort_keys=True))"
                )
                expected_payload = json.dumps(PDF_VERSIONS, sort_keys=True)
                checks["pdf_versions"] = _version_check(
                    [str(paths["pdf_python"]), "-I", "-c", package_probe], expected_payload, env=env, cwd=job
                )
            elif require_pdf:
                checks["pdf_absent"] = {"ok": True}
            else:
                checks["pdf_skipped"] = {"ok": True, "reason": "not-required-for-input"}
        finally:
            checks["jobs_hygiene"] = _doctor_job_hygiene(job, jobs_dir, preexisting_jobs)
    else:
        checks["core_files"] = {"ok": False}
        checks["jobs_hygiene"] = {"ok": preexisting_jobs == 0, "preexisting_entries": preexisting_jobs}
    calibre_state_unchanged = snapshot_calibre_user_state() == calibre_state_before
    checks["calibre_user_state"] = {
        "ok": calibre_state_unchanged,
        "unchanged": calibre_state_unchanged,
    }

    core_keys = (
        "manifest",
        "capacity",
        "core_manifest",
        "calibre_version",
        "java_version",
        "epubcheck_version",
        "calibre_user_state",
        "jobs_hygiene",
    )
    core_verified = status["core_ready"] and all(checks.get(key, {}).get("ok", False) for key in core_keys)
    pdf_present = (root / "pdf").exists() or "pdf" in components
    pdf_keys = ("pdf_manifest", "docling_dependency_lock", "model_hashes", "pdf_versions")
    pdf_verified = bool(require_pdf and status["pdf_ready"] and all(checks.get(key, {}).get("ok", False) for key in pdf_keys))
    status["deep_checks"] = checks
    status["core_verified"] = core_verified
    status["pdf_verified"] = pdf_verified
    status["verification_scope"] = "full" if require_pdf else "core"
    status["deep_ready"] = core_verified and (not require_pdf or not pdf_present or pdf_verified)
    return status
