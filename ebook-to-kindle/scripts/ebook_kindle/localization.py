"""Explicit, ledger-based EPUB footnote localization with a cloud opt-in."""

from __future__ import annotations

import copy
import json
import os
import posixpath
import re
import shutil
import stat
import subprocess
import tempfile
import unicodedata
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree

from .core import SkillError, minimal_subprocess_env, safe_report, sha256_file
from .gates import inspect_epub_structure, validate_epub_container, validate_semantic_retention
from .pipeline import _publish_transaction, _remove_job_strict
from .workspace import cache_key


LEDGER_NAME = "ebook-to-kindle-note-ledger"
LEDGER_SCHEMA_VERSION = 1
DEFAULT_SPARK_MODEL = "gpt-5.3-codex-spark"
APPROVE_CONFIRMATION = "APPROVE_NOTE_TRANSLATIONS"
XHTML_NAMESPACE = "http://www.w3.org/1999/xhtml"
EPUB_NAMESPACE = "http://www.idpf.org/2007/ops"
ElementTree.register_namespace("", XHTML_NAMESPACE)
ElementTree.register_namespace("epub", EPUB_NAMESPACE)
CJK = re.compile(r"[\u3400-\u9fff]")
NUMERIC = re.compile(r"\d+")
URL = re.compile(r"https?://[^\s<>\"']+", re.I)
DOI = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Za-z0-9]+", re.I)
TECHNICAL_NAME = re.compile(r"\b(?:[A-Z]{2,}[A-Za-z0-9-]*|[A-Z][a-z0-9]+[A-Z][A-Za-z0-9-]*)\b")


def _local_name(value: str) -> str:
    return value.rsplit("}", 1)[-1].rsplit(":", 1)[-1].lower()


def _type_tokens(element: ElementTree.Element) -> set[str]:
    tokens = set()
    for key, value in element.attrib.items():
        if _local_name(key) in {"type", "role"}:
            tokens.update(str(value).lower().split())
    return tokens


def _is_noteref(element: ElementTree.Element) -> bool:
    return _local_name(element.tag) == "a" and bool(_type_tokens(element) & {"noteref", "doc-noteref"})


def _member_target(member: str, href: str) -> Optional[Tuple[str, str]]:
    parsed = urlsplit(href)
    if parsed.scheme or parsed.netloc or not parsed.fragment:
        return None
    target_member = member
    if parsed.path:
        target_member = posixpath.normpath(posixpath.join(posixpath.dirname(member), unquote(parsed.path)))
    if target_member.startswith("../") or target_member.startswith("/"):
        return None
    return target_member, unquote(parsed.fragment)


def _normalized_text(value: str) -> str:
    return " ".join(value.replace("\ufffd", " ").split())


def _protected_tokens(value: str) -> List[str]:
    tokens = set(URL.findall(value)) | set(DOI.findall(value)) | set(TECHNICAL_NAME.findall(value))
    return sorted(tokens, key=lambda token: (token.casefold(), token))


def _translation_errors(source: str, translation: str, protected_tokens: Sequence[str]) -> List[str]:
    errors = []
    if re.search(r"[A-Za-z]", source) and not CJK.search(translation):
        errors.append("missing-cjk")
    source_numbers = Counter(NUMERIC.findall(source))
    translated_numbers = Counter(NUMERIC.findall(translation))
    if any(translated_numbers[token] < count for token, count in source_numbers.items()):
        errors.append("missing-number")
    if any(token not in translation for token in protected_tokens):
        errors.append("missing-protected-token")
    return errors


def _element_identifier(element: ElementTree.Element) -> str:
    for key, value in element.attrib.items():
        if _local_name(key) == "id":
            return str(value)
    return ""


def _element_by_id(root: ElementTree.Element, identifier: str) -> Optional[ElementTree.Element]:
    return next((element for element in root.iter() if _element_identifier(element) == identifier), None)


def _href(element: ElementTree.Element) -> str:
    return next((str(value) for key, value in element.attrib.items() if _local_name(key) == "href"), "")


def _authored_note_text(
    note: ElementTree.Element,
    member: str,
    noteref_locations: set[Tuple[str, str]],
) -> str:
    parts: List[str] = []

    def visit(element: ElementTree.Element) -> None:
        if _local_name(element.tag) == "a":
            target = _member_target(member, _href(element))
            if target in noteref_locations:
                return
        if element.text:
            parts.append(element.text)
        for child in list(element):
            visit(child)
            if child.tail:
                parts.append(child.tail)

    visit(note)
    return _normalized_text(" ".join(parts))


def _read_xhtml_members(epub: Path) -> Dict[str, ElementTree.Element]:
    roots = {}
    try:
        with zipfile.ZipFile(epub) as archive:
            for member in archive.namelist():
                if member.lower().endswith((".xhtml", ".html", ".htm")):
                    try:
                        roots[member] = ElementTree.fromstring(archive.read(member))
                    except ElementTree.ParseError as exc:
                        raise SkillError("EPUB_INVALID", "EPUB 正文 XML 无法解析。", {"file": Path(member).name}) from exc
    except zipfile.BadZipFile as exc:
        raise SkillError("EPUB_INVALID", "EPUB 不是有效 ZIP 容器。") from exc
    return roots


def extract_note_ledger(epub: Path) -> Dict[str, Any]:
    epub = epub.expanduser().resolve()
    validate_epub_container(epub)
    structure = inspect_epub_structure(epub, require_toc=False)
    roots = _read_xhtml_members(epub)
    references: Dict[Tuple[str, str], List[Tuple[str, str]]] = {}
    noteref_locations: set[Tuple[str, str]] = set()
    for member, root in roots.items():
        for element in root.iter():
            if not _is_noteref(element):
                continue
            target = _member_target(member, _href(element))
            identifier = _element_identifier(element)
            if target is None or not identifier:
                raise SkillError("CONTENT_GATE_FAILED", "注释引用缺少可解析目标或自身 ID。")
            references.setdefault(target, []).append((member, identifier))
            noteref_locations.add((member, identifier))
    if not references:
        raise SkillError("CONTENT_GATE_FAILED", "EPUB 中没有可本地化的注释引用。")
    notes = []
    grouped: Dict[str, Dict[str, Any]] = {}
    for (member, note_id), source_refs in sorted(references.items()):
        root = roots.get(member)
        note = _element_by_id(root, note_id) if root is not None else None
        if note is None:
            raise SkillError("CONTENT_GATE_FAILED", "注释引用目标不存在。", {"note_id": note_id})
        source_text = _authored_note_text(note, member, noteref_locations)
        if len(source_text) < 2:
            raise SkillError("CONTENT_GATE_FAILED", "注释正文过短或无法提取。", {"note_id": note_id})
        source_sha256 = cache_key({"text": source_text})
        group_key = source_sha256
        note_key = cache_key({"member": member, "note_id": note_id, "source_sha256": source_sha256})
        item = {
            "note_key": note_key,
            "member": member,
            "note_id": note_id,
            "source_text": source_text,
            "source_sha256": source_sha256,
            "group_key": group_key,
            "reference_count": len(source_refs),
            "protected_tokens": _protected_tokens(source_text),
            "translation": None,
            "status": "pending",
        }
        notes.append(item)
        group = grouped.setdefault(
            group_key,
            {
                "group_key": group_key,
                "source_text": source_text,
                "protected_tokens": _protected_tokens(source_text),
                "note_keys": [],
            },
        )
        group["note_keys"].append(note_key)
    return {
        "ledger_name": LEDGER_NAME,
        "schema_version": LEDGER_SCHEMA_VERSION,
        "source_epub_sha256": sha256_file(epub),
        "policy": "per-id-approved-translations-are-authoritative; fan-out-only-before-approval",
        "notes": notes,
        "groups": [grouped[key] for key in sorted(grouped)],
        "metrics": {
            "note_count": len(notes),
            "unique_source_count": len(grouped),
            "duplicate_source_count": len(notes) - len(grouped),
            "noteref_count": int(structure.get("noteref_count", 0) or 0),
        },
    }


def _validate_ledger(value: Mapping[str, Any]) -> Dict[str, Any]:
    if value.get("ledger_name") != LEDGER_NAME or value.get("schema_version") != LEDGER_SCHEMA_VERSION:
        raise SkillError("CONTENT_GATE_FAILED", "注释本地化账本身份或版本不匹配。")
    notes = value.get("notes")
    groups = value.get("groups")
    if not isinstance(notes, list) or not isinstance(groups, list) or not notes:
        raise SkillError("CONTENT_GATE_FAILED", "注释本地化账本内容不完整。")
    note_keys = set()
    for note in notes:
        if not isinstance(note, dict):
            raise SkillError("CONTENT_GATE_FAILED", "注释本地化账本条目类型异常。")
        required = ("note_key", "member", "note_id", "source_text", "source_sha256", "group_key", "status")
        if any(not isinstance(note.get(key), str) or not note.get(key) for key in required):
            raise SkillError("CONTENT_GATE_FAILED", "注释本地化账本条目字段缺失。")
        expected_source = cache_key({"text": note["source_text"]})
        expected_note = cache_key(
            {"member": note["member"], "note_id": note["note_id"], "source_sha256": expected_source}
        )
        if note["source_sha256"] != expected_source or note["note_key"] != expected_note or note["note_key"] in note_keys:
            raise SkillError("CONTENT_GATE_FAILED", "注释本地化账本哈希或唯一性校验失败。")
        status = note["status"]
        translation = note.get("translation")
        if status not in {"pending", "model-reviewed", "approved"}:
            raise SkillError("CONTENT_GATE_FAILED", "注释本地化账本包含未知状态。")
        if status == "pending" and translation not in {None, ""}:
            raise SkillError("CONTENT_GATE_FAILED", "待翻译注释不能预存译文。")
        if status in {"model-reviewed", "approved"}:
            if not isinstance(translation, str) or not translation.strip():
                raise SkillError("CONTENT_GATE_FAILED", "已复核或批准注释缺少译文。")
            expected_translation = cache_key({"text": _normalized_text(translation)})
            if note.get("translation_sha256") != expected_translation:
                raise SkillError("CONTENT_GATE_FAILED", "注释译文哈希校验失败。")
            protected = note.get("protected_tokens", [])
            if not isinstance(protected, list) or any(not isinstance(token, str) for token in protected):
                raise SkillError("CONTENT_GATE_FAILED", "注释受保护词清单类型异常。")
            if _translation_errors(note["source_text"], translation, protected):
                raise SkillError("CONTENT_GATE_FAILED", "注释译文丢失中文、数字或受保护词。")
        note_keys.add(note["note_key"])
    return copy.deepcopy(dict(value))


def read_ledger(path: Path) -> Dict[str, Any]:
    path = path.expanduser().resolve()
    try:
        metadata = os.lstat(path)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise SkillError("CONTENT_GATE_FAILED", "注释本地化账本必须是单链接普通文件。")
        value = json.loads(path.read_text(encoding="utf-8"))
    except SkillError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SkillError("CONTENT_GATE_FAILED", "注释本地化账本无法读取。") from exc
    if not isinstance(value, dict):
        raise SkillError("CONTENT_GATE_FAILED", "注释本地化账本不是 JSON 对象。")
    return _validate_ledger(value)


def write_ledger(path: Path, ledger: Mapping[str, Any], *, overwrite: bool) -> None:
    path = Path(os.path.abspath(os.fspath(path.expanduser())))
    for component in (path.parent, *path.parent.parents):
        if component.is_symlink():
            raise SkillError("UNSUPPORTED_OUTPUT", "注释本地化账本路径不能包含符号链接。")
    if not path.parent.exists():
        path.parent.mkdir(parents=True, mode=0o700)
    if not path.parent.is_dir():
        raise SkillError("UNSUPPORTED_OUTPUT", "注释本地化账本父路径不是目录。")
    if path.is_symlink() or (path.exists() and (not path.is_file() or path.stat().st_nlink != 1)):
        raise SkillError("UNSUPPORTED_OUTPUT", "注释本地化账本目标必须是单链接普通文件。")
    if path.exists() and not overwrite:
        raise SkillError("OUTPUT_EXISTS", "注释本地化账本已存在；如需替换请显式使用 --overwrite。")
    payload = _validate_ledger(ledger)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.stage-", dir=str(path.parent))
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        if overwrite:
            if path.exists() and (path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1):
                raise SkillError("UNSUPPORTED_OUTPUT", "注释本地化账本目标在提交前发生变化。")
            os.replace(temporary, path)
        else:
            try:
                os.link(temporary, path, follow_symlinks=False)
            except FileExistsError as exc:
                raise SkillError("OUTPUT_EXISTS", "注释本地化账本已存在；如需替换请显式使用 --overwrite。") from exc
            temporary.unlink()
    finally:
        try:
            if temporary.exists():
                temporary.unlink()
        except OSError:
            pass


def _translation_schema(keys: Sequence[str]) -> Dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["translations"],
        "properties": {
            "translations": {
                "type": "array",
                "minItems": len(keys),
                "maxItems": len(keys),
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["group_key", "translation"],
                    "properties": {
                        "group_key": {"type": "string", "enum": list(keys)},
                        "translation": {"type": "string", "minLength": 1},
                    },
                },
            }
        },
    }


def _spark_batch(
    items: Sequence[Mapping[str, str]],
    *,
    model: str,
    timeout_seconds: int,
) -> Dict[str, str]:
    executable = shutil.which("codex")
    if not executable:
        raise SkillError("DEPENDENCY_MISSING", "未找到 Codex CLI，无法调用 5.3 Spark。")
    keys = [str(item["group_key"]) for item in items]
    with tempfile.TemporaryDirectory(prefix="ebook-notes-spark-", dir="/private/tmp") as outer:
        job = Path(outer)
        job.chmod(0o700)
        schema_path = job / "schema.json"
        output_path = job / "output.json"
        schema_path.write_text(json.dumps(_translation_schema(keys), ensure_ascii=False), encoding="utf-8")
        schema_path.chmod(0o600)
        prompt = {
            "task": (
                "Translate each English ebook footnote into concise, faithful Simplified Chinese, then internally review it once. "
                "Preserve names, dates, numbers, URLs, DOI and bibliographic facts. Do not add commentary. "
                "Return exactly one translation for every group_key. Do not use tools or read files."
            ),
            "items": [{"group_key": item["group_key"], "source_text": item["source_text"]} for item in items],
        }
        command = [
            executable,
            "exec",
            "--model",
            model,
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--cd",
            str(job),
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(output_path),
            "-",
        ]
        try:
            result = subprocess.run(
                command,
                input=json.dumps(prompt, ensure_ascii=False),
                env=minimal_subprocess_env(),
                cwd=str(job),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SkillError("CONVERSION_FAILED", "5.3 Spark 注释翻译未能安全完成。") from exc
        if result.returncode != 0 or not output_path.is_file():
            raise SkillError("CONVERSION_FAILED", "5.3 Spark 注释翻译失败。", {"return_code": result.returncode})
        try:
            payload = json.loads(output_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SkillError("CONVERSION_FAILED", "5.3 Spark 没有返回合规 JSON。") from exc
    translations = payload.get("translations", []) if isinstance(payload, dict) else []
    result_map = {}
    for item in translations if isinstance(translations, list) else []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("group_key", ""))
        translated = _normalized_text(str(item.get("translation", "")))
        source_item = next((source for source in items if source["group_key"] == key), None)
        protected = _protected_tokens(str(source_item["source_text"])) if source_item is not None else []
        if (
            key not in keys
            or key in result_map
            or not translated
            or "\ufffd" in translated
            or source_item is None
            or _translation_errors(str(source_item["source_text"]), translated, protected)
        ):
            raise SkillError("CONTENT_GATE_FAILED", "5.3 Spark 注释译文缺失、重复或损坏。")
        result_map[key] = translated
    if set(result_map) != set(keys):
        raise SkillError("CONTENT_GATE_FAILED", "5.3 Spark 未返回全部注释译文。")
    return result_map


def translate_pending_groups(
    ledger: Mapping[str, Any],
    *,
    allow_cloud_translation: bool,
    model: str = DEFAULT_SPARK_MODEL,
    batch_size: int = 24,
    timeout_seconds: int = 1200,
    runner: Optional[Callable[[Sequence[Mapping[str, str]]], Dict[str, str]]] = None,
) -> Dict[str, Any]:
    if not allow_cloud_translation:
        raise SkillError("CLOUD_TRANSLATION_NOT_APPROVED", "上传注释文本到 5.3 Spark 需要显式 --allow-cloud-translation。")
    value = _validate_ledger(ledger)
    notes_by_group: Dict[str, List[Dict[str, Any]]] = {}
    for note in value["notes"]:
        notes_by_group.setdefault(note["group_key"], []).append(note)
    pending_groups = []
    for group in value["groups"]:
        group_notes = notes_by_group.get(group["group_key"], [])
        if group_notes and all(note["status"] == "pending" and not note.get("translation") for note in group_notes):
            pending_groups.append(
                {
                    "group_key": group["group_key"],
                    "source_text": group["source_text"],
                    "protected_tokens": group.get("protected_tokens", []),
                }
            )
    if not pending_groups:
        return value
    if batch_size < 1 or batch_size > 50:
        raise SkillError("CONTENT_GATE_FAILED", "Spark 批大小必须在 1 到 50 之间。")
    translations: Dict[str, str] = {}
    for index in range(0, len(pending_groups), batch_size):
        batch = pending_groups[index : index + batch_size]
        batch_result = (
            runner(batch)
            if runner is not None
            else _spark_batch(batch, model=model, timeout_seconds=timeout_seconds)
        )
        if set(batch_result) != {item["group_key"] for item in batch}:
            raise SkillError("CONTENT_GATE_FAILED", "注释翻译批次返回的键不完整。")
        for item in batch:
            errors = _translation_errors(
                str(item["source_text"]),
                _normalized_text(str(batch_result[item["group_key"]])),
                item.get("protected_tokens", []),
            )
            if errors:
                raise SkillError(
                    "CONTENT_GATE_FAILED",
                    "注释译文丢失中文、数字或受保护词。",
                    {"quality_errors": errors},
                )
        translations.update(batch_result)
    for note in value["notes"]:
        translated = translations.get(note["group_key"])
        if translated is not None and note["status"] == "pending":
            note["translation"] = _normalized_text(translated)
            note["translation_sha256"] = cache_key({"text": note["translation"]})
            note["status"] = "model-reviewed"
            note["translation_model"] = model
    value["translation_summary"] = {
        "model": model,
        "translated_unique_sources": len(translations),
        "translated_note_ids": sum(note["group_key"] in translations for note in value["notes"]),
        "fan_out_before_approval": True,
    }
    return _validate_ledger(value)


def approve_translations(ledger: Mapping[str, Any], confirmation: str) -> Dict[str, Any]:
    if confirmation != APPROVE_CONFIRMATION:
        raise SkillError("CONTENT_GATE_FAILED", "批准注释译文需要精确确认词 APPROVE_NOTE_TRANSLATIONS。")
    value = _validate_ledger(ledger)
    pending = [
        note["note_id"]
        for note in value["notes"]
        if note["status"] not in {"model-reviewed", "approved"} or not note.get("translation")
    ]
    if pending:
        raise SkillError("CONTENT_GATE_FAILED", "仍有未完成模型复核的注释，不能整批批准。", {"pending_count": len(pending)})
    for note in value["notes"]:
        if note["status"] == "model-reviewed":
            note["status"] = "approved"
            note["approval_basis"] = "explicit-user-confirmation"
    value["approval_summary"] = {"approved_note_ids": len(value["notes"]), "per_id_frozen": True}
    return _validate_ledger(value)


def _replace_note_text(
    note: ElementTree.Element,
    member: str,
    noteref_locations: set[Tuple[str, str]],
    translation: str,
) -> None:
    backlinks = []
    for element in note.iter():
        if _local_name(element.tag) == "a" and _member_target(member, _href(element)) in noteref_locations:
            clone = copy.deepcopy(element)
            clone.tail = " "
            backlinks.append(clone)
    container = note if _local_name(note.tag) == "p" else ElementTree.Element(f"{{{XHTML_NAMESPACE}}}p")
    for child in list(container):
        container.remove(child)
    container.text = translation + (" " if backlinks else "")
    for backlink in backlinks:
        container.append(backlink)
    if container is not note:
        for child in list(note):
            note.remove(child)
        note.text = None
        note.append(container)


def _serialized_localized_members(epub: Path, ledger: Mapping[str, Any]) -> Tuple[Dict[str, bytes], Dict[str, Any]]:
    value = _validate_ledger(ledger)
    if sha256_file(epub) != value.get("source_epub_sha256"):
        raise SkillError("CONTENT_GATE_FAILED", "注释账本不属于当前 EPUB 字节版本。")
    roots = _read_xhtml_members(epub)
    noteref_locations = set()
    for member, root in roots.items():
        for element in root.iter():
            if _is_noteref(element):
                identifier = _element_identifier(element)
                if identifier:
                    noteref_locations.add((member, identifier))
    changed_members = set()
    for item in value["notes"]:
        if item["status"] != "approved" or not isinstance(item.get("translation"), str) or not item["translation"].strip():
            raise SkillError("CONTENT_GATE_FAILED", "存在未按编号批准的注释译文。", {"note_id": item["note_id"]})
        member = item["member"]
        root = roots.get(member)
        note = _element_by_id(root, item["note_id"]) if root is not None else None
        if note is None:
            raise SkillError("CONTENT_GATE_FAILED", "本地化目标注释不存在。", {"note_id": item["note_id"]})
        current_text = _authored_note_text(note, member, noteref_locations)
        if cache_key({"text": current_text}) != item["source_sha256"]:
            raise SkillError("CONTENT_GATE_FAILED", "注释原文在批准后发生变化。", {"note_id": item["note_id"]})
        _replace_note_text(note, member, noteref_locations, item["translation"])
        changed_members.add(member)
    serialized = {
        member: ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)
        for member, root in roots.items()
        if member in changed_members
    }
    return serialized, value


def _write_localized_epub(source: Path, replacements: Mapping[str, bytes], destination: Path) -> None:
    with zipfile.ZipFile(source) as archive, zipfile.ZipFile(destination, "w") as output:
        infos = archive.infolist()
        mimetype = next((info for info in infos if info.filename == "mimetype"), None)
        if mimetype is None:
            raise SkillError("EPUB_INVALID", "EPUB 缺少 mimetype。")
        mime_info = copy.copy(mimetype)
        mime_info.compress_type = zipfile.ZIP_STORED
        output.writestr(mime_info, replacements.get("mimetype", archive.read("mimetype")))
        for info in infos:
            if info.filename == "mimetype":
                continue
            output.writestr(info, replacements.get(info.filename, archive.read(info.filename)))
    os.chmod(destination, 0o600)


def apply_approved_translations(
    source_epub: Path,
    ledger: Mapping[str, Any],
    output_epub: Path,
    *,
    overwrite: bool,
) -> Dict[str, Any]:
    source_epub = source_epub.expanduser().resolve()
    output_epub = Path(os.path.abspath(os.fspath(output_epub.expanduser())))
    source_key = unicodedata.normalize("NFC", str(source_epub)).casefold()
    output_key = unicodedata.normalize("NFC", str(output_epub)).casefold()
    if output_key == source_key:
        raise SkillError("UNSUPPORTED_OUTPUT", "本地化成品不能覆盖源 EPUB。")
    if output_epub.exists() and source_epub.samefile(output_epub):
        raise SkillError("UNSUPPORTED_OUTPUT", "本地化成品不能覆盖源 EPUB。")
    replacements, value = _serialized_localized_members(source_epub, ledger)
    if not output_epub.parent.exists():
        output_epub.parent.mkdir(parents=True, mode=0o700)
    if not output_epub.parent.is_dir() or output_epub.parent.is_symlink():
        raise SkillError("UNSUPPORTED_OUTPUT", "本地化输出父路径必须是普通目录。")
    job = Path(tempfile.mkdtemp(prefix="ebook-notes-apply-", dir="/private/tmp"))
    job.chmod(0o700)
    committed = False
    try:
        staged = job / "localized.epub"
        _write_localized_epub(source_epub, replacements, staged)
        validate_epub_container(staged)
        source_structure = inspect_epub_structure(source_epub, require_toc=False)
        localized_structure = inspect_epub_structure(staged, require_toc=False)
        semantic = validate_semantic_retention(source_structure, localized_structure, require_ncx=False)
        localized_ledger = extract_note_ledger(staged)
        localized_by_id = {item["note_id"]: item["source_text"] for item in localized_ledger["notes"]}
        mismatches = [
            item["note_id"]
            for item in value["notes"]
            if _normalized_text(localized_by_id.get(item["note_id"], "")) != _normalized_text(item["translation"])
        ]
        if mismatches:
            raise SkillError("CONTENT_GATE_FAILED", "本地化 EPUB 中的译文与批准账本不一致。", {"mismatch_count": len(mismatches)})
        report_path = output_epub.parent / f"{output_epub.stem}.ebook-notes.json"
        report = safe_report(
            status="PASS",
            input_path=source_epub,
            output_dir=output_epub.parent,
            outputs=[output_epub],
            route="approved-note-localization",
            details={
                "approved_note_ids": len(value["notes"]),
                "localized_note_ids": len(localized_ledger["notes"]),
                "semantic_retention": semantic,
                "source_sha256": value["source_epub_sha256"],
                "ledger_sha256": cache_key(value),
                "output_sha256": sha256_file(staged),
                "cloud_translation_during_apply": False,
            },
        )
        published = _publish_transaction(
            {output_epub: staged},
            report_path,
            report,
            overwrite,
            before_commit=lambda: _remove_job_strict(job),
        )
        committed = True
        return published
    finally:
        if not committed and job.exists():
            _remove_job_strict(job)
