"""Private, content-addressed, single-book workspace primitives."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import stat
import tempfile
import unicodedata
import uuid
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterator, Mapping, Optional, Sequence, Set

from .core import SkillError, sha256_file


WORKSPACE_NAME = "ebook-to-kindle-book-workspace"
WORKSPACE_SCHEMA_VERSION = 1
WORKSPACE_LOCK_NAME = ".workspace.lock"
WORKSPACE_MANIFEST_NAME = "workspace.json"
WORKSPACE_LAYERS = ("semantic", "master", "final", "localization")
MAX_WORKSPACE_BYTES = 2 * 1024 * 1024 * 1024
PURGE_CONFIRMATION = "PURGE_EBOOK_WORKSPACE"


def lexical_absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path.expanduser())))


def _portable_path_key(path: Path) -> str:
    return unicodedata.normalize("NFC", str(lexical_absolute(path))).casefold().rstrip(os.sep)


def _overlaps(left: Path, right: Path) -> bool:
    left_key = _portable_path_key(left)
    right_key = _portable_path_key(right)
    if left_key == right_key:
        return True
    return left_key.startswith(right_key + os.sep) or right_key.startswith(left_key + os.sep)


def _reject_symlink_components(path: Path) -> None:
    value = lexical_absolute(path)
    for component in (value, *value.parents):
        if component.is_symlink():
            raise SkillError("WORKSPACE_INVALID", "单书工作区路径不能包含符号链接。")


def _regular_single_link(path: Path) -> os.stat_result:
    try:
        metadata = os.lstat(path)
    except OSError as exc:
        raise SkillError("WORKSPACE_INVALID", "单书工作区条目无法安全读取。") from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise SkillError("WORKSPACE_INVALID", "单书工作区文件必须是单链接普通文件。")
    return metadata


def _validate_hash(value: str) -> str:
    normalized = str(value).lower()
    if len(normalized) != 64 or any(character not in "0123456789abcdef" for character in normalized):
        raise SkillError("WORKSPACE_INVALID", "单书工作区包含非法内容哈希。")
    return normalized


def _validate_layer(layer: str) -> str:
    if layer not in WORKSPACE_LAYERS:
        raise SkillError("WORKSPACE_INVALID", "单书工作区包含未知缓存层。")
    return layer


def cache_key(payload: Mapping[str, Any]) -> str:
    """Return a canonical SHA-256 key for a JSON-compatible recipe."""
    try:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise SkillError("WORKSPACE_INVALID", "缓存配方无法规范序列化。") from exc
    return hashlib.sha256(encoded).hexdigest()


def fingerprint_files(root: Path, relative_paths: Sequence[str]) -> Dict[str, str]:
    result: Dict[str, str] = {}
    root = lexical_absolute(root)
    for relative in relative_paths:
        candidate = root / relative
        try:
            resolved = candidate.resolve(strict=True)
            resolved_root = root.resolve(strict=True)
        except OSError as exc:
            raise SkillError("WORKSPACE_INVALID", "缓存配方依赖文件缺失。") from exc
        if resolved_root not in resolved.parents or not resolved.is_file() or candidate.is_symlink():
            raise SkillError("WORKSPACE_INVALID", "缓存配方依赖文件越界或类型异常。")
        result[relative] = sha256_file(candidate)
    return result


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.stage-", dir=str(path.parent))
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try:
            if temporary.exists():
                temporary.unlink()
        except OSError:
            pass


def _read_json(path: Path) -> Dict[str, Any]:
    _regular_single_link(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SkillError("WORKSPACE_INVALID", "单书工作区 JSON 已损坏。") from exc
    if not isinstance(value, dict):
        raise SkillError("WORKSPACE_INVALID", "单书工作区 JSON 类型异常。")
    return value


def _workspace_bytes(root: Path) -> int:
    total = 0

    def visit(directory: Path) -> None:
        nonlocal total
        try:
            with os.scandir(directory) as entries:
                ordered = sorted(entries, key=lambda entry: entry.name)
        except OSError as exc:
            raise SkillError("WORKSPACE_INVALID", "无法完整扫描单书工作区。") from exc
        for entry in ordered:
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise SkillError("WORKSPACE_INVALID", "无法读取单书工作区条目。") from exc
            if stat.S_ISLNK(metadata.st_mode):
                raise SkillError("WORKSPACE_INVALID", "单书工作区不能包含符号链接。")
            if stat.S_ISDIR(metadata.st_mode):
                visit(Path(entry.path))
            elif stat.S_ISREG(metadata.st_mode):
                if metadata.st_nlink != 1:
                    raise SkillError("WORKSPACE_INVALID", "单书工作区不能包含硬链接。")
                if not (Path(entry.path).parent == root and entry.name == WORKSPACE_LOCK_NAME):
                    total += metadata.st_size
            else:
                raise SkillError("WORKSPACE_INVALID", "单书工作区不能包含特殊文件。")

    visit(root)
    return total


class BookWorkspace:
    """A caller-locked content store bound to exactly one private root."""

    def __init__(self, root: Path) -> None:
        self.root = lexical_absolute(root)
        self.manifest_path = self.root / WORKSPACE_MANIFEST_NAME
        self.lock_path = self.root / WORKSPACE_LOCK_NAME
        self.objects = self.root / "objects"
        self.nodes = self.root / "nodes"

    @classmethod
    def open(cls, root: Path, *, create: bool = True) -> "BookWorkspace":
        _reject_symlink_components(root)
        value = lexical_absolute(root)
        existed = value.exists()
        if existed and not value.is_dir():
            raise SkillError("WORKSPACE_INVALID", "单书工作区必须是目录。")
        if not existed:
            if not create:
                raise SkillError("WORKSPACE_INVALID", "单书工作区不存在。")
            try:
                value.mkdir(parents=True, mode=0o700)
            except OSError as exc:
                raise SkillError("WORKSPACE_INVALID", "无法创建单书工作区。") from exc
        if existed and not (value / WORKSPACE_MANIFEST_NAME).exists():
            try:
                foreign = [item.name for item in value.iterdir() if item.name != WORKSPACE_LOCK_NAME]
            except OSError as exc:
                raise SkillError("WORKSPACE_INVALID", "无法检查单书工作区身份。") from exc
            if foreign or not create:
                raise SkillError("WORKSPACE_INVALID", "目录不是可识别的 ebook-to-kindle 单书工作区。")
        try:
            value.chmod(0o700)
        except OSError as exc:
            raise SkillError("WORKSPACE_INVALID", "无法设置单书工作区私有权限。") from exc
        store = cls(value)
        with store.locked():
            store._ensure_layout(create=create)
            store._validate_tree()
        return store

    @contextmanager
    def locked(self) -> Iterator["BookWorkspace"]:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.lock_path, flags, 0o600)
        except OSError as exc:
            raise SkillError("WORKSPACE_INVALID", "无法安全打开单书工作区锁。") from exc
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or metadata.st_nlink != 1
                or metadata.st_size != 0
            ):
                raise SkillError("WORKSPACE_INVALID", "单书工作区锁身份或类型异常。")
            os.fchmod(descriptor, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise SkillError("WORKSPACE_BUSY", "另一项单书工作区操作正在进行；请稍后重试。") from exc
            yield self
        except SkillError:
            raise
        except OSError as exc:
            raise SkillError("WORKSPACE_INVALID", "单书工作区锁操作失败。") from exc
        finally:
            try:
                os.close(descriptor)
            except OSError:
                pass

    def _ensure_layout(self, *, create: bool) -> None:
        if self.manifest_path.exists():
            manifest = _read_json(self.manifest_path)
            if (
                manifest.get("workspace_name") != WORKSPACE_NAME
                or manifest.get("schema_version") != WORKSPACE_SCHEMA_VERSION
            ):
                raise SkillError("WORKSPACE_INVALID", "单书工作区身份或版本不匹配。")
        elif create:
            _atomic_json(
                self.manifest_path,
                {
                    "workspace_name": WORKSPACE_NAME,
                    "schema_version": WORKSPACE_SCHEMA_VERSION,
                    "max_bytes": MAX_WORKSPACE_BYTES,
                },
            )
        else:
            raise SkillError("WORKSPACE_INVALID", "目录不是可识别的 ebook-to-kindle 单书工作区。")
        for directory in (self.objects, self.nodes, *(self.nodes / layer for layer in WORKSPACE_LAYERS)):
            if directory.exists() and (directory.is_symlink() or not directory.is_dir()):
                raise SkillError("WORKSPACE_INVALID", "单书工作区目录类型异常。")
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            directory.chmod(0o700)

    def _validate_tree(self) -> None:
        if stat.S_IMODE(os.lstat(self.root).st_mode) != 0o700:
            raise SkillError("WORKSPACE_INVALID", "单书工作区根目录权限不是 0700。")
        manifest = _read_json(self.manifest_path)
        if (
            manifest.get("workspace_name") != WORKSPACE_NAME
            or manifest.get("schema_version") != WORKSPACE_SCHEMA_VERSION
        ):
            raise SkillError("WORKSPACE_INVALID", "单书工作区身份或版本不匹配。")
        try:
            top_level = {item.name for item in self.root.iterdir()}
            node_layers = {item.name for item in self.nodes.iterdir()}
            object_names = [item.name for item in self.objects.iterdir()]
        except OSError as exc:
            raise SkillError("WORKSPACE_INVALID", "无法枚举单书工作区身份条目。") from exc
        expected_top_level = {WORKSPACE_MANIFEST_NAME, WORKSPACE_LOCK_NAME, "objects", "nodes"}
        if top_level != expected_top_level or node_layers != set(WORKSPACE_LAYERS):
            raise SkillError("WORKSPACE_INVALID", "单书工作区包含未知顶层条目或缓存层。")
        for name in object_names:
            _validate_hash(name)
        for layer in WORKSPACE_LAYERS:
            try:
                entries = list((self.nodes / layer).iterdir())
            except OSError as exc:
                raise SkillError("WORKSPACE_INVALID", "无法枚举单书工作区节点。") from exc
            for node in entries:
                if node.suffix != ".json":
                    raise SkillError("WORKSPACE_INVALID", "单书工作区包含未知节点文件。")
                _validate_hash(node.stem)
        total = _workspace_bytes(self.root)
        limit = int(manifest.get("max_bytes", MAX_WORKSPACE_BYTES) or 0)
        if limit != MAX_WORKSPACE_BYTES or total > limit:
            raise SkillError("WORKSPACE_INVALID", "单书工作区容量合同未通过。")

    def validate_boundaries(
        self,
        *,
        input_path: Path,
        output_dir: Path,
        runtime_root: Path,
        skill_root: Path,
    ) -> None:
        for protected in (input_path, output_dir, runtime_root, skill_root):
            if _overlaps(self.root, protected):
                raise SkillError("WORKSPACE_INVALID", "单书工作区不能与输入、输出、运行时或 Skill 路径重叠。")

    def object_path(self, digest: str) -> Path:
        return self.objects / _validate_hash(digest)

    def _ensure_capacity(self, additional_bytes: int) -> None:
        if additional_bytes < 0 or _workspace_bytes(self.root) + additional_bytes > MAX_WORKSPACE_BYTES:
            raise SkillError("WORKSPACE_INVALID", "单书工作区超过 2 GiB 容量上限。")

    def put_bytes(self, payload: bytes) -> Dict[str, Any]:
        digest = hashlib.sha256(payload).hexdigest()
        destination = self.object_path(digest)
        reference = {"sha256": digest, "bytes": len(payload)}
        if destination.exists():
            self.read_object(reference)
            return reference
        self._ensure_capacity(len(payload))
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{digest}.stage-", dir=str(self.objects))
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, destination)
        finally:
            try:
                if temporary.exists():
                    temporary.unlink()
            except OSError:
                pass
        self.read_object(reference)
        return reference

    def put_file(self, source: Path) -> Dict[str, Any]:
        metadata = _regular_single_link(source)
        digest = sha256_file(source)
        reference = {"sha256": digest, "bytes": metadata.st_size}
        destination = self.object_path(digest)
        if destination.exists():
            self.read_object(reference)
            return reference
        self._ensure_capacity(metadata.st_size)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{digest}.stage-", dir=str(self.objects))
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            shutil.copyfile(source, temporary)
            os.chmod(temporary, 0o600)
            if sha256_file(temporary) != digest or temporary.stat().st_size != metadata.st_size:
                raise SkillError("WORKSPACE_INVALID", "单书工作区对象复制校验失败。")
            os.replace(temporary, destination)
        finally:
            try:
                if temporary.exists():
                    temporary.unlink()
            except OSError:
                pass
        self.read_object(reference)
        return reference

    def read_object(self, reference: Mapping[str, Any]) -> Path:
        try:
            digest = _validate_hash(str(reference["sha256"]))
            expected_bytes = int(reference["bytes"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SkillError("WORKSPACE_INVALID", "单书工作区对象引用无效。") from exc
        path = self.object_path(digest)
        metadata = _regular_single_link(path)
        if metadata.st_size != expected_bytes or sha256_file(path) != digest:
            raise SkillError("WORKSPACE_INVALID", "单书工作区对象哈希或大小不匹配。")
        return path

    def write_node(self, layer: str, key: str, payload: Mapping[str, Any]) -> None:
        layer = _validate_layer(layer)
        key = _validate_hash(key)
        node = dict(payload)
        node.update({"schema_version": WORKSPACE_SCHEMA_VERSION, "layer": layer, "key": key})
        node["payload_sha256"] = cache_key(node)
        try:
            json.dumps(node, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise SkillError("WORKSPACE_INVALID", "单书工作区节点无法规范序列化。") from exc
        _atomic_json(self.nodes / layer / f"{key}.json", node)
        self._validate_tree()

    def read_node(self, layer: str, key: str) -> Optional[Dict[str, Any]]:
        layer = _validate_layer(layer)
        key = _validate_hash(key)
        path = self.nodes / layer / f"{key}.json"
        if not path.exists():
            return None
        value = _read_json(path)
        observed_digest = value.get("payload_sha256")
        digest_payload = dict(value)
        digest_payload.pop("payload_sha256", None)
        if (
            value.get("schema_version") != WORKSPACE_SCHEMA_VERSION
            or value.get("layer") != layer
            or value.get("key") != key
            or observed_digest != cache_key(digest_payload)
        ):
            raise SkillError("WORKSPACE_INVALID", "单书工作区节点身份不匹配。")
        return value

    def put_tree(self, source: Path, *, exclude_names: Sequence[str] = ()) -> Dict[str, Any]:
        source = lexical_absolute(source)
        if source.is_symlink() or not source.is_dir():
            raise SkillError("WORKSPACE_INVALID", "语义缓存来源必须是普通目录。")
        excluded: Set[str] = set(exclude_names)
        files = []
        directories = []

        def visit(directory: Path, relative: PurePosixPath) -> None:
            try:
                with os.scandir(directory) as entries:
                    ordered = sorted(entries, key=lambda entry: entry.name)
            except OSError as exc:
                raise SkillError("WORKSPACE_INVALID", "无法完整读取语义缓存目录。") from exc
            for entry in ordered:
                if entry.name in excluded:
                    continue
                try:
                    metadata = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    raise SkillError("WORKSPACE_INVALID", "无法读取语义缓存条目。") from exc
                child_relative = relative / entry.name
                if stat.S_ISLNK(metadata.st_mode):
                    raise SkillError("WORKSPACE_INVALID", "语义缓存不能包含符号链接。")
                if stat.S_ISDIR(metadata.st_mode):
                    directories.append(child_relative.as_posix())
                    visit(Path(entry.path), child_relative)
                elif stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1:
                    files.append({"path": child_relative.as_posix(), "object": self.put_file(Path(entry.path))})
                else:
                    raise SkillError("WORKSPACE_INVALID", "语义缓存只能包含单链接普通文件和目录。")

        visit(source, PurePosixPath("."))
        return {"directories": directories, "files": files}

    def restore_tree(self, tree: Mapping[str, Any], destination: Path) -> None:
        destination = lexical_absolute(destination)
        if destination.exists():
            raise SkillError("WORKSPACE_INVALID", "语义缓存恢复目标必须不存在。")
        destination.mkdir(parents=True, mode=0o700)
        destination.chmod(0o700)

        def safe_relative(raw: Any) -> PurePosixPath:
            relative = PurePosixPath(str(raw))
            if relative.is_absolute() or not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
                raise SkillError("WORKSPACE_INVALID", "语义缓存包含越界路径。")
            return relative

        try:
            directories = tree.get("directories", [])
            files = tree.get("files", [])
            if not isinstance(directories, list) or not isinstance(files, list):
                raise SkillError("WORKSPACE_INVALID", "语义缓存树清单类型异常。")
            for raw in sorted(directories, key=lambda value: len(PurePosixPath(str(value)).parts)):
                target = destination.joinpath(*safe_relative(raw).parts)
                target.mkdir(parents=True, exist_ok=True, mode=0o700)
                target.chmod(0o700)
            seen = set()
            for entry in files:
                if not isinstance(entry, dict):
                    raise SkillError("WORKSPACE_INVALID", "语义缓存文件清单类型异常。")
                relative = safe_relative(entry.get("path"))
                if relative.as_posix() in seen:
                    raise SkillError("WORKSPACE_INVALID", "语义缓存文件清单包含重复路径。")
                seen.add(relative.as_posix())
                source = self.read_object(entry.get("object", {}))
                target = destination.joinpath(*relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                target.parent.chmod(0o700)
                if target.exists() or target.is_symlink():
                    raise SkillError("WORKSPACE_INVALID", "语义缓存恢复目标发生冲突。")
                shutil.copyfile(source, target)
                os.chmod(target, 0o600)
                if sha256_file(target) != entry["object"]["sha256"]:
                    raise SkillError("WORKSPACE_INVALID", "语义缓存恢复后哈希不一致。")
        except Exception:
            try:
                shutil.rmtree(destination)
            except OSError:
                pass
            raise


def inspect_workspace(root: Path) -> Dict[str, Any]:
    store = BookWorkspace.open(root, create=False)
    with store.locked():
        store._validate_tree()
        counts = {}
        for layer in WORKSPACE_LAYERS:
            directory = store.nodes / layer
            node_files = [path for path in directory.iterdir() if path.is_file() and path.suffix == ".json"]
            for node_path in node_files:
                store.read_node(layer, node_path.stem)
            counts[layer] = len(node_files)
        objects = len([path for path in store.objects.iterdir() if path.is_file()])
        return {
            "status": "READY",
            "workspace_name": WORKSPACE_NAME,
            "schema_version": WORKSPACE_SCHEMA_VERSION,
            "persistent_bytes": _workspace_bytes(store.root),
            "max_bytes": MAX_WORKSPACE_BYTES,
            "object_count": objects,
            "node_counts": counts,
        }


def purge_workspace(root: Path, confirmation: str) -> Dict[str, Any]:
    if confirmation != PURGE_CONFIRMATION:
        raise SkillError("WORKSPACE_INVALID", "清空单书工作区需要精确确认词 PURGE_EBOOK_WORKSPACE。")
    store = BookWorkspace.open(root, create=False)
    moved = store.root.parent / f".{store.root.name}.purge-{uuid.uuid4().hex}"
    with store.locked():
        store._validate_tree()
        try:
            os.replace(store.root, moved)
        except OSError as exc:
            raise SkillError("WORKSPACE_INVALID", "无法隔离待清空的单书工作区。") from exc
    try:
        shutil.rmtree(moved)
    except OSError as exc:
        raise SkillError(
            "WORKSPACE_INVALID",
            "单书工作区未能完整清空。",
            {"recovery_entries": [moved.name]},
        ) from exc
    return {"status": "PURGED", "workspace_name": WORKSPACE_NAME}
