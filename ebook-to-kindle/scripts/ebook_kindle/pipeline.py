"""Deterministic conversion pipeline for Paperwhite 2 targets."""

from __future__ import annotations

import hashlib
import json
import inspect
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
import unicodedata
import uuid
import zipfile
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple
from xml.etree import ElementTree

from . import normalize
from .core import (
    SkillError,
    build_job_env,
    ensure_private_dir,
    make_private_job_dir,
    offline_subprocess_command,
    safe_report,
    sha256_file,
    snapshot_calibre_user_state,
    unique_output_names,
    validate_local_input,
)
from .gates import (
    compare_content,
    docling_page_character_counts,
    inspect_epub_structure,
    inspect_source_resources,
    inspect_source_semantics,
    parse_epubcheck_report,
    validate_image_retention,
    validate_source_text,
    validate_epub_container,
    validate_kindle_container,
    validate_ocr_text,
    validate_semantic_retention,
)
from .pdf import choose_pdf_mode
from .performance import build_performance_review
from .runtime import guard_runtime_root, load_manifest, readiness, runtime_operation_lock, runtime_paths, verify_runtime
from .workspace import BookWorkspace, cache_key, fingerprint_files


CACHE_CONTRACT_VERSION = "0.3.0-node-v1"
SEMANTIC_RECIPE_VERSION = "0.3.0-pdf-semantic-v1"
MASTER_RECIPE_VERSION = "0.3.2-metadata-readability-master-v1"
FINAL_RECIPE_VERSION = "0.3.1-readability-final-v1"
SEMANTIC_RECIPE_FILES = (
    "scripts/pdf_backend.py",
    "scripts/pdf_semantics.py",
)
MASTER_RECIPE_FILES = (
    "scripts/ebook_kindle/normalize.py",
    "assets/pw2.css",
)
FINAL_RECIPE_FILES = (
    "scripts/ebook_kindle/normalize.py",
    "scripts/ebook_kindle/gates.py",
    "assets/pw2.css",
)


@dataclass(frozen=True)
class ConversionRequest:
    input_value: str
    output_dir: Path
    formats: Tuple[str, ...]
    runtime_root: Path
    pdf_mode: str = "auto"
    title: Optional[str] = None
    authors: Optional[str] = None
    language: Optional[str] = None
    overwrite: bool = False
    timeout_seconds: int = 1200
    report_path: Optional[Path] = None
    workspace_root: Optional[Path] = None
    reuse_policy: str = "auto"


def _clean_metadata_value(value: Optional[str], label: str, maximum: int) -> Optional[str]:
    if value is None:
        return None
    cleaned = " ".join(value.split())
    if not cleaned or len(cleaned) > maximum or any(
        unicodedata.category(character).startswith("C") for character in cleaned
    ):
        raise SkillError("CONVERSION_FAILED", f"{label} 元数据不合法。")
    return cleaned


def _conversion_metadata(request: ConversionRequest, input_path: Path) -> Dict[str, Optional[str]]:
    title = _clean_metadata_value(request.title, "title", 300) or input_path.stem
    authors = _clean_metadata_value(request.authors, "authors", 500)
    language = _clean_metadata_value(request.language, "language", 35)
    if language is not None and re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*", language) is None:
        raise SkillError("CONVERSION_FAILED", "language 必须是 BCP 47 语言标签。")
    return {"title": title, "authors": authors, "language": language.casefold() if language else None}


def _runtime_cache_contract(runtime_root: Path) -> Dict[str, Any]:
    manifest = load_manifest(runtime_root)
    components = manifest.get("components", {}) if isinstance(manifest.get("components"), dict) else {}
    return {
        "runtime_name": manifest.get("runtime_name"),
        "runtime_schema": manifest.get("schema_version"),
        "runtime_version": manifest.get("runtime_version"),
        "core": components.get("core"),
        "pdf": components.get("pdf"),
    }


def _callable_fingerprints(functions: Sequence[Callable[..., Any]]) -> Dict[str, str]:
    result = {}
    for function in functions:
        try:
            source = inspect.getsource(function).encode("utf-8")
        except (OSError, TypeError) as exc:
            raise SkillError("WORKSPACE_INVALID", "无法提取缓存配方函数。") from exc
        result[f"{function.__module__}.{function.__name__}"] = hashlib.sha256(source).hexdigest()
    return result


def _workspace_cache_plan(
    *,
    input_path: Path,
    request: ConversionRequest,
    runtime_root: Path,
    skill_root: Path,
) -> Dict[str, Any]:
    source_sha256 = sha256_file(input_path)
    metadata = _conversion_metadata(request, input_path)
    suffix = input_path.suffix.lower()
    source_resource_contract = inspect_source_resources(input_path)
    runtime_contract = _runtime_cache_contract(runtime_root)
    all_files = sorted(set((*SEMANTIC_RECIPE_FILES, *MASTER_RECIPE_FILES, *FINAL_RECIPE_FILES)))
    fingerprints = fingerprint_files(skill_root, all_files)
    semantic_key = None
    if suffix == ".pdf":
        semantic_key = cache_key(
            {
                "recipe_version": SEMANTIC_RECIPE_VERSION,
                "layer": "semantic",
                "source_sha256": source_sha256,
                "pdf_mode_request": request.pdf_mode,
                "pdf_runtime": runtime_contract.get("pdf"),
                "files": {name: fingerprints[name] for name in SEMANTIC_RECIPE_FILES},
                "functions": _callable_fingerprints(
                    (choose_pdf_mode, _validate_pdf_semantic_metrics, docling_page_character_counts, validate_ocr_text)
                ),
            }
        )
    master_key = cache_key(
        {
            "recipe_version": MASTER_RECIPE_VERSION,
            "layer": "master",
            "source_sha256": source_sha256,
            "source_title": input_path.stem,
            "book_metadata": metadata,
            "input_suffix": suffix,
            "source_resource_fingerprint": source_resource_contract.get("resource_fingerprint"),
            "pdf_mode_request": request.pdf_mode if suffix == ".pdf" else None,
            "semantic_key": semantic_key,
            "core_runtime": runtime_contract.get("core"),
            "files": {name: fingerprints[name] for name in MASTER_RECIPE_FILES},
            "functions": _callable_fingerprints(
                (
                    _build_master_epub,
                    _build_pdf_master_from_export,
                    kindle_master_options,
                    kindle_direct_options,
                    kindle_existing_epub_options,
                    _materialize_master_start_location,
                    _extract_master_baseline,
                    inspect_source_resources,
                    inspect_source_semantics,
                    inspect_epub_structure,
                    validate_image_retention,
                    validate_semantic_retention,
                    compare_content,
                )
            ),
        }
    )
    final_keys = {
        fmt: cache_key(
            {
                "recipe_version": FINAL_RECIPE_VERSION,
                "layer": "final",
                "source_sha256": source_sha256,
                "master_key": master_key,
                "format": fmt,
                "core_runtime": runtime_contract.get("core"),
                "files": {name: fingerprints[name] for name in FINAL_RECIPE_FILES},
                "functions": _callable_fingerprints(
                    (
                        kindle_layout_options,
                        validate_kindle_container,
                        inspect_epub_structure,
                        validate_semantic_retention,
                        compare_content,
                        validate_image_retention,
                    )
                ),
            }
        )
        for fmt in request.formats
    }
    return {
        "source_sha256": source_sha256,
        "semantic_key": semantic_key,
        "master_key": master_key,
        "final_keys": final_keys,
    }


def _require_cache_node(
    node: Mapping[str, Any],
    *,
    source_sha256: str,
    master_key: Optional[str] = None,
    output_format: Optional[str] = None,
) -> None:
    valid = node.get("contract") == CACHE_CONTRACT_VERSION and node.get("source_sha256") == source_sha256
    if master_key is not None:
        valid = valid and node.get("master_key") == master_key
    if output_format is not None:
        valid = valid and node.get("format") == output_format
    if not valid:
        raise SkillError("WORKSPACE_INVALID", "单书工作区节点与当前输入或配方不匹配。")


def _workspace_lookup(
    store: BookWorkspace,
    plan: Mapping[str, Any],
    formats: Sequence[str],
    reuse_policy: str,
) -> Dict[str, Any]:
    if reuse_policy != "auto":
        return {"hit_level": "cold"}
    source_sha256 = str(plan["source_sha256"])
    master_key = str(plan["master_key"])
    finals: Dict[str, Dict[str, Any]] = {}
    exact = True
    for fmt in formats:
        key = str(plan["final_keys"][fmt])
        node = store.read_node("final", key)
        if node is None:
            exact = False
            break
        _require_cache_node(node, source_sha256=source_sha256, master_key=master_key, output_format=fmt)
        artifact = store.read_object(node.get("artifact", {}))
        quality = node.get("quality")
        if not isinstance(quality, dict):
            raise SkillError("WORKSPACE_INVALID", "最终缓存节点缺少质量证据。")
        if fmt == "epub":
            validate_epub_container(artifact)
            if quality.get("sha256") != sha256_file(artifact) or int(quality.get("bytes", -1)) != artifact.stat().st_size:
                raise SkillError("WORKSPACE_INVALID", "EPUB 缓存实物与质量证据不一致。")
        else:
            container = validate_kindle_container(artifact, fmt)
            if quality.get("sha256") != container.get("sha256") or int(quality.get("bytes", -1)) != container.get("bytes"):
                raise SkillError("WORKSPACE_INVALID", "Kindle 缓存实物与质量证据不一致。")
        finals[fmt] = {"node": node, "artifact": artifact}
    if exact and len(finals) == len(formats):
        return {"hit_level": "exact", "finals": finals}

    master = store.read_node("master", master_key)
    if master is not None:
        _require_cache_node(master, source_sha256=source_sha256)
        for field in ("master", "baseline", "source_body"):
            store.read_object(master.get(field, {}))
        if not isinstance(master.get("route_details"), dict) or not isinstance(master.get("route"), str):
            raise SkillError("WORKSPACE_INVALID", "主 EPUB 缓存节点缺少生成证据。")
        return {"hit_level": "master", "master": master}

    semantic_key = plan.get("semantic_key")
    if semantic_key:
        semantic = store.read_node("semantic", str(semantic_key))
        if semantic is not None:
            _require_cache_node(semantic, source_sha256=source_sha256)
            if not isinstance(semantic.get("tree"), dict):
                raise SkillError("WORKSPACE_INVALID", "PDF 语义缓存节点缺少文件清单。")
            if not isinstance(semantic.get("probe"), dict) or not isinstance(semantic.get("semantic_metrics"), dict):
                raise SkillError("WORKSPACE_INVALID", "PDF 语义缓存节点缺少路由证据。")
            return {"hit_level": "semantic", "semantic": semantic}
    return {"hit_level": "cold"}


def _copy_workspace_object(store: BookWorkspace, reference: Mapping[str, Any], destination: Path) -> Path:
    source = store.read_object(reference)
    shutil.copyfile(source, destination)
    os.chmod(destination, 0o600)
    if sha256_file(destination) != reference.get("sha256") or destination.stat().st_size != int(reference.get("bytes", -1)):
        raise SkillError("WORKSPACE_INVALID", "工作区对象恢复后校验失败。")
    return destination


def _extract_master_baseline(master: Path, destination: Path) -> str:
    """Extract the EPUB spine once instead of spawning a second Calibre TXT conversion."""
    try:
        baseline = normalize.source_body_text(master)
        destination.write_text(baseline, encoding="utf-8")
        os.chmod(destination, 0o600)
    except (OSError, KeyError, ValueError, UnicodeError, zipfile.BadZipFile, ElementTree.ParseError) as exc:
        raise SkillError("CONTENT_GATE_FAILED", "无法从主 EPUB 提取可见正文。") from exc
    return baseline


def _workspace_report_details(hit_level: str, reuse_policy: str, timings: Mapping[str, float]) -> Dict[str, Any]:
    skipped = {
        "exact": ["runtime-deep-verify", "Docling/OCR", "Calibre", "EPUBCheck", "Spark"],
        "master": ["Docling/OCR", "source-to-master-generation", "Spark"],
        "semantic": ["Docling/OCR", "Spark"],
        "cold": ["Spark"],
        "disabled": ["Spark"],
    }
    return {
        "enabled": hit_level != "disabled",
        "reuse_policy": reuse_policy,
        "hit_level": hit_level,
        "skipped_tools": skipped.get(hit_level, []),
        "stage_timings_seconds": {key: round(float(value), 3) for key, value in timings.items()},
    }


def _requires_pdf_runtime(input_path: Path, hit_level: str) -> bool:
    return input_path.suffix.lower() == ".pdf" and hit_level in {"cold", "disabled"}


def _run(
    command: Sequence[str],
    *,
    env: Mapping[str, str],
    cwd: Path,
    timeout: int,
    error_code: str = "CONVERSION_FAILED",
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            offline_subprocess_command(list(command)),
            cwd=str(cwd),
            env=dict(env),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise SkillError(error_code, "转换步骤超时。", {"timeout_seconds": timeout}) from exc
    except OSError as exc:
        raise SkillError("DEPENDENCY_MISSING", "无法启动隔离运行时命令。") from exc
    if result.returncode != 0:
        executable = Path(command[0]).name if command else "unknown"
        raise SkillError(error_code, f"{executable} 执行失败。", {"return_code": result.returncode})
    return result


def _last_json(stdout: str) -> Dict[str, Any]:
    for line in reversed(stdout.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise SkillError("CONVERSION_FAILED", "后端没有返回可解析的 JSON。")


def kindle_layout_options(css: Path) -> List[str]:
    """Calibre output options shared by every PW2-targeted route."""
    return [
        "--output-profile",
        "kindle_pw",
        "--margin-top",
        "0",
        "--margin-right",
        "0",
        "--margin-bottom",
        "0",
        "--margin-left",
        "0",
        "--filter-css",
        "font-family,font-size,line-height",
        "--extra-css",
        str(css),
    ]


def kindle_metadata_options(title: str, authors: Optional[str] = None, language: Optional[str] = None) -> List[str]:
    options = ["--title", title]
    if authors:
        options.extend(("--authors", authors))
    if language:
        options.extend(("--language", language))
    return options


def kindle_master_options(
    css: Path, title: str, authors: Optional[str] = None, language: Optional[str] = None
) -> List[str]:
    """Build a two-level semantic TOC and a real start-reading target."""
    return [
        *kindle_layout_options(css),
        "--epub-version",
        "3",
        *kindle_metadata_options(title, authors, language),
        "--epub-inline-toc",
        "--use-auto-toc",
        "--max-toc-links",
        "1000",
        "--level1-toc",
        "//*[contains(concat(' ', normalize-space(@class), ' '), ' kindle-toc-level-1 ')]",
        "--level2-toc",
        "//*[contains(concat(' ', normalize-space(@class), ' '), ' kindle-toc-level-2 ')]",
        "--start-reading-at",
        "//*[contains(concat(' ', normalize-space(@class), ' '), ' kindle-start-reading ')]",
    ]


def kindle_direct_options(
    css: Path, title: str, authors: Optional[str] = None, language: Optional[str] = None
) -> List[str]:
    """Conservatively derive navigation from ordinary document headings."""
    return [
        *kindle_layout_options(css),
        "--epub-version",
        "3",
        *kindle_metadata_options(title, authors, language),
        "--epub-inline-toc",
        "--use-auto-toc",
        "--max-toc-links",
        "1000",
        "--level1-toc",
        "//h:h1",
        "--level2-toc",
        "//h:h2",
        "--start-reading-at",
        "//h:h1[1]",
    ]


def _materialize_master_start_location(master: Path) -> Dict[str, int]:
    try:
        return dict(normalize.ensure_epub_start_location(master))
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, ElementTree.ParseError) as exc:
        raise SkillError("EPUB_INVALID", "主 EPUB 无法建立可解析的开始阅读位置。") from exc


def _normalize_master_readability(master: Path) -> Dict[str, int]:
    try:
        return dict(normalize.normalize_epub_readability(master))
    except (OSError, KeyError, ValueError, UnicodeError, zipfile.BadZipFile, ElementTree.ParseError) as exc:
        raise SkillError("EPUB_INVALID", "主 EPUB 无法完成中文换行与字号规范化。") from exc


def _validate_readability(path: Path, *, error_code: str, typography: bool = True) -> Dict[str, int]:
    try:
        metrics = dict(normalize.inspect_epub_readability(path))
    except (OSError, KeyError, ValueError, UnicodeError, zipfile.BadZipFile, ElementTree.ParseError) as exc:
        raise SkillError(error_code, "无法检查中文换行与字号合同。") from exc
    checked_keys = [
        "cjk_spacing_anomalies",
        "suspicious_cjk_block_breaks",
        "prose_hard_breaks",
    ]
    if typography:
        checked_keys.extend(
            (
                "inline_typography_declarations",
                "unsafe_font_size_declarations",
                "font_family_declarations",
                "unsafe_line_height_declarations",
            )
        )
    failures = {
        key: int(metrics.get(key, 0) or 0)
        for key in checked_keys
        if int(metrics.get(key, 0) or 0)
    }
    if failures:
        raise SkillError(error_code, "中文换行或可调字号合同未通过。", failures)
    metrics["typography_contract_checked"] = 1 if typography else 0
    if not typography:
        for key in (
            "inline_typography_declarations",
            "unsafe_font_size_declarations",
            "font_family_declarations",
            "unsafe_line_height_declarations",
        ):
            metrics.pop(key, None)
    return metrics


def kindle_existing_epub_options(
    css: Path, title: str, authors: Optional[str] = None, language: Optional[str] = None
) -> List[str]:
    """Preserve an existing EPUB navigation model while normalizing layout."""
    return [
        *kindle_layout_options(css),
        "--epub-version",
        "3",
        *kindle_metadata_options(title, authors, language),
    ]


def _calibre_convert(
    paths: Mapping[str, Path],
    source: Path,
    destination: Path,
    env: Mapping[str, str],
    cwd: Path,
    timeout: int,
    extra: Sequence[str] = (),
) -> None:
    command = [str(paths["ebook_convert"]), str(source), str(destination), *extra]
    _run(command, env=env, cwd=cwd, timeout=timeout)
    if not destination.is_file() or destination.stat().st_size == 0:
        raise SkillError("CONVERSION_FAILED", "Calibre 没有生成有效输出。")


def _epubcheck(
    paths: Mapping[str, Path], master_epub: Path, report_path: Path, env: Mapping[str, str], cwd: Path, timeout: int
) -> Dict[str, int]:
    validate_epub_container(master_epub)
    command = [str(paths["java"]), "-jar", str(paths["epubcheck_jar"]), str(master_epub), "-j", str(report_path)]
    try:
        _run(command, env=env, cwd=cwd, timeout=timeout, error_code="EPUB_INVALID")
    except SkillError as exc:
        if not report_path.is_file():
            raise
        counts = parse_epubcheck_report(report_path)
        try:
            payload = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise exc
        identifiers = []
        diagnostics = []
        for message in payload.get("messages", []) if isinstance(payload, dict) else []:
            if not isinstance(message, dict):
                continue
            identifier = str(message.get("ID") or message.get("id") or "UNKNOWN")
            if identifier not in identifiers:
                identifiers.append(identifier)
            locations = message.get("locations", [])
            location = locations[0] if isinstance(locations, list) and locations and isinstance(locations[0], dict) else {}
            diagnostics.append(
                {
                    "id": identifier,
                    "file": Path(str(location.get("path", "document"))).name,
                    "line": int(location.get("line", 0) or 0),
                    "column": int(location.get("column", 0) or 0),
                    "message": str(message.get("message", "EPUB validation error"))[:240],
                }
            )
        raise SkillError(
            "EPUB_INVALID",
            "EPUBCheck 发现规范错误；成品未发布。",
            {**counts, "message_ids": identifiers[:12], "diagnostics": diagnostics[:6]},
        ) from exc
    counts = parse_epubcheck_report(report_path)
    if any(counts.values()):
        raise SkillError("EPUB_INVALID", "EPUBCheck 报告不是 0 fatal / 0 error / 0 warning。", counts)
    return counts


def resolve_publication_paths(
    *,
    input_path: Path,
    output_dir: Path,
    formats: Sequence[str],
    report_override: Optional[Path],
) -> Tuple[Dict[str, Path], Path]:
    output_dir = output_dir.expanduser().resolve()
    names = unique_output_names(input_path.stem, formats)
    destinations = {fmt: output_dir / name for fmt, name in names.items()}
    if report_override is None:
        report_path = output_dir / f"{input_path.stem}.ebook-to-kindle.json"
    else:
        candidate = report_override.expanduser()
        if not candidate.is_absolute():
            candidate = output_dir / candidate
        report_path = Path(os.path.abspath(os.fspath(candidate)))
    targets = [*destinations.values(), report_path]
    if any(path.parent != output_dir for path in targets):
        raise SkillError("UNSUPPORTED_OUTPUT", "成品和报告必须直接位于 --output-dir。")
    if any(path.is_symlink() for path in targets):
        raise SkillError("UNSUPPORTED_OUTPUT", "成品或报告路径不能是符号链接。")
    if any(path.exists() and not path.is_file() for path in targets):
        raise SkillError("UNSUPPORTED_OUTPUT", "成品或报告路径只能覆盖普通文件。")
    if any(path.exists() and path.stat().st_nlink != 1 for path in targets):
        raise SkillError("UNSUPPORTED_OUTPUT", "成品或报告路径不能是硬链接。")
    if report_path.parent != output_dir:
        raise SkillError("UNSUPPORTED_OUTPUT", "报告文件必须直接位于 --output-dir。")
    portable_keys = [unicodedata.normalize("NFC", path.name).casefold() for path in targets]
    if len(set(portable_keys)) != len(portable_keys):
        raise SkillError("UNSUPPORTED_OUTPUT", "报告与电子书成品名称在大小写或 Unicode 归一化后冲突。")
    input_parent = input_path.parent.resolve()
    input_key = unicodedata.normalize("NFC", input_path.name).casefold()
    if input_parent == output_dir and input_key in portable_keys:
        raise SkillError("UNSUPPORTED_OUTPUT", "输出成品或报告不能覆盖输入文件。")
    existing_paths = [path for path in [input_path, *targets] if path.exists()]
    for index, left in enumerate(existing_paths):
        for right in existing_paths[index + 1 :]:
            try:
                aliases = left.samefile(right)
            except OSError:
                aliases = False
            if aliases:
                raise SkillError("UNSUPPORTED_OUTPUT", "输入、成品或报告不能引用同一现有文件。")
    return destinations, report_path


def _portable_path_key(path: Path) -> str:
    value = Path(os.path.abspath(os.fspath(path.expanduser())))
    return unicodedata.normalize("NFC", str(value)).casefold().rstrip(os.sep)


def _inside_runtime_alias(path: Path, runtime_root: Path) -> bool:
    """Reject lexical, case/Unicode, resolved-symlink, and inode aliases."""
    candidate = Path(os.path.abspath(os.fspath(path.expanduser())))
    root = Path(os.path.abspath(os.fspath(runtime_root.expanduser())))
    candidate_key = _portable_path_key(candidate)
    root_key = _portable_path_key(root)
    if candidate_key == root_key or candidate_key.startswith(root_key + os.sep):
        return True
    try:
        resolved = candidate.resolve(strict=False)
    except OSError:
        resolved = candidate
    resolved_key = _portable_path_key(resolved)
    if resolved_key == root_key or resolved_key.startswith(root_key + os.sep):
        return True
    cursor = candidate if candidate.exists() else candidate.parent
    while True:
        if cursor.exists():
            try:
                resolved_cursor = cursor.resolve(strict=True)
                resolved_root = root.resolve(strict=True)
                if resolved_cursor == resolved_root or resolved_root in resolved_cursor.parents:
                    return True
            except OSError:
                pass
        if cursor == cursor.parent:
            return False
        cursor = cursor.parent


def reject_runtime_publication_paths(
    output_dir: Path,
    targets: Sequence[Path],
    runtime_root: Path,
) -> None:
    """The private runtime tree can never be used as a publication surface."""
    if _inside_runtime_alias(output_dir, runtime_root) or any(
        _inside_runtime_alias(target, runtime_root) for target in targets
    ):
        raise SkillError("UNSUPPORTED_OUTPUT", "输出目录、成品和报告不能位于私有运行时目录内。")
    protected = [runtime_root / name for name in ("runtime.json", ".operation.lock", "jobs", "core", "pdf", "packages")]
    for target in targets:
        if not target.exists():
            continue
        for runtime_path in protected:
            if not runtime_path.exists():
                continue
            try:
                if os.path.samefile(target, runtime_path):
                    raise SkillError("UNSUPPORTED_OUTPUT", "输出成品或报告不能引用运行时条目。")
            except OSError:
                continue


def _stage_copy(source: Path, destination: Path) -> Path:
    fd, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.stage-", dir=str(destination.parent))
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        shutil.copy2(source, temporary)
        os.chmod(temporary, 0o600)
        return temporary
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise


def _entry_stat(directory_fd: int, name: str) -> Optional[os.stat_result]:
    try:
        return os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _unlink_at(directory_fd: int, name: str) -> None:
    os.unlink(name, dir_fd=directory_fd)


def _create_stage_at(directory_fd: int, prefix: str, source: Optional[Path] = None) -> str:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    for _ in range(32):
        name = f".{prefix}.stage-{uuid.uuid4().hex}"
        try:
            descriptor = os.open(name, flags, 0o600, dir_fd=directory_fd)
        except FileExistsError:
            continue
        try:
            if source is not None:
                with source.open("rb") as source_handle, os.fdopen(descriptor, "wb", closefd=False) as target_handle:
                    shutil.copyfileobj(source_handle, target_handle)
                    target_handle.flush()
                    os.fsync(target_handle.fileno())
            os.fchmod(descriptor, 0o600)
            return name
        except Exception:
            try:
                _unlink_at(directory_fd, name)
            except OSError:
                pass
            raise
        finally:
            try:
                os.close(descriptor)
            except OSError:
                pass
    raise SkillError("CONVERSION_FAILED", "无法创建唯一的成品暂存文件。")


def _write_json_entry(directory_fd: int, name: str, payload: Mapping[str, Any]) -> None:
    flags = os.O_WRONLY | os.O_TRUNC | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(name, flags, dir_fd=directory_fd)
    try:
        encoded = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.fchmod(descriptor, 0o600)
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass


def _atomic_json_entry(directory_fd: int, destination_name: str, payload: Mapping[str, Any]) -> None:
    stage_name = _create_stage_at(directory_fd, destination_name)
    try:
        _write_json_entry(directory_fd, stage_name, payload)
        os.replace(stage_name, destination_name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
    finally:
        try:
            if _entry_stat(directory_fd, stage_name) is not None:
                _unlink_at(directory_fd, stage_name)
        except OSError:
            # This helper only finalizes an already committed conservative
            # report. Post-commit cleanup cannot reverse the durable truth.
            pass


def _open_publication_anchor(output_dir: Path) -> Dict[str, Any]:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(output_dir, flags)
    except OSError as exc:
        raise SkillError("UNSUPPORTED_OUTPUT", "无法安全打开成品输出目录。") from exc
    metadata = os.fstat(descriptor)
    return {
        "fd": descriptor,
        "path": output_dir,
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
    }


def _close_publication_anchor(anchor: Mapping[str, Any]) -> None:
    descriptor = int(anchor["fd"])
    if descriptor < 0:
        return
    try:
        os.close(descriptor)
    except OSError:
        pass
    if isinstance(anchor, dict):
        anchor["fd"] = -1


def _publish_transaction(
    staged_books: Mapping[Path, Path],
    report_path: Path,
    report: Mapping[str, Any],
    overwrite: bool,
    before_commit: Optional[Callable[[], None]] = None,
    directory_anchor: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    publications: List[Tuple[str, str]] = []
    backups: Dict[str, str] = {}
    replaced: List[str] = []
    linked: List[Tuple[str, str]] = []
    targets = [*staged_books.keys(), report_path]
    pending_report: Optional[Dict[str, Any]] = None
    output_dir = report_path.parent
    if any(path.parent != output_dir for path in targets):
        raise SkillError("UNSUPPORTED_OUTPUT", "成品与报告必须位于同一直接输出目录。")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    if directory_anchor is None:
        try:
            directory_fd = os.open(output_dir, flags)
        except OSError as exc:
            raise SkillError("UNSUPPORTED_OUTPUT", "无法安全打开成品输出目录。") from exc
        anchor_device, anchor_inode = os.fstat(directory_fd).st_dev, os.fstat(directory_fd).st_ino
    else:
        try:
            directory_fd = os.dup(int(directory_anchor["fd"]))
        except OSError as exc:
            raise SkillError("ISOLATION_FAILED", "转换期间输出目录锦定失效。") from exc
        anchor_device = int(directory_anchor["device"])
        anchor_inode = int(directory_anchor["inode"])
        duplicated = os.fstat(directory_fd)
        if (duplicated.st_dev, duplicated.st_ino) != (anchor_device, anchor_inode):
            try:
                os.close(directory_fd)
            except OSError:
                pass
            raise SkillError("ISOLATION_FAILED", "转换期间输出目录锦定失效。")

    def assert_anchor() -> None:
        try:
            current = os.stat(output_dir, follow_symlinks=False)
        except OSError as exc:
            raise SkillError("ISOLATION_FAILED", "发布期间输出目录身份发生变化。") from exc
        if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != (anchor_device, anchor_inode):
            raise SkillError("ISOLATION_FAILED", "发布期间输出目录身份发生变化。")

    def validate_targets() -> None:
        assert_anchor()
        invalid = []
        for path in targets:
            metadata = _entry_stat(directory_fd, path.name)
            if metadata is not None and (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1):
                invalid.append(path)
        if invalid:
            raise SkillError(
                "UNSUPPORTED_OUTPUT",
                "成品或报告路径只能覆盖单链接的普通文件。",
                {"files": [path.name for path in invalid]},
            )

    def report_with_cleanup(status: str, files: Sequence[str]) -> Dict[str, Any]:
        value = deepcopy(dict(report))
        details = value.get("details")
        if not isinstance(details, dict):
            details = {}
            value["details"] = details
        details["publication_cleanup"] = {
            "status": status,
            "pending_files": sorted(set(files)),
        }
        return value

    try:
        validate_targets()
        for destination, source in staged_books.items():
            assert_anchor()
            publications.append((_create_stage_at(directory_fd, destination.name, source), destination.name))
            assert_anchor()
        report_stage = _create_stage_at(directory_fd, report_path.name)
        publications.append((report_stage, report_path.name))
        validate_targets()
        collisions = [destination for _, destination in publications if _entry_stat(directory_fd, destination) is not None]
        if collisions and not overwrite:
            raise SkillError("OUTPUT_EXISTS", "输出或报告已存在；如需替换请显式使用 --overwrite。", {"files": collisions})
        if before_commit is not None:
            before_commit()
        validate_targets()
        if not overwrite:
            pending_report = report_with_cleanup("pending", [staged for staged, _ in publications])
            _write_json_entry(directory_fd, report_stage, pending_report)
            for staged, destination in publications:
                assert_anchor()
                try:
                    os.link(
                        staged,
                        destination,
                        src_dir_fd=directory_fd,
                        dst_dir_fd=directory_fd,
                        follow_symlinks=False,
                    )
                except FileExistsError as exc:
                    raise SkillError(
                        "OUTPUT_EXISTS",
                        "输出或报告已存在；如需替换请显式使用 --overwrite。",
                        {"files": [destination]},
                    ) from exc
                linked.append((staged, destination))
        else:
            for _, destination in publications:
                assert_anchor()
                destination_metadata = _entry_stat(directory_fd, destination)
                if destination_metadata is None:
                    continue
                if not stat.S_ISREG(destination_metadata.st_mode) or destination_metadata.st_nlink != 1:
                    raise SkillError("UNSUPPORTED_OUTPUT", "覆盖目标在提交前变成了非单链接普通文件。")
                backup = f".{destination}.rollback-{uuid.uuid4().hex}"
                os.replace(destination, backup, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
                backups[destination] = backup
                backup_metadata = _entry_stat(directory_fd, backup)
                if backup_metadata is None or not stat.S_ISREG(backup_metadata.st_mode) or backup_metadata.st_nlink != 1:
                    raise SkillError("UNSUPPORTED_OUTPUT", "覆盖目标在提交时类型发生变化。")
            pending_report = report_with_cleanup("pending", list(backups.values()))
            _write_json_entry(directory_fd, report_stage, pending_report)
            for staged, destination in publications:
                assert_anchor()
                os.replace(staged, destination, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
                replaced.append(destination)
        assert_anchor()
    except Exception as exc:
        rollback_pending: List[str] = []
        for staged, destination in reversed(linked):
            try:
                staged_stat = _entry_stat(directory_fd, staged)
                destination_stat = _entry_stat(directory_fd, destination)
                if staged_stat is not None and destination_stat is not None and os.path.samestat(staged_stat, destination_stat):
                    _unlink_at(directory_fd, destination)
            except OSError:
                rollback_pending.append(destination)
        for destination in reversed(replaced):
            try:
                if _entry_stat(directory_fd, destination) is not None:
                    _unlink_at(directory_fd, destination)
            except OSError:
                rollback_pending.append(destination)
        for destination, backup in backups.items():
            try:
                backup_metadata = _entry_stat(directory_fd, backup)
                if backup_metadata is None:
                    continue
                if _entry_stat(directory_fd, destination) is not None:
                    rollback_pending.append(backup)
                    continue
                os.replace(backup, destination, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            except OSError:
                # A backup probe can fail after the destructive rename has
                # already succeeded. Preserve its basename for manual recovery
                # and continue restoring every other destination.
                rollback_pending.append(backup)
        cleanup_pending: List[str] = []
        for staged, _ in publications:
            try:
                if _entry_stat(directory_fd, staged) is not None:
                    _unlink_at(directory_fd, staged)
            except OSError:
                cleanup_pending.append(staged)
        if rollback_pending or cleanup_pending:
            try:
                os.close(directory_fd)
            except OSError:
                pass
            raise SkillError(
                "ISOLATION_FAILED",
                "发布事务失败；未恢复的旧文件或暂存文件已保留，未被删除。",
                {
                    "rollback_pending": sorted(set(rollback_pending)),
                    "cleanup_pending": sorted(set(cleanup_pending)),
                },
            ) from exc
        try:
            os.close(directory_fd)
        except OSError:
            pass
        if isinstance(exc, SkillError):
            raise
        raise SkillError("CONVERSION_FAILED", "发布事务失败，原文件已回滚。") from exc
    cleanup_pending: List[str] = []
    for staged, _ in publications:
        try:
            if _entry_stat(directory_fd, staged) is not None:
                _unlink_at(directory_fd, staged)
        except OSError:
            cleanup_pending.append(staged)
    for backup in backups.values():
        try:
            if _entry_stat(directory_fd, backup) is not None:
                _unlink_at(directory_fd, backup)
        except OSError:
            cleanup_pending.append(backup)
    final_report = (
        report_with_cleanup("pending", cleanup_pending)
        if cleanup_pending
        else report_with_cleanup("complete", [])
    )
    if pending_report != final_report:
        try:
            assert_anchor()
            _atomic_json_entry(directory_fd, report_path.name, final_report)
        except Exception:
            # The committed report already contains a conservative pending
            # state. Return that exact persisted state instead of claiming a
            # clean finish that could not be durably recorded.
            assert pending_report is not None
            try:
                os.close(directory_fd)
            except OSError:
                pass
            return pending_report
    try:
        assert_anchor()
    except SkillError:
        # All writes were anchored to the original directory descriptor, so a
        # swapped pathname cannot redirect output into runtime or input trees.
        pass
    try:
        os.close(directory_fd)
    except OSError:
        pass
    return final_report


def _remove_job_strict(job: Path) -> None:
    try:
        shutil.rmtree(job)
    except OSError as exc:
        raise SkillError("ISOLATION_FAILED", "临时原文目录清理失败；成品未发布。") from exc
    if job.exists():
        raise SkillError("ISOLATION_FAILED", "临时原文目录仍然存在；成品未发布。")


def _validate_pdf_semantic_metrics(semantic_metrics: Mapping[str, Any]) -> None:
    outline_entries = int(semantic_metrics.get("outline_entries", 0) or 0)
    mapped_outline_entries = int(semantic_metrics.get("mapped_outline_entries", 0) or 0)
    if outline_entries < 1 or mapped_outline_entries != outline_entries:
        raise SkillError(
            "EPUB_INVALID",
            "PDF 目录语义未能完整映射到正文锚点。",
            {
                "outline_entries": outline_entries,
                "mapped_outline_entries": mapped_outline_entries,
                "unmapped_outline_entries": int(semantic_metrics.get("unmapped_outline_entries", 0) or 0),
            },
        )
    remaining_printed_toc_tables = int(semantic_metrics.get("remaining_printed_toc_tables", 0) or 0)
    if remaining_printed_toc_tables:
        raise SkillError(
            "EPUB_INVALID",
            "PDF 仍包含被误识别为正文表格的印刷目录。",
            {"remaining_printed_toc_tables": remaining_printed_toc_tables},
        )
    unresolved_noteref_markers = int(semantic_metrics.get("unresolved_noteref_markers", 0) or 0)
    if unresolved_noteref_markers:
        raise SkillError(
            "EPUB_INVALID",
            "PDF 正文仍包含存在对应定义但未生成链接的注释编号。",
            {"unresolved_noteref_markers": unresolved_noteref_markers},
        )
    source_reference_mismatches = int(semantic_metrics.get("source_reference_mismatches", 0) or 0)
    if source_reference_mismatches:
        raise SkillError(
            "EPUB_INVALID",
            "PDF 源正文注释编号与 Kindle 链接未能逐编号完整对应。",
            {"source_reference_mismatches": source_reference_mismatches},
        )


def _build_pdf_master_from_export(
    *,
    input_path: Path,
    export_dir: Path,
    probe_payload: Mapping[str, Any],
    semantic_metrics: Mapping[str, Any],
    mode: str,
    master: Path,
    paths: Mapping[str, Path],
    env: Mapping[str, str],
    request: ConversionRequest,
    skill_root: Path,
) -> Tuple[Path, str, Dict[str, Any], str]:
    _validate_pdf_semantic_metrics(semantic_metrics)
    source_html = export_dir / "document.html"
    semantic_html = export_dir / "semantic.html"
    source_json = export_dir / "document.json"
    if not source_html.is_file() or not semantic_html.is_file() or not source_json.is_file():
        raise SkillError("CONVERSION_FAILED", "Docling 未生成 HTML/JSON/语义中间件。")
    try:
        document = json.loads(source_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SkillError("CONVERSION_FAILED", "Docling JSON 无法解析。") from exc
    source_text = normalize.html_text(source_html.read_text(encoding="utf-8"))
    source_quality = validate_source_text(source_text)
    source_resources = inspect_source_resources(source_html)
    page_count = int(probe_payload.get("page_count", 1))
    page_minimums = [15] * max(1, page_count)
    for raw_page in probe_payload.get("blank_pages", []):
        try:
            page_number = int(raw_page)
        except (TypeError, ValueError):
            continue
        if 1 <= page_number <= page_count:
            page_minimums[page_number - 1] = 0
    sparse_pages = probe_payload.get("sparse_text_pages", {})
    if isinstance(sparse_pages, Mapping):
        for raw_page, raw_count in sparse_pages.items():
            try:
                page_number = int(raw_page)
                character_count = int(raw_count)
            except (TypeError, ValueError):
                continue
            if 1 <= page_number <= page_count and page_minimums[page_number - 1] != 0:
                page_minimums[page_number - 1] = min(15, max(1, character_count))
    ocr_quality = (
        validate_ocr_text(
            source_text,
            page_count,
            docling_page_character_counts(document, page_count),
            page_minimums=page_minimums,
        )
        if mode == "scan"
        else None
    )
    furniture = normalize.select_unique_furniture(normalize.furniture_from_docling(document), source_text)
    normalized_html = export_dir / "normalized.html"
    normalize.normalize_file(semantic_html, normalized_html, furniture)
    metadata = _conversion_metadata(request, input_path)
    _calibre_convert(
        paths,
        normalized_html,
        master,
        env,
        export_dir,
        request.timeout_seconds,
        kindle_master_options(
            skill_root / "assets" / "pw2.css",
            str(metadata["title"]),
            metadata["authors"],
            metadata["language"],
        ),
    )
    readability_normalization = _normalize_master_readability(master)
    start_location = _materialize_master_start_location(master)
    route = "docling-digital-pdf" if mode == "digital" else "docling-scanned-pdf-ocrmac"
    details = {
        "pdf_mode": mode,
        "probe": dict(probe_payload),
        "source_quality": source_quality,
        "source_resources": source_resources,
        "recovered_furniture_count": len(furniture),
        "semantic_source": dict(semantic_metrics),
        "readability_normalization": readability_normalization,
        "start_location": start_location,
    }
    if ocr_quality is not None:
        details["ocr_quality"] = ocr_quality
    return master, route, details, source_text


def _build_master_epub(
    *,
    input_path: Path,
    job: Path,
    paths: Mapping[str, Path],
    env: Mapping[str, str],
    request: ConversionRequest,
    runtime_root: Path,
    skill_root: Path,
) -> Tuple[Path, str, Dict[str, Any], str]:
    master = job / "master.epub"
    suffix = input_path.suffix.lower()
    css = skill_root / "assets" / "pw2.css"
    metadata = _conversion_metadata(request, input_path)
    if suffix == ".epub":
        try:
            source_resources = inspect_source_resources(input_path)
            source_body = normalize.source_body_text(input_path)
            source_quality = validate_source_text(source_body)
            source_semantics = inspect_epub_structure(input_path, require_toc=False)
        except (OSError, KeyError, ValueError, UnicodeError, zipfile.BadZipFile, ElementTree.ParseError) as exc:
            raise SkillError("CONTENT_GATE_FAILED", "无法从 EPUB 提取真实正文。") from exc
        options = (
            kindle_existing_epub_options(css, str(metadata["title"]), metadata["authors"], metadata["language"])
            if int(source_semantics.get("toc_entries", 0) or 0) > 0
            else kindle_direct_options(css, str(metadata["title"]), metadata["authors"], metadata["language"])
        )
        _calibre_convert(paths, input_path, master, env, job, request.timeout_seconds, options)
        readability_normalization = _normalize_master_readability(master)
        start_location = _materialize_master_start_location(master)
        semantic_source = {
            "outline_entries": max(1, int(source_semantics.get("toc_entries", 0) or 0)),
            "synthesized_noterefs": int(source_semantics.get("noteref_count", 0) or 0),
            "synthesized_footnotes": int(source_semantics.get("footnote_count", 0) or 0),
        }
        return master, "epub-normalized", {
            "source_quality": source_quality,
            "source_resources": source_resources,
            "source_epub_structure": source_semantics,
            "semantic_source": semantic_source,
            "readability_normalization": readability_normalization,
            "start_location": start_location,
        }, source_body
    if suffix != ".pdf":
        try:
            source_body = normalize.source_body_text(input_path)
            source_quality = validate_source_text(source_body)
            source_resources = inspect_source_resources(input_path)
            source_semantics = inspect_source_semantics(input_path)
        except (OSError, KeyError, ValueError, UnicodeError, zipfile.BadZipFile, ElementTree.ParseError) as exc:
            raise SkillError("CONTENT_GATE_FAILED", "无法从源文档提取真实正文。") from exc
        calibre_source = input_path
        direct_semantics: Dict[str, Any] = {}
        if suffix == ".txt":
            calibre_source = job / "plain-text-semantic.html"
            direct_semantics = dict(
                normalize.plain_text_to_semantic_html(input_path, calibre_source, str(metadata["title"]))
            )
        _calibre_convert(
            paths,
            calibre_source,
            master,
            env,
            job,
            request.timeout_seconds,
            kindle_direct_options(css, str(metadata["title"]), metadata["authors"], metadata["language"]),
        )
        readability_normalization = _normalize_master_readability(master)
        start_location = _materialize_master_start_location(master)
        return master, "calibre-direct", {
            "source_quality": source_quality,
            "source_resources": source_resources,
            "semantic_source": source_semantics,
            "readability_normalization": readability_normalization,
            **({"direct_semantic_normalization": direct_semantics} if direct_semantics else {}),
            "start_location": start_location,
        }, source_body

    if not readiness(runtime_root)["pdf_ready"]:
        raise SkillError("DEPENDENCY_MISSING", "PDF 后端尚未安装；先运行 bootstrap_runtime.py --component pdf。")
    backend = skill_root / "scripts" / "pdf_backend.py"
    pdf_probe_started = time.monotonic()
    probe = _run(
        [str(paths["pdf_python"]), "-I", str(backend), "probe", str(input_path)],
        env=env,
        cwd=job,
        timeout=min(request.timeout_seconds, 180),
    )
    pdf_probe_seconds = time.monotonic() - pdf_probe_started
    probe_payload = _last_json(probe.stdout)
    mode = choose_pdf_mode(probe_payload, request.pdf_mode)
    export_dir = job / "docling"
    pdf_backend_started = time.monotonic()
    converted = _run(
        [
            str(paths["pdf_python"]),
            "-I",
            str(backend),
            "convert",
            str(input_path),
            "--output-dir",
            str(export_dir),
            "--artifacts",
            str(paths["models"]),
            "--mode",
            mode,
        ],
        env=env,
        cwd=job,
        timeout=request.timeout_seconds,
        error_code="OCR_QUALITY_FAILED" if mode == "scan" else "CONVERSION_FAILED",
    )
    pdf_backend_seconds = time.monotonic() - pdf_backend_started
    converted_payload = _last_json(converted.stdout)
    semantic_metrics = converted_payload.get("semantic_metrics", {})
    master, route, details, source_text = _build_pdf_master_from_export(
        input_path=input_path,
        export_dir=export_dir,
        probe_payload=probe_payload,
        semantic_metrics=semantic_metrics,
        mode=mode,
        master=master,
        paths=paths,
        env=env,
        request=request,
        skill_root=skill_root,
    )
    details["_workspace_semantic_export"] = export_dir
    details["pdf_stage_timings_seconds"] = {
        "input_probe": round(pdf_probe_seconds, 3),
        "docling_backend": round(pdf_backend_seconds, 3),
    }
    details["pdf_backend_stage_timings_seconds"] = converted_payload.get("stage_timings_seconds", {})
    return master, route, details, source_text


def convert(request: ConversionRequest, skill_root: Path) -> Dict[str, Any]:
    runtime_root = guard_runtime_root(request.runtime_root)
    with runtime_operation_lock(runtime_root):
        return _convert_locked(request, skill_root)


def _convert_locked(request: ConversionRequest, skill_root: Path) -> Dict[str, Any]:
    if request.reuse_policy not in {"auto", "refresh", "off"}:
        raise SkillError("WORKSPACE_INVALID", "--reuse 仅支持 auto、refresh 或 off。")
    if request.workspace_root is None and request.reuse_policy not in {"auto", "off"}:
        raise SkillError("WORKSPACE_INVALID", "--reuse refresh 需要显式 --workspace。")
    store = None
    if request.workspace_root is not None and request.reuse_policy != "off":
        store = BookWorkspace.open(request.workspace_root)
    if store is not None:
        with store.locked():
            return _convert_with_workspace_locked(request, skill_root, store)
    return _convert_with_workspace_locked(request, skill_root, None)


def _convert_with_workspace_locked(
    request: ConversionRequest,
    skill_root: Path,
    store: Optional[BookWorkspace],
) -> Dict[str, Any]:
    started = time.monotonic()
    timings: Dict[str, float] = {}
    calibre_state_before = snapshot_calibre_user_state()
    stage_started = time.monotonic()
    input_path = validate_local_input(request.input_value)
    runtime_root = guard_runtime_root(request.runtime_root)
    lexical_output_dir = Path(os.path.abspath(os.fspath(request.output_dir.expanduser())))
    if _inside_runtime_alias(lexical_output_dir, runtime_root):
        raise SkillError("UNSUPPORTED_OUTPUT", "输出目录不能位于私有运行时目录内。")
    output_dir = lexical_output_dir.resolve()
    destinations, report_path = resolve_publication_paths(
        input_path=input_path,
        output_dir=output_dir,
        formats=request.formats,
        report_override=request.report_path,
    )
    reject_runtime_publication_paths(output_dir, [*destinations.values(), report_path], runtime_root)
    if store is not None:
        store.validate_boundaries(
            input_path=input_path,
            output_dir=output_dir,
            runtime_root=runtime_root,
            skill_root=skill_root,
        )
    if not request.overwrite:
        collisions = [path.name for path in [*destinations.values(), report_path] if path.exists()]
        if collisions:
            raise SkillError("OUTPUT_EXISTS", "输出或报告已存在；如需替换请显式使用 --overwrite。", {"files": collisions})
    timings["preflight"] = time.monotonic() - stage_started

    plan: Optional[Dict[str, Any]] = None
    lookup: Dict[str, Any] = {"hit_level": "disabled" if store is None else "cold"}
    if store is not None:
        stage_started = time.monotonic()
        plan = _workspace_cache_plan(
            input_path=input_path,
            request=request,
            runtime_root=runtime_root,
            skill_root=skill_root,
        )
        lookup = _workspace_lookup(store, plan, request.formats, request.reuse_policy)
        timings["workspace_lookup"] = time.monotonic() - stage_started

    if not output_dir.exists():
        ensure_private_dir(output_dir)
    elif not output_dir.is_dir():
        raise SkillError("UNSUPPORTED_OUTPUT", "输出位置不是目录。")
    publication_anchor = _open_publication_anchor(output_dir)

    if lookup["hit_level"] == "exact":
        try:
            finals = lookup["finals"]
            first_node = finals[request.formats[0]]["node"]
            route_details = deepcopy(first_node.get("route_details", {}))
            quality: Dict[str, Any] = {
                "epubcheck": deepcopy(first_node.get("epubcheck", {})),
                "epub_structure": deepcopy(first_node.get("epub_structure", {})),
            }
            staged: Dict[str, Path] = {}
            for fmt in request.formats:
                node = finals[fmt]["node"]
                if node.get("route") != first_node.get("route") or node.get("route_details") != first_node.get("route_details"):
                    raise SkillError("WORKSPACE_INVALID", "同一精确命中的格式证据不一致。")
                quality[fmt] = deepcopy(node["quality"])
                staged[fmt] = finals[fmt]["artifact"]
            elapsed = round(time.monotonic() - started, 3)
            timings["total_before_publication"] = time.monotonic() - started
            workspace_details = _workspace_report_details("exact", request.reuse_policy, timings)
            performance_review = build_performance_review(
                route=str(first_node.get("route", "workspace-exact-hit")),
                elapsed_seconds=elapsed,
                workspace=workspace_details,
                route_details=route_details,
            )
            report = safe_report(
                status="PASS",
                input_path=input_path,
                output_dir=output_dir,
                outputs=[destinations[fmt] for fmt in request.formats],
                route=str(first_node.get("route", "workspace-exact-hit")),
                details={
                    **route_details,
                    "formats": list(request.formats),
                    "quality": quality,
                    "workspace": workspace_details,
                    "performance_review": performance_review,
                    "runtime_verification": "skipped-exact-hit",
                    "elapsed_seconds": elapsed,
                    "calibre_user_state_unchanged": True,
                    "paperwhite2_manual_check_required": bool(first_node.get("paperwhite2_manual_check_required", True)),
                },
            )

            def exact_before_commit() -> None:
                assert store is not None
                assert plan is not None
                store._validate_tree()
                if sha256_file(input_path) != plan["source_sha256"]:
                    raise SkillError("ISOLATION_FAILED", "复用期间输入文件内容发生变化；成品未发布。")
                if snapshot_calibre_user_state() != calibre_state_before:
                    raise SkillError("ISOLATION_FAILED", "Calibre 用户状态发生变化；成品未发布。")

            return _publish_transaction(
                {destinations[fmt]: source for fmt, source in staged.items()},
                report_path,
                report,
                request.overwrite,
                before_commit=exact_before_commit,
                directory_anchor=publication_anchor,
            )
        finally:
            _close_publication_anchor(publication_anchor)

    require_pdf = _requires_pdf_runtime(input_path, str(lookup["hit_level"]))
    stage_started = time.monotonic()
    verified = verify_runtime(runtime_root, skill_root, require_pdf=require_pdf)
    timings["runtime_verification"] = time.monotonic() - stage_started
    if snapshot_calibre_user_state() != calibre_state_before:
        _close_publication_anchor(publication_anchor)
        raise SkillError("ISOLATION_FAILED", "运行时核验改写了 Calibre 用户状态；转换未启动。")
    jobs_hygiene = verified.get("deep_checks", {}).get("jobs_hygiene", {})
    if not jobs_hygiene.get("ok", False):
        _close_publication_anchor(publication_anchor)
        raise SkillError("ISOLATION_FAILED", "运行时临时目录卫生检查失败；转换未启动。")
    if not verified["core_verified"]:
        _close_publication_anchor(publication_anchor)
        raise SkillError("DEPENDENCY_MISSING", "核心运行时未通过版本、完整性、容量或临时目录核验。")
    if require_pdf and not verified["pdf_verified"]:
        _close_publication_anchor(publication_anchor)
        raise SkillError("DEPENDENCY_MISSING", "PDF 运行时未通过版本、锁文件或模型哈希核验。")
    paths = runtime_paths(runtime_root)
    try:
        ensure_private_dir(paths["jobs"])
        job = make_private_job_dir(paths["jobs"])
    except Exception:
        _close_publication_anchor(publication_anchor)
        raise
    publication_committed = False
    try:
        env = build_job_env(job, runtime_root)
    except Exception as exc:
        try:
            _remove_job_strict(job)
        except SkillError as cleanup_exc:
            raise cleanup_exc from exc
        finally:
            _close_publication_anchor(publication_anchor)
        raise
    try:
        stage_started = time.monotonic()
        baseline: Optional[str] = None
        if lookup["hit_level"] == "master":
            assert store is not None
            master_node = lookup["master"]
            master = _copy_workspace_object(store, master_node["master"], job / "master.epub")
            baseline_path = _copy_workspace_object(store, master_node["baseline"], job / "baseline.txt")
            source_body_path = store.read_object(master_node["source_body"])
            try:
                source_body = source_body_path.read_text(encoding="utf-8")
                baseline = baseline_path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                raise SkillError("WORKSPACE_INVALID", "主 EPUB 缓存正文无法解码。") from exc
            route = str(master_node["route"])
            route_details = deepcopy(master_node["route_details"])
        elif lookup["hit_level"] == "semantic":
            assert store is not None
            semantic_node = lookup["semantic"]
            export_dir = job / "docling"
            store.restore_tree(semantic_node["tree"], export_dir)
            master, route, route_details, source_body = _build_pdf_master_from_export(
                input_path=input_path,
                export_dir=export_dir,
                probe_payload=semantic_node["probe"],
                semantic_metrics=semantic_node["semantic_metrics"],
                mode=str(semantic_node["pdf_mode"]),
                master=job / "master.epub",
                paths=paths,
                env=env,
                request=request,
                skill_root=skill_root,
            )
            route_details["_workspace_semantic_export"] = export_dir
        else:
            master, route, route_details, source_body = _build_master_epub(
                input_path=input_path,
                job=job,
                paths=paths,
                env=env,
                request=request,
                runtime_root=runtime_root,
                skill_root=skill_root,
            )
        timings["master_materialization"] = time.monotonic() - stage_started

        semantic_export = route_details.pop("_workspace_semantic_export", None)
        stage_started = time.monotonic()
        epub_counts = _epubcheck(paths, master, job / "epubcheck.json", env, job, request.timeout_seconds)
        epub_structure = inspect_epub_structure(master)
        route_details["master_readability"] = _validate_readability(master, error_code="EPUB_INVALID")
        semantic_expectations = route_details.get("semantic_source") or epub_structure
        route_details["source_to_master_semantics"] = validate_semantic_retention(
            semantic_expectations or epub_structure,
            epub_structure,
            require_ncx=False,
        )
        route_details["source_to_master_resources"] = validate_image_retention(
            route_details.get("source_resources", {}), epub_structure
        )
        if baseline is None:
            baseline_path = job / "baseline.txt"
            baseline = _extract_master_baseline(master, baseline_path)
        else:
            baseline_path = job / "baseline.txt"
        route_details["master_text_quality"] = validate_source_text(baseline, (input_path.stem,))
        try:
            route_details["source_to_master"] = compare_content(
                source_body,
                baseline,
                min_length_ratio=0.90,
                min_marker_ratio=0.90,
                min_ordered_marker_ratio=0.90,
                max_length_ratio=1.6,
                max_marker_occurrence_ratio=1.6,
            )
        except SkillError as exc:
            raise SkillError("CONTENT_GATE_FAILED", "源正文到主 EPUB 的内容或顺序保留率未通过门槛。", exc.details) from exc
        timings["master_validation"] = time.monotonic() - stage_started

        stage_started = time.monotonic()
        staged: Dict[str, Path] = {}
        quality: Dict[str, Any] = {"epubcheck": epub_counts, "epub_structure": epub_structure}
        css = skill_root / "assets" / "pw2.css"
        if "epub" in request.formats:
            staged["epub"] = master
            quality["epub"] = {"sha256": sha256_file(master), "bytes": master.stat().st_size}
        for fmt in ("azw3", "mobi"):
            if fmt not in request.formats:
                continue
            target = job / f"book.{fmt}"
            options: List[str] = kindle_layout_options(css)
            if fmt == "mobi":
                options.extend(["--mobi-file-type", "old"])
            _calibre_convert(paths, master, target, env, job, request.timeout_seconds, options)
            container = validate_kindle_container(target, fmt)
            roundtrip_epub = job / f"roundtrip-{fmt}.epub"
            _calibre_convert(paths, target, roundtrip_epub, env, job, request.timeout_seconds)
            roundtrip_structure = inspect_epub_structure(roundtrip_epub)
            roundtrip_readability = _validate_readability(
                roundtrip_epub,
                error_code="KINDLE_ROUNDTRIP_FAILED",
                typography=False,
            )
            try:
                roundtrip_text = normalize.source_body_text(roundtrip_epub)
            except (OSError, KeyError, ValueError, UnicodeError, zipfile.BadZipFile, ElementTree.ParseError) as exc:
                raise SkillError("KINDLE_ROUNDTRIP_FAILED", "无法从 Kindle 回读 EPUB 提取正文。") from exc
            retention = compare_content(baseline, roundtrip_text)
            semantic_retention = validate_semantic_retention(
                epub_structure,
                roundtrip_structure,
                require_ncx=True,
            )
            source_image_retention = validate_image_retention(
                route_details.get("source_resources", {}), roundtrip_structure
            )
            quality[fmt] = {
                **container,
                **retention,
                "roundtrip_epub_structure": roundtrip_structure,
                "roundtrip_readability": roundtrip_readability,
                "semantic_retention": semantic_retention,
                "source_image_retention": source_image_retention,
            }
            staged[fmt] = target
        timings["final_conversion_and_validation"] = time.monotonic() - stage_started

        if snapshot_calibre_user_state() != calibre_state_before:
            raise SkillError("ISOLATION_FAILED", "Calibre 用户状态发生变化；成品未发布。")
        if plan is not None and sha256_file(input_path) != plan["source_sha256"]:
            raise SkillError("ISOLATION_FAILED", "转换期间输入文件内容发生变化；成品未发布。")

        if store is not None and plan is not None:
            stage_started = time.monotonic()
            if semantic_export is not None and lookup["hit_level"] == "cold":
                semantic_tree = store.put_tree(Path(semantic_export), exclude_names=("normalized.html",))
                store.write_node(
                    "semantic",
                    str(plan["semantic_key"]),
                    {
                        "contract": CACHE_CONTRACT_VERSION,
                        "source_sha256": plan["source_sha256"],
                        "pdf_mode": route_details.get("pdf_mode"),
                        "probe": route_details.get("probe", {}),
                        "semantic_metrics": route_details.get("semantic_source", {}),
                        "tree": semantic_tree,
                    },
                )
            if lookup["hit_level"] != "master":
                store.write_node(
                    "master",
                    str(plan["master_key"]),
                    {
                        "contract": CACHE_CONTRACT_VERSION,
                        "source_sha256": plan["source_sha256"],
                        "route": route,
                        "route_details": deepcopy(route_details),
                        "master": store.put_file(master),
                        "baseline": store.put_file(baseline_path),
                        "source_body": store.put_bytes(source_body.encode("utf-8")),
                    },
                )
            for fmt in request.formats:
                store.write_node(
                    "final",
                    str(plan["final_keys"][fmt]),
                    {
                        "contract": CACHE_CONTRACT_VERSION,
                        "source_sha256": plan["source_sha256"],
                        "master_key": plan["master_key"],
                        "format": fmt,
                        "route": route,
                        "route_details": deepcopy(route_details),
                        "epubcheck": deepcopy(epub_counts),
                        "epub_structure": deepcopy(epub_structure),
                        "quality": deepcopy(quality[fmt]),
                        "artifact": store.put_file(staged[fmt]),
                        "paperwhite2_manual_check_required": True,
                    },
                )
            timings["workspace_store"] = time.monotonic() - stage_started
        elapsed = round(time.monotonic() - started, 3)
        workspace_details = _workspace_report_details(lookup["hit_level"], request.reuse_policy, timings)
        performance_review = build_performance_review(
            route=route,
            elapsed_seconds=elapsed,
            workspace=workspace_details,
            route_details=route_details,
        )
        report = safe_report(
            status="PASS",
            input_path=input_path,
            output_dir=output_dir,
            outputs=[destinations[fmt] for fmt in request.formats],
            route=route,
            details={
                **route_details,
                "formats": list(request.formats),
                "quality": quality,
                "workspace": workspace_details,
                "performance_review": performance_review,
                "runtime_verification": verified.get("verification_scope", "full"),
                "elapsed_seconds": elapsed,
                "calibre_user_state_unchanged": True,
                "paperwhite2_manual_check_required": True,
            },
        )

        def before_commit() -> None:
            _remove_job_strict(job)
            if store is not None:
                store._validate_tree()
            if snapshot_calibre_user_state() != calibre_state_before:
                raise SkillError("ISOLATION_FAILED", "Calibre 用户状态发生变化；成品未发布。")

        published_report = _publish_transaction(
            {destinations[fmt]: source for fmt, source in staged.items()},
            report_path,
            report,
            request.overwrite,
            before_commit=before_commit,
            directory_anchor=publication_anchor,
        )
        publication_committed = True
        _close_publication_anchor(publication_anchor)
        return published_report
    finally:
        if not publication_committed:
            _close_publication_anchor(publication_anchor)
            cleanup_error = None
            if job.exists():
                try:
                    _remove_job_strict(job)
                except SkillError as exc:
                    cleanup_error = exc
            if snapshot_calibre_user_state() != calibre_state_before:
                raise SkillError("ISOLATION_FAILED", "Calibre 用户状态发生变化；运行已停止。")
            if cleanup_error is not None:
                raise cleanup_error
