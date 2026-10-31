#!/usr/bin/env python3
"""
ebook_translate.py: CLI and automation engine for translating ebooks.
Supports unpacking EPUB, tracking translation status,
and assembling translated chapters into refined EPUB and AZW3 for Kindle.
"""

import argparse
import hashlib
import html
import json
import os
import posixpath
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote, unquote, urlsplit

from pipeline_setup import default_output_dir

DEFAULT_OUTPUT_DIR = default_output_dir()
DEFAULT_WORKSPACE_BASE = Path(os.path.expanduser("~/.local/share/ebook-translate-workspaces"))

def find_calibre_convert() -> Optional[Path]:
    runtime = Path(os.environ.get("EBOOK_TO_KINDLE_RUNTIME") or "~/.local/share/ebook-to-kindle").expanduser()
    candidates = [
        runtime / "core" / "Calibre.app" / "Contents" / "MacOS" / "ebook-convert",
    ]
    which_path = shutil.which("ebook-convert")
    if which_path:
        candidates.append(Path(which_path))
    for p in candidates:
        if p.is_file() and os.access(p, os.X_OK):
            return p
    return None

def slugify(text: str) -> str:
    s = re.sub(r"[^\w\s-]", "", text.strip().lower())
    s = re.sub(r"[-\s]+", "-", s)
    return s[:40] if s else "section"

# ---------------------------------------------------------------------------
# Unpack Workflow
# ---------------------------------------------------------------------------

def source_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

def check_source_hash(data: Dict, actual_hash: str):
    if not data.get("source_sha256"):
        raise ValueError("Legacy workspace has no source SHA-256. Use a new --workspace and verify old translations before copying them, or manually clear the old workspace.")
    if data["source_sha256"] != actual_hash:
        raise ValueError("Source EPUB SHA-256 does not match this workspace. Use a different --workspace or manually clear the old workspace.")

def zip_href(base_dir: str, href: str) -> str:
    uri = urlsplit(href)
    path = unquote(uri.path)
    if uri.scheme or uri.netloc or path.startswith("/"):
        raise ValueError(f"EPUB href must be relative to the ZIP root: {href}")
    resolved = posixpath.normpath(posixpath.join(base_dir, path))
    if resolved == ".." or resolved.startswith("../"):
        raise ValueError(f"EPUB href escapes the ZIP root: {href}")
    return resolved

def rewrite_image_references(content: str, chapter_path: str, image_map: Dict[str, str]) -> str:
    def rewrite_url(ref):
        try:
            source = zip_href(posixpath.dirname(chapter_path), ref)
        except ValueError:
            return ref
        if source not in image_map:
            return ref
        uri = urlsplit(ref)
        return (quote(image_map[source], safe="/") + ("?" + uri.query if uri.query else "")
                + ("#" + uri.fragment if uri.fragment else ""))

    def rewrite_srcset(value):
        parts = []
        pos = 0
        for_candidate = re.compile(r"[^\s,]+\S*")
        while pos < len(value):
            candidate = for_candidate.search(value, pos)
            if candidate is None:
                break
            url = candidate[0].rstrip(",")
            parts.extend((value[pos:candidate.start()], rewrite_url(url), candidate[0][len(url):]))
            pos = candidate.end()
            if candidate[0].endswith(","):
                continue
            # Keep descriptors (1x, 640w, etc.) and separators exactly as written.
            comma = value.find(",", pos)
            end = len(value) if comma < 0 else comma + 1
            parts.append(value[pos:end])
            pos = end
        return "".join(parts) + value[pos:]

    def rewrite_attribute(match):
        name = match.group(2).lower()
        value = html.unescape(match.group(5))
        if name in {"src", "href", "xlink:href"}:
            changed = rewrite_url(value)
        elif name == "srcset":
            # A URL ends at whitespace; trailing commas separate descriptor-less candidates.
            # Commas inside a URL (notably data: URLs) belong to that URL.
            changed = rewrite_srcset(value)
        elif name == "style":
            changed = re.sub(r"url\(\s*([\"']?)(.*?)\1\s*\)",
                             lambda m: m[0].replace(m[2], rewrite_url(m[2].strip()), 1),
                             value, flags=re.IGNORECASE)
        else:
            return match[0]
        if changed == value:
            return match[0]
        return match[1] + match[2] + match[3] + match[4] + html.escape(changed, quote=True) + match[4]

    def rewrite_tag(match):
        tag = match[0]
        if not re.match(r"<[A-Za-z_][\w:.-]*(?=[\s/>])", tag):
            return tag
        return re.sub(r"(\s+)([\w:.-]+)(\s*=\s*)([\"'])(.*?)\4",
                      rewrite_attribute, tag, flags=re.DOTALL)

    # Consume comments/CDATA as whole tokens, and allow '>' inside quoted attributes.
    return re.sub(r"<!--[\s\S]*?-->|<!\[CDATA\[[\s\S]*?\]\]>|<(?:[^\"'<>]|\"[^\"]*\"|'[^']*')*>",
                  rewrite_tag, content)

def extract_epub(epub_path: Path, workspace: Path, title_override: Optional[str] = None, author_override: Optional[str] = None) -> Dict:
    source_hash = source_sha256(epub_path)
    manifest_path = workspace / "manifest.json"
    if manifest_path.is_file():
        check_source_hash(json.loads(manifest_path.read_text(encoding="utf-8")), source_hash)
    workspace.mkdir(parents=True, exist_ok=True)
    source_dir = workspace / "source"
    chapters_dir = workspace / "chapters"
    images_dir = workspace / "images"
    source_dir.mkdir(exist_ok=True)
    chapters_dir.mkdir(exist_ok=True)
    images_dir.mkdir(exist_ok=True)

    with zipfile.ZipFile(epub_path, "r") as z:
        # Find rootfile OPF
        container_data = z.read("META-INF/container.xml")
        root = ET.fromstring(container_data)
        ns = {"c": "urn:oasis:names:tc:opendocument:xmlns:container"}
        rootfile_elem = root.find(".//c:rootfile", ns)
        if rootfile_elem is None:
            raise ValueError("Invalid EPUB: META-INF/container.xml has no rootfile")
        opf_path = rootfile_elem.attrib["full-path"]
        opf_dir = Path(opf_path).parent

        # Read OPF
        opf_data = z.read(opf_path)
        opf_root = ET.fromstring(opf_data)
        opf_ns = {
            "opf": "http://www.idpf.org/2007/opf",
            "dc": "http://purl.org/dc/elements/1.1/",
        }

        # Metadata
        title = title_override or "Unknown Title"
        author = author_override or "Unknown Author"
        title_elem = opf_root.find(".//dc:title", opf_ns)
        if title_elem is not None and title_elem.text and not title_override:
            title = title_elem.text.strip()
        creator_elem = opf_root.find(".//dc:creator", opf_ns)
        if creator_elem is not None and creator_elem.text and not author_override:
            author = creator_elem.text.strip()

        # Manifest: id -> href, media-type
        manifest: Dict[str, Tuple[str, str]] = {}
        for item in opf_root.findall(".//opf:manifest/opf:item", opf_ns):
            item_id = item.attrib["id"]
            href = item.attrib["href"]
            media_type = item.attrib.get("media-type", "")
            manifest[item_id] = (href, media_type)

        # Account for every reading item before extracting anything from the ZIP.
        spine_items = []
        spine_results = []
        unsupported = []
        for idx, itemref in enumerate(opf_root.findall(".//opf:spine/opf:itemref", opf_ns), start=1):
            idref = itemref.attrib["idref"]
            if idref not in manifest:
                raise ValueError(f"EPUB spine item not found in manifest: {idref}")
            href, media_type = manifest[idref]
            full_href = zip_href(opf_dir.as_posix(), href)
            if full_href not in z.namelist():
                raise ValueError(f"EPUB spine content not found: {full_href}")
            supported = media_type in {"application/xhtml+xml", "text/html"}
            result = {"num": idx, "idref": idref, "source_path": full_href,
                      "media_type": media_type, "status": "pending" if supported else "unsupported"}
            spine_results.append(result)
            if supported:
                spine_items.append((idx, idref, href, full_href, result))
            else:
                unsupported.append(f"{full_href} ({media_type or 'unknown media type'})")
        if unsupported:
            for result in spine_results:
                print(f"[*] Spine {result['num']}: {result['source_path']} [{result['status']}]")
            raise ValueError("Unsupported EPUB spine content; no complete workspace created: " + "; ".join(unsupported))

        # Keep ZIP hierarchy so relative dependencies inside SVGs remain resolvable.
        image_map = {}
        image_destinations = {}
        image_files = []
        for item_id, (href, media_type) in manifest.items():
            if media_type.startswith("image/"):
                img_full_path = zip_href(opf_dir.as_posix(), href)
                try:
                    img_bytes = z.read(img_full_path)
                    dest_img = images_dir / img_full_path
                    if images_dir.is_symlink() or not dest_img.resolve().is_relative_to(images_dir.resolve()):
                        raise ValueError(f"Image path escapes images directory: {img_full_path}")
                    current = images_dir
                    for part in Path(img_full_path).parts:
                        current = current / part
                        relative = current.relative_to(images_dir).as_posix()
                        previous = image_destinations.get(relative.casefold())
                        if previous is not None and previous != relative:
                            raise ValueError(f"Image path case collision (OPF-060): {previous} and {relative}")
                        image_destinations[relative.casefold()] = relative
                        if current.parent.is_dir():
                            for existing in current.parent.iterdir():
                                if existing.name.casefold() == part.casefold() and existing.name != part:
                                    raise ValueError(f"Image destination collision; refusing to overwrite: {relative}")
                        if current.is_symlink():
                            raise ValueError(f"Image destination symlink; refusing to overwrite: {relative}")
                    if dest_img.exists() and (not dest_img.is_file() or dest_img.read_bytes() != img_bytes):
                        raise ValueError(f"Image destination collision; refusing to overwrite: {img_full_path}")
                    image_files.append((dest_img, img_bytes))
                    image_map[img_full_path] = dest_img.relative_to(workspace).as_posix()
                except KeyError:
                    pass
        # Validate all destinations before writing any images.
        for dest_img, img_bytes in image_files:
            dest_img.parent.mkdir(parents=True, exist_ok=True)
            dest_img.write_bytes(img_bytes)

        # Parse TOC if available
        toc_titles: Dict[str, str] = {}
        # Try NCX
        ncx_item = None
        for item_id, (href, media_type) in manifest.items():
            if media_type == "application/x-dtbncx+xml":
                ncx_item = zip_href(opf_dir.as_posix(), href)
                break
        if ncx_item and ncx_item in z.namelist():
            try:
                ncx_root = ET.fromstring(z.read(ncx_item))
                ncx_ns = {"ncx": "http://www.daisy.org/z3986/2005/ncx/"}
                for navpoint in ncx_root.findall(".//ncx:navPoint", ncx_ns):
                    label_elem = navpoint.find(".//ncx:text", ncx_ns)
                    content_elem = navpoint.find(".//ncx:content", ncx_ns)
                    if label_elem is not None and content_elem is not None:
                        src = content_elem.attrib.get("src", "").split("#")[0]
                        label = label_elem.text.strip() if label_elem.text else ""
                        if src and label:
                            toc_titles.setdefault(zip_href(posixpath.dirname(ncx_item), src), label)
            except ET.ParseError:
                pass

        chapters_manifest = []
        for idx, idref, href, full_href, result in spine_items:
            raw_content = z.read(full_href).decode("utf-8", errors="ignore")

            # Try to extract a meaningful title
            chapter_name = toc_titles.get(full_href)
            if not chapter_name:
                h_match = re.search(r"<h[1-2][^>]*>(.*?)</h[1-2]>", raw_content, re.IGNORECASE | re.DOTALL)
                if h_match:
                    clean_h = re.sub(r"<[^>]+>", "", h_match.group(1)).strip()
                    if clean_h and len(clean_h) < 120:
                        chapter_name = clean_h
            if not chapter_name:
                chapter_name = f"Chapter {idx:02d} ({Path(href).stem})"

            # Extract clean text and paragraphs
            text_only = re.sub(r"<script[^>]*>.*?</script>", "", raw_content, flags=re.DOTALL | re.IGNORECASE)
            text_only = re.sub(r"<style[^>]*>.*?</style>", "", text_only, flags=re.DOTALL | re.IGNORECASE)
            
            # Words count
            plain_text = re.sub(r"<[^>]+>", " ", text_only)
            plain_text = html.unescape(plain_text)
            words = len(re.findall(r"\b\w+\b", plain_text))

            # Only skip truly empty bodies, never short chapters or image pages.
            body_match = re.search(r"<body\b[^>]*>(.*?)</body\s*>", text_only, re.DOTALL | re.IGNORECASE)
            body_content = body_match.group(1) if body_match else text_only
            body_content = re.sub(r"<!--.*?-->", "", body_content, flags=re.DOTALL)
            body_text = html.unescape(re.sub(r"<[^>]+>", " ", body_content)).strip()
            has_img = re.search(r"<(?:[\w.-]+:)?(?:img|image|svg)\b", body_content, re.IGNORECASE)
            if not body_text and not has_img:
                result["status"] = "empty"
                print(f"[*] Skipping empty page: {full_href}")
                continue

            slug = f"ch{idx:02d}_{slugify(chapter_name)}"
            source_file = source_dir / f"{slug}.txt"
            target_file = chapters_dir / f"{slug}.md"

            raw_content = rewrite_image_references(raw_content, full_href, image_map)
            source_file.write_text(raw_content, encoding="utf-8")
            result.update(status="chapter", source_file=str(source_file.relative_to(workspace)))
            print(f"[*] Spine {idx}: {full_href} [chapter]")

            chapters_manifest.append({
                "num": idx,
                "slug": slug,
                "name": chapter_name,
                "source_file": str(source_file.relative_to(workspace)),
                "target_file": str(target_file.relative_to(workspace)),
                "words": words,
                "status": "pending",
            })

    manifest_data = {
        "book_title": title,
        "book_author": author,
        "source_format": "epub",
        "source_path": str(epub_path.resolve()),
        "source_sha256": source_hash,
        "target_language": "zh-CN",
        "total_chapters": len(chapters_manifest),
        "total_words": sum(c["words"] for c in chapters_manifest),
        "spine_results": spine_results,
        "image_map": image_map,
        "chapters": chapters_manifest,
    }

    manifest_path = workspace / "manifest.json"
    manifest_path.write_text(json.dumps(manifest_data, indent=2, ensure_ascii=False), encoding="utf-8")
    return manifest_data

# ---------------------------------------------------------------------------
# Status Workflow
# ---------------------------------------------------------------------------

def inspect_status(workspace: Path):
    manifest_path = workspace / "manifest.json"
    if not manifest_path.is_file():
        print(f"Error: manifest.json not found in {workspace}")
        sys.exit(1)

    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (not isinstance(data, dict) or not isinstance(data.get("chapters"), list)
                or any(not isinstance(ch, dict) for ch in data["chapters"])):
            raise ValueError
    except (ValueError, UnicodeError):
        print("工作区清单损坏，可以重新解包新建工作区，已有译稿保留在原目录。")
        sys.exit(1)
    chapters = data["chapters"]
    
    done_count = 0
    done_words = 0
    total_words = data.get("total_words", 0)

    print(f"\n=======================================================")
    print(f" 《{data.get('book_title')}》")
    print(f" 作者: {data.get('book_author', 'Unknown')} | 源格式: {data.get('source_format', 'epub').upper()}")
    print(f" 工作区: {workspace}")
    print(f"=======================================================")
    print(f"{'#':<4} {'章节标识 (Slug)':<24} {'字数':<7} {'状态':<10} {'章节名称'}")
    print("-" * 75)

    for ch in chapters:
        target_path = workspace / ch["target_file"]
        is_done = target_path.is_file() and bool(target_path.read_text(encoding="utf-8").strip())
        ch["status"] = "done" if is_done else "pending"
        status = "DONE" if is_done else "PENDING"
        if is_done:
            done_count += 1
            done_words += ch.get("words", 0)
        print(f"{ch['num']:<4} {ch['slug']:<24} {ch.get('words', 0):<7} {status:<10} {ch['name'][:35]}")

    percent = (done_words / total_words * 100) if total_words > 0 else 0
    print("-" * 75)
    print(f"总进度: {done_count}/{len(chapters)} 章完成 | {done_words}/{total_words} 词已翻译 ({percent:.1f}%)\n")
    manifest_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

# ---------------------------------------------------------------------------
# Pack / Build Workflow
# ---------------------------------------------------------------------------

CSS_STYLES = """
@charset "utf-8";
body {
    font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "Source Han Serif CN", Georgia, serif;
    line-height: 1.75;
    margin: 1.5rem auto;
    max-width: 48rem;
    padding: 0 1.25rem;
    color: #1a1a1a;
}
h1.book-title {
    font-size: 2.2rem;
    font-weight: 700;
    margin-bottom: 0.5rem;
    text-align: center;
}
p.book-subtitle {
    font-size: 1.1rem;
    color: #555;
    text-align: center;
    margin-bottom: 0.5rem;
}
p.book-author {
    font-size: 1rem;
    color: #666;
    text-align: center;
    margin-bottom: 3rem;
}
.toc-container {
    margin: 3rem 0;
    padding: 1.5rem;
    background: #fdfdfd;
    border: 1px solid #e2e8f0;
    border-radius: 8px;
    page-break-after: always;
}
.toc-title {
    font-size: 1.4rem;
    font-weight: bold;
    margin-bottom: 1rem;
    border-bottom: 2px solid #e2e8f0;
    padding-bottom: 0.5rem;
}
ul.toc-list {
    list-style: none;
    padding-left: 0;
    margin: 0;
}
ul.toc-list li {
    margin: 0.5rem 0;
    line-height: 1.5;
}
ul.toc-list li a {
    color: #2b6cb0;
    text-decoration: none;
}
.chapter-container {
    margin-top: 3.5rem;
    page-break-before: always;
}
h2.chapter-title {
    font-size: 1.6rem;
    font-weight: bold;
    margin-top: 2rem;
    margin-bottom: 1.2rem;
    border-bottom: 1px solid #eaeaea;
    padding-bottom: 0.4rem;
}
h3 {
    font-size: 1.3rem;
    margin-top: 1.5rem;
    margin-bottom: 0.8rem;
}
p {
    margin-bottom: 1.2rem;
    text-align: justify;
    text-indent: 2em;
}
p.no-indent {
    text-indent: 0;
}
blockquote {
    border-left: 4px solid #4a5568;
    margin: 1.5rem 0;
    padding: 0.75rem 1.25rem;
    color: #4a5568;
    background: #f7fafc;
}
blockquote p {
    text-indent: 0;
    margin-bottom: 0.5rem;
}
img {
    max-width: 100%;
    height: auto;
    display: block;
    margin: 1.5rem auto;
}
"""

BILINGUAL_CSS = """
p.bilingual-original {
    font-size: 0.9em;
    color: #555;
    text-indent: 0;
    margin-bottom: 0.35em;
}
p.bilingual-translation {
    font-size: 1em;
    color: #1a1a1a;
}
"""

def md_to_html(md_text: str, bilingual: bool = False) -> str:
    lines = md_text.splitlines()
    html_parts = []
    in_quote = False
    quote_buf = []

    def flush_quote():
        nonlocal in_quote, quote_buf
        if in_quote and quote_buf:
            html_parts.append(f"<blockquote><p>{'<br/>'.join(quote_buf)}</p></blockquote>")
            quote_buf = []
            in_quote = False

    for line in lines:
        stripped = line.strip()
        if not stripped:
            flush_quote()
            continue

        if stripped.startswith(">"):
            in_quote = True
            quote_buf.append(html.escape(stripped.lstrip(">").strip()))
            continue
        else:
            flush_quote()

        if bilingual and stripped.startswith("[EN] "):
            html_parts.append(f"<p class='bilingual-original' lang='en'>{html.escape(stripped[5:])}</p>")
        elif stripped.startswith("### "):
            html_parts.append(f"<h3>{html.escape(stripped[4:])}</h3>")
        elif stripped.startswith("## "):
            html_parts.append(f"<h2 class='chapter-title'>{html.escape(stripped[3:])}</h2>")
        elif stripped.startswith("# "):
            html_parts.append(f"<h2 class='chapter-title'>{html.escape(stripped[2:])}</h2>")
        elif stripped.startswith("!["):
            img_m = re.match(r"!\[(.*?)\]\((.*?)\)", stripped)
            if img_m:
                alt, src = img_m.groups()
                html_parts.append(f"<p class='no-indent'><img src='{html.escape(src)}' alt='{html.escape(alt)}'/></p>")
            else:
                html_parts.append(f"<p>{html.escape(stripped)}</p>")
        elif bilingual:
            html_parts.append(f"<p class='bilingual-translation' lang='zh-CN'>{html.escape(stripped)}</p>")
        else:
            html_parts.append(f"<p>{html.escape(stripped)}</p>")

    flush_quote()
    return "\n".join(html_parts)

def encode_epub_manifest_urls(epub_path: Path):
    # Calibre's OPF input can emit literal spaces in manifest hrefs (RSC-020).
    # Fix only those resource URLs, leaving archive names and resource bytes alone.
    with zipfile.ZipFile(epub_path) as source:
        container = ET.fromstring(source.read("META-INF/container.xml"))
        opf_path = container.find(".//{urn:oasis:names:tc:opendocument:xmlns:container}rootfile").attrib["full-path"]
        root = ET.fromstring(source.read(opf_path))
        changed = False
        for item in root.findall(".//{http://www.idpf.org/2007/opf}manifest/{http://www.idpf.org/2007/opf}item"):
            href = item.attrib["href"]
            uri = urlsplit(href)
            encoded = quote(unquote(uri.path), safe="/") + ("?" + uri.query if uri.query else "") + ("#" + uri.fragment if uri.fragment else "")
            if encoded != href:
                item.set("href", encoded)
                changed = True
        if not changed:
            return
        with tempfile.NamedTemporaryFile(dir=epub_path.parent, suffix=".epub", delete=False) as temp:
            temp_path = Path(temp.name)
        try:
            with zipfile.ZipFile(temp_path, "w") as dest:
                dest.comment = source.comment
                for entry in source.infolist():
                    dest.writestr(entry, ET.tostring(root, encoding="utf-8", xml_declaration=True)
                                  if entry.filename == opf_path else source.read(entry))
            temp_path.replace(epub_path)
        finally:
            temp_path.unlink(missing_ok=True)

def build_pack(workspace: Path, output_dir: Path, formats: List[str], allow_partial: bool = False, push_kindle: bool = False, bilingual: bool = False):
    if not formats or any(fmt not in {"epub", "azw3", "all"} for fmt in formats):
        raise ValueError("Invalid --formats: use epub, azw3, all, or epub,azw3.")
    manifest_path = workspace / "manifest.json"
    if not manifest_path.is_file():
        print(f"Error: manifest.json not found in {workspace}")
        sys.exit(1)

    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not data.get("source_sha256"):
        check_source_hash(data, "")
    check_source_hash(data, source_sha256(Path(data["source_path"])))
    book_title = data.get("book_title", "Translated Book")
    book_author = data.get("book_author", "Unknown Author")
    chapters = data.get("chapters", [])
    if not chapters:
        raise ValueError("Cannot build: workspace has no content chapters.")
    edition_title = book_title + ("（中英对照版）" if bilingual else " (中文精排版)")

    missing = []
    chapter_html_list = []
    toc_links = []

    for ch in chapters:
        target_path = workspace / ch["target_file"]
        slug = ch["slug"]
        content = target_path.read_text(encoding="utf-8") if target_path.is_file() else ""
        if not content.strip():
            missing.append(slug)
            if not allow_partial:
                continue
            content = f"## {ch['name']}\n\n（待翻译）"
        body_html = md_to_html(content, bilingual=bilingual)
        toc_links.append(f"<li><a href='#{html.escape(slug)}'>{html.escape(ch['name'])}</a></li>")
        chapter_html_list.append(f"<div id='{slug}' class='chapter-container'>\n{body_html}\n</div>")

    if missing and not allow_partial:
        print(f"[-] Cannot build: {len(missing)} chapters still pending translation.")
        print(f"    Missing: {', '.join(missing[:8])}{'...' if len(missing) > 8 else ''}")
        print("    Use --allow-partial if you want to preview completed chapters.")
        sys.exit(1)

    output_dir.mkdir(parents=True, exist_ok=True)
    master_html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8"/>
<title>{html.escape(edition_title)}</title>
<style>
{CSS_STYLES + BILINGUAL_CSS if bilingual else CSS_STYLES}
</style>
</head>
<body>

<div style="text-align:center; margin-top:4rem;">
    <h1 class="book-title">{html.escape(edition_title if bilingual else book_title)}</h1>
    <p class="book-subtitle">{('部分章节中英对照预览（含待翻译章节）' if missing else '中英对照版') if bilingual else ('部分章节翻译预览（含待翻译章节）' if missing else '中文翻译版')}</p>
    <p class="book-author">{html.escape(book_author)} 著</p>
</div>

<div class="toc-container">
    <div class="toc-title">目录 ｜ Contents</div>
    <ul class="toc-list">
        {''.join(toc_links)}
    </ul>
</div>

{''.join(chapter_html_list)}

</body>
</html>
"""
    master_html_path = workspace / ("master_book-bilingual.html" if bilingual else "master_book.html")
    master_html_path.write_text(master_html, encoding="utf-8")
    print(f"[+] Master HTML generated: {master_html_path}")

    # Compile with Calibre
    calibre_bin = find_calibre_convert()
    if not calibre_bin:
        print("[-] Calibre ebook-convert not found. Install ebook-to-kindle core, set EBOOK_TO_KINDLE_RUNTIME, or add ebook-convert to PATH.")
        print(f"    Master HTML is available at: {master_html_path}")
        sys.exit(1)

    # HTML input flattens resource names. An OPF input preserves the image tree
    # and includes dependencies used only inside SVGs, without rewriting workspace SVGs.
    calibre_input = master_html_path
    input_options = []
    if data.get("image_map"):
        opf_ns = "{http://www.idpf.org/2007/opf}"
        dc_ns = "{http://purl.org/dc/elements/1.1/}"
        package = ET.Element(opf_ns + "package", {"version": "2.0", "unique-identifier": "source"})
        metadata = ET.SubElement(package, opf_ns + "metadata")
        ET.SubElement(metadata, dc_ns + "identifier", {"id": "source"}).text = "urn:sha256:" + data["source_sha256"]
        for key, value in (("title", edition_title), ("creator", book_author), ("language", "zh-CN")):
            ET.SubElement(metadata, dc_ns + key).text = value
        items = ET.SubElement(package, opf_ns + "manifest")
        ET.SubElement(items, opf_ns + "item", {"id": "body", "href": master_html_path.name,
                                              "media-type": "application/xhtml+xml"})
        with zipfile.ZipFile(data["source_path"]) as source_zip:
            container = ET.fromstring(source_zip.read("META-INF/container.xml"))
            original_opf = container.find(".//{urn:oasis:names:tc:opendocument:xmlns:container}rootfile").attrib["full-path"]
            original = ET.fromstring(source_zip.read(original_opf))
            for item in original.findall(".//" + opf_ns + "manifest/" + opf_ns + "item"):
                if not item.attrib.get("media-type", "").startswith("image/"):
                    continue
                origin = zip_href(posixpath.dirname(original_opf), item.attrib["href"])
                dest = data["image_map"].get(origin)
                if dest:
                    if not (workspace / dest).is_file():
                        raise ValueError(f"Missing workspace image: {dest}")
                    ET.SubElement(items, opf_ns + "item", {"id": "image" + str(len(items)),
                                  "href": quote(dest, safe="/"), "media-type": item.attrib["media-type"]})
        ET.SubElement(ET.SubElement(package, opf_ns + "spine"), opf_ns + "itemref", {"idref": "body"})
        calibre_input = master_html_path.with_suffix(".opf")
        ET.ElementTree(package).write(calibre_input, encoding="utf-8", xml_declaration=True)
        input_options = ["--dont-package"]

    clean_title = re.sub(r'[/\\:*?"<>|]', "_", book_title)
    suffix = "（中英对照版）" if bilingual else " (中文精排版)"
    epub_out = output_dir / f"{clean_title}{suffix}.epub"
    azw3_out = output_dir / f"{clean_title}{suffix}.azw3"
    generated = []

    if "epub" in formats or "all" in formats:
        print(f"[*] Compiling EPUB to: {epub_out}")
        cmd = [
            str(calibre_bin),
            str(calibre_input),
            str(epub_out),
            "--title", edition_title,
            "--authors", book_author,
            "--language", "zh-CN",
            "--level1-toc", "//h:h2[@class='chapter-title']",
            "--chapter", "//h:div[contains(@class,'chapter-container')]",
            "--no-default-epub-cover",
        ] + input_options
        previous_stat = epub_out.stat() if epub_out.is_file() else None
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode == 0 and epub_out.is_file() and epub_out.stat().st_size > 0 and epub_out.stat() != previous_stat:
            if input_options:
                encode_epub_manifest_urls(epub_out)
            generated.append(epub_out)
            print(f"[+] EPUB compiled successfully: {epub_out}")
        else:
            print(f"[-] EPUB compilation failed: {res.stderr[:300]}")
            sys.exit(1)

    if "azw3" in formats or "all" in formats:
        source_for_azw3 = epub_out if "epub" in formats or "all" in formats else calibre_input
        print(f"[*] Compiling AZW3 to: {azw3_out}")
        cmd = [
            str(calibre_bin),
            str(source_for_azw3),
            str(azw3_out),
            "--output-profile", "kindle_pw",
        ]
        if source_for_azw3 == calibre_input:
            cmd.extend(["--title", edition_title, "--authors", book_author,
                        "--language", "zh-CN",
                        "--level1-toc", "//h:h2[@class='chapter-title']",
                        "--chapter", "//h:div[contains(@class,'chapter-container')]"] + input_options)
        elif bilingual:
            cmd.extend(["--title", edition_title])
        previous_stat = azw3_out.stat() if azw3_out.is_file() else None
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode == 0 and azw3_out.is_file() and azw3_out.stat().st_size > 0 and azw3_out.stat() != previous_stat:
            generated.append(azw3_out)
            print(f"[+] AZW3 compiled successfully: {azw3_out}")
        else:
            print(f"[-] AZW3 compilation failed: {res.stderr[:300]}")
            sys.exit(1)

    if not generated:
        raise ValueError("No output files were generated.")

    if push_kindle:
        push_script = Path(__file__).resolve().parents[2] / "ebook-to-kindle" / "scripts" / "send_to_kindle.py"
        if not push_script.is_file():
            print("[-] Kindle push requires ebook-to-kindle/scripts/send_to_kindle.py alongside ebook-translate. Use the kindle-book-pipeline repository.")
            sys.exit(1)
        target_file = epub_out if "epub" in formats or "all" in formats else azw3_out
        if target_file.is_file():
            print(f"[*] Pushing {target_file} to Kindle...")
            subprocess.run([sys.executable, str(push_script), str(target_file), "--method", "auto"], check=True)

# ---------------------------------------------------------------------------
# Main CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Ebook Translation and Compilation Pipeline")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # unpack
    p_unpack = subparsers.add_parser("unpack", help="Unpack EPUB into translation workspace")
    p_unpack.add_argument("file", help="Path to input .epub file")
    p_unpack.add_argument("--workspace", help="Custom workspace directory")
    p_unpack.add_argument("--title", help="Override book title")
    p_unpack.add_argument("--author", help="Override book author")

    # status
    p_status = subparsers.add_parser("status", help="Inspect translation progress")
    p_status.add_argument("--workspace", required=True, help="Workspace directory")

    # pack
    p_pack = subparsers.add_parser("pack", help="Assemble translated chapters into EPUB and AZW3")
    p_pack.add_argument("--workspace", required=True, help="Workspace directory")
    p_pack.add_argument("--output-dir", default=str(default_output_dir()), help="Output directory (CLI > $EBOOK_TO_KINDLE_OUTPUT_DIR > profile.json > ~/Documents/KindleBooks)")
    p_pack.add_argument("--formats", default="epub,azw3", help="Output formats (epub,azw3,all)")
    p_pack.add_argument("--allow-partial", action="store_true", help="Allow compiling partial book")
    p_pack.add_argument("--bilingual", action="store_true", help="Build Chinese-English edition from Agent-authored [EN] original / Chinese paragraph pairs")
    p_pack.add_argument("--push-kindle", action="store_true", help="Push finished book to Kindle")

    args = parser.parse_args()

    if args.command == "unpack":
        epub_p = Path(args.file).expanduser().resolve()
        if not epub_p.is_file():
            print(f"[-] Input file not found: {epub_p}")
            sys.exit(1)
        ws_name = slugify(epub_p.stem)
        ws = Path(args.workspace).expanduser() if args.workspace else DEFAULT_WORKSPACE_BASE / ws_name
        manifest = extract_epub(epub_p, ws, args.title, args.author)
        print(f"[+] Successfully unpacked 《{manifest['book_title']}》")
        print(f"    Workspace: {ws}")
        print(f"    Total chapters: {manifest['total_chapters']}")
        print(f"    Total words: {manifest['total_words']}")
        next_command = shlex.join([sys.executable, str(Path(__file__).resolve()), "pack", "--workspace", str(ws)])
        print(f"    Next step: Translate chapters in {ws / 'chapters'} and run `{next_command}`")

    elif args.command == "status":
        ws = Path(args.workspace).expanduser().resolve()
        inspect_status(ws)

    elif args.command == "pack":
        ws = Path(args.workspace).expanduser().resolve()
        out_d = Path(args.output_dir).expanduser().resolve()
        fmts = [f.strip().lower() for f in args.formats.split(",")]
        build_pack(ws, out_d, fmts, args.allow_partial, args.push_kindle, bilingual=args.bilingual)

if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, zipfile.BadZipFile, ET.ParseError) as exc:
        print(f"[-] {exc}", file=sys.stderr)
        sys.exit(1)
