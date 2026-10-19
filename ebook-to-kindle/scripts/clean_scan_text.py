#!/usr/bin/env python3
"""Build a clean, reflowable HTML source from a cached Docling document.json.

This utility is intentionally offline and does not invoke OCR. It is meant for
scan conversions whose recognized text is acceptable but whose page furniture,
illustrations, typography, or navigation need to be rebuilt.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, List, Mapping, Optional, Sequence, Tuple


CJK = r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
CJK_CHAR = re.compile(f"[{CJK}]")
DEFAULT_DROP_LABELS = {"page_header", "page_footer", "page_number", "caption"}

DEFAULT_DROP_PATTERNS = (
    r"^\s*[ivxlcdm\d\-\—\–\·\.\s]+\s*$",
    r"(?:綫上書店|线上书店|網上書店|网上书店)",
    r"^\s*ISBN\b",
    r"^\s*Printed in\b",
    r"^\s*(?:總經銷|总经销|版權所有|版权所有)\b",
)
# Book-specific watermarks (shop names, contact numbers, domains) belong in the
# per-book config: {"drop_patterns": [...]}.

INLINE_AD_PATTERNS = [
    re.compile(r"(?<!\d)(?:QQ|Q0|0Q)\s*[:：]\s*[\d\s\-\|]{5,}", re.I),
    re.compile(r"(?:綫上書店|线上书店|網上書店|网上书店)\s*[:：]?\s*(?:(?:www\.)?[a-zA-Z0-9\.\-]+)?", re.I),
]

TERMINAL_PUNCTUATION = set("。！？!?;；:：”’」』》）】…—~")


def _load_json(path: Path) -> Mapping[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping) or not isinstance(value.get("texts"), list):
        raise ValueError("document.json 缺少 texts 数组")
    return value


def _page_set(values: Iterable[Any]) -> set[int]:
    pages: set[int] = set()
    for value in values:
        if isinstance(value, int):
            pages.add(value)
            continue
        token = str(value).strip()
        if not token:
            continue
        if "-" in token:
            left, right = token.split("-", 1)
            pages.update(range(int(left), int(right) + 1))
        else:
            pages.add(int(token))
    return pages


def _clean_text(value: Any, strip_inline_ads: bool = True) -> str:
    text = str(value or "").replace("\u00a0", " ").replace("\u3000", " ").replace("\u200b", "").replace("\ufeff", "")
    if strip_inline_ads:
        for pat in INLINE_AD_PATTERNS:
            text = pat.sub("", text)
    text = re.sub(r"[\s\|\:：]+$", "", text)
    text = re.sub(r"^[\|\:：\s]+", "", text)
    text = re.sub(r"[ \t\r\n]+", " ", text).strip()
    text = re.sub(fr"(?<=[{CJK}])[ \t]+(?=[{CJK}])", "", text)
    text = re.sub(r"\s+([，。！？；：、）》】」』”’])", r"\1", text)
    text = re.sub(r"([（《【「『“‘])\s+", r"\1", text)
    return text.strip()


def _item_page(item: Mapping[str, Any]) -> int | None:
    provenance = item.get("prov")
    if not isinstance(provenance, list) or not provenance or not isinstance(provenance[0], Mapping):
        return None
    page = provenance[0].get("page_no")
    return int(page) if isinstance(page, (int, float)) else None


def _heading_key(page: int | None, text: str) -> str:
    return f"{page}|{text}"


def _is_terminal(text: str) -> bool:
    if not text:
        return False
    return text[-1] in TERMINAL_PUNCTUATION or (len(text) >= 2 and text[-2:] in ("…”", "。”", "！”", "？”", "』。", "』！"))


def _render(config: Mapping[str, Any], document: Mapping[str, Any]) -> tuple[str, Mapping[str, Any]]:
    title = _clean_text(config.get("title"), strip_inline_ads=False)
    author = _clean_text(config.get("author"), strip_inline_ads=False)
    language = _clean_text(config.get("language") or "zh", strip_inline_ads=False)
    if not title:
        raise ValueError("配置缺少 title")

    start_page = int(config.get("start_page", 0))
    end_page = int(config.get("end_page", 10**9))
    excluded_pages = _page_set(config.get("exclude_pages", []))
    drop_labels = DEFAULT_DROP_LABELS | {str(value) for value in config.get("drop_labels", [])}
    drop_exact = {_clean_text(value, strip_inline_ads=False) for value in config.get("drop_exact", [])}
    drop_patterns = [re.compile(value, re.I) for value in (*DEFAULT_DROP_PATTERNS, *config.get("drop_patterns", []))]
    paragraph_patterns = [re.compile(value, re.I) for value in config.get("paragraph_patterns", [])]
    keep_unmapped_headers = bool(config.get("keep_unmapped_section_headers", True))
    max_h2_length = int(config.get("max_h2_length", 45))
    merge_paragraphs = bool(config.get("merge_paragraphs", True))

    heading_map: dict[str, tuple[int, str]] = {}
    for heading in config.get("headings", []):
        if not isinstance(heading, Mapping):
            continue
        page = int(heading["page"])
        original = _clean_text(heading["match"], strip_inline_ads=False)
        level = int(heading.get("level", 1))
        replacement = _clean_text(heading.get("text") or original, strip_inline_ads=False)
        heading_map[_heading_key(page, original)] = (level, replacement)

    # Optional pattern-based headings
    pattern_headings = []
    for ph in config.get("pattern_headings", []):
        if isinstance(ph, Mapping):
            pat = re.compile(ph["pattern"], re.I)
            lvl = int(ph.get("level", 2))
            pattern_headings.append((pat, lvl))

    raw_blocks: list[tuple[str, str, int | None]] = []
    dropped = Counter()
    source_items = 0

    for raw_item in document["texts"]:
        if not isinstance(raw_item, Mapping):
            dropped["invalid_item"] += 1
            continue
        source_items += 1
        page = _item_page(raw_item)
        if page is None or page < start_page or page > end_page or page in excluded_pages:
            dropped["page_scope"] += 1
            continue
        label = str(raw_item.get("label") or "text")
        if str(raw_item.get("content_layer") or "") == "furniture":
            dropped["furniture"] += 1
            continue
        if label in drop_labels:
            dropped[f"label:{label}"] += 1
            continue
        text = _clean_text(raw_item.get("text"))
        if not text:
            dropped["empty"] += 1
            continue
        if text in drop_exact:
            dropped["exact"] += 1
            continue
        if any(pattern.search(text) for pattern in drop_patterns):
            dropped["pattern"] += 1
            continue

        mapped = heading_map.get(_heading_key(page, text))
        if mapped:
            level, replacement = mapped
            tag = f"h{max(1, min(level, 3))}"
            if not raw_blocks or raw_blocks[-1][1] != replacement:
                raw_blocks.append((tag, replacement, page))
            continue

        # Check pattern headings
        matched_pattern_level = None
        for pat, lvl in pattern_headings:
            if pat.search(text):
                matched_pattern_level = lvl
                break
        if matched_pattern_level:
            tag = f"h{max(1, min(matched_pattern_level, 3))}"
            if not raw_blocks or raw_blocks[-1][1] != text:
                raw_blocks.append((tag, text, page))
            continue

        if label == "section_header":
            if (
                not keep_unmapped_headers
                or len(text) > max_h2_length
                or any(pattern.search(text) for pattern in paragraph_patterns)
            ):
                raw_blocks.append(("p", text, page))
            else:
                if not raw_blocks or raw_blocks[-1][1] != text:
                    raw_blocks.append(("h2", text, page))
            continue
        raw_blocks.append(("p", text, page))

    # Second pass: paragraph merging
    blocks: list[tuple[str, str, int | None]] = []
    if merge_paragraphs:
        for tag, text, page in raw_blocks:
            if not blocks:
                blocks.append((tag, text, page))
                continue
            prev_tag, prev_text, prev_page = blocks[-1]
            if prev_tag == "p" and tag == "p" and not _is_terminal(prev_text):
                # Join sentences
                if prev_text and text and CJK_CHAR.search(prev_text[-1]) and CJK_CHAR.search(text[0]):
                    joined_text = prev_text + text
                else:
                    joined_text = prev_text + " " + text
                blocks[-1] = ("p", joined_text, prev_page)
            else:
                blocks.append((tag, text, page))
    else:
        blocks = raw_blocks

    h1_count = sum(1 for tag, _, _ in blocks if tag == "h1")
    if h1_count == 0:
        raise ValueError("没有生成任何一级目录标题")

    lines = [
        "<!doctype html>",
        f'<html lang="{html.escape(language, quote=True)}">',
        "<head>",
        '<meta charset="utf-8">',
        f"<title>{html.escape(title)}</title>",
        "<style>",
        "@charset \"utf-8\";",
        "body { margin: 0; padding: 0; font-size: 1em; line-height: 1.65; }",
        ".title-page { text-align: center; page-break-after: always; padding: 30% 0 10%; }",
        ".title-page h1 { font-size: 1.75em; line-height: 1.35; margin-bottom: 0.8em; }",
        ".title-page p { font-size: 1.1em; text-indent: 0; margin-top: 1.5em; text-align: center; }",
        "h1, .kindle-toc-level-1 { page-break-before: always; font-size: 1.45em; line-height: 1.35; margin: 1.6em 0 0.8em; font-weight: bold; }",
        "h2, .kindle-toc-level-2 { font-size: 1.2em; line-height: 1.4; margin: 1.3em 0 0.6em; font-weight: bold; }",
        "h3, .kindle-toc-level-3 { font-size: 1.05em; line-height: 1.4; margin: 1.1em 0 0.5em; font-weight: bold; }",
        "p { font-size: 1em; line-height: 1.65; text-align: justify; text-indent: 2em; margin: 0.35em 0; }",
        "blockquote { margin: 0.8em 1.5em; font-size: 0.95em; line-height: 1.6; }",
        "</style>",
        "</head>",
        "<body>",
        '<section class="title-page">',
        f"<h1>{html.escape(title)}</h1>",
    ]
    if author:
        lines.append(f"<p>{html.escape(author)} 著</p>")
    lines.extend(["</section>"])

    first_heading_seen = False
    for tag, text, _page in blocks:
        classes = []
        if tag == "h1":
            classes.append("kindle-toc-level-1")
            if not first_heading_seen:
                classes.append("kindle-start-reading")
                first_heading_seen = True
        elif tag == "h2":
            classes.append("kindle-toc-level-2")
        elif tag == "h3":
            classes.append("kindle-toc-level-3")

        class_attr = f' class="{" ".join(classes)}"' if classes else ""
        lines.append(f"<{tag}{class_attr}>{html.escape(text)}</{tag}>")

    lines.extend(["</body>", "</html>", ""])

    report = {
        "status": "PASS",
        "title": title,
        "author": author,
        "source_text_items": source_items,
        "retained_blocks": len(blocks),
        "retained_characters": sum(len(text) for _, text, _ in blocks),
        "h1_entries": h1_count,
        "h2_entries": sum(1 for tag, _, _ in blocks if tag == "h2"),
        "h3_entries": sum(1 for tag, _, _ in blocks if tag == "h3"),
        "content_images": 0,
        "excluded_pages": sorted(excluded_pages),
        "dropped": dict(sorted(dropped.items())),
    }
    return "\n".join(lines), report


def main() -> int:
    parser = argparse.ArgumentParser(description="Rebuild cached scan text as clean reflowable HTML without OCR.")
    parser.add_argument("document_json", type=Path)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    try:
        document = _load_json(args.document_json)
        config = _load_json(args.config) if args.config.name == "document.json" else json.loads(args.config.read_text(encoding="utf-8"))
        if not isinstance(config, Mapping):
            raise ValueError("配置必须是 JSON 对象")
        rendered, report = _render(config, document)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
        report_path = args.report or args.output.with_suffix(args.output.suffix + ".report.json")
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError, KeyError) as exc:
        print(json.dumps({"status": "FAIL", "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

