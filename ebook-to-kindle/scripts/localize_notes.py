#!/usr/bin/env python3
"""Explicit note-localization CLI for ebook-to-kindle."""

from __future__ import annotations

import argparse
import json
import os
import unicodedata
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from ebook_kindle.core import SkillError
from ebook_kindle.localization import (
    DEFAULT_SPARK_MODEL,
    apply_approved_translations,
    approve_translations,
    extract_note_ledger,
    read_ledger,
    translate_pending_groups,
    write_ledger,
)


SCRIPT_INTERFACE = "cli"
SCRIPT_INTERFACE_REASON = "Explicit per-book note extraction, cloud translation opt-in, approval, and local apply workflow."


EXIT_CODES = {
    "UNSUPPORTED_OUTPUT": 2,
    "OUTPUT_EXISTS": 4,
    "CONTENT_GATE_FAILED": 6,
    "EPUB_INVALID": 7,
    "DEPENDENCY_MISSING": 3,
    "CLOUD_TRANSLATION_NOT_APPROVED": 14,
    "CONVERSION_FAILED": 10,
}


def _json(payload: Dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _paths_alias(left: Path, right: Path) -> bool:
    left = Path(os.path.abspath(os.fspath(left.expanduser())))
    right = Path(os.path.abspath(os.fspath(right.expanduser())))
    if unicodedata.normalize("NFC", str(left)).casefold() == unicodedata.normalize("NFC", str(right)).casefold():
        return True
    if left.exists() and right.exists():
        try:
            return left.samefile(right)
        except OSError:
            return False
    return False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Localize EPUB footnotes through an explicit per-ID approval ledger.")
    commands = parser.add_subparsers(dest="command", required=True)
    extract = commands.add_parser("extract")
    extract.add_argument("input", type=Path)
    extract.add_argument("--ledger", type=Path, required=True)
    extract.add_argument("--overwrite", action="store_true")
    translate = commands.add_parser("translate")
    translate.add_argument("--ledger", type=Path, required=True)
    translate.add_argument("--model", default=DEFAULT_SPARK_MODEL)
    translate.add_argument("--batch-size", type=int, default=24)
    translate.add_argument("--timeout", type=int, default=1200)
    translate.add_argument("--allow-cloud-translation", action="store_true")
    approve = commands.add_parser("approve")
    approve.add_argument("--ledger", type=Path, required=True)
    approve.add_argument("--confirm", required=True)
    apply = commands.add_parser("apply")
    apply.add_argument("input", type=Path)
    apply.add_argument("--ledger", type=Path, required=True)
    apply.add_argument("--output", type=Path, required=True)
    apply.add_argument("--overwrite", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "extract":
            if _paths_alias(args.input, args.ledger):
                raise SkillError("UNSUPPORTED_OUTPUT", "注释账本不能覆盖源 EPUB。")
            ledger = extract_note_ledger(args.input)
            write_ledger(args.ledger, ledger, overwrite=args.overwrite)
            result = {"status": "EXTRACTED", **ledger["metrics"]}
        elif args.command == "translate":
            ledger = translate_pending_groups(
                read_ledger(args.ledger),
                allow_cloud_translation=args.allow_cloud_translation,
                model=args.model,
                batch_size=args.batch_size,
                timeout_seconds=max(30, args.timeout),
            )
            write_ledger(args.ledger, ledger, overwrite=True)
            result = {"status": "MODEL_REVIEWED", **ledger.get("translation_summary", {})}
        elif args.command == "approve":
            ledger = approve_translations(read_ledger(args.ledger), args.confirm)
            write_ledger(args.ledger, ledger, overwrite=True)
            result = {"status": "APPROVED", **ledger.get("approval_summary", {})}
        else:
            result = apply_approved_translations(
                args.input,
                read_ledger(args.ledger),
                args.output,
                overwrite=args.overwrite,
            )
        _json(result)
        return 0
    except SkillError as exc:
        _json({"status": "FAIL", "error_code": exc.code, "details": {"message": exc.message, **exc.details}})
        return EXIT_CODES.get(exc.code, 10)
    except Exception:
        _json({"status": "FAIL", "error_code": "CONVERSION_FAILED", "details": {"message": "注释本地化无法安全完成。"}})
        return EXIT_CODES["CONVERSION_FAILED"]


if __name__ == "__main__":
    raise SystemExit(main())
