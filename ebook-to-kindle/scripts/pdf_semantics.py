#!/usr/bin/env python3
"""Conservatively enrich Docling HTML with Kindle navigation and notes.

This module runs only inside the pinned private PDF environment, where
BeautifulSoup is already a locked Docling dependency. It never fetches data.
"""

from __future__ import annotations

import argparse
import copy
import difflib
import json
import re
import unicodedata
from collections import Counter, defaultdict, deque
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


EPUB_NS = "http://www.idpf.org/2007/ops"
SCRIPT_INTERFACE = "internal-module"
SCRIPT_INTERFACE_REASON = "Loaded by the private PDF backend; not a user-facing command."
NOTE_MARKER = re.compile(r"\[\s*(\d{1,4})\s*\]")
DEFINITION_START = re.compile(r"^(?:\d{1,4}\s*)?\[\s*(\d{1,4})\s*\]")
DECORATIVE_STYLE = re.compile(r"(?:max-width\s*:|box-shadow\s*:|\.page\s*\{|body\s*\{[^}]*padding\s*:)", re.I | re.S)
PRINTED_TOC_CHAPTER = re.compile(
    r"(?:第\s*[一二三四五六七八九十百零〇0-9]+\s*[章节篇部卷]|chapter\s+\d+\b)",
    re.I,
)
PRINTED_TOC_PAGE = re.compile(r"^\s*(?:第\s*)?(\d{1,4})(?:\s*页)?\s*$")
# Docling's text sanitizer folds curly quotes to ASCII and docling-parse emits
# "——" as "--"; these patterns restore them only inside CJK-dominant blocks.
CJK_TEXT = re.compile("[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
LATIN_LETTER = re.compile(r"[A-Za-z]")
CJK_CONTEXT = "[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff，。！？；：︰、（）《》【】「」『』“”‘’]"
CJK_PUNCTUATION = "[，。！？；：︰、（）《》【】「」『』“”‘’]"
# Both neighbours must be CJK and at least one a CJK punctuation mark, so
# "--help" and an operator between two characters ("甲--乙") stay as typed.
CJK_DOUBLE_HYPHEN = re.compile(
    rf"(?<={CJK_PUNCTUATION})\s*-{{2}}\s*(?={CJK_CONTEXT})"
    rf"|(?<={CJK_CONTEXT})\s*-{{2}}\s*(?={CJK_PUNCTUATION})"
)
FOLDED_QUOTE = re.compile(r"""(?<![A-Za-z])['"]|['"](?![A-Za-z])""")
CURLY_QUOTE = re.compile("[\u2018\u2019\u201c\u201d]")
APOSTROPHE = re.compile(r"(?<=[A-Za-z])'|'(?=[A-Za-z])")
# Only the marks Docling was seen to split off; a lone "！" or "？" may be a verse line.
LONE_PUNCTUATION = re.compile(r"^\s*[，。、；：︰]\s*$")
SENTENCE_END = re.compile(r"[，。！？；：︰、」』”’）》'\"…—]\s*$")
# A dialogue tag between two quotations must end in a speech verb ("”母親說“");
# "“好”与“坏”" is not one. Docling may still space the tag out and fold the
# quotes ("' 母 親 說 '"): lone marks are merged before quotes are restored.
DIALOGUE_GAP = re.compile(
    "([”'\"]\\s*(?:[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]\\s*){0,8}?"
    "[說说道問问答講讲喊叫嚷])(\\s*[“'\"])"
)
# A running footer such as "· 71 ·" that Docling tagged as a list item: the
# decoration is required, so plain numbers ("1998", "-20") are never removed.
PAGE_NUMBER_ITEM = re.compile(r"^[·•]?\s*\d{1,4}\s*[·•]$|^[·•]\s*\d{1,4}$")
# Marks indexed in the PDF text layer (restored forms and the literal forms
# they could be confused with), and how many visible characters on each side
# must match there before a restored mark is trusted.
EVIDENCE_MARK = re.compile("——|--|[—\\-\"'“”‘’，。、；：︰]")
QUOTE_MARKS = "\"'“”‘’"
LONE_MARKS = "，。、；：︰"
EVIDENCE_WINDOW = 4
WHITESPACE = re.compile(r"\s+")
HIDING_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*(?:hidden|collapse)", re.IGNORECASE)
HIDING_TAGS = {"script", "style", "template", "noscript"}
# Neighbouring marks may themselves still be folded or undecided, so the
# context around a mark is compared with every quote read as '"' and every
# dash as "—"; only the mark being checked keeps its exact form.
CONTEXT_FOLD = re.compile("——|--|—|[\"'“”‘’]")
CJK_RESTORE_BLOCKS = ("p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "td", "th", "figcaption")


def normalized_title(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    return "".join(character for character in value if character.isalnum())


def _add_class(tag: Any, value: str) -> None:
    classes = list(tag.get("class", []))
    if value not in classes:
        classes.append(value)
    tag["class"] = classes


def _page_for(item: Mapping[str, Any]) -> Optional[int]:
    provenance = item.get("prov", [])
    if not isinstance(provenance, list):
        return None
    for entry in provenance:
        if not isinstance(entry, Mapping):
            continue
        try:
            page = int(entry.get("page_no", 0))
        except (TypeError, ValueError):
            continue
        if page > 0:
            return page
    return None


def _text_page_queues(document: Mapping[str, Any]) -> Dict[str, deque[int]]:
    queues: Dict[str, deque[int]] = defaultdict(deque)
    texts = document.get("texts", [])
    if not isinstance(texts, list):
        return queues
    for item in texts:
        if not isinstance(item, Mapping) or str(item.get("content_layer", "")) == "furniture":
            continue
        value = item.get("text", "")
        page = _page_for(item)
        key = normalized_title(value) if isinstance(value, str) else ""
        if key and page is not None:
            queues[key].append(page)
    return queues


def _candidate_elements(soup: Any, document: Mapping[str, Any]) -> List[Dict[str, Any]]:
    queues = _text_page_queues(document)
    candidates: List[Dict[str, Any]] = []
    for order, tag in enumerate(soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "figcaption"])):
        if tag.find_parent(["table", "nav", "aside"]):
            continue
        text = " ".join(tag.get_text(" ", strip=True).split())
        key = normalized_title(text)
        if not key:
            continue
        page = queues[key].popleft() if queues.get(key) else None
        candidates.append(
            {
                "tag": tag,
                "text": text,
                "key": key,
                "page": page,
                "order": order,
                "heading": tag.name.lower().startswith("h"),
            }
        )
    return candidates


def _derived_outline(document: Mapping[str, Any]) -> List[Dict[str, Any]]:
    outline: List[Dict[str, Any]] = []
    first_body_page: Optional[int] = None
    texts = document.get("texts", [])
    if not isinstance(texts, list):
        return outline
    for item in texts:
        if not isinstance(item, Mapping) or str(item.get("content_layer", "")) == "furniture":
            continue
        if first_body_page is None and isinstance(item.get("text"), str) and str(item.get("text", "")).strip():
            first_body_page = _page_for(item)
        if str(item.get("label", "")) not in {"title", "section_header"}:
            continue
        title = " ".join(str(item.get("text", "")).split())
        if not title or len(title) > 160:
            continue
        try:
            raw_level = int(item.get("level", 1) or 1)
        except (TypeError, ValueError):
            raw_level = 1
        outline.append({"title": title, "level": 0 if raw_level <= 1 else 1, "page": _page_for(item)})
    if not outline and first_body_page is not None:
        outline.append({"title": "正文", "level": 0, "page": first_body_page})
    return outline


def _choose_outline_candidate(
    item: Mapping[str, Any], candidates: Sequence[Mapping[str, Any]], used: set[int], previous_order: int
) -> Optional[Mapping[str, Any]]:
    title = str(item.get("title", "")).strip()
    key = normalized_title(title)
    if not key:
        return None
    try:
        page = int(item.get("page")) if item.get("page") is not None else None
    except (TypeError, ValueError):
        page = None
    scored: List[Tuple[float, Mapping[str, Any]]] = []
    for candidate in candidates:
        if id(candidate["tag"]) in used:
            continue
        if int(candidate["order"]) < previous_order:
            continue
        candidate_key = str(candidate["key"])
        exact = candidate_key == key
        contains = key in candidate_key or candidate_key in key
        if not exact and (not contains or min(len(key), len(candidate_key)) < 4):
            continue
        score = 100.0 if exact else 45.0
        if page is not None and candidate.get("page") == page:
            score += 90.0
        elif page is not None and candidate.get("page") is not None:
            score -= min(40.0, abs(int(candidate["page"]) - page) * 4.0)
        if candidate.get("heading"):
            score += 20.0
        if int(candidate["order"]) >= previous_order:
            score += 12.0
        else:
            score -= 35.0
        score -= abs(len(candidate_key) - len(key)) * 0.5
        scored.append((score, candidate))
    return max(scored, key=lambda pair: (pair[0], -int(pair[1]["order"])))[1] if scored else None


def _page_start_candidate(
    item: Mapping[str, Any], candidates: Sequence[Mapping[str, Any]], used: set[int], previous_order: int
) -> Optional[Mapping[str, Any]]:
    try:
        page = int(item.get("page")) if item.get("page") is not None else None
    except (TypeError, ValueError):
        page = None
    if page is None:
        return None
    page_candidates = [
        candidate
        for candidate in candidates
        if id(candidate["tag"]) not in used
        and candidate.get("page") == page
        and int(candidate["order"]) >= previous_order
    ]
    return min(page_candidates, key=lambda candidate: int(candidate["order"])) if page_candidates else None


def _map_outline(
    soup: Any, document: Mapping[str, Any], outline: Sequence[Mapping[str, Any]]
) -> Tuple[int, int, int, int, List[Any]]:
    candidates = _candidate_elements(soup, document)
    used: set[int] = set()
    previous_order = -1
    mapped_tags: List[Any] = []
    generated = 0
    title_matched = 0
    page_start_anchors = 0
    for index, item in enumerate(outline, 1):
        title = str(item.get("title", "")).strip()
        candidate = _choose_outline_candidate(item, candidates, used, previous_order)
        matched_by_title = candidate is not None
        if candidate is None:
            candidate = _page_start_candidate(item, candidates, used, previous_order)
        if candidate is None:
            continue
        tag = candidate["tag"]
        previous_order = int(candidate["order"])
        if matched_by_title:
            used.add(id(tag))
        level = 1 if int(item.get("level", 0) or 0) <= 0 else 2
        title_key = normalized_title(title)
        candidate_key = str(candidate["key"])
        if not matched_by_title or title_key != candidate_key:
            anchor = soup.new_tag("span", id=f"kindle-toc-{index:04d}")
            anchor.string = title
            _add_class(anchor, f"kindle-toc-level-{level}")
            _add_class(anchor, "kindle-toc-anchor")
            if tag.name == "li":
                tag.insert(0, anchor)
            else:
                tag.insert_before(anchor)
            mapped = anchor
            generated += 1
            if not matched_by_title:
                page_start_anchors += 1
        else:
            tag["id"] = f"kindle-toc-{index:04d}"
            _add_class(tag, f"kindle-toc-level-{level}")
            mapped = tag
        if matched_by_title:
            title_matched += 1
        if not mapped_tags:
            _add_class(mapped, "kindle-start-reading")
        mapped_tags.append(mapped)
    return len(mapped_tags), generated, title_matched, page_start_anchors, mapped_tags


def _table_looks_like_printed_toc(table: Any, outline_keys: Sequence[str]) -> bool:
    cells = [" ".join(cell.get_text(" ", strip=True).split()) for cell in table.find_all(["td", "th"])]
    cells = [cell for cell in cells if cell]
    if not cells:
        return False
    table_key = normalized_title(" ".join(cells))
    overlap = sum(1 for key in outline_keys if key in table_key)
    overlap_threshold = 2 if len(outline_keys) <= 5 else 4
    if overlap >= overlap_threshold:
        return True
    page_values = []
    for cell in cells:
        match = PRINTED_TOC_PAGE.fullmatch(cell)
        if match:
            page_values.append(int(match.group(1)))
    if len(page_values) < 3:
        return False
    numeric_ratio = len(page_values) / len(cells)
    monotonic_ratio = sum(
        right >= left for left, right in zip(page_values, page_values[1:])
    ) / max(1, len(page_values) - 1)
    chapter_signal = any(PRINTED_TOC_CHAPTER.search(cell) for cell in cells)
    return 0.20 <= numeric_ratio <= 0.67 and monotonic_ratio >= 0.75 and chapter_signal


def _remove_printed_toc_tables(soup: Any, outline: Sequence[Mapping[str, Any]]) -> Tuple[int, int]:
    keys = [normalized_title(str(item.get("title", ""))) for item in outline]
    keys = [key for key in keys if len(key) >= 2]
    removed = 0
    for table in list(soup.find_all("table")):
        if _table_looks_like_printed_toc(table, keys):
            table.decompose()
            removed += 1
    remaining = sum(1 for table in soup.find_all("table") if _table_looks_like_printed_toc(table, keys))
    return removed, remaining


def _definition_run(blocks: Sequence[Any]) -> List[Any]:
    candidates: List[Tuple[int, Any, List[int]]] = []
    for index, block in enumerate(blocks):
        text = " ".join(block.get_text(" ", strip=True).split())
        if not DEFINITION_START.match(text):
            continue
        numbers = [int(value) for value in NOTE_MARKER.findall(text)]
        if numbers and all(right > left for left, right in zip(numbers, numbers[1:])):
            candidates.append((index, block, numbers))
    runs: List[List[Tuple[int, Any, List[int]]]] = []
    current: List[Tuple[int, Any, List[int]]] = []
    for candidate in candidates:
        if current:
            previous = current[-1]
            close_in_document = candidate[0] - previous[0] <= 5
            close_in_sequence = 0 < candidate[2][0] - previous[2][-1] <= 5
            if not (close_in_document and close_in_sequence):
                runs.append(current)
                current = []
        current.append(candidate)
    if current:
        runs.append(current)
    if not runs:
        return []
    strong_runs = [run for run in runs if sum(len(item[2]) for item in run) >= 20]
    if not strong_runs:
        return []
    # Docling can split one long endnote section into multiple lists and a
    # packed pre/code block, then emit those fragments out of numeric order.
    # The first dense sequential run is the conservative start boundary; all
    # later definition-shaped blocks belong to the same endnote region.
    first_definition_index = min(run[0][0] for run in strong_runs)
    first_definition_number = min(
        run[0][2][0] for run in strong_runs if run[0][0] == first_definition_index
    )
    selected = [candidate for candidate in candidates if candidate[0] >= first_definition_index]
    # A section heading or explanatory paragraphs can separate note [1] from
    # the dense [2]... run. Pull in only the latest numeric predecessor that
    # bridges directly into the first strong run; this avoids mistaking an
    # earlier body citation with the same number for its definition.
    preceding = [candidate for candidate in candidates if candidate[0] < first_definition_index]
    while preceding:
        bridges = [
            candidate
            for candidate in preceding
            if 0 < first_definition_number - candidate[2][-1] <= 5
        ]
        if not bridges:
            break
        predecessor = max(bridges, key=lambda candidate: candidate[0])
        selected.append(predecessor)
        first_definition_number = predecessor[2][0]
        preceding = [candidate for candidate in preceding if candidate[0] < predecessor[0]]
    return [block for _index, block, _numbers in sorted(selected, key=lambda candidate: candidate[0])]


def _inside_semantic_footnote(node: Any) -> bool:
    for parent in getattr(node, "parents", []):
        if getattr(parent, "name", None) != "aside":
            continue
        tokens = set(str(parent.get("epub:type", "")).split())
        tokens.update(str(parent.get("type", "")).split())
        role = str(parent.get("role", ""))
        if tokens.intersection({"footnote", "endnote"}) or role in {"doc-footnote", "doc-endnote"}:
            return True
    return False


def _add_fallback_definition_blocks(
    blocks: Sequence[Any],
    definition_blocks: Sequence[Any],
    fallback_definitions: Mapping[int, str],
) -> List[Any]:
    if not definition_blocks or not fallback_definitions:
        return list(definition_blocks)
    positions = {id(block): index for index, block in enumerate(blocks)}
    first_definition_index = min(positions[id(block)] for block in definition_blocks)
    preceding: Dict[int, Tuple[int, Any]] = {}
    for index, block in enumerate(blocks[:first_definition_index]):
        text = " ".join(block.get_text(" ", strip=True).split())
        match = DEFINITION_START.match(text)
        if match is None:
            continue
        number = int(match.group(1))
        fallback = fallback_definitions.get(number)
        if fallback is None:
            continue
        authored_key = normalized_title(NOTE_MARKER.sub("", text, count=1))[:240]
        fallback_key = normalized_title(NOTE_MARKER.sub("", fallback, count=1))[:240]
        if not authored_key or not fallback_key:
            continue
        prefix_match = authored_key[:16] in fallback_key or fallback_key[:16] in authored_key
        similarity = difflib.SequenceMatcher(None, authored_key, fallback_key).ratio()
        if prefix_match or similarity >= 0.45:
            preceding[number] = (index, block)
    added = [value for _number, value in sorted(preceding.items())]
    combined = [*definition_blocks, *(block for _index, block in added)]
    return sorted({id(block): block for block in combined}.values(), key=lambda block: positions[id(block)])


def _reference_text_nodes(soup: Any, first_definition_block: Any) -> List[Any]:
    from bs4 import NavigableString

    nodes: List[Any] = []
    for item in soup.descendants:
        if item is first_definition_block:
            break
        if not isinstance(item, NavigableString):
            continue
        parent = item.parent
        if parent is None:
            continue
        excluded = {"head", "nav", "script", "style", "title"}
        if parent.name in excluded or parent.find_parent(list(excluded)):
            continue
        if _inside_semantic_footnote(item):
            continue
        nodes.append(item)
    return nodes


def _count_unresolved_reference_markers(nodes: Sequence[Any], definitions: set[int]) -> int:
    unresolved = 0
    for node in nodes:
        if (
            node.parent is None
            or node.find_parent(["html", "body"]) is None
            or node.find_parent(["script", "style"])
            or _inside_semantic_footnote(node)
        ):
            continue
        unresolved += sum(
            1 for match in NOTE_MARKER.finditer(str(node)) if int(match.group(1)) in definitions
        )
    return unresolved


def _replace_text_marker(
    soup: Any,
    node: Any,
    definitions: set[int],
    reference_ids: Dict[int, List[str]],
    expected_reference_counts: Optional[Mapping[int, int]] = None,
) -> Tuple[int, int]:
    from bs4 import NavigableString

    value = str(node)
    matches = [match for match in NOTE_MARKER.finditer(value) if int(match.group(1)) in definitions]
    if not matches:
        return 0, 0
    pieces: List[Any] = []
    cursor = 0
    suppressed = 0
    for match in matches:
        if match.start() > cursor:
            pieces.append(NavigableString(value[cursor : match.start()]))
        number = int(match.group(1))
        expected = (expected_reference_counts or {}).get(number)
        if expected is not None and len(reference_ids[number]) >= expected:
            suppressed += 1
            cursor = match.end()
            continue
        identifier = f"kindle-noteref-{number}-{len(reference_ids[number]) + 1}"
        reference_ids[number].append(identifier)
        sup = soup.new_tag("sup")
        _add_class(sup, "kindle-noteref")
        anchor = soup.new_tag("a", id=identifier, href=f"#kindle-note-{number}")
        anchor["epub:type"] = "noteref"
        anchor.string = match.group(0)
        sup.append(anchor)
        pieces.append(sup)
        cursor = match.end()
    if cursor < len(value):
        pieces.append(NavigableString(value[cursor:]))
    for piece in pieces:
        node.insert_before(piece)
    node.extract()
    return len(matches) - suppressed, suppressed


def _footnote_aside(soup: Any, number: int, text: str) -> Any:
    aside = soup.new_tag("aside", id=f"kindle-note-{number}")
    aside["epub:type"] = "footnote"
    _add_class(aside, "kindle-footnote")
    paragraph = soup.new_tag("p")
    paragraph.string = text
    aside.append(paragraph)
    return aside


def _synthesize_notes(
    soup: Any,
    fallback_definitions: Optional[Mapping[int, str]] = None,
    expected_reference_counts: Optional[Mapping[int, int]] = None,
) -> Dict[str, int]:
    if any("noteref" in str(tag.get("epub:type", "")).split() for tag in soup.find_all(True)):
        return {
            "synthesized_noterefs": 0,
            "synthesized_footnotes": 0,
            "unlinked_footnotes": 0,
            "unresolved_noteref_markers": 0,
            "recovered_pdf_footnotes": 0,
            "placeholder_footnotes": 0,
            "source_body_noteref_expectations": 0,
            "source_reference_mismatches": 0,
            "deduplicated_noteref_markers": 0,
        }
    blocks = [
        block
        for block in soup.find_all(["p", "li", "pre"])
        if not block.find_parent("nav") and not _inside_semantic_footnote(block)
    ]
    definition_blocks = _definition_run(blocks)
    normalized_fallbacks = {
        int(number): " ".join(str(text).split())
        for number, text in (fallback_definitions or {}).items()
        if str(number).isdigit() and str(text).strip()
    }
    definition_blocks = _add_fallback_definition_blocks(
        blocks, definition_blocks, normalized_fallbacks
    )
    if not definition_blocks:
        return {
            "synthesized_noterefs": 0,
            "synthesized_footnotes": 0,
            "unlinked_footnotes": 0,
            "unresolved_noteref_markers": 0,
            "recovered_pdf_footnotes": 0,
            "placeholder_footnotes": 0,
            "source_body_noteref_expectations": 0,
            "source_reference_mismatches": 0,
            "deduplicated_noteref_markers": 0,
        }
    definitions: Dict[int, Any] = {}
    definition_ids = {id(block) for block in definition_blocks}
    first_definition_order = min(index for index, block in enumerate(blocks) if id(block) in definition_ids)
    first_definition_block = blocks[first_definition_order]
    reference_nodes = _reference_text_nodes(soup, first_definition_block)
    for block in definition_blocks:
        text = " ".join(block.get_text(" ", strip=True).split())
        matches = list(NOTE_MARKER.finditer(text))
        prefix = text[: matches[0].start()].strip() if matches else ""
        replacements: List[Any] = []
        if prefix:
            prefix_tag = soup.new_tag("p")
            _add_class(prefix_tag, "kindle-note-prefix")
            prefix_tag.string = prefix
            replacements.append(prefix_tag)
        for position, match in enumerate(matches):
            number = int(match.group(1))
            if number in definitions:
                continue
            end = matches[position + 1].start() if position + 1 < len(matches) else len(text)
            segment = text[match.start() : end].strip()
            aside = _footnote_aside(soup, number, segment)
            definitions[number] = aside
            replacements.append(aside)
        if block.name == "li":
            # EPUB content models require an ol/ul child to remain an li.
            # Put semantic footnotes inside that list item instead of making
            # aside elements illegal direct children of the list container.
            block.clear()
            for replacement in replacements:
                block.append(replacement)
        else:
            for replacement in replacements:
                block.insert_before(replacement)
            block.decompose()

    body_marker_numbers = {
        int(match.group(1))
        for node in reference_nodes
        if node.parent is not None
        for match in NOTE_MARKER.finditer(str(node))
    }
    max_definition_number = max(definitions, default=0)
    recoverable_numbers = sorted(
        number
        for number in body_marker_numbers
        if number not in definitions
        and (number in normalized_fallbacks or (max_definition_number and number <= max_definition_number))
    )
    recovered_pdf_footnotes = 0
    placeholder_footnotes = 0
    if recoverable_numbers:
        recovered_section = soup.new_tag("section")
        _add_class(recovered_section, "kindle-recovered-notes")
        for number in recoverable_numbers:
            text = normalized_fallbacks.get(number)
            if text:
                recovered_pdf_footnotes += 1
            else:
                text = f"[{number}] 源 PDF 未提供可恢复的注释内容。"
                placeholder_footnotes += 1
            aside = _footnote_aside(soup, number, text)
            definitions[number] = aside
            recovered_section.append(aside)
        (soup.body or soup.html).append(recovered_section)

    normalized_expected_counts = {
        int(number): int(count)
        for number, count in (expected_reference_counts or {}).items()
        if str(number).isdigit() and int(count) > 0
    }
    reference_ids: Dict[int, List[str]] = defaultdict(list)
    synthesized_noterefs = 0
    deduplicated_noteref_markers = 0
    for node in reference_nodes:
        if node.parent is None or node.find_parent(["script", "style"]) or _inside_semantic_footnote(node):
            continue
        anchor = node.find_parent("a")
        if anchor is not None:
            anchor_text = " ".join(anchor.get_text(" ", strip=True).split())
            anchor_match = NOTE_MARKER.fullmatch(anchor_text)
            if anchor_match is None or int(anchor_match.group(1)) not in definitions:
                continue
            from bs4 import NavigableString

            node = NavigableString(anchor_text)
            anchor.replace_with(node)
        linked, suppressed = _replace_text_marker(
            soup,
            node,
            set(definitions),
            reference_ids,
            normalized_expected_counts or None,
        )
        synthesized_noterefs += linked
        deduplicated_noteref_markers += suppressed

    for number, aside in definitions.items():
        identifiers = reference_ids.get(number, [])
        if not identifiers:
            continue
        paragraph = aside.find("p") or aside
        paragraph.append(" ")
        for index, identifier in enumerate(identifiers, 1):
            backlink = soup.new_tag("a", href=f"#{identifier}")
            _add_class(backlink, "kindle-footnote-backlink")
            backlink["aria-label"] = "返回正文"
            backlink.string = "↩" if len(identifiers) == 1 else f"↩{index}"
            paragraph.append(backlink)
            if index < len(identifiers):
                paragraph.append(" ")
    unresolved_noteref_markers = _count_unresolved_reference_markers(reference_nodes, set(definitions))
    actual_reference_counts = {
        number: len(identifiers) for number, identifiers in reference_ids.items() if identifiers
    }
    source_reference_mismatches = sum(
        abs(normalized_expected_counts.get(number, 0) - actual_reference_counts.get(number, 0))
        for number in set(normalized_expected_counts).union(actual_reference_counts)
    ) if normalized_expected_counts else 0
    return {
        "synthesized_noterefs": synthesized_noterefs,
        "synthesized_footnotes": len(definitions),
        "unlinked_footnotes": sum(1 for number in definitions if not reference_ids.get(number)),
        "unresolved_noteref_markers": unresolved_noteref_markers,
        "recovered_pdf_footnotes": recovered_pdf_footnotes,
        "placeholder_footnotes": placeholder_footnotes,
        "source_body_noteref_expectations": sum(normalized_expected_counts.values()),
        "source_reference_mismatches": source_reference_mismatches,
        "deduplicated_noteref_markers": deduplicated_noteref_markers,
    }


def _fold_context(text: str) -> str:
    return CONTEXT_FOLD.sub(lambda match: '"' if match.group() in QUOTE_MARKS else "—", text)


class TextLayerEvidence:
    """The PDF's own text layer, used to confirm each mark before it is restored.

    Docling folds "——" and curly quotes, but the text layer still has them. A
    candidate is restored only when the text layer has the same mark between
    the same visible neighbours, never shows those neighbours around another
    form of the mark (a literal "--" or straight quote elsewhere makes the
    context ambiguous), and has at least as many such places as the book has
    candidates with that context. Neighbours are compared folded (see
    ``CONTEXT_FOLD``), so a neighbour that is itself unconfirmed cannot vouch
    for this mark. The candidate count comes from a dry run of the same stage
    on the same document (``begin_dry_run``); a context the dry run never saw
    is refused.
    """

    def __init__(self, text: str) -> None:
        key = WHITESPACE.sub("", unicodedata.normalize("NFC", text))
        # offsets[i]: where raw character i lands in the folded context string.
        # tokens[j]: the raw text behind folded character j.
        offsets = [0] * (len(key) + 1)
        tokens: List[str] = []
        cursor = 0
        for match in CONTEXT_FOLD.finditer(key):
            for index in range(cursor, match.start()):
                offsets[index] = len(tokens)
                tokens.append(key[index])
            for index in range(match.start(), match.end()):
                offsets[index] = len(tokens)
            tokens.append(match.group())
            cursor = match.end()
        for index in range(cursor, len(key)):
            offsets[index] = len(tokens)
            tokens.append(key[index])
        offsets[len(key)] = len(tokens)
        self._tokens = tokens
        self._context = _fold_context(key)
        self._full: Counter = Counter()
        self._left: Counter = Counter()
        self._right: Counter = Counter()
        for match in EVIDENCE_MARK.finditer(key):
            start, end = offsets[match.start()], offsets[match.end()]
            before = self._context[max(0, start - EVIDENCE_WINDOW) : start]
            after = self._context[end : end + EVIDENCE_WINDOW]
            self._full[(before, match.group(), after)] += 1
            self._left[(before, match.group())] += 1
            self._right[(match.group(), after)] += 1
        self.recording = False
        self._demand: Counter = Counter()
        self._used: Counter = Counter()

    def begin_dry_run(self) -> None:
        self.recording = True
        self._demand.clear()
        self._used.clear()

    def end_dry_run(self) -> None:
        self.recording = False

    def _count(self, before: str, mark: str, after: str) -> int:
        if not mark:  # No index for "nothing between"; it needs both sides.
            return self._context.count(before + after) if before and after else 0
        if len(before) == EVIDENCE_WINDOW and len(after) == EVIDENCE_WINDOW:
            return self._full[(before, mark, after)]
        if len(before) == EVIDENCE_WINDOW and not after:
            return self._left[(before, mark)]
        if len(after) == EVIDENCE_WINDOW and not before:
            return self._right[(mark, after)]
        # Short on both sides (a block's first or last words): scan the folded
        # text and check the exact mark behind each hit.
        pattern = before + _fold_context(mark) + after
        count = 0
        found = self._context.find(pattern)
        while found >= 0:
            if self._tokens[found + len(before)] == mark:
                count += 1
            found = self._context.find(pattern, found + 1)
        return count

    def confirms(self, before: str, mark: str, after: str, alternatives: Iterable[str]) -> bool:
        """Whether the text layer backs ``mark`` here and no ``alternatives``."""
        before = _fold_context(WHITESPACE.sub("", unicodedata.normalize("NFC", before)))[-EVIDENCE_WINDOW:]
        after = _fold_context(WHITESPACE.sub("", unicodedata.normalize("NFC", after)))[:EVIDENCE_WINDOW]
        if len(before) + len(after) < EVIDENCE_WINDOW:
            return False  # Too little context to tell this mark from any other.
        supply = self._count(before, mark, after)
        if not supply or any(self._count(before, other, after) for other in alternatives):
            return False
        window = (before, mark, after)
        if self.recording:
            self._demand[window] += 1
            return True
        demand = self._demand.get(window, 0)
        if not demand or demand > supply or self._used[window] >= supply:
            return False
        self._used[window] += 1
        return True


def _cjk_dominant(text: str) -> bool:
    cjk = len(CJK_TEXT.findall(text))
    return cjk > 0 and cjk > len(LATIN_LETTER.findall(text))


def _inside_code(node: Any) -> bool:
    return any(getattr(parent, "name", None) in {"pre", "code"} for parent in node.parents)


def _visible_strings(node: Any) -> List[Any]:
    """Text a reader sees: no comments, CDATA, doctypes, script or style text."""
    from bs4 import NavigableString

    return [item for item in node.find_all(string=True) if type(item) is NavigableString]


def _hides(tag: Any) -> bool:
    return (
        tag.name in HIDING_TAGS
        or tag.has_attr("hidden")
        or bool(HIDING_STYLE.search(str(tag.get("style") or "")))
    )


def _has_hidden(node: Any) -> bool:
    """Whether hidden text sits in or around this block.

    Such a block is left as Docling produced it: what a reader sees there is
    not what the HTML says, so no context taken from it can be trusted.
    """
    return any(_hides(tag) for tag in (node, *node.parents, *node.find_all(True)) if getattr(tag, "name", None))


def _inside_table(node: Any) -> bool:
    return any(getattr(parent, "name", None) in {"td", "th"} for parent in node.parents)


def _restore_quotes(soup: Any, text_nodes: Sequence[Any], evidence: TextLayerEvidence) -> Tuple[int, int]:
    """Pair folded quotes per kind; leave the block alone when pairing is unclear.

    Returns (restored, unconfirmed): a paired mark the text layer does not
    confirm keeps its folded form.
    """
    values = [str(node) for node in text_nodes]
    joined = "".join(values)
    if CURLY_QUOTE.search(joined):
        return 0, 0  # Mixed with surviving curly quotes: nesting cannot be recovered.
    positions = [match.start() for match in FOLDED_QUOTE.finditer(joined)]
    doubles = [index for index in positions if joined[index] == '"']
    singles = [index for index in positions if joined[index] == "'"]
    if APOSTROPHE.search(joined):
        singles = []  # don't / students' / don<em>'t</em>: apostrophes, not quotes.
    if len(doubles) % 2 or len(singles) % 2:
        return 0, 0  # An unpaired mark (quote spanning blocks) makes every pairing a guess.
    replacements: Dict[int, str] = {}
    if doubles:
        for order, index in enumerate(doubles):
            replacements[index] = "“" if order % 2 == 0 else "”"
    if singles:
        # Without double quotes in the block, Docling's "'" stood for "“”".
        marks = ("‘", "’") if doubles else ("“", "”")
        for order, index in enumerate(singles):
            replacements[index] = marks[order % 2]
    if not replacements:
        return 0, 0
    # Each mark is checked against the block as Docling left it: a neighbouring
    # quote counts only as "some quote", never as already restored.
    confirmed = {
        index: mark
        for index, mark in replacements.items()
        if evidence.confirms(
            joined[:index], mark, joined[index + 1 :], [other for other in QUOTE_MARKS if other != mark]
        )
    }
    unconfirmed = len(replacements) - len(confirmed)
    replacements = confirmed
    offset = 0
    for node, value in zip(text_nodes, values):
        local = {index - offset: mark for index, mark in replacements.items() if offset <= index < offset + len(value)}
        offset += len(value)
        if local:
            node.replace_with(
                soup.new_string("".join(local.get(i, character) for i, character in enumerate(value)))
            )
    return len(replacements), unconfirmed


def _adjacent_paragraph(paragraph: Any, forward: bool) -> Any:
    """The neighbouring p node, skipping only whitespace and comments."""
    from bs4 import Comment

    sibling = paragraph.next_sibling if forward else paragraph.previous_sibling
    while sibling is not None and getattr(sibling, "name", None) is None:
        if isinstance(sibling, Comment) or not str(sibling).strip():
            sibling = sibling.next_sibling if forward else sibling.previous_sibling
            continue
        return None  # Visible bare text sits between them.
    if sibling is None or sibling.name != "p" or _inside_code(sibling) or _has_hidden(sibling):
        return None
    return sibling if _cjk_dominant(sibling.get_text("", strip=False)) else None


def _restore_cjk_punctuation(soup: Any, text_layer: Optional[str]) -> Dict[str, int]:
    """Undo Docling's lossy CJK punctuation folding without touching code or Latin prose.

    Text patterns only propose candidates; each one is applied only when the
    PDF text layer confirms it. Without a text layer (scanned books) dashes,
    quotes and lone marks are left exactly as Docling produced them.
    """
    page_numbers = 0
    for item in list(soup.find_all("li")):
        parent = item.parent
        if parent is None or parent.name not in {"ul", "ol"} or _inside_table(parent) or _inside_code(parent):
            continue
        if len(parent.find_all("li", recursive=False)) != 1 or item.find(True) is not None:
            continue
        if parent.get("id") or item.get("id"):
            continue
        if PAGE_NUMBER_ITEM.fullmatch(item.get_text("", strip=True)):
            page_numbers += 1
            parent.decompose()
    evidence = TextLayerEvidence(text_layer) if text_layer else None
    metrics = {"restored_cjk_dashes": 0, "restored_cjk_quotes": 0, "merged_lone_punctuation": 0}
    unconfirmed = 0
    if evidence is not None:
        # Dashes, then lone marks, then quotes: lone marks go before quotes so a
        # quote's context already has its full stop back ("”母親說。“"). Before
        # each stage a dry run on a copy of the document as it now stands counts
        # how many candidates share each context, so a context the text layer
        # has fewer times than the book is left alone.
        for name, stage in (
            ("restored_cjk_dashes", _restore_dashes),
            ("merged_lone_punctuation", _merge_lone_marks),
            ("restored_cjk_quotes", _restore_block_quotes),
        ):
            evidence.begin_dry_run()
            stage(copy.copy(soup), evidence)
            evidence.end_dry_run()
            restored, skipped = stage(soup, evidence)
            metrics[name] = restored
            unconfirmed += skipped
    return {
        **metrics,
        "removed_page_number_items": page_numbers,
        "punctuation_text_layer_available": int(evidence is not None),
        "unconfirmed_punctuation_restorations": unconfirmed,
    }


def _restore_blocks(soup: Any) -> List[Any]:
    return [
        block
        for block in soup.find_all(CJK_RESTORE_BLOCKS)
        if block.find(CJK_RESTORE_BLOCKS) is None
        and not _inside_code(block)
        and not _has_hidden(block)
        and _cjk_dominant(block.get_text("", strip=False))
    ]


def _restore_dashes(soup: Any, evidence: TextLayerEvidence) -> Tuple[int, int]:
    dashes = unconfirmed = 0
    for block in _restore_blocks(soup):
        if _inside_table(block) or block.name in {"td", "th"}:
            continue
        # Context comes from the whole block, so inline markup cannot clip a
        # neighbour ("<em>-</em>-甲，--乙" still reads "--甲，" before the dash).
        nodes = _visible_strings(block)
        values = [str(node) for node in nodes]
        joined = "".join(values)
        starts = []
        offset = 0
        for value in values:
            starts.append(offset)
            offset += len(value)
        edits: Dict[int, List[Tuple[int, int]]] = {}
        for match in CJK_DOUBLE_HYPHEN.finditer(joined):
            owner = next(
                (
                    index
                    for index, start in enumerate(starts)
                    if start <= match.start() and match.end() <= start + len(values[index])
                ),
                None,
            )
            if owner is None or _inside_code(nodes[owner]):
                unconfirmed += 1  # Split across tags or inside code: never rewritten.
                continue
            if evidence.confirms(joined[: match.start()], "——", joined[match.end() :], ("--", "—", "-")):
                edits.setdefault(owner, []).append((match.start() - starts[owner], match.end() - starts[owner]))
            else:
                unconfirmed += 1
        for owner, spans in edits.items():
            value = values[owner]
            pieces: List[str] = []
            cursor = 0
            for start, end in spans:
                pieces.extend((value[cursor:start], "——"))
                cursor = end
            dashes += len(spans)
            nodes[owner].replace_with(soup.new_string("".join(pieces) + value[cursor:]))
    return dashes, unconfirmed


def _merge_lone_marks(soup: Any, evidence: TextLayerEvidence) -> Tuple[int, int]:
    merged = unconfirmed = 0
    for paragraph in list(soup.find_all("p")):
        if paragraph.parent is None:
            continue
        text = paragraph.get_text("", strip=False)
        if not LONE_PUNCTUATION.fullmatch(text) or paragraph.find(True) is not None:
            continue
        if paragraph.get("id") or _inside_code(paragraph) or _has_hidden(paragraph):
            continue
        # Merge only into the paragraph directly before it, when that one is
        # left without an ending; never across headings, lists or other blocks.
        mark = text.strip()
        alternatives = ["", *(other for other in LONE_MARKS if other != mark)]
        previous = _adjacent_paragraph(paragraph, forward=False)
        following = _adjacent_paragraph(paragraph, forward=True)
        if previous is not None and not SENTENCE_END.search(previous.get_text("", strip=False)):
            after = following.get_text("", strip=False) if following is not None else ""
            if evidence.confirms(previous.get_text("", strip=False), mark, after, alternatives):
                previous.append(soup.new_string(mark))
                paragraph.decompose()
                merged += 1
            else:
                unconfirmed += 1
            continue
        # Docling sometimes lifts the full stop after a dialogue tag ("”母亲说。“")
        # into its own block; reinsert it only when the gap is unambiguous.
        if following is None:
            continue
        gaps = [
            (node, match)
            for node in _visible_strings(following)
            if not _inside_code(node)
            for match in DIALOGUE_GAP.finditer(str(node))
        ]
        if len(gaps) != 1:
            continue
        node, match = gaps[0]
        value = str(node)
        # Read the gap with the whole block around it, as the dashes are.
        strings = _visible_strings(following)
        position = next(index for index, other in enumerate(strings) if other is node)
        before_node = "".join(str(other) for other in strings[:position])
        after_node = "".join(str(other) for other in strings[position + 1 :])
        if not evidence.confirms(
            before_node + value[: match.end(1)], mark, value[match.end(1) :] + after_node, alternatives
        ):
            unconfirmed += 1
            continue
        node.replace_with(soup.new_string(value[: match.end(1)] + mark + value[match.end(1) :]))
        paragraph.decompose()
        merged += 1
    return merged, unconfirmed


def _restore_block_quotes(soup: Any, evidence: TextLayerEvidence) -> Tuple[int, int]:
    quotes = unconfirmed = 0
    for block in _restore_blocks(soup):
        if any(_inside_code(node) and str(node).strip() for node in _visible_strings(block)):
            # Leaving inline code out would splice the text around it into a
            # context the book never has ("示例文本""供读者"), so such a block
            # keeps its quotes as Docling left them.
            continue
        text_nodes = _visible_strings(block)
        restored, skipped = _restore_quotes(soup, text_nodes, evidence)
        quotes += restored
        unconfirmed += skipped
    return quotes, unconfirmed


def enrich_html(
    html: str,
    document: Mapping[str, Any],
    outline: Sequence[Mapping[str, Any]],
    fallback_definitions: Optional[Mapping[int, str]] = None,
    expected_reference_counts: Optional[Mapping[int, int]] = None,
    text_layer: Optional[str] = None,
) -> Tuple[str, Dict[str, int]]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    if soup.html is None:
        raise ValueError("HTML root missing")
    soup.html["xmlns:epub"] = EPUB_NS
    for style in list(soup.find_all("style")):
        if DECORATIVE_STYLE.search(style.get_text(" ", strip=False)):
            style.decompose()
    if soup.head is None:
        head = soup.new_tag("head")
        soup.html.insert(0, head)
    semantic_style = soup.new_tag("style")
    semantic_style.string = (
        "html,body{margin:0!important;padding:0!important;}"
        ".kindle-toc-anchor{display:block;height:0;overflow:hidden;line-height:0;font-size:0;color:transparent;}"
        ".kindle-footnote{margin:.6em 0;}"
    )
    soup.head.append(semantic_style)
    punctuation_metrics = _restore_cjk_punctuation(soup, text_layer)
    source_outline_entries = len(outline)
    effective_outline = list(outline) if outline else _derived_outline(document)
    mapped, generated, title_matched, page_start_anchors, mapped_tags = _map_outline(
        soup, document, effective_outline
    )
    removed_tables, remaining_tables = _remove_printed_toc_tables(soup, effective_outline)
    note_metrics = _synthesize_notes(soup, fallback_definitions, expected_reference_counts)
    metrics = {
        "source_outline_entries": source_outline_entries,
        "outline_entries": len(effective_outline),
        "derived_outline_entries": len(effective_outline) if not source_outline_entries else 0,
        "mapped_outline_entries": mapped,
        "title_matched_outline_entries": title_matched,
        "page_start_outline_anchors": page_start_anchors,
        "generated_outline_anchors": generated,
        "unmapped_outline_entries": max(0, len(effective_outline) - mapped),
        "printed_toc_tables_removed": removed_tables,
        "remaining_printed_toc_tables": remaining_tables,
        "destination_outline_entries": sum(
            1 for item in effective_outline if item.get("page_resolution") == "destination"
        ),
        "title_search_outline_entries": sum(
            1 for item in effective_outline if item.get("page_resolution") == "title-search"
        ),
        "neighbor_page_outline_entries": sum(
            1 for item in effective_outline if item.get("page_resolution") == "neighbor-title-page"
        ),
        "neighbor_next_page_outline_entries": sum(
            1 for item in effective_outline if item.get("page_resolution") == "neighbor-next-page"
        ),
        "interpolated_outline_entries": sum(
            1 for item in effective_outline if item.get("page_resolution") == "interpolated"
        ),
        **note_metrics,
        **punctuation_metrics,
    }
    return str(soup), metrics


def main() -> int:
    parser = argparse.ArgumentParser(description="Enrich private Docling HTML with Kindle semantics")
    parser.add_argument("--html", type=Path, required=True)
    parser.add_argument("--document", type=Path, required=True)
    parser.add_argument("--outline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--fallback-definitions", type=Path)
    parser.add_argument("--expected-reference-counts", type=Path)
    parser.add_argument("--text-layer", type=Path, help="Plain text of the PDF's own text layer.")
    args = parser.parse_args()
    html = args.html.read_text(encoding="utf-8")
    document = json.loads(args.document.read_text(encoding="utf-8"))
    outline = json.loads(args.outline.read_text(encoding="utf-8"))
    fallback_definitions: Dict[int, str] = {}
    if args.fallback_definitions is not None:
        raw_fallbacks = json.loads(args.fallback_definitions.read_text(encoding="utf-8"))
        fallback_definitions = {int(number): str(text) for number, text in raw_fallbacks.items()}
    expected_reference_counts: Dict[int, int] = {}
    if args.expected_reference_counts is not None:
        raw_counts = json.loads(args.expected_reference_counts.read_text(encoding="utf-8"))
        expected_reference_counts = {int(number): int(count) for number, count in raw_counts.items()}
    text_layer = args.text_layer.read_text(encoding="utf-8") if args.text_layer is not None else None
    enriched, metrics = enrich_html(
        html, document, outline, fallback_definitions, expected_reference_counts, text_layer
    )
    args.output.write_text(enriched, encoding="utf-8")
    args.report.write_text(json.dumps(metrics, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    print(json.dumps(metrics, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
