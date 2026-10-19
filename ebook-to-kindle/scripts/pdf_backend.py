#!/usr/bin/env python3
"""Run inside the isolated Docling environment for PDF probing and export."""

from __future__ import annotations

import argparse
from collections import Counter
import importlib.util
import json
import re
import time
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


SCRIPT_INTERFACE = "internal-module"
SCRIPT_INTERFACE_REASON = "Invoked only by the public CLI inside the isolated PDF environment."
BRACKET_NOTE = re.compile(r"\[\s*(\d{1,4})\s*\]")


def _note_profile_from_page_texts(page_texts: List[str]) -> Tuple[Dict[int, str], Dict[int, int]]:
    """Recover high-confidence note definitions that Docling omitted.

    A dense multi-page bracket-number run identifies the printed endnote
    section. Only markers beginning a PDF text line are eligible definition
    starts; body markers with no such source definition are deliberately left
    for the semantic layer to represent as explicit source-missing placeholders.
    """
    counts = [len(BRACKET_NOTE.findall(text)) for text in page_texts]
    dense_start: Optional[int] = None
    for index in range(max(0, len(counts) - 2)):
        if sum(1 for count in counts[index : index + 3] if count >= 10) == 3:
            dense_start = index
            break
    if dense_start is None:
        return {}, {}
    body_counts = Counter(
        int(value)
        for text in page_texts[:dense_start]
        for value in BRACKET_NOTE.findall(text)
    )
    body_numbers = set(body_counts)
    note_text = "\n".join(page_texts[dense_start:])
    recovered: Dict[int, str] = {}
    for number in sorted(body_numbers):
        start_pattern = re.compile(rf"(?m)^[ \t\r]*\[\s*{number}\s*\]")
        starts = list(start_pattern.finditer(note_text))
        if len(starts) != 1:
            continue
        start = starts[0]
        next_exact = re.search(rf"\[\s*{number + 1}\s*\]", note_text[start.end() :])
        if next_exact is not None:
            end = start.end() + next_exact.start()
        else:
            end = len(note_text)
            for candidate in BRACKET_NOTE.finditer(note_text, start.end()):
                if int(candidate.group(1)) > number:
                    end = candidate.start()
                    break
        segment = " ".join(note_text[start.start() : end].split())
        authored = BRACKET_NOTE.sub("", segment, count=1).strip()
        if 4 <= len(authored) <= 4000:
            recovered[number] = segment
    return recovered, dict(body_counts)


def _fallback_definitions_from_page_texts(page_texts: List[str]) -> Dict[int, str]:
    return _note_profile_from_page_texts(page_texts)[0]


def extract_pdf_note_profile(path: Path) -> Tuple[Dict[int, str], Dict[int, int]]:
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(str(path))
    try:
        page_texts = [page.get_textpage().get_text_range() for page in document]
    finally:
        document.close()
    return _note_profile_from_page_texts(page_texts)


def extract_pdf_note_fallbacks(path: Path) -> Dict[int, str]:
    return extract_pdf_note_profile(path)[0]


def _page_is_visually_blank(page: Any) -> bool:
    bitmap = page.render(scale=0.25, grayscale=True)
    try:
        image = bitmap.to_pil().convert("L")
        histogram = image.histogram()
        total = max(1, sum(histogram))
        ink_ratio = sum(histogram[:245]) / total
        return ink_ratio < 0.0005
    finally:
        close = getattr(bitmap, "close", None)
        if callable(close):
            close()


def _assign_missing_outline_pages(
    items: List[Dict[str, Any]],
    matched_pages: List[Optional[int]],
    *,
    previous_page: int,
    next_page: int,
) -> None:
    known = [(position, page) for position, page in enumerate(matched_pages) if page is not None]
    for position, item in enumerate(items):
        match_page = matched_pages[position]
        if match_page is not None:
            item["page"] = int(match_page)
            item["page_resolution"] = "title-search"
            continue
        if known:
            _nearest_position, nearest_page = min(
                known,
                key=lambda pair: (abs(pair[0] - position), pair[0]),
            )
            item["page"] = int(nearest_page)
            item["page_resolution"] = "neighbor-title-page"
            continue
        item["page"] = max(previous_page, next_page - 1)
        item["page_resolution"] = "neighbor-next-page"


def probe_pdf(path: Path) -> Dict[str, Any]:
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(str(path))
    indexes = list(range(len(document)))
    readable = 0
    selectable = 0
    characters = 0
    blank_pages: List[int] = []
    image_only_pages: List[int] = []
    sparse_text_pages: Dict[str, int] = {}
    for index in indexes:
        page = document[index]
        text_page = page.get_textpage()
        text = text_page.get_text_range()
        count = len("".join(text.split()))
        characters += count
        if count > 0:
            selectable += 1
        if count >= 40:
            readable += 1
        if count < 15:
            if _page_is_visually_blank(page):
                blank_pages.append(index + 1)
            elif count > 0:
                sparse_text_pages[str(index + 1)] = count
            else:
                image_only_pages.append(index + 1)
        text_page.close()
        page.close()
    return {
        "page_count": len(document),
        "sampled_pages": len(indexes),
        "readable_pages": readable,
        "selectable_pages": selectable,
        "text_characters": characters,
        "blank_pages": blank_pages,
        "image_only_pages": image_only_pages,
        "sparse_text_pages": sparse_text_pages,
    }


def extract_outline(path: Path) -> List[Dict[str, Any]]:
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(str(path))
    outline: List[Dict[str, Any]] = []
    for bookmark in document.get_toc():
        title = bookmark.get_title().strip()
        if not title:
            continue
        destination = bookmark.get_dest()
        page: Optional[int] = None
        if destination is not None:
            try:
                index = destination.get_index()
            except Exception:
                index = None
            if isinstance(index, int) and 0 <= index < len(document):
                page = index + 1
        outline.append(
            {
                "title": title,
                "level": int(bookmark.level),
                "page": page,
                "page_resolution": "destination" if page is not None else "unresolved",
            }
        )
    if any(item["page"] is None for item in outline):
        page_keys: List[str] = []
        for page_index in range(len(document)):
            page = document[page_index]
            text_page = page.get_textpage()
            value = unicodedata.normalize("NFKC", text_page.get_text_range()).casefold()
            page_keys.append("".join(character for character in value if character.isalnum()))
            text_page.close()
            page.close()
        index = 0
        while index < len(outline):
            if outline[index]["page"] is not None:
                index += 1
                continue
            start = index
            while index < len(outline) and outline[index]["page"] is None:
                index += 1
            end = index
            previous_page = int(outline[start - 1]["page"]) if start else 1
            next_page = int(outline[end]["page"]) if end < len(outline) else len(document)
            missing_items = outline[start:end]
            missing_count = len(missing_items)
            matched_pages: List[Optional[int]] = []
            for offset, item in enumerate(missing_items, 1):
                key = "".join(
                    character
                    for character in unicodedata.normalize("NFKC", str(item["title"])).casefold()
                    if character.isalnum()
                )
                target = round(previous_page + (next_page - previous_page) * offset / (missing_count + 1))
                bounded = [
                    page_number
                    for page_number, page_key in enumerate(page_keys, 1)
                    if previous_page <= page_number <= next_page and key and key in page_key
                ]
                match_page = min(bounded, key=lambda page_number: abs(page_number - target)) if bounded else None
                matched_pages.append(match_page)
            _assign_missing_outline_pages(
                missing_items,
                matched_pages,
                previous_page=previous_page,
                next_page=next_page,
            )
    return outline


def _semantic_module() -> Any:
    source = Path(__file__).with_name("pdf_semantics.py")
    spec = importlib.util.spec_from_file_location("ebook_to_kindle_pdf_semantics", source)
    if spec is None or spec.loader is None:
        raise RuntimeError("PDF semantic module unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def convert_pdf(path: Path, output_dir: Path, artifacts: Path, mode: str) -> Dict[str, Any]:
    total_started = time.monotonic()
    imports_started = time.monotonic()
    from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import (
        HeadingHierarchyOptions,
        OcrMacOptions,
        OcrMode,
        PdfPipelineOptions,
        TableFormerMode,
        TableStructureOptions,
    )
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling_core.types.doc import ImageRefMode
    imports_seconds = time.monotonic() - imports_started

    setup_started = time.monotonic()
    options = PdfPipelineOptions(
        artifacts_path=artifacts,
        enable_remote_services=False,
        allow_external_plugins=False,
        accelerator_options=AcceleratorOptions(device=AcceleratorDevice.CPU, num_threads=4),
        do_ocr=mode == "scan",
        do_table_structure=True,
        table_structure_options=TableStructureOptions(mode=TableFormerMode.ACCURATE, do_cell_matching=True),
        images_scale=1.0,
        generate_page_images=True,
        generate_picture_images=True,
        generate_parsed_pages=True,
        heading_hierarchy_options=HeadingHierarchyOptions(enabled=True),
        document_timeout=1200,
    )
    if mode == "scan":
        options.ocr_options = OcrMacOptions(mode=OcrMode.FULL_PAGE, lang=["zh-Hans", "en-US"])

    converter = DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)}
    )
    setup_seconds = time.monotonic() - setup_started
    conversion_started = time.monotonic()
    result = converter.convert(path)
    conversion_seconds = time.monotonic() - conversion_started
    export_started = time.monotonic()
    output_dir.mkdir(parents=True, exist_ok=True)
    assets_dir = output_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    html_path = output_dir / "document.html"
    json_path = output_dir / "document.json"
    result.document.save_as_html(
        html_path,
        artifacts_dir=Path(assets_dir.name),
        image_mode=ImageRefMode.REFERENCED,
        html_lang="zh-CN",
    )
    result.document.save_as_json(
        json_path,
        artifacts_dir=Path(assets_dir.name),
        image_mode=ImageRefMode.REFERENCED,
    )
    export_seconds = time.monotonic() - export_started
    semantics_started = time.monotonic()
    outline = extract_outline(path)
    outline_path = output_dir / "outline.json"
    outline_path.write_text(json.dumps(outline, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    if mode == "digital":
        fallback_definitions, expected_reference_counts = extract_pdf_note_profile(path)
    else:
        fallback_definitions, expected_reference_counts = {}, {}
    semantic_path = output_dir / "semantic.html"
    semantic_report_path = output_dir / "semantics.json"
    document_payload = json.loads(json_path.read_text(encoding="utf-8"))
    enriched, semantic_metrics = _semantic_module().enrich_html(
        html_path.read_text(encoding="utf-8"),
        document_payload,
        outline,
        fallback_definitions,
        expected_reference_counts,
    )
    semantic_path.write_text(enriched, encoding="utf-8")
    semantic_report_path.write_text(
        json.dumps(semantic_metrics, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )
    semantics_seconds = time.monotonic() - semantics_started
    return {
        "html": html_path.name,
        "semantic_html": semantic_path.name,
        "json": json_path.name,
        "outline": outline_path.name,
        "semantic_report": semantic_report_path.name,
        "assets": assets_dir.name,
        "mode": mode,
        "semantic_metrics": semantic_metrics,
        "stage_timings_seconds": {
            "dependency_imports": round(imports_seconds, 3),
            "model_and_pipeline_setup": round(setup_seconds, 3),
            "document_conversion": round(conversion_seconds, 3),
            "docling_export": round(export_seconds, 3),
            "semantic_enrichment": round(semantics_seconds, 3),
            "total": round(time.monotonic() - total_started, 3),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Private PDF backend for ebook-to-kindle")
    subparsers = parser.add_subparsers(dest="command", required=True)
    probe = subparsers.add_parser("probe")
    probe.add_argument("input", type=Path)
    convert = subparsers.add_parser("convert")
    convert.add_argument("input", type=Path)
    convert.add_argument("--output-dir", type=Path, required=True)
    convert.add_argument("--artifacts", type=Path, required=True)
    convert.add_argument("--mode", choices=("digital", "scan"), required=True)
    args = parser.parse_args()
    if args.command == "probe":
        payload = probe_pdf(args.input)
    else:
        payload = convert_pdf(args.input, args.output_dir, args.artifacts, args.mode)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
