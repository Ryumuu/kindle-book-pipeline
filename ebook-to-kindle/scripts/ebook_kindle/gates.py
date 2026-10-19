"""Content, EPUB, and Kindle-format quality gates."""

from __future__ import annotations

import json
import hashlib
import os
import posixpath
import re
import struct
import unicodedata
import zipfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree

from .core import SkillError, sha256_file
from .normalize import (
    archive_member_path,
    css_resource_references,
    decode_document_bytes,
    html_resource_references,
    markdown_image_references,
)


IMAGE_SUFFIXES = {".avif", ".bmp", ".gif", ".jpeg", ".jpg", ".png", ".svg", ".tif", ".tiff", ".webp"}
EPUB_MARKUP_MEDIA_TYPES = {
    "application/smil+xml",
    "application/xhtml+xml",
    "image/svg+xml",
    "text/html",
}
HTML_HEADING = re.compile(r"<h([12])\b", re.I)
HTML_NOTEREF = re.compile(r"epub:type\s*=\s*['\"][^'\"]*\bnoteref\b", re.I)
HTML_FOOTNOTE = re.compile(r"epub:type\s*=\s*['\"][^'\"]*\b(?:footnote|endnote)\b", re.I)
MARKDOWN_HEADING = re.compile(r"^\s{0,3}#{1,2}\s+\S", re.M)
MARKDOWN_FOOTNOTE_DEFINITION = re.compile(r"^\s*\[\^([^\]]+)\]:\s+", re.M)
MARKDOWN_FOOTNOTE_REFERENCE = re.compile(r"\[\^([^\]]+)\]")
TEXT_CHAPTER = re.compile(
    r"^\s*(?:第\s*[一二三四五六七八九十百零〇0-9]+\s*[章节篇部卷]|chapter\s+\d+)\b",
    re.I | re.M,
)
PRINTED_TOC_CHAPTER = re.compile(
    r"(?:第\s*[一二三四五六七八九十百零〇0-9]+\s*[章节篇部卷]|chapter\s+\d+\b)",
    re.I,
)
PRINTED_TOC_PAGE_CELL = re.compile(r"^\s*(?:第\s*)?(\d{1,4})(?:\s*页)?\s*$")


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value)
    value = "".join(character for character in value if character == "\n" or not unicodedata.category(character).startswith("C"))
    return re.sub(r"\s+", " ", value).strip()


def meaningful_character_count(value: str, excluded: Iterable[str] = ()) -> int:
    normalized = normalize_text(value)
    for item in excluded:
        known = normalize_text(item)
        if known:
            normalized = normalized.replace(known, " ")
    return len(normalized.replace(" ", ""))


def validate_source_text(text: str, excluded: Iterable[str] = ()) -> Dict[str, Any]:
    normalized = normalize_text(text)
    count = meaningful_character_count(text, excluded)
    replacement_count = normalized.count("�")
    replacement_ratio = replacement_count / max(1, len(normalized))
    result = {
        "body_characters": count,
        "replacement_ratio": round(replacement_ratio, 5),
    }
    if count < 20 or replacement_ratio > 0.01:
        raise SkillError("CONTENT_GATE_FAILED", "正文长度或乱码率未通过门槛。", result)
    return result


def _empty_resource_summary() -> Dict[str, Any]:
    return {
        "local_resources": 0,
        "image_resources": 0,
        "content_image_resources": 0,
        "cover_image_resources": 0,
        "inline_resources": 0,
        "remote_resources": 0,
        "missing_resources": 0,
        "resource_fingerprint": hashlib.sha256(b"").hexdigest(),
    }


def _local_resource_path(document_root: Path, base_dir: Path, reference: str) -> Tuple[str, Optional[Path]]:
    raw = reference.strip()
    if raw.startswith("#"):
        return "internal", None
    parsed = urlsplit(raw)
    if parsed.scheme.lower() == "data":
        return "inline", None
    if parsed.scheme or parsed.netloc or raw.startswith("//"):
        return "remote", None
    if not parsed.path or parsed.path.startswith("/"):
        return "missing", None
    root = Path(os.path.abspath(os.fspath(document_root)))
    candidate = Path(os.path.abspath(os.fspath(base_dir / unquote(parsed.path))))
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        return "missing", None
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return "missing", None
    if not candidate.is_file():
        return "missing", None
    return "local", candidate


def _inspect_html_like_resources(path: Path, references: Iterable[tuple[str, str]]) -> Dict[str, Any]:
    summary = _empty_resource_summary()
    document_root = path.parent.resolve()
    queue = [(reference, kind, document_root) for reference, kind in references]
    seen_local = set()
    seen_images = set()
    seen_inline = set()
    scanned_stylesheets = set()
    while queue:
        reference, kind, base_dir = queue.pop(0)
        status, candidate = _local_resource_path(document_root, base_dir, reference)
        is_image = kind == "image" or Path(urlsplit(reference).path).suffix.lower() in IMAGE_SUFFIXES
        if status == "remote":
            summary["remote_resources"] += 1
            continue
        if status == "missing":
            summary["missing_resources"] += 1
            continue
        if status == "internal":
            continue
        if status == "inline":
            digest = hashlib.sha256(reference.encode("utf-8", errors="replace")).hexdigest()
            if digest not in seen_inline:
                seen_inline.add(digest)
                summary["inline_resources"] += 1
                if reference.lstrip().lower().startswith("data:image/") or is_image:
                    seen_images.add("inline:" + digest)
            continue
        assert candidate is not None
        key = str(candidate)
        if key not in seen_local:
            seen_local.add(key)
            summary["local_resources"] += 1
        if is_image:
            seen_images.add(key)
        if (kind == "stylesheet" or candidate.suffix.lower() == ".css") and key not in scanned_stylesheets:
            scanned_stylesheets.add(key)
            try:
                css = decode_document_bytes(candidate.read_bytes())
            except (OSError, UnicodeError):
                summary["missing_resources"] += 1
            else:
                queue.extend((value, resource_kind, candidate.parent) for value, resource_kind in css_resource_references(css))
    summary["image_resources"] = len(seen_images)
    summary["content_image_resources"] = len(seen_images)
    if summary["remote_resources"] or summary["missing_resources"]:
        raise SkillError(
            "CONTENT_GATE_FAILED",
            "源文档包含远程、缺失、越界或符号链接资源；离线转换已拒绝。",
            summary,
        )
    fingerprint = hashlib.sha256()
    for key in sorted(seen_local):
        candidate = Path(key)
        relative = candidate.relative_to(document_root).as_posix()
        fingerprint.update(relative.encode("utf-8", errors="surrogateescape"))
        fingerprint.update(b"\0")
        fingerprint.update(sha256_file(candidate).encode("ascii"))
        fingerprint.update(b"\0")
    for digest in sorted(seen_inline):
        fingerprint.update(b"inline\0")
        fingerprint.update(digest.encode("ascii"))
        fingerprint.update(b"\0")
    summary["resource_fingerprint"] = fingerprint.hexdigest()
    return summary


def inspect_source_resources(path: Path) -> Dict[str, Any]:
    """Preflight local source resources and count images expected in the master EPUB."""
    suffix = path.suffix.lower()
    if suffix in {".html", ".htm"}:
        html = decode_document_bytes(path.read_bytes(), html=True)
        return _inspect_html_like_resources(path, html_resource_references(html))
    if suffix in {".md", ".markdown"}:
        markdown = decode_document_bytes(path.read_bytes())
        references = [*markdown_image_references(markdown), *html_resource_references(markdown)]
        return _inspect_html_like_resources(path, references)
    if suffix == ".docx":
        summary = _empty_resource_summary()
        local_images = set()
        with zipfile.ZipFile(path) as archive:
            names = {posixpath.normpath(name) for name in archive.namelist()}
            relationship_files = [name for name in names if name.startswith("word/") and "/_rels/" in name and name.endswith(".rels")]
            for relationship_file in relationship_files:
                root = ElementTree.fromstring(archive.read(relationship_file))
                base = relationship_file.split("/_rels/", 1)[0]
                for relationship in root.iter():
                    if not relationship.tag.endswith("Relationship") or not relationship.attrib.get("Type", "").endswith("/image"):
                        continue
                    target = relationship.attrib.get("Target", "")
                    if relationship.attrib.get("TargetMode", "").lower() == "external":
                        summary["remote_resources"] += 1
                        continue
                    try:
                        member = archive_member_path("", target.lstrip("/")) if target.startswith("/") else archive_member_path(base, target)
                    except ValueError:
                        summary["remote_resources"] += 1
                        continue
                    if not member or member not in names:
                        summary["missing_resources"] += 1
                    else:
                        local_images.add(member)
        summary["local_resources"] = len(local_images)
        summary["image_resources"] = len(local_images)
        summary["content_image_resources"] = len(local_images)
        summary["resource_fingerprint"] = sha256_file(path)
        if summary["remote_resources"] or summary["missing_resources"]:
            raise SkillError("CONTENT_GATE_FAILED", "DOCX 包含远程或缺失图片资源。", summary)
        return summary
    if suffix == ".epub":
        structure = inspect_epub_structure(path, require_toc=False)
        return {
            "local_resources": structure["manifest_resources"] - structure["inline_resources"],
            "image_resources": structure["image_resources"],
            "content_image_resources": structure["content_image_resources"],
            "cover_image_resources": structure["cover_image_resources"],
            "inline_resources": structure["inline_resources"],
            "remote_resources": structure["remote_resources"],
            "missing_resources": structure["missing_resources"],
            "resource_fingerprint": sha256_file(path),
        }
    return _empty_resource_summary()


def inspect_source_semantics(path: Path) -> Dict[str, int]:
    """Extract conservative two-level navigation and note expectations."""
    suffix = path.suffix.lower()
    headings = 0
    noterefs = 0
    footnotes = 0
    if suffix in {".html", ".htm"}:
        value = decode_document_bytes(path.read_bytes(), html=True)
        headings = len(HTML_HEADING.findall(value))
        noterefs = len(HTML_NOTEREF.findall(value))
        footnotes = len(HTML_FOOTNOTE.findall(value))
    elif suffix in {".md", ".markdown"}:
        value = decode_document_bytes(path.read_bytes())
        headings = len(MARKDOWN_HEADING.findall(value))
        definitions = MARKDOWN_FOOTNOTE_DEFINITION.findall(value)
        definition_keys = {key.casefold() for key in definitions}
        references = [key for key in MARKDOWN_FOOTNOTE_REFERENCE.findall(value) if key.casefold() in definition_keys]
        footnotes = len(definition_keys)
        noterefs = max(0, len(references) - len(definitions))
    elif suffix == ".txt":
        value = decode_document_bytes(path.read_bytes())
        headings = len(TEXT_CHAPTER.findall(value))
    elif suffix == ".docx":
        with zipfile.ZipFile(path) as archive:
            document = ElementTree.fromstring(archive.read("word/document.xml"))
            for paragraph in document.iter():
                if _xml_local_name(paragraph.tag) != "p":
                    continue
                style = next(
                    (
                        value
                        for element in paragraph.iter()
                        if _xml_local_name(element.tag) == "pstyle"
                        for key, value in element.attrib.items()
                        if _xml_local_name(key) == "val"
                    ),
                    "",
                )
                normalized_style = re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", style)).casefold()
                if normalized_style in {"heading1", "heading2", "1", "2", "标题1", "标题2"}:
                    headings += 1
                noterefs += sum(1 for element in paragraph.iter() if _xml_local_name(element.tag) == "footnotereference")
            if "word/footnotes.xml" in archive.namelist():
                note_root = ElementTree.fromstring(archive.read("word/footnotes.xml"))
                for element in note_root.iter():
                    if _xml_local_name(element.tag) != "footnote":
                        continue
                    identifier = next(
                        (value for key, value in element.attrib.items() if _xml_local_name(key) == "id"),
                        "",
                    )
                    if str(identifier) not in {"-1", "0"}:
                        footnotes += 1
    return {
        "outline_entries": max(1, headings),
        "source_heading_entries": headings,
        "synthesized_noterefs": noterefs,
        "synthesized_footnotes": footnotes,
    }


def validate_image_retention(source: Mapping[str, int], master: Mapping[str, int]) -> Dict[str, int]:
    expected = int(source.get("content_image_resources", source.get("image_resources", 0)))
    retained = int(master.get("content_image_resources", master.get("image_resources", 0)))
    expected_cover = int(source.get("cover_image_resources", 0))
    retained_cover = int(master.get("cover_image_resources", 0))
    result = {
        "expected_content_images": expected,
        "retained_content_images": retained,
        "expected_cover_images": expected_cover,
        "retained_cover_images": retained_cover,
    }
    if retained < expected or retained_cover < expected_cover:
        raise SkillError("CONTENT_GATE_FAILED", "源图片未全部进入主 EPUB。", result)
    return result


def _markers(value: str, count: int = 64, width: int = 24) -> List[str]:
    if len(value) <= width:
        return [value] if value else []
    possible = len(value) - width
    if count <= 1:
        return [value[:width]]
    indexes = sorted({round(possible * index / (count - 1)) for index in range(count)})
    return [value[index : index + width] for index in indexes]


def compare_content(
    baseline: str,
    candidate: str,
    *,
    min_length_ratio: float = 0.90,
    min_marker_ratio: float = 0.95,
    min_ordered_marker_ratio: float = 0.95,
    max_length_ratio: float = 1.50,
    max_marker_occurrence_ratio: float = 1.5,
) -> Dict[str, Any]:
    baseline_n = normalize_text(baseline).replace(" ", "")
    candidate_n = normalize_text(candidate).replace(" ", "")
    if len(baseline_n) < 20:
        raise SkillError("CONTENT_GATE_FAILED", "主 EPUB 的可回读正文不足 20 个字符。")
    if not candidate_n:
        raise SkillError("KINDLE_ROUNDTRIP_FAILED", "Kindle 成品无法回读正文。")
    markers = _markers(baseline_n)
    hits = sum(1 for marker in markers if marker in candidate_n)
    marker_ratio = hits / len(markers) if markers else 0.0
    cursor = 0
    ordered_hits = 0
    for marker in markers:
        position = candidate_n.find(marker, cursor)
        if position < 0:
            continue
        ordered_hits += 1
        cursor = position + 1
    ordered_marker_ratio = ordered_hits / len(markers) if markers else 0.0
    length_ratio = len(candidate_n) / len(baseline_n)
    baseline_replacement_ratio = baseline_n.count("�") / max(1, len(baseline_n))
    candidate_replacement_ratio = candidate_n.count("�") / max(1, len(candidate_n))
    baseline_occurrences = sum(max(1, baseline_n.count(marker)) for marker in markers)
    candidate_occurrences = sum(candidate_n.count(marker) for marker in markers)
    marker_occurrence_ratio = candidate_occurrences / max(1, baseline_occurrences)
    passed = (
        length_ratio >= min_length_ratio
        and length_ratio <= max_length_ratio
        and marker_ratio >= min_marker_ratio
        and ordered_marker_ratio >= min_ordered_marker_ratio
        and marker_occurrence_ratio <= max_marker_occurrence_ratio
        and baseline_replacement_ratio <= 0.01
        and candidate_replacement_ratio <= 0.01
    )
    result = {
        "baseline_characters": len(baseline_n),
        "roundtrip_characters": len(candidate_n),
        "length_ratio": round(length_ratio, 4),
        "min_length_ratio": min_length_ratio,
        "max_length_ratio": max_length_ratio,
        "marker_ratio": round(marker_ratio, 4),
        "min_marker_ratio": min_marker_ratio,
        "ordered_marker_ratio": round(ordered_marker_ratio, 4),
        "min_ordered_marker_ratio": min_ordered_marker_ratio,
        "baseline_replacement_ratio": round(baseline_replacement_ratio, 5),
        "roundtrip_replacement_ratio": round(candidate_replacement_ratio, 5),
        "marker_occurrence_ratio": round(marker_occurrence_ratio, 4),
        "max_marker_occurrence_ratio": max_marker_occurrence_ratio,
        "marker_count": len(markers),
    }
    if not passed:
        raise SkillError("KINDLE_ROUNDTRIP_FAILED", "Kindle 回读内容保留率未通过门槛。", result)
    return result


def validate_epub_container(path: Path) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if "mimetype" not in names or archive.read("mimetype") != b"application/epub+zip":
                raise SkillError("EPUB_INVALID", "EPUB 容器缺少正确 mimetype。")
            if not any(name.upper().endswith(".OPF") for name in names):
                raise SkillError("EPUB_INVALID", "EPUB 容器缺少 OPF。")
    except zipfile.BadZipFile as exc:
        raise SkillError("EPUB_INVALID", "EPUB 不是有效 ZIP 容器。") from exc


def _xml_local_name(value: str) -> str:
    return value.rsplit("}", 1)[-1].rsplit(":", 1)[-1].lower()


def _epub_type_tokens(element: ElementTree.Element) -> set[str]:
    values = []
    for key, value in element.attrib.items():
        if _xml_local_name(key) == "type":
            values.extend(value.lower().split())
    return set(values)


def _xml_table_looks_like_printed_toc(table: ElementTree.Element) -> bool:
    cells = []
    for element in table.iter():
        if _xml_local_name(element.tag) not in {"td", "th"}:
            continue
        value = " ".join("".join(element.itertext()).split())
        if value:
            cells.append(value)
    if not cells:
        return False
    page_values = []
    for cell in cells:
        match = PRINTED_TOC_PAGE_CELL.fullmatch(cell)
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


def inspect_epub_structure(path: Path, *, require_toc: bool = True) -> Dict[str, int]:
    validate_epub_container(path)
    try:
        with zipfile.ZipFile(path) as archive:
            member_names = {posixpath.normpath(name): name for name in archive.namelist()}
            container = ElementTree.fromstring(archive.read("META-INF/container.xml"))
            rootfile = next((item.attrib.get("full-path") for item in container.iter() if item.tag.endswith("rootfile")), None)
            try:
                rootfile_member = archive_member_path("", rootfile or "")
            except ValueError as exc:
                raise SkillError("EPUB_INVALID", "EPUB container.xml 指向非法 OPF。") from exc
            if not rootfile_member or rootfile_member not in member_names:
                raise SkillError("EPUB_INVALID", "EPUB container.xml 未指向有效 OPF。")
            opf = ElementTree.fromstring(archive.read(member_names[rootfile_member]))
            opf_dir = posixpath.dirname(rootfile_member)
            manifest: Dict[str, str] = {}
            manifest_media: Dict[str, str] = {}
            manifest_properties: Dict[str, List[str]] = {}
            nav_items: List[str] = []
            ncx_items: List[str] = []
            for item in opf.iter():
                if not item.tag.endswith("item"):
                    continue
                identifier = item.attrib.get("id")
                href = item.attrib.get("href")
                if identifier and href:
                    manifest[identifier] = href
                    properties = item.attrib.get("properties", "").split()
                    manifest_properties[identifier] = properties
                    media_type = item.attrib.get("media-type", "")
                    manifest_media[identifier] = media_type
                    if "nav" in properties:
                        nav_items.append(href)
                    if media_type == "application/x-dtbncx+xml":
                        ncx_items.append(href)
            spine_ids = [item.attrib.get("idref", "") for item in opf.iter() if item.tag.endswith("itemref")]
            if not spine_ids or any(identifier not in manifest for identifier in spine_ids):
                raise SkillError("EPUB_INVALID", "EPUB spine 为空或引用未知资源。")
            missing = []
            remote_resources = 0
            inline_resources = 0
            manifest_targets: Dict[str, Optional[str]] = {}
            markup_roots: Dict[str, ElementTree.Element] = {}
            for identifier, href in manifest.items():
                if "remote-resources" in manifest_properties.get(identifier, []):
                    remote_resources += 1
                try:
                    resolved = archive_member_path(opf_dir, href)
                except ValueError:
                    remote_resources += 1
                    continue
                manifest_targets[identifier] = resolved
                if resolved is None:
                    inline_resources += 1
                    continue
                if resolved not in member_names:
                    missing.append(resolved)
            for identifier, target in manifest_targets.items():
                if target is None or target not in member_names:
                    continue
                media_type = manifest_media.get(identifier, "").lower()
                if media_type == "text/css":
                    try:
                        text = decode_document_bytes(archive.read(member_names[target]))
                    except UnicodeError as exc:
                        raise SkillError("EPUB_INVALID", "EPUB CSS 无法按受支持编码解析。") from exc
                    references = css_resource_references(text)
                elif media_type in EPUB_MARKUP_MEDIA_TYPES:
                    try:
                        text = decode_document_bytes(archive.read(member_names[target]), html=True)
                        markup_roots[target] = ElementTree.fromstring(text)
                    except UnicodeError as exc:
                        raise SkillError("EPUB_INVALID", "EPUB 标记资源无法按受支持编码解析。") from exc
                    references = html_resource_references(text)
                else:
                    continue
                base = posixpath.dirname(target)
                for reference, _kind in references:
                    raw = reference.strip()
                    parsed = urlsplit(raw)
                    if not raw or raw.startswith("#") or (not parsed.path and not parsed.scheme and not parsed.netloc):
                        continue
                    try:
                        referenced_target = archive_member_path(base, raw)
                    except ValueError:
                        remote_resources += 1
                        continue
                    if referenced_target is None:
                        inline_resources += 1
                    elif referenced_target not in member_names:
                        missing.append(referenced_target)
            if remote_resources:
                raise SkillError(
                    "EPUB_INVALID",
                    "EPUB 包含远程资源声明或内容引用；离线转换拒绝。",
                    {"remote_resources": remote_resources},
                )
            if missing:
                raise SkillError("EPUB_INVALID", "EPUB manifest 引用了缺失资源。", {"missing_resources": len(missing)})
            cover_ids = {
                identifier
                for identifier, properties in manifest_properties.items()
                if "cover-image" in properties
            }
            for element in opf.iter():
                if element.tag.endswith("meta") and element.attrib.get("name", "").lower() == "cover":
                    content = element.attrib.get("content", "")
                    if content:
                        cover_ids.add(content)
                if element.tag.endswith("reference") and "cover" in element.attrib.get("type", "").lower().split():
                    href = element.attrib.get("href", "")
                    try:
                        guide_target = archive_member_path(opf_dir, href)
                    except ValueError as exc:
                        raise SkillError("EPUB_INVALID", "EPUB cover guide 引用了远程或越界资源。") from exc
                    guide_ids = {
                        identifier for identifier, target in manifest_targets.items() if target == guide_target
                    }
                    cover_ids.update(guide_ids)
                    guide_is_page = any(
                        manifest_media.get(identifier, "") in {"application/xhtml+xml", "text/html"}
                        for identifier in guide_ids
                    )
                    if guide_is_page and guide_target is not None and guide_target in member_names:
                        cover_page = ElementTree.fromstring(archive.read(member_names[guide_target]))
                        for image in cover_page.iter():
                            if not (image.tag.endswith("img") or image.tag.endswith("image")):
                                continue
                            image_href = image.attrib.get("src") or image.attrib.get("href") or next(
                                (value for key, value in image.attrib.items() if key.endswith("}href")),
                                "",
                            )
                            if not image_href:
                                continue
                            try:
                                image_target = archive_member_path(posixpath.dirname(guide_target), image_href)
                            except ValueError as exc:
                                raise SkillError("EPUB_INVALID", "EPUB cover page 引用了远程或越界图片。") from exc
                            cover_ids.update(
                                identifier for identifier, target in manifest_targets.items() if target == image_target
                            )
            image_ids = {identifier for identifier, media_type in manifest_media.items() if media_type.startswith("image/")}
            cover_image_ids = image_ids & cover_ids

            ids_by_member: Dict[str, set[str]] = {}
            elements_by_member: Dict[str, Dict[str, ElementTree.Element]] = {}
            for member, root in markup_roots.items():
                elements = {}
                for element in root.iter():
                    identifier = element.attrib.get("id") or element.attrib.get("name")
                    if identifier:
                        elements[identifier] = element
                elements_by_member[member] = elements
                ids_by_member[member] = set(elements)

            body_printed_toc_table_count = sum(
                1
                for root in markup_roots.values()
                for element in root.iter()
                if _xml_local_name(element.tag) == "table" and _xml_table_looks_like_printed_toc(element)
            )
            if body_printed_toc_table_count:
                raise SkillError(
                    "EPUB_INVALID",
                    "EPUB 正文仍包含被误编码为表格的印刷目录。",
                    {"body_printed_toc_table_count": body_printed_toc_table_count},
                )

            def resolve_internal(source_member: str, href: str) -> Tuple[Optional[bool], Optional[str], str]:
                raw = href.strip()
                parsed = urlsplit(raw)
                if parsed.scheme or parsed.netloc or raw.startswith("//"):
                    return None, None, ""
                try:
                    target = (
                        source_member
                        if not parsed.path
                        else archive_member_path(posixpath.dirname(source_member), raw)
                    )
                except ValueError:
                    return False, None, unquote(parsed.fragment)
                fragment = unquote(parsed.fragment)
                if target is None or target not in member_names:
                    return False, target, fragment
                if fragment and fragment not in ids_by_member.get(target, set()):
                    return False, target, fragment
                return True, target, fragment

            nav_toc_entries = 0
            valid_nav_toc_entries = 0
            toc_table_count = 0
            bodymatter_landmarks = 0
            broken_navigation_targets = 0
            for href in nav_items:
                try:
                    target = archive_member_path(opf_dir, href)
                except ValueError as exc:
                    raise SkillError("EPUB_INVALID", "EPUB 导航引用了远程或越界资源。") from exc
                if target is None or target not in member_names:
                    raise SkillError("EPUB_INVALID", "EPUB 导航资源缺失。")
                root = markup_roots.get(target) or ElementTree.fromstring(archive.read(member_names[target]))
                nav_nodes = [element for element in root.iter() if _xml_local_name(element.tag) == "nav"]
                toc_nodes = [element for element in nav_nodes if "toc" in _epub_type_tokens(element)]
                if not toc_nodes and nav_nodes:
                    # EPUB 3 requires epub:type=toc. Retain compatibility with
                    # older locally-authored files while still validating every target.
                    toc_nodes = nav_nodes[:1]
                for toc_node in toc_nodes:
                    toc_table_count += sum(1 for element in toc_node.iter() if _xml_local_name(element.tag) == "table")
                    for element in toc_node.iter():
                        if _xml_local_name(element.tag) != "a" or not element.attrib.get("href"):
                            continue
                        nav_toc_entries += 1
                        resolved, _member, _fragment = resolve_internal(target, element.attrib["href"])
                        if resolved is True:
                            valid_nav_toc_entries += 1
                        else:
                            broken_navigation_targets += 1
                for landmark in nav_nodes:
                    if "landmarks" not in _epub_type_tokens(landmark):
                        continue
                    for element in landmark.iter():
                        if _xml_local_name(element.tag) != "a" or not element.attrib.get("href"):
                            continue
                        types = _epub_type_tokens(element)
                        if not types.intersection({"bodymatter", "text", "start"}):
                            continue
                        resolved, _member, _fragment = resolve_internal(target, element.attrib["href"])
                        if resolved is True:
                            bodymatter_landmarks += 1
                        else:
                            broken_navigation_targets += 1

            ncx_toc_entries = 0
            valid_ncx_toc_entries = 0
            for href in ncx_items:
                try:
                    target = archive_member_path(opf_dir, href)
                except ValueError as exc:
                    raise SkillError("EPUB_INVALID", "EPUB NCX 引用了远程或越界资源。") from exc
                if target is None or target not in member_names:
                    raise SkillError("EPUB_INVALID", "EPUB NCX 资源缺失。")
                root = ElementTree.fromstring(archive.read(member_names[target]))
                for nav_point in root.iter():
                    if _xml_local_name(nav_point.tag) != "navpoint":
                        continue
                    ncx_toc_entries += 1
                    content = next(
                        (element for element in nav_point.iter() if _xml_local_name(element.tag) == "content"),
                        None,
                    )
                    source = content.attrib.get("src", "") if content is not None else ""
                    resolved, _member, _fragment = resolve_internal(target, source) if source else (False, None, "")
                    if resolved is True:
                        valid_ncx_toc_entries += 1
                    else:
                        broken_navigation_targets += 1

            guide_toc_entries = 0
            for element in opf.iter():
                if _xml_local_name(element.tag) != "reference" or not element.attrib.get("href"):
                    continue
                types = set(element.attrib.get("type", "").lower().split())
                if "toc" in types:
                    guide_toc_entries += 1
                if types.intersection({"bodymatter", "text", "start"}):
                    resolved, _member, _fragment = resolve_internal(rootfile_member, element.attrib["href"])
                    if resolved is True:
                        bodymatter_landmarks += 1
                    else:
                        broken_navigation_targets += 1

            internal_link_count = 0
            broken_internal_links = 0
            noteref_count = 0
            resolved_noteref_count = 0
            bidirectional_note_count = 0
            footnote_count = 0
            for member, root in markup_roots.items():
                for element in root.iter():
                    types = _epub_type_tokens(element)
                    if types.intersection({"footnote", "endnote"}):
                        footnote_count += 1
                    if _xml_local_name(element.tag) != "a" or not element.attrib.get("href"):
                        continue
                    href = element.attrib["href"]
                    resolved, target_member, fragment = resolve_internal(member, href)
                    if resolved is not None:
                        internal_link_count += 1
                        if resolved is not True:
                            broken_internal_links += 1
                    if "noteref" not in types:
                        continue
                    noteref_count += 1
                    source_id = element.attrib.get("id", "")
                    target_element = (
                        elements_by_member.get(target_member or "", {}).get(fragment)
                        if resolved is True and fragment
                        else None
                    )
                    if target_element is None or not _epub_type_tokens(target_element).intersection({"footnote", "endnote"}):
                        continue
                    resolved_noteref_count += 1
                    if not source_id:
                        continue
                    for backlink in target_element.iter():
                        if _xml_local_name(backlink.tag) != "a" or not backlink.attrib.get("href"):
                            continue
                        back_ok, back_member, back_fragment = resolve_internal(target_member or "", backlink.attrib["href"])
                        if back_ok is True and back_member == member and back_fragment == source_id:
                            bidirectional_note_count += 1
                            break

            toc_entries = max(nav_toc_entries, ncx_toc_entries)
            if require_toc and toc_entries < 1:
                raise SkillError("EPUB_INVALID", "EPUB 没有可用目录条目。")
            if broken_navigation_targets:
                raise SkillError(
                    "EPUB_INVALID",
                    "EPUB 目录或前往目标无法解析。",
                    {"broken_navigation_targets": broken_navigation_targets},
                )
            if toc_table_count:
                raise SkillError(
                    "EPUB_INVALID",
                    "EPUB 目录被错误编码为表格。",
                    {"toc_table_count": toc_table_count},
                )
            if broken_internal_links:
                raise SkillError(
                    "EPUB_INVALID",
                    "EPUB 包含无法解析的内部跳转。",
                    {"broken_internal_links": broken_internal_links},
                )
            if noteref_count and (
                resolved_noteref_count != noteref_count or bidirectional_note_count != noteref_count
            ):
                raise SkillError(
                    "EPUB_INVALID",
                    "EPUB 注释不是可解析的双向链接。",
                    {
                        "noteref_count": noteref_count,
                        "resolved_noteref_count": resolved_noteref_count,
                        "bidirectional_note_count": bidirectional_note_count,
                    },
                )
            return {
                "manifest_resources": len(manifest),
                "spine_items": len(spine_ids),
                "toc_entries": toc_entries,
                "nav_toc_entries": nav_toc_entries,
                "valid_nav_toc_entries": valid_nav_toc_entries,
                "ncx_toc_entries": ncx_toc_entries,
                "valid_ncx_toc_entries": valid_ncx_toc_entries,
                "guide_toc_entries": guide_toc_entries,
                "broken_navigation_targets": 0,
                "toc_table_count": 0,
                "body_printed_toc_table_count": 0,
                "bodymatter_landmarks": bodymatter_landmarks,
                "internal_link_count": internal_link_count,
                "broken_internal_links": 0,
                "noteref_count": noteref_count,
                "resolved_noteref_count": resolved_noteref_count,
                "footnote_count": footnote_count,
                "bidirectional_note_count": bidirectional_note_count,
                "missing_resources": 0,
                "remote_resources": 0,
                "inline_resources": inline_resources,
                "image_resources": len(image_ids),
                "cover_image_resources": len(cover_image_ids),
                "content_image_resources": len(image_ids - cover_image_ids),
            }
    except (KeyError, ElementTree.ParseError) as exc:
        raise SkillError("EPUB_INVALID", "EPUB 结构 XML 无法解析。") from exc


def validate_semantic_retention(
    expected: Mapping[str, Any], actual: Mapping[str, Any], *, require_ncx: bool = False
) -> Dict[str, int]:
    required_toc = int(expected.get("outline_entries", expected.get("toc_entries", 1)) or 0)
    required_noterefs = int(expected.get("synthesized_noterefs", expected.get("noteref_count", 0)) or 0)
    required_footnotes = int(expected.get("synthesized_footnotes", expected.get("footnote_count", 0)) or 0)
    result = {
        "required_toc_entries": required_toc,
        "retained_toc_entries": int(actual.get("toc_entries", 0) or 0),
        "retained_ncx_entries": int(actual.get("valid_ncx_toc_entries", 0) or 0),
        "required_noterefs": required_noterefs,
        "retained_noterefs": int(actual.get("resolved_noteref_count", 0) or 0),
        "retained_bidirectional_notes": int(actual.get("bidirectional_note_count", 0) or 0),
        "required_footnotes": required_footnotes,
        "retained_footnotes": int(actual.get("footnote_count", 0) or 0),
        "retained_bodymatter_landmarks": int(actual.get("bodymatter_landmarks", 0) or 0),
    }
    failed = (
        result["retained_toc_entries"] < required_toc
        or (require_ncx and result["retained_ncx_entries"] < required_toc)
        or result["retained_noterefs"] < required_noterefs
        or result["retained_bidirectional_notes"] < required_noterefs
        or result["retained_footnotes"] < required_footnotes
        or result["retained_bodymatter_landmarks"] < 1
    )
    if failed:
        raise SkillError("EPUB_INVALID", "目录、前往或注释语义在转换中丢失。", result)
    return result


def parse_epubcheck_report(path: Path) -> Dict[str, int]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SkillError("EPUB_INVALID", "无法解析 EPUBCheck 报告。") from exc
    messages = payload.get("messages", []) if isinstance(payload, Mapping) else []
    counts = {"fatal": 0, "error": 0, "warning": 0}
    if isinstance(messages, list):
        for message in messages:
            if not isinstance(message, Mapping):
                continue
            severity = str(message.get("severity", "")).lower()
            if severity in counts:
                counts[severity] += 1
    return counts


def docling_page_character_counts(document: Mapping[str, Any], page_count: int) -> List[int]:
    """Count attributable non-furniture OCR characters for every PDF page."""
    counts = {page: 0 for page in range(1, max(1, page_count) + 1)}
    excluded_labels = {"page_header", "page_footer", "page_number"}

    def add_item_text(item: Mapping[str, Any], value: str) -> None:
        provenance = item.get("prov", [])
        if not isinstance(provenance, list):
            return
        valid_entries = [entry for entry in provenance if isinstance(entry, Mapping)]
        multiple_pages = len(valid_entries) > 1
        for entry in valid_entries:
            try:
                page = int(entry.get("page_no", 0))
            except (TypeError, ValueError):
                continue
            if page not in counts:
                continue
            segment = value
            charspan = entry.get("charspan")
            if (
                isinstance(charspan, list)
                and len(charspan) == 2
                and all(isinstance(index, int) for index in charspan)
                and 0 <= charspan[0] < charspan[1] <= len(value)
            ):
                segment = value[charspan[0] : charspan[1]]
            elif multiple_pages:
                # Never credit a whole cross-page item to every page when
                # Docling did not provide valid spans for attribution.
                continue
            counts[page] += len(normalize_text(segment).replace(" ", ""))

    texts = document.get("texts", [])
    if isinstance(texts, list):
        for item in texts:
            if (
                not isinstance(item, Mapping)
                or str(item.get("label", "")) in excluded_labels
                or str(item.get("content_layer", "")) == "furniture"
            ):
                continue
            value = item.get("text", "")
            if isinstance(value, str):
                add_item_text(item, value)

    tables = document.get("tables", [])
    if isinstance(tables, list):
        for item in tables:
            if (
                not isinstance(item, Mapping)
                or str(item.get("label", "")) in excluded_labels
                or str(item.get("content_layer", "")) == "furniture"
            ):
                continue
            data = item.get("data", {})
            cells = data.get("table_cells", []) if isinstance(data, Mapping) else []
            if not isinstance(cells, list):
                continue
            value = " ".join(
                str(cell.get("text", ""))
                for cell in cells
                if isinstance(cell, Mapping) and isinstance(cell.get("text", ""), str)
            )
            add_item_text(item, value)
    return [counts[page] for page in sorted(counts)]


def validate_ocr_text(
    text: str,
    page_count: int,
    page_characters: Optional[Sequence[int]] = None,
    *,
    page_minimums: Optional[Sequence[int]] = None,
) -> Dict[str, Any]:
    normalized = normalize_text(text)
    nonspace = len(normalized.replace(" ", ""))
    replacement_count = normalized.count("�")
    replacement_ratio = replacement_count / max(1, len(normalized))
    per_page = nonspace / max(1, page_count)
    per_page_counts = [max(0, int(value)) for value in page_characters] if page_characters is not None else []
    minimums = [15] * max(1, page_count) if page_minimums is None else [int(value) for value in page_minimums]
    minimums_valid = len(minimums) == max(1, page_count) and all(0 <= value <= 15 for value in minimums)
    page_counts_valid = len(per_page_counts) == max(1, page_count) and minimums_valid
    if page_count == 1 and not per_page_counts:
        per_page_counts = [nonspace]
        page_counts_valid = True
    covered_pages = (
        sum(value >= minimum for value, minimum in zip(per_page_counts, minimums))
        if page_counts_valid
        else 0
    )
    coverage_ratio = covered_pages / max(1, page_count)
    result = {
        "ocr_characters": nonspace,
        "ocr_characters_per_page_average": round(per_page, 2),
        "min_page_characters": min(per_page_counts) if page_counts_valid else 0,
        "blank_page_exemptions": sum(value == 0 for value in minimums) if minimums_valid else 0,
        "sparse_page_thresholds": sum(0 < value < 15 for value in minimums) if minimums_valid else 0,
        "page_coverage_ratio": round(coverage_ratio, 5),
        "replacement_ratio": round(replacement_ratio, 5),
    }
    if (
        nonspace < 20
        or per_page < 15
        or not page_counts_valid
        or coverage_ratio < 1.0
        or replacement_ratio > 0.01
    ):
        raise SkillError("OCR_QUALITY_FAILED", "扫描 PDF 的逐页 OCR 覆盖、文本密度或乱码率未通过门槛。", result)
    return result


def mobi_version(path: Path) -> int:
    data = path.read_bytes()
    if len(data) < 100 or data[60:68] != b"BOOKMOBI":
        raise SkillError("KINDLE_ROUNDTRIP_FAILED", "成品不是 PalmDB/MOBI 容器。")
    record_zero = struct.unpack(">I", data[78:82])[0]
    mobi = record_zero + 16
    if mobi + 24 > len(data) or data[mobi : mobi + 4] != b"MOBI":
        raise SkillError("KINDLE_ROUNDTRIP_FAILED", "成品缺少 MOBI 头。")
    # MOBI file format version is 20 bytes from the MOBI header start.
    return struct.unpack(">I", data[mobi + 20 : mobi + 24])[0]


def validate_kindle_container(path: Path, fmt: str) -> Dict[str, Any]:
    version = mobi_version(path)
    expected = 8 if fmt == "azw3" else 6
    if version != expected:
        raise SkillError("KINDLE_ROUNDTRIP_FAILED", f"{fmt} 容器版本为 {version}，期望 {expected}。")
    return {"container_version": version, "sha256": sha256_file(path), "bytes": path.stat().st_size}
