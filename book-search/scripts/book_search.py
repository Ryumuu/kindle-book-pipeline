#!/usr/bin/env python3
"""Multi-platform book search & download.

    python3 book_search.py providers
    python3 book_search.py search "Pride and Prejudice" --provider gutenberg
    python3 book_search.py download gutenberg:1342 --out-dir ~/Downloads/Books
    python3 book_search.py info --provider zlibrary
    python3 book_search.py zlib-sync-cookie

Add your own platform: drop a ``Provider`` subclass into
``~/.config/kindle-book-pipeline/providers/<name>.py`` (see ``examples/my_provider.py``).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from booksearch import USER_PROVIDER_DIR, ProviderError, load_providers  # noqa: E402

DEFAULT_DOWNLOAD_DIR = Path(os.environ.get("BOOK_SEARCH_DOWNLOAD_DIR") or Path.home() / "Downloads" / "Books")


def warn(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr)


def describe_error(exc: BaseException) -> str:
    if isinstance(exc, ProviderError):
        return str(exc)
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}"
    if isinstance(exc, urllib.error.URLError):
        return f"network error: {exc.reason}"
    return f"{type(exc).__name__}: {exc}"


def check_ready(provider):
    """``is_ready()`` from a plugin may itself crash; treat that as "not ready" instead of aborting."""
    try:
        ready, reason = provider.is_ready()
        return bool(ready), str(reason or "")
    except Exception as exc:
        return False, f"is_ready() failed: {describe_error(exc)}"


def pick(registry, spec: str):
    if spec == "all":
        return list(registry.values())
    chosen = []
    for name in [part.strip() for part in spec.split(",") if part.strip()]:
        if name not in registry:
            raise SystemExit(f"unknown provider '{name}'. Available: {', '.join(sorted(registry)) or 'none'}")
        chosen.append(registry[name])
    return chosen


def cmd_providers(args, registry) -> int:
    rows = []
    for name in sorted(registry):
        provider = registry[name]
        ready, reason = check_ready(provider)
        rows.append({
            "name": name,
            "label": provider.label or name,
            "ready": ready,
            "reason": reason,
            "needs_login": provider.needs_login,
            "source": getattr(provider, "source_path", ""),
        })
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    for row in rows:
        status = "ready" if row["ready"] else f"not ready ({row['reason']})"
        print(f"{row['name']:<12} {row['label']:<22} {status}")
        print(f"{'':<12} {row['source']}")
    print(f"\nUser provider folder: {USER_PROVIDER_DIR}")
    return 0


def cmd_search(args, registry) -> int:
    results, errors = [], {}
    explicit = args.provider != "all"
    for provider in pick(registry, args.provider):
        ready, reason = check_ready(provider)
        if not ready:
            if explicit:
                errors[provider.name] = reason
            else:
                warn(f"skip {provider.name}: {reason}")
            continue
        try:
            results.extend(provider.search(args.query, language=args.lang, extension=args.ext, limit=args.limit))
        except Exception as exc:  # keep other providers' results
            errors[provider.name] = describe_error(exc)
    if args.json:
        print(json.dumps({"results": [b.to_dict() for b in results], "errors": errors}, ensure_ascii=False, indent=2))
        return 0 if results or not errors else 1
    for name, message in errors.items():
        warn(f"{name}: {message}")
    if not results:
        print("No results.")
        return 1 if errors else 0
    for index, book in enumerate(results, 1):
        meta = " | ".join(x for x in (", ".join(book.authors), book.year, book.language, book.extension, book.size) if x)
        print(f"{index:>2}. {book.title}")
        if meta:
            print(f"    {meta}")
        print(f"    ref: {book.ref}")
    return 0


def cmd_download(args, registry) -> int:
    name, sep, book_id = args.ref.partition(":")
    if not sep or not book_id:
        raise SystemExit("ref must look like <provider>:<id>, as printed by `search`.")
    provider = pick(registry, name)[0]
    ready, reason = check_ready(provider)
    if not ready:
        raise SystemExit(f"{name}: {reason}")
    try:
        path = provider.download(book_id, Path(args.out_dir).expanduser())
    except Exception as exc:
        raise SystemExit(f"download failed ({name}): {describe_error(exc)}")
    print(f"SAVED_PATH={path}")
    return 0


def cmd_info(args, registry) -> int:
    payload, failed = {}, False
    for provider in pick(registry, args.provider):
        ready, reason = check_ready(provider)
        if not ready:
            payload[provider.name] = {"ready": False, "reason": reason}
            continue
        try:
            payload[provider.name] = {"ready": True, **provider.info()}
        except Exception as exc:
            payload[provider.name] = {"ready": True, "error": describe_error(exc)}
            failed = True
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 1 if failed else 0


def cmd_zlib_sync_cookie(args, registry) -> int:
    provider = registry.get("zlibrary")
    module = sys.modules.get(type(provider).__module__) if provider else None
    sync = getattr(module, "sync_cookie_from_chrome", None)
    if sync is None:
        raise SystemExit("zlibrary provider is not installed.")
    ok, message = sync()
    print(message, file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Search and download books from pluggable providers.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("providers", help="list installed providers")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_providers)

    p = sub.add_parser("search", help="search one or more providers")
    p.add_argument("query")
    p.add_argument("--provider", default="all", help="'all' (default) or comma-separated names")
    p.add_argument("--lang", help="language, e.g. english / chinese")
    p.add_argument("--ext", help="file extension, e.g. epub")
    p.add_argument("--limit", type=int, default=8, help="max results per provider")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("download", help="download a book by ref")
    p.add_argument("ref", help="<provider>:<id> from search output")
    p.add_argument("--out-dir", default=str(DEFAULT_DOWNLOAD_DIR), help=f"default: {DEFAULT_DOWNLOAD_DIR}")
    p.set_defaults(func=cmd_download)

    p = sub.add_parser("info", help="account / quota info")
    p.add_argument("--provider", default="all")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("zlib-sync-cookie", help="macOS: copy Z-Library login from the active Chrome tab")
    p.set_defaults(func=cmd_zlib_sync_cookie)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    registry = load_providers(warn=warn)
    return args.func(args, registry)


if __name__ == "__main__":
    sys.exit(main())
