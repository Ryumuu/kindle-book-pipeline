#!/usr/bin/env python3
"""Verify known synthetic marker order after a real Kindle text roundtrip."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import unicodedata
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Mapping, Optional


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from ebook_kindle.core import (  # noqa: E402
    SkillError,
    build_job_env,
    make_private_job_dir,
    offline_subprocess_command,
    snapshot_calibre_user_state,
)
from ebook_kindle.runtime import (  # noqa: E402
    default_runtime_root,
    guard_runtime_root,
    runtime_operation_lock,
    runtime_paths,
    verify_runtime,
)


def normalize(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value))


def lexical_absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path.expanduser())))


def _reject_symlink_components(path: Path) -> None:
    for component in (path, *path.parents):
        if component.is_symlink():
            raise SkillError("UNSAFE_OUTPUT", "验收路径不能包含符号链接。")


def _path_alias(left: Path, right: Path) -> bool:
    left = lexical_absolute(left)
    right = lexical_absolute(right)
    if unicodedata.normalize("NFC", str(left)).casefold() == unicodedata.normalize("NFC", str(right)).casefold():
        return True
    if os.path.lexists(left) and os.path.lexists(right):
        try:
            return os.path.samefile(left, right)
        except OSError:
            return False
    return False


def _normalized_path_key(path: Path) -> str:
    return unicodedata.normalize("NFC", str(lexical_absolute(path))).casefold().rstrip(os.sep)


def _inside_runtime_alias(path: Path, root: Path) -> bool:
    candidate_key = _normalized_path_key(path)
    root_key = _normalized_path_key(root)
    if candidate_key == root_key or candidate_key.startswith(root_key + os.sep):
        return True
    cursor = path if path.exists() else path.parent
    while True:
        if cursor.exists():
            try:
                if os.path.samefile(cursor, root):
                    return True
            except OSError:
                pass
        if cursor == cursor.parent:
            return False
        cursor = cursor.parent


def prepare_verification_paths(args: argparse.Namespace) -> tuple[Path, Path]:
    """Validate every read/write alias and duplicate key before a job exists."""
    root = guard_runtime_root(args.runtime)
    truth = lexical_absolute(args.truth)
    _reject_symlink_components(truth)
    if not truth.is_file() or truth.is_symlink():
        raise SkillError("VERIFICATION_FAILED", "验收真值文件无效。")

    try:
        truth_payload = json.loads(truth.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SkillError("VERIFICATION_FAILED", "验收真值文件无法解析。") from exc
    normalized_items = []
    keys = set()
    ebooks = []
    for kind, output_format, ebook_value in args.item:
        if ":" in kind or ":" in output_format:
            raise SkillError("DUPLICATE_ITEM", "验收项目键不能包含冒号。")
        key = (
            unicodedata.normalize("NFC", str(kind)).casefold(),
            unicodedata.normalize("NFC", str(output_format)).casefold(),
        )
        if key in keys:
            raise SkillError("DUPLICATE_ITEM", "验收项目键重复。")
        keys.add(key)
        if (
            kind not in truth_payload
            or not isinstance(truth_payload.get(kind), dict)
            or "ordered_markers" not in truth_payload[kind]
            or output_format not in {"azw3", "mobi6", "epub"}
        ):
            raise SkillError("VERIFICATION_FAILED", "验收项目类型或格式不在固定真值合同内。")
        ebook = lexical_absolute(Path(ebook_value))
        _reject_symlink_components(ebook)
        if not ebook.is_file() or ebook.is_symlink():
            raise SkillError("VERIFICATION_FAILED", "验收电子书文件无效。")
        ebooks.append(ebook)
        normalized_items.append((kind, output_format, str(ebook)))

    output_path = lexical_absolute(args.output)
    _reject_symlink_components(output_path)
    if _inside_runtime_alias(output_path, root):
        raise SkillError("UNSAFE_OUTPUT", "验收报告不能写入运行时目录。")
    protected = [truth, *ebooks, root / "runtime.json"]
    if output_path.suffix.lower() != ".json" or any(_path_alias(output_path, path) for path in protected):
        raise SkillError("UNSAFE_OUTPUT", "验收报告不能覆盖真值、输入或运行时文件。")
    if output_path.exists():
        metadata = output_path.stat()
        if output_path.is_symlink() or not output_path.is_file() or metadata.st_nlink != 1:
            raise SkillError("UNSAFE_OUTPUT", "验收报告目标类型或链接数无效。")
    if output_path.exists() and not args.overwrite:
        raise SkillError("OUTPUT_EXISTS", "验收报告已存在。")

    args.truth = truth
    args.item = normalized_items
    return output_path, root


def _entry_stat(directory_fd: int, name: str) -> Optional[os.stat_result]:
    try:
        return os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _assert_output_anchor(anchor: Mapping[str, object]) -> None:
    parent = anchor["parent"]
    assert isinstance(parent, Path)
    try:
        current = os.stat(parent, follow_symlinks=False)
    except OSError as exc:
        raise SkillError("UNSAFE_OUTPUT", "验收期间报告目录身份发生变化。") from exc
    if (
        not stat.S_ISDIR(current.st_mode)
        or current.st_dev != anchor["device"]
        or current.st_ino != anchor["inode"]
    ):
        raise SkillError("UNSAFE_OUTPUT", "验收期间报告目录身份发生变化。")
    for protected, device, inode in anchor["protected"]:
        try:
            metadata = os.stat(protected, follow_symlinks=False)
        except OSError as exc:
            raise SkillError("UNSAFE_OUTPUT", "验收期间真值或输入身份发生变化。") from exc
        if metadata.st_dev != device or metadata.st_ino != inode or not stat.S_ISREG(metadata.st_mode):
            raise SkillError("UNSAFE_OUTPUT", "验收期间真值或输入身份发生变化。")


@contextmanager
def verification_output_anchor(
    output_path: Path,
    protected_paths: list[Path],
    overwrite: bool,
) -> Iterator[dict]:
    parent = output_path.parent
    _reject_symlink_components(parent)
    if parent.exists():
        if not parent.is_dir():
            raise SkillError("UNSAFE_OUTPUT", "验收报告父路径不是目录。")
    else:
        parent.mkdir(parents=True, mode=0o700)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        directory_fd = os.open(parent, flags)
    except OSError as exc:
        raise SkillError("UNSAFE_OUTPUT", "无法安全打开验收报告目录。") from exc
    try:
        metadata = os.fstat(directory_fd)
        protected = []
        for path in protected_paths:
            item = os.stat(path, follow_symlinks=False)
            protected.append((path, item.st_dev, item.st_ino))
        anchor = {
            "fd": directory_fd,
            "parent": parent,
            "device": metadata.st_dev,
            "inode": metadata.st_ino,
            "protected": protected,
            "overwrite": overwrite,
        }
        _assert_output_anchor(anchor)
        existing = _entry_stat(directory_fd, output_path.name)
        if existing is not None and (not stat.S_ISREG(existing.st_mode) or existing.st_nlink != 1):
            raise SkillError("UNSAFE_OUTPUT", "验收报告目标类型或链接数无效。")
        if existing is not None and not overwrite:
            raise SkillError("OUTPUT_EXISTS", "验收报告已存在。")
        yield anchor
    finally:
        try:
            os.close(directory_fd)
        except OSError:
            pass


def publish_verification_report(
    anchor: Mapping[str, object], output_path: Path, payload: Mapping[str, object]
) -> dict:
    _assert_output_anchor(anchor)
    directory_fd = int(anchor["fd"])
    destination = output_path.name
    existing = _entry_stat(directory_fd, destination)
    if existing is not None and (not stat.S_ISREG(existing.st_mode) or existing.st_nlink != 1):
        raise SkillError("UNSAFE_OUTPUT", "验收报告目标类型或链接数无效。")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)

    def payload_with_cleanup(status: str, pending_files: list[str]) -> dict:
        value = dict(payload)
        value["publication_cleanup"] = {
            "status": status,
            "pending_files": sorted(set(pending_files)),
        }
        return value

    def create_json_stage(value: Mapping[str, object], stage_name: Optional[str] = None) -> str:
        stage_name = stage_name or f".{destination}.stage-{uuid.uuid4().hex}"
        descriptor = os.open(stage_name, flags, 0o600, dir_fd=directory_fd)
        try:
            encoded = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
            with os.fdopen(descriptor, "wb", closefd=False) as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.fchmod(descriptor, 0o600)
            return stage_name
        except Exception:
            try:
                os.unlink(stage_name, dir_fd=directory_fd)
            except OSError:
                pass
            raise
        finally:
            try:
                os.close(descriptor)
            except OSError:
                pass

    stage = f".{destination}.stage-{uuid.uuid4().hex}"
    pending = payload_with_cleanup("pending", [stage] if not bool(anchor["overwrite"]) else [])
    create_json_stage(pending, stage)
    try:
        _assert_output_anchor(anchor)
        if bool(anchor["overwrite"]):
            os.replace(stage, destination, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        else:
            try:
                os.link(
                    stage,
                    destination,
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            except FileExistsError as exc:
                raise SkillError("OUTPUT_EXISTS", "验收报告已存在。") from exc
            try:
                os.unlink(stage, dir_fd=directory_fd)
            except OSError:
                # The public report already says exactly which hidden hardlink
                # remains. A cleanup warning must not reverse a durable PASS.
                return pending
    except Exception:
        try:
            if _entry_stat(directory_fd, stage) is not None:
                os.unlink(stage, dir_fd=directory_fd)
        except OSError:
            pass
        raise

    complete = payload_with_cleanup("complete", [])
    final_stage: Optional[str] = None
    try:
        final_stage = create_json_stage(complete)
        os.replace(final_stage, destination, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        return complete
    except Exception:
        # `destination` already contains the conservative pending report.
        # Never turn that durable PASS into a contradictory CLI failure.
        return pending
    finally:
        try:
            if final_stage is not None and _entry_stat(directory_fd, final_stage) is not None:
                os.unlink(final_stage, dir_fd=directory_fd)
        except OSError:
            # A post-commit stat/unlink error cannot contradict the durable
            # complete or conservative pending report already returned.
            pass


def _verify_locked(args: argparse.Namespace, output_path: Path, root: Path, output_anchor: Mapping[str, object]) -> int:
    calibre_state_before = snapshot_calibre_user_state()
    if not verify_runtime(root, SKILL_ROOT)["core_verified"]:
        print(json.dumps({"status": "FAIL", "error_code": "DEPENDENCY_MISSING"}))
        return 3
    if snapshot_calibre_user_state() != calibre_state_before:
        raise SkillError("ISOLATION_FAILED", "运行时核验改变了 Calibre 用户状态。")
    truth = json.loads(args.truth.read_text(encoding="utf-8"))
    paths = runtime_paths(root)
    job = make_private_job_dir(paths["jobs"])
    results = {
        "schema_version": 1,
        "fixture_version": truth["fixture_version"],
        "calibre_user_state_unchanged": True,
        "items": {},
    }
    try:
        env = build_job_env(job, root)
        for index, (kind, output_format, ebook_value) in enumerate(args.item):
            ebook = Path(ebook_value).expanduser().resolve()
            text_path = job / f"roundtrip-{index}.txt"
            try:
                proc = subprocess.run(
                    offline_subprocess_command([str(paths["ebook_convert"]), str(ebook), str(text_path)]),
                    env=env,
                    cwd=str(job),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=300,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                results["items"][f"{kind}:{output_format}"] = {"pass": False, "error_code": "ROUNDTRIP_FAILED"}
                continue
            if proc.returncode != 0 or not text_path.is_file():
                results["items"][f"{kind}:{output_format}"] = {"pass": False, "error_code": "ROUNDTRIP_FAILED"}
                continue
            raw = text_path.read_bytes()
            decoded = raw.decode("utf-8", errors="replace")
            text = normalize(decoded)
            markers = truth[kind]["ordered_markers"]
            overrides = truth[kind].get("expected_marker_counts", {})
            positions = {marker: text.find(normalize(marker)) for marker in markers}
            counts = {marker: text.count(normalize(marker)) for marker in markers}
            missing = [marker for marker in markers if positions[marker] < 0]
            count_mismatches = {
                marker: {"expected": overrides.get(marker, 1), "actual": counts[marker]}
                for marker in markers
                if counts[marker] != overrides.get(marker, 1)
            }
            position_list = [positions[marker] for marker in markers]
            ordered = not missing and position_list == sorted(position_list) and len(set(position_list)) == len(position_list)
            missing_recognition = [
                value for value in truth[kind].get("must_recognize", []) if normalize(value) not in text
            ]
            results["items"][f"{kind}:{output_format}"] = {
                "ebook": ebook.name,
                "roundtrip_text_bytes": len(raw),
                "roundtrip_text_sha256": hashlib.sha256(raw).hexdigest(),
                "marker_positions": positions,
                "marker_counts": counts,
                "missing_markers": missing,
                "count_mismatches": count_mismatches,
                "ordered": ordered,
                "missing_must_recognize": missing_recognition,
                "replacement_character": "�" in decoded,
                "pass": ordered and not count_mismatches and not missing_recognition and "�" not in decoded,
            }
    finally:
        try:
            shutil.rmtree(job)
        except OSError as exc:
            raise SkillError("ISOLATION_FAILED", "验收临时目录清理失败。") from exc
        if job.exists():
            raise SkillError("ISOLATION_FAILED", "验收临时目录仍然存在。")
    if snapshot_calibre_user_state() != calibre_state_before:
        raise SkillError("ISOLATION_FAILED", "验收回读改变了 Calibre 用户状态。")
    results["status"] = "PASS" if all(item.get("pass") for item in results["items"].values()) else "FAIL"
    results = publish_verification_report(output_anchor, output_path, results)
    print(json.dumps(results, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if results["status"] == "PASS" else 1


def _main() -> int:
    parser = argparse.ArgumentParser(description="Check synthetic fixture marker order in AZW3/MOBI outputs.")
    parser.add_argument("--truth", type=Path, default=Path(__file__).with_name("ground-truth.json"))
    parser.add_argument("--runtime", type=Path, default=default_runtime_root())
    parser.add_argument("--item", action="append", nargs=3, metavar=("KIND", "FORMAT", "EBOOK"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    output_path, root = prepare_verification_paths(args)
    protected = [args.truth, *[Path(item[2]) for item in args.item], root / "runtime.json"]
    with verification_output_anchor(output_path, protected, args.overwrite) as output_anchor:
        with runtime_operation_lock(root):
            return _verify_locked(args, output_path, root, output_anchor)


def main() -> int:
    try:
        return _main()
    except SkillError as exc:
        print(json.dumps({"status": "FAIL", "error_code": exc.code}, ensure_ascii=False))
        return 3 if exc.code == "DEPENDENCY_MISSING" else 2
    except (OSError, RuntimeError, ValueError, KeyError, json.JSONDecodeError, UnicodeError):
        print(json.dumps({"status": "FAIL", "error_code": "VERIFICATION_FAILED"}, ensure_ascii=False))
        return 2
    except Exception:
        print(json.dumps({"status": "FAIL", "error_code": "VERIFICATION_FAILED"}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
