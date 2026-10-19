#!/usr/bin/env python3
"""Install pinned, local-only dependencies under one private runtime root."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import uuid
import zipfile
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener
from urllib.parse import urlparse


SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

SCRIPT_INTERFACE = "cli"
SCRIPT_INTERFACE_REASON = "Confirmed, hash-pinned installer for the private local runtime."

from ebook_kindle.core import SkillError, atomic_write_json, ensure_private_dir, minimal_subprocess_env, sha256_file  # noqa: E402
from ebook_kindle.contract import (  # noqa: E402
    MAX_PERSISTENT_BYTES,
    MODEL_FILES,
    MODEL_HASHES,
    RUNTIME_NAME,
    RUNTIME_SCHEMA_VERSION,
    RUNTIME_VERSION,
)
from ebook_kindle.runtime import (  # noqa: E402
    RUNTIME_LOCK_NAME,
    default_runtime_root,
    guard_runtime_root,
    load_manifest,
    runtime_operation_lock,
    verify_runtime,
)


MIN_FREE_BYTES = 7 * 1024**3
ASSETS: Dict[str, Dict[str, object]] = {
    "calibre": {
        "version": "9.13.0",
        "url": "https://github.com/kovidgoyal/calibre/releases/download/v9.13.0/calibre-9.13.0.dmg",
        "sha256": "001be6ed70d8acfd793fa7d8c95ea50045abedaf0e756b20e564275a0b0a7667",
        "filename": "calibre-9.13.0.dmg",
        "bytes": 345669486,
    },
    "epubcheck": {
        "version": "5.3.0",
        "url": "https://github.com/w3c/epubcheck/releases/download/v5.3.0/epubcheck-5.3.0.zip",
        "sha256": "6c07e68584b2e2ce2f89fe06e1246dfead3eb36b46b340e7d93524f29dcff6c5",
        "filename": "epubcheck-5.3.0.zip",
        "bytes": 33071108,
    },
    "temurin": {
        "version": "21.0.12+8",
        "url": "https://github.com/adoptium/temurin21-binaries/releases/download/jdk-21.0.12%2B8/OpenJDK21U-jre_aarch64_mac_hotspot_21.0.12_8.tar.gz",
        "sha256": "36bb71d6fa5184e12a6483e7662783c2cbd383f5dca8034140f0a84dd5aa797d",
        "filename": "OpenJDK21U-jre_aarch64_mac_hotspot_21.0.12_8.tar.gz",
        "bytes": 48155581,
    },
    "python": {
        "version": "3.12.13+20260807",
        "url": "https://github.com/astral-sh/python-build-standalone/releases/download/20260807/cpython-3.12.13%2B20260807-aarch64-apple-darwin-install_only_stripped.tar.gz",
        "sha256": "25baa97c65b3f0aa90e21131b4f9e80aef8899e8144006db8a9d2c1ab9e807e3",
        "filename": "cpython-3.12.13+20260807-aarch64-apple-darwin-install_only_stripped.tar.gz",
    },
}
DIRECT_DOWNLOAD_HOSTS = frozenset({"github.com", "release-assets.githubusercontent.com"})
ALLOWED_RUNTIME_ENTRIES = frozenset({"runtime.json", "core", "pdf", "jobs", "packages", RUNTIME_LOCK_NAME})


def _platform_check() -> None:
    if sys.platform != "darwin" or platform.machine() != "arm64":
        raise SkillError("UNSUPPORTED_PLATFORM", "当前安装器仅支持 Apple Silicon macOS。")
    version = platform.mac_ver()[0].split(".")
    if not version or int(version[0] or "0") < 14:
        raise SkillError("UNSUPPORTED_PLATFORM", "Calibre 9.13.0 需要 macOS 14 或更高版本。")


def _guard_install_root(root: Path) -> None:
    if root.name != "ebook-to-kindle":
        raise SkillError("INSTALL_CONFLICT", "运行时目录名必须为 ebook-to-kindle。")
    if not root.exists():
        return
    if not root.is_dir():
        raise SkillError("INSTALL_CONFLICT", "运行时目标不是目录。")
    entries = {item.name for item in root.iterdir()} - {RUNTIME_LOCK_NAME}
    if not entries:
        return
    if not entries.issubset(ALLOWED_RUNTIME_ENTRIES):
        raise SkillError("INSTALL_CONFLICT", "运行时目录包含不属于本 Skill 的条目。")
    manifest = load_manifest(root)
    if (
        manifest.get("schema_version") != RUNTIME_SCHEMA_VERSION
        or manifest.get("runtime_version") != RUNTIME_VERSION
        or manifest.get("runtime_name") != RUNTIME_NAME
    ):
        raise SkillError("INSTALL_CONFLICT", "运行时目录缺少正确身份清单。")


def _ensure_runtime_identity_shell(root: Path) -> None:
    """Create the minimal retryable identity before any fallible install gate."""
    manifest_path = root / "runtime.json"
    if manifest_path.exists():
        return
    atomic_write_json(
        manifest_path,
        {
            "schema_version": RUNTIME_SCHEMA_VERSION,
            "runtime_version": RUNTIME_VERSION,
            "runtime_name": RUNTIME_NAME,
            "components": {},
            "rollback_shell": True,
        },
    )


def _remove_tree_strict(path: Path) -> None:
    if not os.path.lexists(path):
        return
    if path.is_symlink() or not path.is_dir():
        raise SkillError(
            "ROLLBACK_INCOMPLETE",
            "回滚目标类型异常，已停止删除。",
            {"recovery_entries": [path.name]},
        )
    try:
        shutil.rmtree(path)
    except OSError as exc:
        raise SkillError(
            "ROLLBACK_INCOMPLETE",
            "回滚目录未能完整删除。",
            {"recovery_entries": [path.name]},
        ) from exc
    if os.path.lexists(path):
        raise SkillError(
            "ROLLBACK_INCOMPLETE",
            "回滚目录仍然存在。",
            {"recovery_entries": [path.name]},
        )


def _rollback_components(
    root: Path,
    components: Sequence[str],
    remove_root: bool,
    preexisting_entries: Sequence[str] = (),
) -> None:
    manifest = _load_runtime_manifest(root)
    manifest_components = manifest.get("components")
    if not isinstance(manifest_components, dict):
        manifest_components = {}
        manifest["components"] = manifest_components
    cleanup_failures: List[str] = []
    for component in reversed(list(components)):
        try:
            _remove_tree_strict(root / component)
        except SkillError:
            cleanup_failures.append(component)
            # Keep a removable identity even when an install failed before it
            # had written this component into the manifest.
            manifest_components.setdefault(component, {"rollback_incomplete": True})
        else:
            manifest_components.pop(component, None)
    preexisting = set(preexisting_entries)
    auxiliary_cleanup_failures: List[str] = []
    for name in ("jobs", "packages"):
        path = root / name
        if name not in preexisting and os.path.lexists(path):
            try:
                _remove_tree_strict(path)
            except SkillError:
                auxiliary_cleanup_failures.append(name)
    recovery_entries = cleanup_failures + auxiliary_cleanup_failures
    manifest_path = root / "runtime.json"
    try:
        if manifest_components:
            if recovery_entries:
                manifest["rollback_shell"] = True
            atomic_write_json(manifest_path, manifest)
        elif recovery_entries or "runtime.json" in preexisting or (remove_root and (root / RUNTIME_LOCK_NAME).is_file()):
            # A fixed flock inode must not be unlinked after release: another
            # process could already hold it. Keep a tiny identified shell that
            # is safe to retry or remove with the guarded uninstaller.
            atomic_write_json(
                manifest_path,
                {
                    "schema_version": RUNTIME_SCHEMA_VERSION,
                    "runtime_version": RUNTIME_VERSION,
                    "runtime_name": RUNTIME_NAME,
                    "components": {},
                    "rollback_shell": True,
                },
            )
        elif manifest_path.exists():
            manifest_path.unlink()
    except (OSError, SkillError):
        recovery_entries.append(manifest_path.name)
    remaining: List[Path] = []
    if root.exists():
        try:
            remaining = [item for item in root.iterdir() if item.name != RUNTIME_LOCK_NAME]
        except OSError:
            recovery_entries.append(root.name)
    if remove_root and root.exists() and not recovery_entries and not remaining and not (root / RUNTIME_LOCK_NAME).exists():
        try:
            root.rmdir()
        except OSError:
            recovery_entries.append(root.name)
    if recovery_entries:
        raise SkillError(
            "ROLLBACK_INCOMPLETE",
            "安装失败且回滚未完成；已保留身份和全部恢复条目。",
            {"recovery_entries": sorted(set(recovery_entries))},
        )


def _download(asset: Mapping[str, object], destination: Path) -> None:
    expected = str(asset["sha256"])
    url = str(asset["url"])
    request = Request(url, headers={"User-Agent": "ebook-to-kindle/0.1"})
    digest = hashlib.sha256()
    received = 0
    opener = build_opener(ProxyHandler({}))
    with opener.open(request, timeout=60) as response, destination.open("wb") as handle:
        final_host = (urlparse(response.geturl()).hostname or "").lower()
        if urlparse(response.geturl()).scheme.lower() != "https" or final_host not in DIRECT_DOWNLOAD_HOSTS:
            raise SkillError("DOWNLOAD_HOST_DENIED", "依赖下载被重定向到未批准的主机。")
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            received += len(chunk)
            if received > 2 * 1024**3:
                raise SkillError("DOWNLOAD_LIMIT", "单个依赖下载超过 2GiB 上限。")
            digest.update(chunk)
            handle.write(chunk)
            if received % (32 * 1024**2) < len(chunk):
                print(f"downloaded {destination.name}: {received // (1024**2)} MiB", flush=True)
    os.chmod(destination, 0o600)
    if digest.hexdigest() != expected:
        raise SkillError("CHECKSUM_MISMATCH", f"{destination.name} SHA-256 校验失败。")


def _safe_tar_extract(archive: Path, destination: Path) -> None:
    destination_resolved = destination.resolve()
    with tarfile.open(archive) as bundle:
        for member in bundle.getmembers():
            target = (destination / member.name).resolve()
            if destination_resolved not in target.parents and target != destination_resolved:
                raise SkillError("ARCHIVE_UNSAFE", "tar 包含越界路径。")
            if member.issym() or member.islnk():
                link_target = (target.parent / member.linkname).resolve()
                if destination_resolved not in link_target.parents and link_target != destination_resolved:
                    raise SkillError("ARCHIVE_UNSAFE", "tar 包含越界符号链接。")
        bundle.extractall(destination)


def _safe_zip_extract(archive: Path, destination: Path) -> None:
    destination_resolved = destination.resolve()
    with zipfile.ZipFile(archive) as bundle:
        for info in bundle.infolist():
            target = (destination / info.filename).resolve()
            if destination_resolved not in target.parents and target != destination_resolved:
                raise SkillError("ARCHIVE_UNSAFE", "zip 包含越界路径。")
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise SkillError("ARCHIVE_UNSAFE", "zip 包含符号链接。")
        bundle.extractall(destination)


def _command(command: Sequence[str], *, env: Optional[Mapping[str, str]] = None, cwd: Optional[Path] = None, timeout: int = 1800) -> str:
    result = subprocess.run(
        list(command),
        env=dict(env) if env is not None else minimal_subprocess_env(),
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        raise SkillError("INSTALL_FAILED", f"{Path(command[0]).name} 返回 {result.returncode}。")
    return result.stdout


def _build_install_env(private_root: Path) -> Dict[str, str]:
    """Keep third-party installer state inside a disposable runtime subtree."""
    env = minimal_subprocess_env()
    install_home = private_root / "home"
    paths = {
        "HOME": install_home,
        "CFFIXED_USER_HOME": install_home,
        "TMPDIR": private_root / "tmp",
        "XDG_CACHE_HOME": private_root / "xdg-cache",
        "XDG_CONFIG_HOME": private_root / "xdg-config",
        "XDG_DATA_HOME": private_root / "xdg-data",
    }
    for key, target in paths.items():
        ensure_private_dir(target)
        env[key] = str(target)
    return env


def _load_runtime_manifest(root: Path) -> Dict[str, object]:
    path = root / "runtime.json"
    if not path.is_file():
        return {
            "schema_version": RUNTIME_SCHEMA_VERSION,
            "runtime_version": RUNTIME_VERSION,
            "runtime_name": RUNTIME_NAME,
            "components": {},
        }
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        value = {}
    if not isinstance(value, dict):
        value = {}
    value.setdefault("schema_version", RUNTIME_SCHEMA_VERSION)
    value.setdefault("runtime_version", RUNTIME_VERSION)
    value.setdefault("runtime_name", RUNTIME_NAME)
    value.setdefault("components", {})
    return value


def _write_manifest(root: Path, component: str, payload: Mapping[str, object]) -> None:
    manifest = _load_runtime_manifest(root)
    components = manifest.get("components")
    if not isinstance(components, dict):
        components = {}
        manifest["components"] = components
    components[component] = dict(payload)
    manifest.pop("persistent_bytes", None)
    manifest.pop("rollback_shell", None)
    atomic_write_json(root / "runtime.json", manifest)


def install_core(root: Path) -> bool:
    target = root / "core"
    if target.exists():
        if verify_runtime(root, SKILL_ROOT)["core_verified"]:
            print("core already ready")
            return False
        raise SkillError("INSTALL_CONFLICT", "core 目录存在但不完整；先运行卸载器或手工审计。")
    staging = root / f".core-installing-{uuid.uuid4().hex}"
    downloads = staging / "downloads"
    mountpoint = staging / "mount"
    install_env_root = staging / "install-env"
    committed = False
    try:
        ensure_private_dir(downloads)
        core_env = _build_install_env(install_env_root)
        for name in ("calibre", "epubcheck", "temurin"):
            asset = ASSETS[name]
            _download(asset, downloads / str(asset["filename"]))

        ensure_private_dir(mountpoint)
        calibre_dmg = downloads / str(ASSETS["calibre"]["filename"])
        _command(
            ["/usr/bin/hdiutil", "attach", "-readonly", "-nobrowse", "-mountpoint", str(mountpoint), str(calibre_dmg)],
            env=core_env,
            timeout=180,
        )
        try:
            app = mountpoint / "calibre.app"
            if not app.is_dir():
                raise SkillError("INSTALL_FAILED", "Calibre DMG 缺少 calibre.app。")
            shutil.copytree(app, staging / "Calibre.app", symlinks=True)
        finally:
            _command(["/usr/bin/hdiutil", "detach", str(mountpoint)], env=core_env, timeout=180)

        epub_unpack = staging / "epub-unpack"
        ensure_private_dir(epub_unpack)
        _safe_zip_extract(downloads / str(ASSETS["epubcheck"]["filename"]), epub_unpack)
        epub_roots = [path for path in epub_unpack.iterdir() if path.is_dir() and (path / "epubcheck.jar").is_file()]
        if len(epub_roots) != 1:
            raise SkillError("INSTALL_FAILED", "EPUBCheck 压缩包结构异常。")
        shutil.copytree(epub_roots[0], staging / "epubcheck")

        jre_unpack = staging / "jre-unpack"
        ensure_private_dir(jre_unpack)
        _safe_tar_extract(downloads / str(ASSETS["temurin"]["filename"]), jre_unpack)
        jre_roots = [path for path in jre_unpack.iterdir() if path.is_dir() and (path / "Contents" / "Home" / "bin" / "java").is_file()]
        if len(jre_roots) != 1:
            raise SkillError("INSTALL_FAILED", "Temurin 压缩包结构异常。")
        shutil.copytree(jre_roots[0] / "Contents", staging / "jre" / "Contents", symlinks=True)

        for disposable in (downloads, mountpoint, epub_unpack, jre_unpack, install_env_root):
            _remove_tree_strict(disposable)
        if not (staging / "Calibre.app" / "Contents" / "MacOS" / "ebook-convert").is_file():
            raise SkillError("INSTALL_FAILED", "Calibre CLI 缺失。")
        os.replace(str(staging), str(target))
        committed = True
        _write_manifest(
            root,
            "core",
            {
                "calibre": {"version": ASSETS["calibre"]["version"], "sha256": ASSETS["calibre"]["sha256"]},
                "epubcheck": {"version": ASSETS["epubcheck"]["version"], "sha256": ASSETS["epubcheck"]["sha256"]},
                "temurin_jre": {"version": ASSETS["temurin"]["version"], "sha256": ASSETS["temurin"]["sha256"]},
            },
        )
        return True
    except Exception as original:
        recovery_entries: List[str] = []
        cleanup_targets = [staging]
        if committed:
            cleanup_targets.append(target)
        for cleanup_target in cleanup_targets:
            try:
                _remove_tree_strict(cleanup_target)
            except SkillError:
                recovery_entries.append(cleanup_target.name)
        if recovery_entries:
            raise SkillError(
                "ROLLBACK_INCOMPLETE",
                "core 安装失败且临时或已提交目录未能回滚。",
                {"recovery_entries": sorted(set(recovery_entries))},
            ) from original
        raise


def _verify_model_files(root: Path) -> Dict[str, str]:
    verified: Dict[str, str] = {}
    for relative, expected in MODEL_FILES.items():
        path = root / relative
        if not path.is_file() or sha256_file(path) != expected:
            raise SkillError("CHECKSUM_MISMATCH", "Docling 模型缺少固定哈希。", {"model": Path(relative).name})
        verified[relative] = expected
    return verified


def install_pdf(root: Path) -> bool:
    if not verify_runtime(root, SKILL_ROOT)["core_verified"]:
        raise SkillError("DEPENDENCY_MISSING", "安装 PDF 组件前必须先安装 core。")
    target = root / "pdf"
    if target.exists():
        if verify_runtime(root, SKILL_ROOT)["pdf_verified"]:
            print("pdf already ready")
            return False
        raise SkillError("INSTALL_CONFLICT", "pdf 目录存在但不完整；先运行卸载器或手工审计。")
    uv = shutil.which("uv")
    if not uv:
        raise SkillError("DEPENDENCY_MISSING", "未找到 uv；安装器不会修改系统环境。")
    cache = target / "install-cache"
    try:
        ensure_private_dir(target)
        ensure_private_dir(cache)
        python_archive = cache / str(ASSETS["python"]["filename"])
        _download(ASSETS["python"], python_archive)
        python_unpack = cache / "python-unpack"
        ensure_private_dir(python_unpack)
        _safe_tar_extract(python_archive, python_unpack)
        candidates = [path for path in python_unpack.iterdir() if path.is_dir() and (path / "bin" / "python3.12").is_file()]
        if len(candidates) != 1:
            raise SkillError("INSTALL_FAILED", "CPython 压缩包结构异常。")
        shutil.move(str(candidates[0]), str(target / "python"))

        uv_env = _build_install_env(cache / "install-env")
        uv_env.update(
            {
                "UV_CACHE_DIR": str(cache / "uv"),
                "UV_LINK_MODE": "copy",
                "UV_NO_PROGRESS": "1",
                "UV_NO_CONFIG": "1",
                "UV_INDEX_URL": "https://pypi.org/simple",
                "PIP_DISABLE_PIP_VERSION_CHECK": "1",
                "PYTHONNOUSERSITE": "1",
            }
        )
        python = target / "python" / "bin" / "python3.12"
        _command([uv, "venv", "--no-project", "--relocatable", "--python", str(python), str(target / "venv")], env=uv_env, timeout=300)
        venv_python = target / "venv" / "bin" / "python"
        dependency_lock = SKILL_ROOT / "assets" / "docling-requirements.lock"
        if not dependency_lock.is_file():
            raise SkillError("DEPENDENCY_MISSING", "缺少 Docling 版本锁文件。")
        _command(
            [uv, "pip", "install", "--python", str(venv_python), "--no-cache", "--no-config", "--index-url", "https://pypi.org/simple", "-r", str(dependency_lock)],
            env=uv_env,
            timeout=2400,
        )

        models = target / "models"
        ensure_private_dir(models)
        model_env = uv_env.copy()
        model_env.update(
            {
                "DOCLING_ARTIFACTS_PATH": str(models),
                "DOCLING_CACHE_DIR": str(cache / "docling"),
                "HF_HOME": str(cache / "huggingface"),
                "HF_ENDPOINT": "https://huggingface.co",
                "HF_HUB_DISABLE_XET": "1",
                "TORCH_HOME": str(cache / "torch"),
                "HF_HUB_DISABLE_TELEMETRY": "1",
                "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1",
                "DO_NOT_TRACK": "1",
            }
        )
        docling_tools = target / "venv" / "bin" / "docling-tools"
        _command([str(docling_tools), "models", "download", "layout", "tableformer", "-o", str(models)], env=model_env, timeout=3600)
        matches = _verify_model_files(models)
        _command([str(venv_python), "-I", "-c", "import docling, ocrmac, pypdfium2; print(docling.__version__)"] , env=model_env, timeout=120)
        frozen = _command([uv, "pip", "freeze", "--python", str(venv_python), "--no-config"], env=uv_env, timeout=180)
        frozen_lines = [line.strip() for line in frozen.splitlines() if line.strip() and not line.startswith("Using Python ")]
        (target / "requirements.lock").write_text("\n".join(frozen_lines) + "\n", encoding="utf-8")
        os.chmod(target / "requirements.lock", 0o600)
        installer_version = _command([uv, "--version"], env=uv_env, timeout=30).strip()
        _remove_tree_strict(cache)
        _write_manifest(
            root,
            "pdf",
            {
                "python": {"version": ASSETS["python"]["version"], "sha256": ASSETS["python"]["sha256"]},
                "docling": "2.119.0",
                "ocrmac": "1.0.1",
                "requirements_lock_sha256": sha256_file(dependency_lock),
                "runtime_freeze_sha256": sha256_file(target / "requirements.lock"),
                "model_hashes": MODEL_HASHES,
                "model_files": MODEL_FILES,
                "verified_model_files": matches,
                "installer": installer_version,
            },
        )
        return True
    except Exception as original:
        try:
            _remove_tree_strict(target)
        except SkillError as rollback_error:
            raise SkillError(
                "ROLLBACK_INCOMPLETE",
                "PDF 安装失败且目标目录未能回滚。",
                {"recovery_entries": [target.name]},
            ) from rollback_error
        raise


def plan(component: str, root: Path) -> Dict[str, object]:
    names = ["calibre", "epubcheck", "temurin"] if component == "core" else ["python"]
    if component == "all":
        names = ["calibre", "epubcheck", "temurin", "python"]
    return {
        "component": component,
        "runtime_path": str(root),
        "downloads": [
            {"name": name, "version": ASSETS[name]["version"], "bytes": ASSETS[name].get("bytes"), "sha256": ASSETS[name]["sha256"]}
            for name in names
        ],
        "network_for_pdf_models": component in {"pdf", "all"},
        "persistent_limit_bytes": MAX_PERSISTENT_BYTES,
    }


def _install_requested(
    root: Path,
    component: str,
    remove_root_on_failure: bool,
    preexisting_entries: Sequence[str] = (),
) -> Dict[str, object]:
    created: List[str] = []
    try:
        if component in {"core", "all"}:
            if install_core(root):
                created.append("core")
            core_status = verify_runtime(root, SKILL_ROOT)
            if not core_status["core_verified"]:
                raise SkillError("INSTALL_FAILED", "core 安装后未通过版本、清单或容量核验。")
        if component in {"pdf", "all"}:
            if install_pdf(root):
                created.append("pdf")
            pdf_status = verify_runtime(root, SKILL_ROOT)
            if not pdf_status["pdf_verified"]:
                raise SkillError("INSTALL_FAILED", "PDF 运行时安装后未通过版本、锁文件或模型哈希核验。")
        status = verify_runtime(root, SKILL_ROOT)
        if not status["deep_ready"]:
            raise SkillError("INSTALL_FAILED", "运行时未通过最终深度核验。")
        return status
    except Exception as original:
        try:
            _rollback_components(root, created, remove_root_on_failure, preexisting_entries)
        except SkillError as rollback_error:
            raise rollback_error from original
        raise


def _main() -> int:
    parser = argparse.ArgumentParser(description="Install the isolated ebook-to-kindle runtime.")
    parser.add_argument("--component", choices=("core", "pdf", "all"), default="all")
    parser.add_argument("--runtime", type=Path, default=default_runtime_root())
    parser.add_argument("--accept-download", action="store_true", help="Required before network downloads.")
    parser.add_argument("--print-plan", action="store_true")
    args = parser.parse_args()
    try:
        root = guard_runtime_root(args.runtime)
        _guard_install_root(root)
    except SkillError as exc:
        print(json.dumps({"status": "FAIL", "error_code": exc.code, "message": exc.message}, ensure_ascii=False, indent=2, sort_keys=True))
        return 1
    if args.print_plan:
        print(json.dumps(plan(args.component, root), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if not args.accept_download:
        print(json.dumps({"status": "FAIL", "error_code": "CONFIRM_DOWNLOAD_REQUIRED"}, ensure_ascii=False))
        return 2
    try:
        _platform_check()
        root_existed = root.exists()
        preexisting_entries = [item.name for item in root.iterdir()] if root_existed else []
        ensure_private_dir(root)
        with runtime_operation_lock(root):
            _guard_install_root(root)
            _ensure_runtime_identity_shell(root)
            if shutil.disk_usage(root).free < MIN_FREE_BYTES:
                raise SkillError("DISK_LIMIT", "可用空间低于 7GiB，停止安装。")
            status = _install_requested(root, args.component, not root_existed, preexisting_entries)
        print(json.dumps({"status": "PASS", "component": args.component, **status}, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (SkillError, subprocess.TimeoutExpired, OSError, URLError) as exc:
        code = exc.code if isinstance(exc, SkillError) else "INSTALL_FAILED"
        message = exc.message if isinstance(exc, SkillError) else type(exc).__name__
        print(json.dumps({"status": "FAIL", "error_code": code, "message": message}, ensure_ascii=False, indent=2, sort_keys=True))
        return 1


def main() -> int:
    try:
        return _main()
    except Exception:
        print(json.dumps({
            "status": "FAIL",
            "error_code": "INSTALL_FAILED",
            "message": "安装无法安全完成。",
        }, ensure_ascii=False, indent=2, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
