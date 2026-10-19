"""PDF routing policy shared by the orchestrator and tests."""

from __future__ import annotations

from typing import Any, Mapping


def classify_probe(probe: Mapping[str, Any]) -> str:
    sampled = max(1, int(probe.get("sampled_pages", 0)))
    readable = int(probe.get("readable_pages", 0))
    characters = int(probe.get("text_characters", 0))
    if "selectable_pages" in probe:
        selectable = max(0, int(probe.get("selectable_pages", 0)))
        blank_pages = {
            int(value)
            for value in probe.get("blank_pages", [])
            if str(value).isdigit() and 1 <= int(value) <= sampled
        }
        image_only_pages = {
            int(value)
            for value in probe.get("image_only_pages", [])
            if str(value).isdigit() and 1 <= int(value) <= sampled
        }
        edge_pages = {1, 2, sampled - 1, sampled}
        accounted_pages = selectable + len(blank_pages) + len(image_only_pages)
        selectable_ratio = selectable / sampled
        required_characters = max(80, selectable * 20)
        if (
            accounted_pages >= sampled
            and selectable_ratio >= 0.90
            and image_only_pages.issubset(edge_pages)
            and characters >= required_characters
        ):
            return "digital"
        return "scan"
    required_characters = max(80, sampled * 40)
    if readable == sampled and characters >= required_characters:
        return "digital"
    return "scan"


def choose_pdf_mode(probe: Mapping[str, Any], override: str) -> str:
    if override in {"digital", "scan"}:
        return override
    return classify_probe(probe)
