"""Command-line interface with structured output and stable exit codes."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from . import __version__
from .core import SkillError, parse_formats, safe_report
from .pipeline import ConversionRequest, convert
from .performance import review_report
from .runtime import default_runtime_root, guard_runtime_root, readiness, runtime_operation_lock, verify_runtime
from .workspace import inspect_workspace, purge_workspace


EXIT_CODES = {
    "UNSUPPORTED_INPUT": 2,
    "UNSUPPORTED_OUTPUT": 2,
    "DEPENDENCY_MISSING": 3,
    "OUTPUT_EXISTS": 4,
    "OCR_QUALITY_FAILED": 5,
    "CONTENT_GATE_FAILED": 6,
    "EPUB_INVALID": 7,
    "KINDLE_ROUNDTRIP_FAILED": 8,
    "ISOLATION_FAILED": 9,
    "RUNTIME_BUSY": 11,
    "WORKSPACE_INVALID": 12,
    "WORKSPACE_BUSY": 13,
    "CONVERSION_FAILED": 10,
}


def default_output_dir() -> Path:
    configured = os.environ.get("EBOOK_TO_KINDLE_OUTPUT_DIR")
    return Path(configured).expanduser() if configured else Path.home() / "Documents" / "KindleBooks"


def _json(payload: Dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _deep_doctor(root: Path) -> Dict[str, Any]:
    with runtime_operation_lock(root):
        return verify_runtime(root)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ebook-to-kindle",
        description="Convert local documents into PW2-targeted AZW3/MOBI6 with off-device quality gates.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="Check the isolated runtime without reading user documents.")
    doctor.add_argument("--runtime", type=Path, default=default_runtime_root())
    doctor.add_argument("--deep", action="store_true")

    conversion = subparsers.add_parser("convert", help="Convert one local document.")
    conversion.add_argument("input")
    conversion.add_argument(
        "--output-dir",
        type=Path,
        default=default_output_dir(),
        help="Output directory (default: $EBOOK_TO_KINDLE_OUTPUT_DIR or ~/Documents/KindleBooks).",
    )
    conversion.add_argument(
        "--formats",
        default="azw3",
        help="Comma-separated output formats: azw3, epub, mobi6 (default: azw3).",
    )
    conversion.add_argument("--runtime", type=Path, default=default_runtime_root())
    conversion.add_argument("--pdf-mode", choices=("auto", "digital", "scan"), default="auto")
    conversion.add_argument("--title", help="Override the embedded book title without changing the output filename.")
    conversion.add_argument("--authors", help="Override embedded authors, using Calibre's author-list syntax.")
    conversion.add_argument("--language", help="Override the embedded BCP 47 language tag, for example zh or en.")
    conversion.add_argument("--overwrite", action="store_true")
    conversion.add_argument("--timeout", type=int, default=1200)
    conversion.add_argument("--report", type=Path)
    conversion.add_argument("--workspace", type=Path, help="Explicit private single-book workspace.")
    conversion.add_argument("--reuse", choices=("auto", "refresh", "off"), default="auto")

    review = subparsers.add_parser("review", help="Review one completed conversion report without reading book content.")
    review.add_argument("report", type=Path)

    workspace = subparsers.add_parser("workspace", help="Inspect or securely purge one explicit book workspace.")
    workspace_commands = workspace.add_subparsers(dest="workspace_command", required=True)
    inspect = workspace_commands.add_parser("inspect", help="Read workspace identity, size, and node counts.")
    inspect.add_argument("workspace_root", type=Path)
    purge = workspace_commands.add_parser("purge", help="Permanently remove one identified book workspace.")
    purge.add_argument("workspace_root", type=Path)
    purge.add_argument("--confirm", required=True)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "review":
        try:
            report_path = args.report.expanduser()
            if report_path.is_symlink() or not report_path.is_file():
                raise ValueError("unsafe report")
            payload = json.loads(report_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("invalid report")
            _json(review_report(payload))
            return 0
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            _json({"status": "FAIL", "error_code": "CONVERSION_FAILED", "details": {"message": "转换报告无法安全复盘。"}})
            return EXIT_CODES["CONVERSION_FAILED"]
    if args.command == "workspace":
        try:
            result = (
                inspect_workspace(args.workspace_root)
                if args.workspace_command == "inspect"
                else purge_workspace(args.workspace_root, args.confirm)
            )
            _json(result)
            return 0
        except SkillError as exc:
            _json({"status": "FAIL", "error_code": exc.code, "details": {"message": exc.message, **exc.details}})
            return EXIT_CODES.get(exc.code, 10)
        except Exception:
            _json({"status": "FAIL", "error_code": "WORKSPACE_INVALID", "details": {"message": "单书工作区操作无法安全完成。"}})
            return EXIT_CODES["WORKSPACE_INVALID"]
    if args.command == "doctor":
        try:
            root = guard_runtime_root(args.runtime)
            status = _deep_doctor(root) if args.deep else readiness(root)
            _json(status)
            return 0 if status.get("core_ready") and (not args.deep or status.get("deep_ready")) else 3
        except SkillError as exc:
            _json({"status": "FAIL", "error_code": exc.code, "details": {"message": exc.message}})
            return EXIT_CODES.get(exc.code, 10)
        except Exception:
            _json({"status": "FAIL", "error_code": "CONVERSION_FAILED", "details": {"message": "运行时检查无法安全完成。"}})
            return EXIT_CODES["CONVERSION_FAILED"]

    try:
        request = ConversionRequest(
            input_value=args.input,
            output_dir=args.output_dir,
            formats=parse_formats(args.formats),
            runtime_root=guard_runtime_root(args.runtime),
            pdf_mode=args.pdf_mode,
            title=args.title,
            authors=args.authors,
            language=args.language,
            overwrite=args.overwrite,
            timeout_seconds=max(30, args.timeout),
            report_path=args.report,
            workspace_root=args.workspace,
            reuse_policy=args.reuse,
        )
        result = convert(request, Path(__file__).resolve().parents[2])
        _json(result)
        return 0
    except SkillError as exc:
        report = safe_report(
            status="FAIL",
            input_path=Path(getattr(args, "input", "input")),
            output_dir=Path(getattr(args, "output_dir", ".")),
            outputs=[],
            route="not-completed",
            details={"message": exc.message, **exc.details},
            error_code=exc.code,
        )
        _json(report)
        return EXIT_CODES.get(exc.code, 10)
    except Exception as exc:
        report = safe_report(
            status="FAIL",
            input_path=Path(getattr(args, "input", "input")),
            output_dir=Path(getattr(args, "output_dir", ".")),
            outputs=[],
            route="not-completed",
            details={"message": "输入、资源或路径无法安全处理。", "exception": type(exc).__name__},
            error_code="CONVERSION_FAILED",
        )
        _json(report)
        return EXIT_CODES["CONVERSION_FAILED"]


if __name__ == "__main__":
    raise SystemExit(main())
