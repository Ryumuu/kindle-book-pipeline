#!/usr/bin/env python3
"""Guarded uninstall for the exact Skill and runtime targets."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
from pathlib import Path
from typing import List, Tuple

from ebook_kindle.contract import RUNTIME_NAME, RUNTIME_SCHEMA_VERSION, RUNTIME_VERSION
from ebook_kindle.core import SkillError
from ebook_kindle.runtime import RUNTIME_LOCK_NAME, runtime_operation_lock
from install_skill import InstallError, installation_lock


CONFIRM_TOKEN = "REMOVE_EBOOK_TO_KINDLE"
DEFAULT_SKILL = Path.home() / ".agents" / "skills" / "ebook-to-kindle"
ALLOWED_SKILLS = (
    DEFAULT_SKILL,
    Path.home() / ".claude" / "skills" / "ebook-to-kindle",
    Path.home() / ".codex" / "skills" / "ebook-to-kindle",
)
DEFAULT_RUNTIME = Path.home() / ".local" / "share" / "ebook-to-kindle"
SCRIPT_INTERFACE = "cli"
SCRIPT_INTERFACE_REASON = "Dry-run-first exact-target uninstall command."
RUNTIME_ALLOWED_ENTRIES = frozenset({"runtime.json", "core", "pdf", "jobs", "packages", RUNTIME_LOCK_NAME})


def lexical_absolute(path: Path) -> Path:
    """Normalize dots without following any symbolic link."""
    return Path(os.path.abspath(os.fspath(path.expanduser())))


def guarded(path: Path, expected: Path) -> Path:
    value = lexical_absolute(path)
    expected_value = lexical_absolute(expected)
    home = lexical_absolute(Path.home())
    if value != expected_value:
        raise RuntimeError("UNSAFE_TARGET")
    try:
        value.relative_to(home)
    except ValueError as exc:
        raise RuntimeError("UNSAFE_TARGET") from exc
    cursor = value
    while True:
        if cursor.is_symlink():
            raise RuntimeError("UNSAFE_TARGET")
        if cursor == home:
            break
        cursor = cursor.parent
    if value.resolve(strict=False) != expected_value.resolve(strict=False):
        raise RuntimeError("UNSAFE_TARGET")
    return value


def tree_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    for item in path.rglob("*"):
        if item.is_file() and not item.is_symlink():
            total += item.stat().st_size
    return total


def identity_ok(kind: str, path: Path) -> bool:
    if not path.exists():
        return True
    if kind == "skill":
        skill_file = path / "SKILL.md"
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
    try:
        if path.is_symlink() or not path.is_dir():
            return False
        entries = {item.name: item for item in path.iterdir()}
        if not set(entries).issubset(RUNTIME_ALLOWED_ENTRIES):
            return False
        expected_types = {
            "runtime.json": "file",
            "core": "dir",
            "pdf": "dir",
            "jobs": "dir",
            "packages": "dir",
            RUNTIME_LOCK_NAME: "file",
        }
        for name, item in entries.items():
            metadata = os.lstat(item)
            if stat.S_ISLNK(metadata.st_mode) or metadata.st_uid != os.getuid():
                return False
            expected = expected_types[name]
            if expected == "file" and not stat.S_ISREG(metadata.st_mode):
                return False
            if expected == "dir" and not stat.S_ISDIR(metadata.st_mode):
                return False
            if expected == "file" and metadata.st_nlink != 1:
                return False
            if name == RUNTIME_LOCK_NAME and metadata.st_size != 0:
                return False
        manifest = path / "runtime.json"
        if "runtime.json" not in entries:
            return False
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return False
        components = payload.get("components")
        if not isinstance(components, dict) or not set(components).issubset({"core", "pdf"}):
            return False
        if any(not isinstance(value, dict) for value in components.values()):
            return False
        for component in ("core", "pdf"):
            if ((path / component).is_dir()) != (component in components):
                return False
        if "rollback_shell" in payload and payload.get("rollback_shell") is not True:
            return False
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
        return False
    return (
        payload.get("runtime_name") == RUNTIME_NAME
        and payload.get("runtime_version") == RUNTIME_VERSION
        and payload.get("schema_version") == RUNTIME_SCHEMA_VERSION
    )


def describe(targets: List[Tuple[str, Path]]) -> dict:
    return {
        "status": "DRY_RUN",
        "targets": [
            {
                "kind": kind,
                "path": str(path),
                "exists": path.exists(),
                "identity_ok": identity_ok(kind, path),
                "reclaim_bytes": tree_bytes(path),
            }
            for kind, path in targets
        ],
        "confirmation_required": CONFIRM_TOKEN,
        "recoverable": False,
    }


def remove_exact(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def execute_removal(targets: List[Tuple[str, Path]]) -> Tuple[dict, int]:
    removed: List[str] = []
    remaining: List[str] = []
    for kind, path in targets:
        existed_before = os.path.lexists(path)
        try:
            if existed_before and not identity_ok(kind, path):
                remaining.append(kind)
                return {
                    "status": "FAIL",
                    "error_code": "TARGET_IDENTITY_MISMATCH",
                    "partial": bool(removed),
                    "partial_state": "confirmed" if removed else "none-observed",
                    "removed_kinds": removed,
                    "remaining_kinds": sorted(set(remaining + [next_kind for next_kind, _ in targets if next_kind not in removed])),
                }, 3 if removed else 2
            remove_exact(path)
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
            remaining.append(kind)
            remaining.extend(next_kind for next_kind, _ in targets if next_kind not in removed and next_kind != kind)
            return {
                "status": "FAIL",
                "error_code": "UNINSTALL_FAILED",
                "partial": bool(removed) or existed_before,
                "partial_state": "possible" if existed_before else "none-observed",
                "removed_kinds": removed,
                "remaining_kinds": sorted(set(remaining)),
            }, 3
        if os.path.lexists(path):
            remaining.append(kind)
            return {
                "status": "FAIL",
                "error_code": "UNINSTALL_FAILED",
                "partial": bool(removed) or existed_before,
                "partial_state": "possible" if existed_before else "none-observed",
                "removed_kinds": removed,
                "remaining_kinds": remaining,
            }, 3
        removed.append(kind)
    return {
        "status": "REMOVED",
        "targets": [str(path) for _, path in targets],
        "recoverable": False,
    }, 0


def execute_removal_locked(targets: List[Tuple[str, Path]], runtime_target: Path) -> Tuple[dict, int]:
    """Prevent uninstall from racing the Skill installer or any runtime operation."""
    skill_targets = [path for kind, path in targets if kind == "skill"]

    def remove_under_runtime_lock() -> Tuple[dict, int]:
        if not runtime_target.exists():
            return execute_removal(targets)
        if not identity_ok("runtime", runtime_target):
            return {
                "status": "FAIL",
                "error_code": "TARGET_IDENTITY_MISMATCH",
                "target_kinds": ["runtime"],
            }, 2
        with runtime_operation_lock(runtime_target):
            invalid = [kind for kind, path in targets if path.exists() and not identity_ok(kind, path)]
            if invalid:
                return {
                    "status": "FAIL",
                    "error_code": "TARGET_IDENTITY_MISMATCH",
                    "target_kinds": sorted(set(invalid)),
                    "partial": False,
                }, 2
            return execute_removal(targets)

    try:
        if skill_targets:
            with installation_lock(skill_targets[0].parent):
                return remove_under_runtime_lock()
        return remove_under_runtime_lock()
    except InstallError as exc:
        return {
            "status": "FAIL",
            "error_code": exc.code,
            "partial": False,
        }, 4 if exc.code == "INSTALL_BUSY" else 3
    except SkillError as exc:
        return {
            "status": "FAIL",
            "error_code": exc.code,
            "partial": False,
        }, 4 if exc.code == "RUNTIME_BUSY" else 3


def _main() -> int:
    parser = argparse.ArgumentParser(description="Dry-run by default; permanently remove only exact ebook-to-kindle targets.")
    parser.add_argument("--skill-target", type=Path, default=DEFAULT_SKILL)
    parser.add_argument("--runtime", type=Path, default=DEFAULT_RUNTIME)
    parser.add_argument("--keep-skill", action="store_true")
    parser.add_argument("--keep-runtime", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--confirm")
    args = parser.parse_args()
    runtime_target = guarded(args.runtime, DEFAULT_RUNTIME)
    targets: List[Tuple[str, Path]] = []
    if not args.keep_skill:
        requested = lexical_absolute(args.skill_target)
        expected = next((path for path in ALLOWED_SKILLS if lexical_absolute(path) == requested), DEFAULT_SKILL)
        targets.append(("skill", guarded(args.skill_target, expected)))
    if not args.keep_runtime:
        targets.append(("runtime", runtime_target))
    plan = describe(targets)
    if args.dry_run or args.confirm != CONFIRM_TOKEN:
        print(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    invalid = [item for item in plan["targets"] if item["exists"] and not item["identity_ok"]]
    if invalid:
        print(json.dumps({
            "status": "FAIL",
            "error_code": "TARGET_IDENTITY_MISMATCH",
            "target_kinds": [item["kind"] for item in invalid],
        }, ensure_ascii=False, indent=2, sort_keys=True))
        return 2
    result, code = execute_removal_locked(targets, runtime_target)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return code


def main() -> int:
    try:
        return _main()
    except RuntimeError:
        print(json.dumps({"status": "FAIL", "error_code": "UNSAFE_TARGET"}, ensure_ascii=False))
        return 2
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        print(json.dumps({"status": "FAIL", "error_code": "UNINSTALL_FAILED", "partial": True, "partial_state": "possible"}, ensure_ascii=False))
        return 3
    except Exception:
        print(json.dumps({"status": "FAIL", "error_code": "UNINSTALL_FAILED", "partial": True, "partial_state": "possible"}, ensure_ascii=False))
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
