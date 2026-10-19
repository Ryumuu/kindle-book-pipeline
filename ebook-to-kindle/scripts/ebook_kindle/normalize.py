"""Conservative Docling HTML normalization."""

from __future__ import annotations

import os
import re
import posixpath
import uuid
import zipfile
from collections import Counter
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable, List, Mapping, Optional, Tuple
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree


HEADING_TAG = re.compile(r"<(\/?)h([1-6])(\b[^>]*)>", re.I)
PAGE_NUMBER = re.compile(r"^(?:第\s*)?\d{1,5}(?:\s*页)?$")
HTML_CHARSET = re.compile(br"<meta\s+[^>]*charset\s*=\s*['\"]?\s*([a-zA-Z0-9._-]+)", re.I)
HTML_CONTENT_CHARSET = re.compile(br"<meta\s+[^>]*content\s*=\s*['\"][^'\"]*charset\s*=\s*([a-zA-Z0-9._-]+)", re.I)
CSS_URL = re.compile(r"url\(\s*(?:['\"]([^'\"]+)['\"]|([^)'\"\s]+))\s*\)", re.I)
CSS_IMPORT = re.compile(r"@import\s+(?!url\()(?:['\"]([^'\"]+)['\"]|([^;\s]+))", re.I)
MARKDOWN_INLINE_IMAGE = re.compile(r"!\[[^\]]*\]\(\s*(?:<([^>]+)>|([^\s)]+))", re.M)
MARKDOWN_REFERENCE_IMAGE = re.compile(r"!\[([^\]]*)\]\[([^\]]*)\]", re.M)
MARKDOWN_SHORTCUT_IMAGE = re.compile(r"!\[([^\]]+)\](?![\[(])", re.M)
MARKDOWN_REFERENCE_DEFINITION = re.compile(r"^\s*\[((?!\^)[^\]]+)\]:\s*(?:<([^>]+)>|([^\s]+))", re.M)
TEXT_CHAPTER_LINE = re.compile(
    r"^\s*(?:第\s*[一二三四五六七八九十百零〇0-9]+\s*[章节篇部卷]|chapter\s+\d+\b).*$",
    re.I,
)
CJK_RANGE = "\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
CJK_CHARACTER = re.compile(f"[{CJK_RANGE}]")
CJK_INTERNAL_SPACE = re.compile(f"(?<=[{CJK_RANGE}])[ \t\r\n\u00a0\u3000]+(?=[{CJK_RANGE}])")
SPACE_BEFORE_CJK_PUNCTUATION = re.compile(r"[ \t\r\n\u00a0\u3000]+(?=[，。！？；：、）》】」』”’])")
SPACE_AFTER_CJK_OPENING = re.compile(r"(?<=[（《【「『“‘])[ \t\r\n\u00a0\u3000]+")
CSS_RULE = re.compile(r"([^{}]+)\{([^{}]*)\}", re.S)
CSS_FONT_SIZE = re.compile(r"(?:^|;)\s*font-size\s*:\s*([^;!}]+)", re.I)
CSS_FONT_FAMILY = re.compile(r"(?:^|;)\s*font-family\s*:", re.I)
CSS_LINE_HEIGHT = re.compile(r"(?:^|;)\s*line-height\s*:\s*([^;!}]+)", re.I)
INLINE_TYPOGRAPHY = re.compile(r"(?:^|;)\s*(?:font-size|font-family|line-height)\s*:", re.I)
LIST_MARKER = re.compile(r"^(?:[•·●○▪▫→➤▶▸★☆《〈]|\(?\d{1,4}[.)、]|[一二三四五六七八九十]+[、.)])")
PROSE_BLOCKS = {"p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "figcaption", "dt", "dd", "td", "th"}
HEADING_BLOCKS = {"h1", "h2", "h3", "h4", "h5", "h6"}
PRESERVE_WHITESPACE_BLOCKS = {"pre", "code", "script", "style", "textarea"}
NON_PROSE_CONTAINERS = {"nav", "aside", "table"}
SEMANTIC_CLASS = re.compile(r"(?:poem|poetry|verse|stanza|lyrics|code|pre|footnote|endnote|toc|landmark|furniture)", re.I)
TERMINAL_PUNCTUATION = set("。！？!?;；:：")
CLOSING_PUNCTUATION = set("”’」』》）】〕〗〙〛'\"")


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: List[str] = []
        self.body_parts: List[str] = []
        self.saw_body = False
        self.in_body = 0
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        name = tag.lower()
        if name == "body":
            self.saw_body = True
            self.in_body += 1
            return
        if name in {"head", "script", "style", "title", "template", "noscript"}:
            self.skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        name = tag.lower()
        if name == "body":
            self.in_body = max(0, self.in_body - 1)
            return
        if name in {"head", "script", "style", "title", "template", "noscript"}:
            self.skip_depth = max(0, self.skip_depth - 1)

    def handle_data(self, data: str) -> None:
        if self.skip_depth:
            return
        self.parts.append(data)
        if self.in_body:
            self.body_parts.append(data)


class _ResourceExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.references: List[Tuple[str, str]] = []
        self.style_depth = 0
        self.style_parts: List[str] = []

    def _add(self, value: Optional[str], kind: str) -> None:
        if value and value.strip():
            self.references.append((value.strip(), kind))

    def _add_srcset(self, value: Optional[str]) -> None:
        if not value:
            return
        # A srcset URL token may contain commas (notably a data URI). Candidate
        # delimiters are a trailing token comma or the comma after descriptors.
        remaining = value.strip()
        while remaining:
            remaining = remaining.lstrip(" \t\r\n,")
            if not remaining:
                break
            url_end = 0
            while url_end < len(remaining) and not remaining[url_end].isspace():
                url_end += 1
            token = remaining[:url_end]
            trailing_separator = token.endswith(",")
            self._add(token.rstrip(","), "image")
            remaining = remaining[url_end:]
            if trailing_separator:
                continue
            depth = 0
            separator = None
            for index, character in enumerate(remaining):
                if character == "(":
                    depth += 1
                elif character == ")" and depth:
                    depth -= 1
                elif character == "," and depth == 0:
                    separator = index + 1
                    break
            if separator is None:
                break
            remaining = remaining[separator:]

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        name = tag.lower().rsplit(":", 1)[-1]
        values = {key.lower(): value for key, value in attrs}
        if name == "style":
            self.style_depth += 1
        if name in {"img", "source"}:
            self._add(values.get("src"), "image")
            self._add_srcset(values.get("srcset"))
        elif name == "image":
            self._add(values.get("href") or values.get("xlink:href"), "image")
        elif name == "link":
            kind = "stylesheet" if "stylesheet" in (values.get("rel") or "").lower().split() else "resource"
            self._add(values.get("href"), kind)
        elif name in {"audio", "embed", "frame", "iframe", "script", "track"}:
            self._add(values.get("src"), "resource")
        elif name == "video":
            self._add(values.get("src"), "resource")
            self._add(values.get("poster"), "image")
        elif name == "object":
            self._add(values.get("data"), "resource")
        elif name == "input" and (values.get("type") or "").lower() == "image":
            self._add(values.get("src"), "image")
        elif name in {"feimage", "use"}:
            self._add(values.get("href") or values.get("xlink:href"), "resource")
        elif name in {"img", "ref", "text", "textstream"}:
            # SMIL uses src on these elements. The XHTML img branch above
            # handles ordinary images before this fallback is reached.
            self._add(values.get("src"), "resource")
        elif name == "base":
            self._add(values.get("href"), "resource")
        if name not in {"img", "source"} and values.get("srcset"):
            self._add_srcset(values.get("srcset"))
        if name in {"body", "table", "td", "th"}:
            self._add(values.get("background"), "image")
        if values.get("style"):
            self.references.extend(css_resource_references(values["style"] or ""))

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "style" and self.style_depth:
            self.style_depth -= 1
            if not self.style_depth:
                self.references.extend(css_resource_references("".join(self.style_parts)))
                self.style_parts = []

    def handle_data(self, data: str) -> None:
        if self.style_depth:
            self.style_parts.append(data)


def normalize_heading_levels(html: str) -> str:
    levels = [int(match.group(2)) for match in HEADING_TAG.finditer(html) if not match.group(1)]
    if not levels:
        return html
    shift = min(levels) - 1
    if shift <= 0:
        return html

    def replace(match: re.Match[str]) -> str:
        level = max(1, int(match.group(2)) - shift)
        return f"<{match.group(1)}h{level}{match.group(3)}>"

    return HEADING_TAG.sub(replace, html)


def select_unique_furniture(candidates: Iterable[str], body_text: str) -> List[str]:
    cleaned = [re.sub(r"\s+", " ", item).strip() for item in candidates]
    counts = Counter(item for item in cleaned if item)
    body_normalized = re.sub(r"\s+", " ", body_text)
    selected = []
    for item in cleaned:
        if not item or counts[item] != 1 or PAGE_NUMBER.match(item):
            continue
        if len(item) < 2 or len(item) > 160 or item in body_normalized:
            continue
        if item not in selected:
            selected.append(item)
    return selected


def html_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    return " ".join(parser.body_parts if parser.saw_body else parser.parts)


def css_resource_references(css: str) -> List[Tuple[str, str]]:
    references = [((quoted or bare).strip(), "style-resource") for quoted, bare in CSS_URL.findall(css) if quoted or bare]
    references.extend(((quoted or bare).strip(), "stylesheet") for quoted, bare in CSS_IMPORT.findall(css) if quoted or bare)
    return references


def html_resource_references(html: str) -> List[Tuple[str, str]]:
    parser = _ResourceExtractor()
    parser.feed(html)
    return parser.references


def markdown_image_references(value: str) -> List[Tuple[str, str]]:
    references = [((angle or plain).strip(), "image") for angle, plain in MARKDOWN_INLINE_IMAGE.findall(value)]
    definitions = {
        label.strip().casefold(): (angle or plain).strip()
        for label, angle, plain in MARKDOWN_REFERENCE_DEFINITION.findall(value)
    }
    for alt, label in MARKDOWN_REFERENCE_IMAGE.findall(value):
        key = (label or alt).strip().casefold()
        if key in definitions:
            references.append((definitions[key], "image"))
    for label in MARKDOWN_SHORTCUT_IMAGE.findall(value):
        key = label.strip().casefold()
        if key in definitions:
            references.append((definitions[key], "image"))
    return references


def archive_member_path(base: str, href: str) -> Optional[str]:
    """Resolve an EPUB/OOXML member path without escaping the ZIP root."""
    raw = href.strip()
    parsed = urlsplit(raw)
    if parsed.scheme.lower() == "data":
        return None
    if parsed.scheme or parsed.netloc or raw.startswith("//"):
        raise ValueError("remote archive resource")
    path = unquote(parsed.path)
    if not path or path.startswith("/"):
        raise ValueError("invalid archive resource")
    target = posixpath.normpath(posixpath.join(base, path))
    if target == ".." or target.startswith("../") or target.startswith("/"):
        raise ValueError("archive resource escaped root")
    return target


def decode_document_bytes(payload: bytes, *, html: bool = False) -> str:
    encodings = []
    if payload.startswith((b"\xff\xfe", b"\xfe\xff")):
        encodings.append("utf-16")
    if payload.startswith(b"\xef\xbb\xbf"):
        encodings.append("utf-8-sig")
    if html:
        head = payload[:8192]
        match = HTML_CHARSET.search(head) or HTML_CONTENT_CHARSET.search(head)
        if match:
            encodings.append(match.group(1).decode("ascii", errors="ignore"))
    encodings.extend(("utf-8", "gb18030"))
    tried = set()
    for encoding in encodings:
        normalized = encoding.strip().lower()
        if not normalized or normalized in tried:
            continue
        tried.add(normalized)
        try:
            return payload.decode(normalized, errors="strict")
        except (LookupError, UnicodeDecodeError):
            continue
    raise UnicodeError("unsupported or invalid document encoding")


def markdown_visible_text(value: str) -> str:
    value = re.sub(r"\A\ufeff?(?:---|\+\+\+)\s*\n.*?\n(?:---|\+\+\+)\s*(?:\n|\Z)", "", value, flags=re.S)
    definitions = {
        label.strip().casefold(): (angle or plain).strip()
        for label, angle, plain in MARKDOWN_REFERENCE_DEFINITION.findall(value)
    }
    value = MARKDOWN_REFERENCE_DEFINITION.sub("", value)
    value = re.sub(r"^\s*```[^\n]*$", "", value, flags=re.M)
    value = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", value)
    value = re.sub(r"!\[[^\]]*\]\[[^\]]*\]", " ", value)
    value = re.sub(r"!\[[^\]]+\](?![\[(])", " ", value)
    value = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", value)
    value = re.sub(r"(?<!!)\[([^\]]+)\]\[[^\]]*\]", r"\1", value)

    def shortcut_link(match: re.Match[str]) -> str:
        label = match.group(1)
        return label if label.strip().casefold() in definitions else match.group(0)

    value = re.sub(r"(?<!!)\[([^\]]+)\](?![\[(])", shortcut_link, value)
    value = re.sub(r"`([^`]+)`", r"\1", value)
    value = re.sub(r"^\s*(?:={3,}|-{3,}|\*{3,}|_{3,})\s*$", "", value, flags=re.M)
    value = re.sub(r"^\s*\|?(?:\s*:?-{3,}:?\s*\|)+(?:\s*:?-{3,}:?\s*)?\|?\s*$", "", value, flags=re.M)
    value = value.replace("|", " ")
    value = re.sub(r"^\s{0,3}(?:#{1,6}|>+|[-+*]|\d+[.)])\s+", "", value, flags=re.M)
    value = re.sub(r"(?<!\w)[*_~]{1,3}|[*_~]{1,3}(?!\w)", "", value)
    value = re.sub(r"\\([\\`*{}\[\]()#+\-.!_|>~])", r"\1", value)
    value = re.sub(r"<((?:https?|mailto):[^>]+)>", r"\1", value, flags=re.I)
    parser = _TextExtractor()
    parser.feed(value)
    return " ".join(parser.parts)


def source_body_text(path: Path) -> str:
    """Extract source-authored body text without metadata or generated titles."""
    suffix = path.suffix.lower()
    if suffix == ".txt":
        return decode_document_bytes(path.read_bytes())
    if suffix in {".md", ".markdown"}:
        return markdown_visible_text(decode_document_bytes(path.read_bytes()))
    if suffix in {".html", ".htm"}:
        return html_text(decode_document_bytes(path.read_bytes(), html=True))
    if suffix == ".docx":
        with zipfile.ZipFile(path) as archive:
            root = ElementTree.fromstring(archive.read("word/document.xml"))
        return " ".join(str(item.text) for item in root.iter() if item.tag.endswith("}t") and item.text)
    if suffix == ".epub":
        with zipfile.ZipFile(path) as archive:
            container = ElementTree.fromstring(archive.read("META-INF/container.xml"))
            rootfile = next(
                (item.attrib.get("full-path") for item in container.iter() if item.tag.endswith("rootfile")),
                None,
            )
            if not rootfile:
                return ""
            opf = ElementTree.fromstring(archive.read(rootfile))
            manifest = {
                item.attrib.get("id", ""): item.attrib.get("href", "")
                for item in opf.iter()
                if item.tag.endswith("item")
            }
            spine = [item.attrib.get("idref", "") for item in opf.iter() if item.tag.endswith("itemref")]
            opf_dir = posixpath.dirname(rootfile)
            parts = []
            for identifier in spine:
                href = manifest.get(identifier, "")
                if not href:
                    continue
                target = archive_member_path(opf_dir, href)
                if target is None:
                    continue
                parts.append(html_text(decode_document_bytes(archive.read(target), html=True)))
        return " ".join(parts)
    return ""


def plain_text_to_semantic_html(source: Path, destination: Path, title: str) -> Mapping[str, int]:
    """Materialize plain-text chapter lines as real HTML headings for Kindle navigation."""
    value = decode_document_bytes(source.read_bytes())
    body: List[str] = []
    paragraph: List[str] = []
    heading_entries = 0

    def flush_paragraph() -> None:
        if paragraph:
            body.append(f"<p>{escape(' '.join(paragraph))}</p>")
            paragraph.clear()

    for raw_line in value.splitlines():
        line = raw_line.strip()
        if not line:
            flush_paragraph()
            continue
        if len(line) <= 160 and TEXT_CHAPTER_LINE.fullmatch(line):
            flush_paragraph()
            body.append(f"<h1>{escape(line)}</h1>")
            heading_entries += 1
        else:
            paragraph.append(line)
    flush_paragraph()
    if heading_entries == 0:
        body.insert(0, "<h1>正文</h1>")
        heading_entries = 1
    document = (
        '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"/>'
        f"<title>{escape(title)}</title></head><body>{''.join(body)}</body></html>"
    )
    destination.write_text(document, encoding="utf-8")
    return {"heading_entries": heading_entries}


def _xml_local_name(value: str) -> str:
    return value.rsplit("}", 1)[-1].rsplit(":", 1)[-1].lower()


def _class_tokens(element: ElementTree.Element) -> set[str]:
    return {item.casefold() for item in element.attrib.get("class", "").split() if item}


def _has_protected_semantics(element: ElementTree.Element) -> bool:
    if element.attrib.get("id"):
        return True
    semantic_values = " ".join(
        value
        for key, value in element.attrib.items()
        if _xml_local_name(key) in {"type", "role"}
    )
    return bool(SEMANTIC_CLASS.search(" ".join(_class_tokens(element))) or SEMANTIC_CLASS.search(semantic_values))


def _is_non_prose_container(element: ElementTree.Element) -> bool:
    semantic_values = " ".join(
        [element.attrib.get("id", ""), element.attrib.get("class", "")]
        + [
            value
            for key, value in element.attrib.items()
            if _xml_local_name(key) in {"type", "role"}
        ]
    )
    return _xml_local_name(element.tag) in NON_PROSE_CONTAINERS or SEMANTIC_CLASS.search(semantic_values) is not None


def _visible_block_text(element: ElementTree.Element) -> str:
    return re.sub(r"\s+", " ", "".join(element.itertext())).strip()


def _normalized_cjk_text(value: str) -> str:
    value = CJK_INTERNAL_SPACE.sub("", value)
    value = SPACE_BEFORE_CJK_PUNCTUATION.sub("", value)
    return SPACE_AFTER_CJK_OPENING.sub("", value)


def _text_slots(element: ElementTree.Element) -> List[Tuple[ElementTree.Element, str]]:
    slots: List[Tuple[ElementTree.Element, str]] = []

    def visit(node: ElementTree.Element) -> None:
        slots.append((node, "text"))
        for child in list(node):
            if _xml_local_name(child.tag) not in PRESERVE_WHITESPACE_BLOCKS:
                visit(child)
            slots.append((child, "tail"))

    visit(element)
    return slots


def _no_space_boundary(left: str, right: str) -> bool:
    if not left or not right:
        return False
    left_character = left[-1]
    right_character = right[0]
    if CJK_CHARACTER.fullmatch(left_character) and (
        CJK_CHARACTER.fullmatch(right_character) or right_character in "，。！？；：、）》】」』”’"
    ):
        return True
    return left_character in "（《【「『“‘" and CJK_CHARACTER.fullmatch(right_character) is not None


def _normalize_block_spacing(element: ElementTree.Element) -> int:
    slots = _text_slots(element)
    removed = 0
    for node, attribute in slots:
        value = getattr(node, attribute) or ""
        normalized = _normalized_cjk_text(value)
        removed += len(value) - len(normalized)
        setattr(node, attribute, normalized)

    previous: Optional[Tuple[ElementTree.Element, str]] = None
    for node, attribute in slots:
        value = getattr(node, attribute) or ""
        if not value.strip():
            continue
        if previous is not None:
            previous_node, previous_attribute = previous
            previous_value = getattr(previous_node, previous_attribute) or ""
            left = previous_value.rstrip(" \t\r\n\u00a0\u3000")
            right = value.lstrip(" \t\r\n\u00a0\u3000")
            if _no_space_boundary(left, right):
                removed += (len(previous_value) - len(left)) + (len(value) - len(right))
                setattr(previous_node, previous_attribute, left)
                setattr(node, attribute, right)
        previous = (node, attribute)
    return removed


def _ends_with_terminal_punctuation(value: str) -> bool:
    value = value.strip()
    value = re.sub(r"(?:\[\s*\d{1,4}\s*\]|①|②|③|④|⑤|⑥|⑦|⑧|⑨|⑩)+$", "", value).rstrip()
    while value and value[-1] in CLOSING_PUNCTUATION:
        value = value[:-1].rstrip()
    return bool(value and value[-1] in TERMINAL_PUNCTUATION)


def _cjk_ratio(value: str) -> float:
    nonspace = [character for character in value if not character.isspace()]
    if not nonspace:
        return 0.0
    return sum(1 for character in nonspace if CJK_CHARACTER.fullmatch(character)) / len(nonspace)


def _linked_list_item(element: ElementTree.Element) -> bool:
    return _xml_local_name(element.tag) == "li" and any(
        _xml_local_name(child.tag) == "a" and bool(child.attrib.get("href"))
        for child in element.iter()
    )


def _join_kind(left: ElementTree.Element, right: ElementTree.Element) -> Optional[str]:
    left_name = _xml_local_name(left.tag)
    right_name = _xml_local_name(right.tag)
    if left_name not in PROSE_BLOCKS or right_name not in PROSE_BLOCKS:
        return None
    if _has_protected_semantics(right):
        return None
    left_text = _visible_block_text(left)
    right_text = _visible_block_text(right)
    combined = left_text + right_text
    if not left_text or not right_text or _cjk_ratio(combined) < 0.35:
        return None
    if _ends_with_terminal_punctuation(left_text) or LIST_MARKER.match(right_text):
        return None
    if left_name == right_name == "p":
        if _class_tokens(left) != _class_tokens(right) or len(left_text) < 8 or len(combined) > 900:
            return None
        return "paragraph"
    if left_name == right_name == "li":
        if (
            _linked_list_item(left)
            or _linked_list_item(right)
            or _class_tokens(left) != _class_tokens(right)
            or len(left_text) < 8
            or len(combined) > 500
        ):
            return None
        return "list"
    if left_name in HEADING_BLOCKS and right_name == left_name:
        if _class_tokens(left) != _class_tokens(right) or len(left_text) < 6 or len(combined) > 120:
            return None
        return "heading"
    if left_name == "p" and right_name in {"h3", "h4", "h5", "h6"}:
        if len(left_text) < 12 or len(right_text) > 100 or len(combined) > 900:
            return None
        return "heading-continuation"
    return None


def _join_separator(left: ElementTree.Element, right: ElementTree.Element) -> str:
    left_text = _visible_block_text(left)
    right_text = _visible_block_text(right)
    if not left_text or not right_text:
        return ""
    return "" if _no_space_boundary(left_text, right_text) else " "


def _append_element_content(left: ElementTree.Element, right: ElementTree.Element) -> None:
    prefix = _join_separator(left, right) + (right.text or "")
    children = list(left)
    if children:
        children[-1].tail = (children[-1].tail or "") + prefix
    else:
        left.text = (left.text or "") + prefix
    for child in list(right):
        right.remove(child)
        left.append(child)
    left.tail = right.tail


def _remove_prose_breaks(element: ElementTree.Element) -> int:
    if SEMANTIC_CLASS.search(" ".join(_class_tokens(element))) or len(_visible_block_text(element)) < 24:
        return 0
    removed = 0
    while True:
        children = list(element)
        changed = False
        for index, child in enumerate(children):
            if _xml_local_name(child.tag) != "br":
                continue
            before_value = element.text if index == 0 else children[index - 1].tail
            after_value = child.tail or ""
            before = (before_value or "").rstrip()
            after = after_value.lstrip()
            if before and after and _no_space_boundary(before, after):
                if index == 0:
                    element.text = before + after
                else:
                    children[index - 1].tail = before + after
                element.remove(child)
                removed += 1
                changed = True
                break
        if not changed:
            return removed


def _normalize_xhtml_tree(root: ElementTree.Element) -> Mapping[str, int]:
    metrics = {
        "cjk_spacing_removed": 0,
        "cjk_hard_breaks_removed": 0,
        "cjk_paragraph_fragments_merged": 0,
        "cjk_heading_fragments_merged": 0,
        "cjk_list_fragments_merged": 0,
    }

    def visit(parent: ElementTree.Element, excluded: bool = False) -> None:
        parent_name = _xml_local_name(parent.tag)
        excluded = excluded or _is_non_prose_container(parent) or parent_name in PRESERVE_WHITESPACE_BLOCKS
        for child in list(parent):
            visit(child, excluded)
        for child in list(parent):
            if _xml_local_name(child.tag) not in PROSE_BLOCKS:
                continue
            metrics["cjk_spacing_removed"] += _normalize_block_spacing(child)
            if not excluded and not _has_protected_semantics(child):
                metrics["cjk_hard_breaks_removed"] += _remove_prose_breaks(child)
        if excluded:
            return
        index = 0
        while index + 1 < len(parent):
            left = parent[index]
            right = parent[index + 1]
            kind = _join_kind(left, right)
            if kind is None:
                index += 1
                continue
            _append_element_content(left, right)
            parent.remove(right)
            metrics["cjk_spacing_removed"] += _normalize_block_spacing(left)
            if kind == "paragraph":
                metrics["cjk_paragraph_fragments_merged"] += 1
            elif kind == "list":
                metrics["cjk_list_fragments_merged"] += 1
            else:
                metrics["cjk_heading_fragments_merged"] += 1
            # Extending ``left`` can raise the CJK ratio across the preceding
            # boundary (common in bilingual PDF fragments). Recheck that
            # boundary instead of leaving a newly joinable pair behind.
            index = max(0, index - 1)

    visit(root)
    return metrics


def _font_size_is_unsafe(selector: str, declaration_body: str, raw_value: str) -> bool:
    value = raw_value.strip().casefold()
    if value == "0":
        return "kindle-toc-anchor" not in selector
    match = re.fullmatch(r"([0-9]*\.?[0-9]+)(em|rem|%)", value)
    if not match:
        return True
    amount = float(match.group(1))
    if match.group(2) == "%":
        amount /= 100.0
    if amount > 1.65:
        return True
    if amount >= 0.95:
        return False
    small_semantic = any(
        token in selector
        for token in ("kindle-noteref", "kindle-footnote", "docling-furniture", "docling-recovered-furniture", "figcaption")
    )
    small_semantic = small_semantic or "vertical-align" in declaration_body.casefold() or "border-top" in declaration_body.casefold()
    return not (small_semantic and amount >= 0.70)


def _line_height_is_unsafe(selector: str, raw_value: str) -> bool:
    value = raw_value.strip().casefold()
    if value == "normal":
        return False
    if value == "0":
        return "kindle-toc-anchor" not in selector
    match = re.fullmatch(r"([0-9]*\.?[0-9]+)(?:em|rem|%)?", value)
    if not match:
        return True
    amount = float(match.group(1))
    if value.endswith("%"):
        amount /= 100.0
    return amount < 1.15 or amount > 2.0


def _css_readability_metrics(css: str) -> Mapping[str, int]:
    metrics = {
        "unsafe_font_size_declarations": 0,
        "font_family_declarations": 0,
        "unsafe_line_height_declarations": 0,
    }
    for raw_selector, declaration_body in CSS_RULE.findall(css):
        selector = re.sub(r"\s+", " ", raw_selector).strip().casefold()
        metrics["font_family_declarations"] += len(CSS_FONT_FAMILY.findall(declaration_body))
        metrics["unsafe_font_size_declarations"] += sum(
            1
            for value in CSS_FONT_SIZE.findall(declaration_body)
            if _font_size_is_unsafe(selector, declaration_body, value)
        )
        metrics["unsafe_line_height_declarations"] += sum(
            1 for value in CSS_LINE_HEIGHT.findall(declaration_body) if _line_height_is_unsafe(selector, value)
        )
    return metrics


def _xhtml_readability_metrics(root: ElementTree.Element) -> Mapping[str, int]:
    metrics = {
        "cjk_spacing_anomalies": 0,
        "suspicious_cjk_block_breaks": 0,
        "prose_hard_breaks": 0,
        "inline_typography_declarations": 0,
    }

    def visit(parent: ElementTree.Element, excluded: bool = False) -> None:
        parent_name = _xml_local_name(parent.tag)
        excluded = excluded or _is_non_prose_container(parent) or parent_name in PRESERVE_WHITESPACE_BLOCKS
        for child in list(parent):
            visit(child, excluded)
        for child in list(parent):
            if INLINE_TYPOGRAPHY.search(child.attrib.get("style", "")):
                metrics["inline_typography_declarations"] += 1
            if _xml_local_name(child.tag) not in PROSE_BLOCKS:
                continue
            slots = _text_slots(child)
            for node, attribute in slots:
                value = getattr(node, attribute) or ""
                metrics["cjk_spacing_anomalies"] += len(CJK_INTERNAL_SPACE.findall(value))
                metrics["cjk_spacing_anomalies"] += len(SPACE_BEFORE_CJK_PUNCTUATION.findall(value))
                metrics["cjk_spacing_anomalies"] += len(SPACE_AFTER_CJK_OPENING.findall(value))
            if excluded or _has_protected_semantics(child):
                continue
            children = list(child)
            for index, element in enumerate(children):
                if _xml_local_name(element.tag) != "br":
                    continue
                before = child.text if index == 0 else children[index - 1].tail
                after = element.tail or ""
                if before and after and _no_space_boundary(before.rstrip(), after.lstrip()):
                    metrics["prose_hard_breaks"] += 1
        if excluded:
            return
        for left, right in zip(list(parent), list(parent)[1:]):
            if _join_kind(left, right) is not None:
                metrics["suspicious_cjk_block_breaks"] += 1

    visit(root)
    return metrics


ICON_LINK_RELS = {"icon", "apple-touch-icon", "apple-touch-icon-precomposed", "mask-icon"}


def _strip_icon_links(root: ElementTree.Element) -> int:
    """Drop web favicon links (e.g. Project Gutenberg's ``<link rel="icon">``).

    They mean nothing to a reader, and Calibre's AZW3 output keeps the link
    while dropping the target, which then fails the roundtrip resource gate.
    """
    removed = 0
    for parent in list(root.iter()):
        for child in list(parent):
            if _xml_local_name(child.tag) != "link":
                continue
            rel = (child.attrib.get("rel") or "").lower().split()
            if ICON_LINK_RELS.intersection(rel):
                parent.remove(child)
                removed += 1
    return removed


def normalize_epub_readability(path: Path) -> Mapping[str, int]:
    """Normalize deterministic Chinese hard-wrap artifacts and illegal filenames in a generated EPUB."""
    import urllib.parse
    ElementTree.register_namespace("", "http://www.w3.org/1999/xhtml")
    ElementTree.register_namespace("epub", "http://www.idpf.org/2007/ops")
    replacements: dict[str, bytes] = {}
    aggregate = {
        "documents_changed": 0,
        "cjk_spacing_removed": 0,
        "cjk_hard_breaks_removed": 0,
        "cjk_paragraph_fragments_merged": 0,
        "cjk_heading_fragments_merged": 0,
        "cjk_list_fragments_merged": 0,
        "icon_links_removed": 0,
    }
    with zipfile.ZipFile(path) as source:
        infos = source.infolist()
        name_map: dict[str, str] = {}
        for info in infos:
            clean = re.sub(r"\s+", "_", info.filename)
            if clean != info.filename:
                name_map[info.filename] = clean

        for info in infos:
            if not info.filename.casefold().endswith((".xhtml", ".html", ".htm")):
                continue
            payload = source.read(info.filename)
            root = ElementTree.fromstring(payload)
            metrics = dict(_normalize_xhtml_tree(root))
            metrics["icon_links_removed"] = _strip_icon_links(root)
            if any(metrics.values()):
                replacements[info.filename] = ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)
                aggregate["documents_changed"] += 1
                for key, value in metrics.items():
                    aggregate[key] += int(value)

        # If filenames contain spaces or were renamed, update text references in manifest / toc
        if name_map:
            for info in infos:
                target_name = name_map.get(info.filename, info.filename)
                if info.filename.casefold().endswith((".opf", ".ncx", ".xhtml", ".html", ".htm", ".xml")):
                    raw_data = replacements.get(info.filename, source.read(info.filename))
                    text = raw_data.decode("utf-8", errors="ignore")
                    for orig, clean in name_map.items():
                        orig_base = posixpath.basename(orig)
                        clean_base = posixpath.basename(clean)
                        quoted_orig = urllib.parse.quote(orig)
                        quoted_orig_base = urllib.parse.quote(orig_base)
                        text = text.replace(orig, clean)
                        text = text.replace(orig_base, clean_base)
                        text = text.replace(quoted_orig, clean)
                        text = text.replace(quoted_orig_base, clean_base)
                    replacements[info.filename] = text.encode("utf-8")

        if not replacements and not name_map:
            return aggregate

        temporary = path.with_name(f".{path.name}.readability-{uuid.uuid4().hex}")
        try:
            with zipfile.ZipFile(temporary, "w") as destination:
                # Write mimetype first uncompressed
                mimetype_info = next((info for info in infos if info.filename == "mimetype"), None)
                if mimetype_info is not None:
                    destination.writestr("mimetype", source.read("mimetype"), compress_type=zipfile.ZIP_STORED)

                for info in sorted(infos, key=lambda i: i.filename):
                    if info.filename == "mimetype":
                        continue
                    final_name = name_map.get(info.filename, info.filename)
                    data = replacements.get(info.filename, source.read(info.filename))
                    destination.writestr(final_name, data, compress_type=zipfile.ZIP_DEFLATED)
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()
    return aggregate


def inspect_epub_readability(path: Path) -> Mapping[str, int]:
    """Return fail-closed text-flow and typography metrics for a generated EPUB."""
    aggregate = {
        "cjk_spacing_anomalies": 0,
        "suspicious_cjk_block_breaks": 0,
        "prose_hard_breaks": 0,
        "inline_typography_declarations": 0,
        "unsafe_font_size_declarations": 0,
        "font_family_declarations": 0,
        "unsafe_line_height_declarations": 0,
    }
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            name = info.filename.casefold()
            if name.endswith(".css"):
                css_metrics = _css_readability_metrics(decode_document_bytes(archive.read(info.filename)))
                for key, value in css_metrics.items():
                    aggregate[key] += int(value)
                continue
            if not name.endswith((".xhtml", ".html", ".htm")):
                continue
            root = ElementTree.fromstring(archive.read(info.filename))
            xhtml_metrics = _xhtml_readability_metrics(root)
            for key, value in xhtml_metrics.items():
                aggregate[key] += int(value)
            for element in root.iter():
                if _xml_local_name(element.tag) != "style":
                    continue
                css_metrics = _css_readability_metrics(element.text or "")
                for key, value in css_metrics.items():
                    aggregate[key] += int(value)
    return aggregate


def ensure_epub_start_location(path: Path) -> Mapping[str, int]:
    """Add EPUB3 landmarks and an EPUB2 guide start target to a generated master EPUB."""
    xhtml_ns = "http://www.w3.org/1999/xhtml"
    epub_ns = "http://www.idpf.org/2007/ops"
    ElementTree.register_namespace("", xhtml_ns)
    ElementTree.register_namespace("epub", epub_ns)

    def local_name(value: str) -> str:
        return value.rsplit("}", 1)[-1].rsplit(":", 1)[-1].lower()

    def namespace(value: str) -> str:
        return value[1:].split("}", 1)[0] if value.startswith("{") else ""

    def qualified(ns: str, value: str) -> str:
        return f"{{{ns}}}{value}" if ns else value

    def type_tokens(element: ElementTree.Element) -> set[str]:
        values = []
        for key, value in element.attrib.items():
            if local_name(key) == "type":
                values.extend(value.casefold().split())
        return set(values)

    replacements: dict[str, bytes] = {}
    with zipfile.ZipFile(path) as source:
        members = {posixpath.normpath(name): name for name in source.namelist()}
        container = ElementTree.fromstring(source.read("META-INF/container.xml"))
        rootfile = next(
            (element.attrib.get("full-path", "") for element in container.iter() if local_name(element.tag) == "rootfile"),
            "",
        )
        rootfile_member = archive_member_path("", rootfile)
        if not rootfile_member or rootfile_member not in members:
            raise ValueError("EPUB start-location OPF missing")
        opf = ElementTree.fromstring(source.read(members[rootfile_member]))
        opf_ns = namespace(opf.tag)
        opf_dir = posixpath.dirname(rootfile_member)
        manifest: dict[str, tuple[str, set[str]]] = {}
        for element in opf.iter():
            if local_name(element.tag) != "item":
                continue
            identifier = element.attrib.get("id", "")
            href = element.attrib.get("href", "")
            if identifier and href:
                manifest[identifier] = (href, set(element.attrib.get("properties", "").split()))
        nav_item = next((item for item in manifest.values() if "nav" in item[1]), None)
        if nav_item is None:
            raise ValueError("EPUB start-location nav missing")
        nav_member = archive_member_path(opf_dir, nav_item[0])
        if not nav_member or nav_member not in members:
            raise ValueError("EPUB start-location nav resource missing")
        nav_root = ElementTree.fromstring(source.read(members[nav_member]))
        nav_ns = namespace(nav_root.tag) or xhtml_ns
        toc_nodes = [
            element
            for element in nav_root.iter()
            if local_name(element.tag) == "nav" and "toc" in type_tokens(element)
        ]
        first_link = next(
            (
                element.attrib.get("href", "")
                for toc in toc_nodes
                for element in toc.iter()
                if local_name(element.tag) == "a" and element.attrib.get("href")
            ),
            "",
        )
        if first_link:
            parsed = urlsplit(first_link)
            target_member = (
                nav_member
                if not parsed.path
                else archive_member_path(posixpath.dirname(nav_member), first_link)
            )
            fragment = unquote(parsed.fragment)
        else:
            spine_id = next(
                (element.attrib.get("idref", "") for element in opf.iter() if local_name(element.tag) == "itemref"),
                "",
            )
            target_href = manifest.get(spine_id, ("", set()))[0]
            target_member = archive_member_path(opf_dir, target_href) if target_href else None
            fragment = ""
        if not target_member or target_member not in members:
            raise ValueError("EPUB start-location target missing")
        nav_href = posixpath.relpath(target_member, posixpath.dirname(nav_member) or ".")
        opf_href = posixpath.relpath(target_member, opf_dir or ".")
        if fragment:
            nav_href += f"#{fragment}"
            opf_href += f"#{fragment}"

        landmark_nodes = [
            element
            for element in nav_root.iter()
            if local_name(element.tag) == "nav" and "landmarks" in type_tokens(element)
        ]
        has_bodymatter = any(
            local_name(element.tag) == "a"
            and type_tokens(element).intersection({"bodymatter", "text", "start"})
            for landmark in landmark_nodes
            for element in landmark.iter()
        )
        if not has_bodymatter:
            body = next((element for element in nav_root.iter() if local_name(element.tag) == "body"), None)
            if body is None:
                raise ValueError("EPUB start-location nav body missing")
            landmark = landmark_nodes[0] if landmark_nodes else ElementTree.SubElement(
                body, qualified(nav_ns, "nav")
            )
            landmark.set(qualified(epub_ns, "type"), "landmarks")
            ordered = next(
                (element for element in landmark if local_name(element.tag) == "ol"),
                None,
            )
            if ordered is None:
                ordered = ElementTree.SubElement(landmark, qualified(nav_ns, "ol"))
            item = ElementTree.SubElement(ordered, qualified(nav_ns, "li"))
            anchor = ElementTree.SubElement(item, qualified(nav_ns, "a"), {"href": nav_href})
            anchor.set(qualified(epub_ns, "type"), "bodymatter")
            anchor.text = "正文"

        guide = next((element for element in opf if local_name(element.tag) == "guide"), None)
        if guide is None:
            guide = ElementTree.SubElement(opf, qualified(opf_ns, "guide"))
        guide_has_start = any(
            local_name(element.tag) == "reference"
            and set(element.attrib.get("type", "").casefold().split()).intersection({"bodymatter", "text", "start"})
            for element in guide
        )
        if not guide_has_start:
            ElementTree.SubElement(
                guide,
                qualified(opf_ns, "reference"),
                {"type": "text", "title": "正文", "href": opf_href},
            )
        replacements[members[rootfile_member]] = ElementTree.tostring(
            opf, encoding="utf-8", xml_declaration=True
        )
        replacements[members[nav_member]] = ElementTree.tostring(
            nav_root, encoding="utf-8", xml_declaration=True
        )
        infos = source.infolist()
        temporary = path.with_name(f".{path.name}.start-location-{uuid.uuid4().hex}")
        try:
            with zipfile.ZipFile(temporary, "w") as destination:
                ordered_infos = sorted(infos, key=lambda info: 0 if info.filename == "mimetype" else 1)
                for info in ordered_infos:
                    destination.writestr(info, replacements.get(info.filename, source.read(info.filename)))
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()
    return {"bodymatter_landmarks_added": 0 if has_bodymatter else 1, "guide_start_added": 0 if guide_has_start else 1}


def furniture_from_docling(document: Mapping[str, Any]) -> List[str]:
    candidates = []
    texts = document.get("texts", [])
    if not isinstance(texts, list):
        return candidates
    for item in texts:
        if not isinstance(item, Mapping) or item.get("content_layer") != "furniture":
            continue
        value = str(item.get("text", "")).strip()
        if value:
            candidates.append(value)
    return candidates


def inject_trailing_furniture(html: str, furniture: Iterable[str]) -> str:
    items = list(furniture)
    if not items:
        return html
    block = '<section class="docling-furniture">' + "".join(f"<p>{escape(item)}</p>" for item in items) + "</section>"
    closing_body = re.search(r"</body\s*>", html, re.I)
    if not closing_body:
        return html + block
    return html[: closing_body.start()] + block + html[closing_body.start() :]


def normalize_file(source: Path, destination: Path, furniture: Iterable[str] = ()) -> None:
    html = source.read_text(encoding="utf-8")
    normalized = inject_trailing_furniture(normalize_heading_levels(html), furniture)
    destination.write_text(normalized, encoding="utf-8")
