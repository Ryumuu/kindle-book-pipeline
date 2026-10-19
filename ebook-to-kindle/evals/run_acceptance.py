#!/usr/bin/env python3
"""Rebuild the synthetic DOCX/PDF acceptance set with the current Skill."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import unicodedata
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator, List, Mapping, Optional


EVALS = Path(__file__).resolve().parent
SKILL_ROOT = EVALS.parent
CONVERTER = SKILL_ROOT / "scripts" / "ebook_to_kindle.py"
VERIFIER = EVALS / "verify_fixture_roundtrip.py"
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from ebook_kindle.core import SkillError  # noqa: E402
from ebook_kindle.runtime import default_runtime_root, guard_runtime_root  # noqa: E402


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def run(command: List[str]) -> None:
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode:
        print(json.dumps({"status": "FAIL", "error_code": "SUBPROCESS_FAILED", "return_code": result.returncode}, ensure_ascii=False))
        raise SystemExit(result.returncode)


def _portable_path_key(path: Path) -> str:
    value = os.path.normcase(os.path.normpath(os.path.abspath(os.fspath(path.expanduser()))))
    return unicodedata.normalize("NFC", value).casefold()


def _inside_alias(path: Path, boundary: Path) -> bool:
    candidate = Path(os.path.abspath(os.fspath(path.expanduser())))
    root = Path(os.path.abspath(os.fspath(boundary.expanduser())))
    candidate_key = _portable_path_key(candidate)
    root_key = _portable_path_key(root)
    if candidate_key == root_key or candidate_key.startswith(root_key + os.sep):
        return True
    try:
        resolved = candidate.resolve(strict=False)
        resolved_root = root.resolve(strict=False)
    except OSError:
        return False
    resolved_key = _portable_path_key(resolved)
    resolved_root_key = _portable_path_key(resolved_root)
    return resolved_key == resolved_root_key or resolved_key.startswith(resolved_root_key + os.sep)


def prepare_output_root(path: Path, overwrite: bool, runtime_root: Optional[Path] = None) -> Path:
    lexical = Path(os.path.abspath(os.fspath(path.expanduser())))
    if lexical.is_symlink():
        raise RuntimeError("UNSAFE_OUTPUT_ROOT")
    root = lexical.resolve()
    if runtime_root is not None and _inside_alias(root, runtime_root):
        raise RuntimeError("UNSAFE_OUTPUT_ROOT")
    if _inside_alias(root, SKILL_ROOT) and not _inside_alias(root, EVALS / "results"):
        raise RuntimeError("UNSAFE_OUTPUT_ROOT")
    if root.exists():
        if not root.is_dir():
            raise RuntimeError("OUTPUT_NOT_DIRECTORY")
        if any(root.iterdir()) and not overwrite:
            raise RuntimeError("OUTPUT_NOT_EMPTY")
    else:
        root.mkdir(parents=True, exist_ok=False)
    return root


def _assert_root_anchor(anchor: Mapping[str, object]) -> None:
    root = anchor["root"]
    assert isinstance(root, Path)
    try:
        metadata = os.stat(root, follow_symlinks=False)
    except OSError as exc:
        raise RuntimeError("UNSAFE_OUTPUT_ROOT") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_dev != anchor["device"]
        or metadata.st_ino != anchor["inode"]
    ):
        raise RuntimeError("UNSAFE_OUTPUT_ROOT")


@contextmanager
def acceptance_root_anchor(root: Path) -> Iterator[dict]:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(root, flags)
    except OSError as exc:
        raise RuntimeError("UNSAFE_OUTPUT_ROOT") from exc
    try:
        metadata = os.fstat(descriptor)
        anchor = {
            "fd": descriptor,
            "root": root,
            "device": metadata.st_dev,
            "inode": metadata.st_ino,
        }
        _assert_root_anchor(anchor)
        yield anchor
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass


def prepare_case_dir(root: Path, name: str, anchor: Optional[Mapping[str, object]] = None) -> Path:
    destination = root / name
    if destination.parent != root or destination.is_symlink():
        raise RuntimeError("UNSAFE_CASE_DIRECTORY")
    if anchor is not None:
        _assert_root_anchor(anchor)
        directory_fd = int(anchor["fd"])
        created = False
        try:
            try:
                metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                os.mkdir(name, 0o700, dir_fd=directory_fd)
                created = True
                metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if not stat.S_ISDIR(metadata.st_mode):
                raise RuntimeError("UNSAFE_CASE_DIRECTORY")
            _assert_root_anchor(anchor)
        except Exception:
            if created:
                try:
                    os.rmdir(name, dir_fd=directory_fd)
                except OSError as cleanup_error:
                    raise RuntimeError("ACCEPTANCE_CLEANUP_FAILED") from cleanup_error
            raise
        return destination
    if destination.exists() and not destination.is_dir():
        raise RuntimeError("UNSAFE_CASE_DIRECTORY")
    destination.mkdir(parents=False, exist_ok=True)
    if destination.resolve() != destination:
        raise RuntimeError("UNSAFE_CASE_DIRECTORY")
    return destination


def prepare_runtime_argument(path: Path) -> str:
    lexical = Path(os.path.abspath(os.fspath(path.expanduser())))
    return str(guard_runtime_root(lexical))


def _main() -> int:
    parser = argparse.ArgumentParser(description="Run reproducible synthetic DOCX/PDF acceptance conversions.")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--runtime", type=Path)
    parser.add_argument("--timeout", type=int, default=1200)
    parser.add_argument("--overwrite", action="store_true", help="Explicitly replace matching files in a non-empty result root.")
    args = parser.parse_args()
    selected_runtime = guard_runtime_root(args.runtime if args.runtime is not None else default_runtime_root())
    truth = json.loads((EVALS / "ground-truth.json").read_text(encoding="utf-8"))
    fixtures: Dict[str, Dict[str, str]] = truth["fixtures"]
    resolved = {}
    for kind, metadata in fixtures.items():
        path = (EVALS / metadata["file"]).resolve()
        if not path.is_file() or sha256(path) != metadata["sha256"]:
            print(json.dumps({"status": "FAIL", "error_code": "FIXTURE_HASH_MISMATCH", "fixture": kind}))
            return 2
        resolved[kind] = path

    try:
        output_root = prepare_output_root(args.output_dir, args.overwrite, selected_runtime)
    except RuntimeError as exc:
        print(json.dumps({"status": "FAIL", "error_code": str(exc)}))
        return 2
    runtime_args = ["--runtime", prepare_runtime_argument(args.runtime)] if args.runtime else []
    cases = (
        ("docx", "docx", "azw3,mobi6,epub", []),
        ("digital_pdf", "digital_pdf", "azw3", []),
        ("forced_scan", "digital_pdf", "azw3", ["--pdf-mode", "scan"]),
        ("scanned_pdf", "scanned_pdf", "azw3", []),
    )
    case_outputs = {}
    with acceptance_root_anchor(output_root) as root_anchor:
        for case_id, source_kind, formats, extra_args in cases:
            try:
                destination = prepare_case_dir(output_root, case_id.replace("_", "-"), root_anchor)
            except RuntimeError as exc:
                print(json.dumps({"status": "FAIL", "error_code": str(exc)}))
                return 2
            command = [
                sys.executable,
                str(CONVERTER),
                "convert",
                str(resolved[source_kind]),
                "--formats",
                formats,
                "--output-dir",
                str(destination),
                "--timeout",
                str(max(30, args.timeout)),
                *extra_args,
                *runtime_args,
            ]
            if args.overwrite:
                command.append("--overwrite")
            _assert_root_anchor(root_anchor)
            run(command)
            _assert_root_anchor(root_anchor)
            case_outputs[case_id] = destination

        report = output_root / "fixture-roundtrip.json"
        verifier = [
            sys.executable,
            str(VERIFIER),
            "--item",
            "docx",
            "azw3",
            str(case_outputs["docx"] / "fixture-structured.azw3"),
            "--item",
            "docx",
            "mobi6",
            str(case_outputs["docx"] / "fixture-structured.mobi"),
            "--item",
            "digital_pdf",
            "azw3",
            str(case_outputs["digital_pdf"] / "fixture-digital-layout.azw3"),
            "--item",
            "forced_scan",
            "azw3",
            str(case_outputs["forced_scan"] / "fixture-digital-layout.azw3"),
            "--item",
            "scanned_pdf",
            "azw3",
            str(case_outputs["scanned_pdf"] / "fixture-scanned.azw3"),
            "--output",
            str(report),
            *runtime_args,
        ]
        if args.overwrite:
            verifier.append("--overwrite")
        _assert_root_anchor(root_anchor)
        run(verifier)
        _assert_root_anchor(root_anchor)
    print(json.dumps({"status": "PASS", "fixture_version": truth["fixture_version"], "report": report.name}, ensure_ascii=False))
    return 0


def main() -> int:
    try:
        return _main()
    except SkillError as exc:
        print(json.dumps({"status": "FAIL", "error_code": exc.code}, ensure_ascii=False))
        return 2
    except (OSError, RuntimeError, ValueError, KeyError, json.JSONDecodeError):
        print(json.dumps({"status": "FAIL", "error_code": "ACCEPTANCE_FAILED"}, ensure_ascii=False))
        return 2
    except Exception:
        print(json.dumps({"status": "FAIL", "error_code": "ACCEPTANCE_FAILED"}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
