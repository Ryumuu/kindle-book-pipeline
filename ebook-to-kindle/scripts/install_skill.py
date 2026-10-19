#!/usr/bin/env python3
"""Install a clean copy of this Skill into the local .agents skill root."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import stat
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator, List, Optional


SKILL_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TARGET = Path.home() / ".agents" / "skills" / "ebook-to-kindle"
ALLOWED_PARENT = Path.home() / ".agents" / "skills"
# Other common agent skill roots (Claude Code, Codex). --target may point to any of them.
EXTRA_ALLOWED_PARENTS = (Path.home() / ".claude" / "skills", Path.home() / ".codex" / "skills")
ALLOWED_HOME = Path.home()
SCRIPT_INTERFACE = "cli"
SCRIPT_INTERFACE_REASON = "Local installer with atomic staging and rollback backup."
INSTALL_LOCK_NAME = ".ebook-to-kindle.install.lock"


class InstallError(RuntimeError):
    def __init__(self, code: str, details: Optional[Dict[str, object]] = None) -> None:
        super().__init__(code)
        self.code = code
        self.details = dict(details or {})


def ignored(directory: str, names: List[str]) -> List[str]:
    current = Path(directory).resolve()
    try:
        relative = current.relative_to(SKILL_ROOT)
    except ValueError:
        relative = Path(".")
    result = [name for name in names if name in {"__pycache__", ".DS_Store"} or name.endswith((".pyc", ".pyo"))]
    if relative == Path("evals") and "results" in names:
        result.append("results")
    return result


def private_permissions(root: Path) -> None:
    for path in [root, *root.rglob("*")]:
        if path.is_symlink():
            continue
        if path.is_dir():
            path.chmod(0o700)
        elif path.is_file():
            executable = path.suffix == ".py" and path.parent.name == "scripts"
            path.chmod(0o700 if executable else 0o600)


def lexical_absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path.expanduser())))


def guarded_target(value: Path) -> Path:
    target = lexical_absolute(value)
    allowed_parents = {lexical_absolute(parent) for parent in (ALLOWED_PARENT, *EXTRA_ALLOWED_PARENTS)}
    allowed_home = lexical_absolute(ALLOWED_HOME)
    if target.parent not in allowed_parents or target.name != "ebook-to-kindle":
        raise RuntimeError("UNSAFE_TARGET")
    cursor = target
    while True:
        if cursor.is_symlink():
            raise RuntimeError("UNSAFE_TARGET")
        if cursor == allowed_home:
            break
        if cursor.parent == cursor:
            raise RuntimeError("UNSAFE_TARGET")
        cursor = cursor.parent
    return target


def has_skill_identity(target: Path) -> bool:
    skill_file = target / "SKILL.md"
    if skill_file.is_symlink() or not skill_file.is_file():
        return False
    lines = skill_file.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != "---":
        return False
    try:
        end = next(index for index, line in enumerate(lines[1:], 1) if line.strip() == "---")
    except StopIteration:
        return False
    names = []
    for line in lines[1:end]:
        if line.startswith((" ", "\t", "#")) or ":" not in line:
            continue
        key, value = line.split(":", 1)
        if key.strip() == "name":
            names.append(value.strip().strip("\"'"))
    return names == ["ebook-to-kindle"]


def _close_noexcept(descriptor: int) -> None:
    try:
        os.close(descriptor)
    except OSError:
        pass


@contextmanager
def installation_lock(parent: Path) -> Iterator[Path]:
    lock = parent / INSTALL_LOCK_NAME
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock, flags, 0o600)
    except OSError as exc:
        raise InstallError("INSTALL_FAILED") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
            or metadata.st_size != 0
        ):
            raise InstallError("UNSAFE_TARGET")
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise InstallError("INSTALL_BUSY") from exc
        yield lock
    finally:
        _close_noexcept(descriptor)


def _remove_staging_strict(staging: Path) -> None:
    if not os.path.lexists(staging):
        return
    if staging.is_symlink() or not staging.is_dir():
        raise InstallError("ROLLBACK_INCOMPLETE", {"recovery_entries": [staging.name]})
    try:
        shutil.rmtree(staging)
    except OSError as exc:
        raise InstallError("ROLLBACK_INCOMPLETE", {"recovery_entries": [staging.name]}) from exc
    if os.path.lexists(staging):
        raise InstallError("ROLLBACK_INCOMPLETE", {"recovery_entries": [staging.name]})


def _restore_backup(target: Path, backup: Path) -> None:
    if target.exists() or target.is_symlink():
        raise InstallError(
            "ROLLBACK_INCOMPLETE",
            {"recovery_entries": sorted({target.name, backup.name})},
        )
    try:
        os.replace(str(backup), str(target))
    except OSError as exc:
        raise InstallError("ROLLBACK_INCOMPLETE", {"recovery_entries": [backup.name]}) from exc
    if not target.exists() or backup.exists():
        raise InstallError("ROLLBACK_INCOMPLETE", {"recovery_entries": [backup.name]})


def install(target: Path, replace: bool) -> dict:
    target = guarded_target(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target = guarded_target(target)
    with installation_lock(target.parent):
        target = guarded_target(target)
        staging = target.parent / f".{target.name}.installing-{uuid.uuid4().hex}"
        backup: Optional[Path] = None
        try:
            shutil.copytree(SKILL_ROOT, staging, ignore=ignored, symlinks=False)
            private_permissions(staging)
            if target.exists() or target.is_symlink():
                if not replace:
                    raise InstallError("TARGET_EXISTS")
                if not has_skill_identity(target):
                    raise InstallError("TARGET_IDENTITY_MISMATCH")
                backup = target.parent / f".{target.name}.backup-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
                os.replace(str(target), str(backup))
            os.replace(str(staging), str(target))
            return {
                "status": "PASS",
                "installed": target.name,
                "backup": backup.name if backup else None,
                "results_excluded": True,
            }
        except Exception as original:
            recovery_entries: List[str] = []
            try:
                _remove_staging_strict(staging)
            except InstallError as cleanup_error:
                recovery_entries.extend(str(item) for item in cleanup_error.details.get("recovery_entries", [staging.name]))
            if backup is not None and backup.exists():
                try:
                    _restore_backup(target, backup)
                except InstallError as restore_error:
                    recovery_entries.extend(str(item) for item in restore_error.details.get("recovery_entries", [backup.name]))
            if recovery_entries:
                raise InstallError(
                    "ROLLBACK_INCOMPLETE",
                    {"recovery_entries": sorted(set(recovery_entries))},
                ) from original
            raise


def main() -> int:
    parser = argparse.ArgumentParser(description="Install ebook-to-kindle into ~/.agents/skills (default), ~/.claude/skills or ~/.codex/skills.")
    parser.add_argument("--target", type=Path, default=DEFAULT_TARGET)
    parser.add_argument("--replace", action="store_true", help="Back up an existing target before replacement.")
    args = parser.parse_args()
    try:
        print(json.dumps(install(args.target, args.replace), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except InstallError as exc:
        payload = {"status": "FAIL", "error_code": exc.code, **exc.details}
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 3 if exc.code == "ROLLBACK_INCOMPLETE" else 2
    except RuntimeError as exc:
        code = str(exc)
        print(json.dumps({"status": "FAIL", "error_code": code}, ensure_ascii=False))
        return 2
    except OSError as exc:
        print(json.dumps({"status": "FAIL", "error_code": "INSTALL_FAILED", "message": type(exc).__name__}, ensure_ascii=False))
        return 1
    except Exception:
        print(json.dumps({"status": "FAIL", "error_code": "INSTALL_FAILED"}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
