"""Pluggable multi-platform book search.

A *provider* is one book platform (Z-Library, Project Gutenberg, ...).
Each provider is a ``Provider`` subclass living in a single ``.py`` file.

Built-in providers live in ``booksearch/providers/``. User providers are
loaded from ``~/.config/kindle-book-pipeline/providers/*.py`` and from any
directory listed in ``$BOOK_SEARCH_PROVIDER_PATH`` (``os.pathsep``-separated).
A user provider with the same ``name`` as a built-in one replaces it.

Only the Python standard library is used.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
import tempfile
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

__version__ = "0.2.0"

USER_AGENT = "kindle-book-pipeline/book-search (+https://github.com/Ryumuu/kindle-book-pipeline)"
CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "kindle-book-pipeline"
USER_PROVIDER_DIR = CONFIG_HOME / "providers"
BUILTIN_PROVIDER_DIR = Path(__file__).resolve().parent / "providers"


class ProviderError(Exception):
    """A provider-level failure with a short, user-facing message."""


@dataclass
class Book:
    """One search result. ``id`` is opaque: only its provider has to understand it."""

    provider: str
    id: str
    title: str
    authors: List[str] = field(default_factory=list)
    year: str = ""
    language: str = ""
    extension: str = ""
    size: str = ""
    publisher: str = ""
    isbn: str = ""
    url: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def ref(self) -> str:
        """Stable handle accepted by ``book_search.py download``."""
        return f"{self.provider}:{self.id}"

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["ref"] = self.ref
        return data


class Provider:
    """Base class for a book platform. Override ``search`` and ``download``."""

    name: str = ""          # unique id used on the command line, e.g. "zlibrary"
    label: str = ""         # human readable name
    homepage: str = ""
    needs_login: bool = False

    def is_ready(self) -> Tuple[bool, str]:
        """Return (ready, reason). Not-ready providers are skipped by ``search --provider all``."""
        return True, ""

    def info(self) -> Dict[str, Any]:
        """Optional account / quota information."""
        return {}

    def search(
        self,
        query: str,
        *,
        language: Optional[str] = None,
        extension: Optional[str] = None,
        limit: int = 8,
    ) -> List[Book]:
        raise NotImplementedError

    def download(self, book_id: str, dest_dir: Path) -> Path:
        raise NotImplementedError


# --------------------------------------------------------------------------- helpers


def fetch_json(url: str, *, data: Optional[bytes] = None, headers: Optional[Dict[str, str]] = None, timeout: int = 20) -> Any:
    merged = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    merged.update(headers or {})
    request = urllib.request.Request(url, data=data, headers=merged)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


_UNSAFE_FILENAME = re.compile(r'[\\/*?:"<>|\x00-\x1f]')


def safe_filename(stem: str, extension: str) -> str:
    stem = _UNSAFE_FILENAME.sub("_", stem).strip(" .") or "book"
    stem = stem[:150]
    extension = re.sub(r"[^A-Za-z0-9]", "", extension or "") or "bin"
    if stem.lower().endswith("." + extension.lower()):
        return stem
    return f"{stem}.{extension}"


def _check_filename(filename: str) -> str:
    """Only a bare file name is allowed, so a plugin can never write outside ``dest_dir``."""
    name = str(filename or "")
    if (
        not name.strip()
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or "\x00" in name
        or Path(name).name != name
    ):
        raise ProviderError(f"Unsafe download file name: {filename!r}")
    return name


def _claim_path(path: Path) -> Path:
    """Atomically reserve ``path`` (or ``name (2).ext`` ...) as an empty file and return it."""
    for index in range(1, 1000):
        candidate = path if index == 1 else path.with_name(f"{path.stem} ({index}){path.suffix}")
        try:
            fd = os.open(str(candidate), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            continue
        os.close(fd)
        return candidate
    raise ProviderError(f"Too many files named {path.name}")


def stream_download(url: str, dest_dir: Path, filename: str, *, headers: Optional[Dict[str, str]] = None, timeout: int = 60) -> Path:
    """Download to ``<dest_dir>/<filename>`` via a temp file; never overwrites an existing file."""
    filename = _check_filename(filename)
    dest_dir = Path(dest_dir).expanduser()
    dest_dir.mkdir(parents=True, exist_ok=True)
    merged = {"User-Agent": USER_AGENT}
    merged.update(headers or {})
    request = urllib.request.Request(url, headers=merged)
    target: Optional[Path] = None
    fd, tmp_name = tempfile.mkstemp(prefix=".download-", suffix=".part", dir=str(dest_dir))
    try:
        with os.fdopen(fd, "wb") as out, urllib.request.urlopen(request, timeout=timeout) as response:
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                if sys.stderr.isatty():
                    shown = f"{done / 1048576:.1f}MB" + (f" / {total / 1048576:.1f}MB" if total else "")
                    sys.stderr.write(f"\rDownloading {shown}")
                    sys.stderr.flush()
        if sys.stderr.isatty():
            sys.stderr.write("\n")
        if done == 0:
            raise ProviderError("Downloaded file is empty")
        if total and done != total:
            raise ProviderError(f"Download incomplete: got {done} of {total} bytes")
        os.chmod(tmp_name, 0o644)  # mkstemp creates 0600
        target = _claim_path(dest_dir / filename)
        os.replace(tmp_name, target)
        return target
    except BaseException:
        for leftover in (tmp_name, target):
            if leftover is None:
                continue
            try:
                os.unlink(leftover)
            except FileNotFoundError:
                pass
        raise


# --------------------------------------------------------------------------- registry


def provider_dirs() -> List[Path]:
    dirs = [BUILTIN_PROVIDER_DIR, USER_PROVIDER_DIR]
    for raw in os.environ.get("BOOK_SEARCH_PROVIDER_PATH", "").split(os.pathsep):
        if raw.strip():
            dirs.append(Path(raw).expanduser())
    return dirs


def _load_file(path: Path) -> List[Provider]:
    module_name = f"booksearch_provider_{abs(hash(str(path)))}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        return []
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    found = []
    for value in vars(module).values():
        if isinstance(value, type) and issubclass(value, Provider) and value is not Provider and value.name:
            if value.__module__ == module_name:
                found.append(value())
    return found


def load_providers(warn=None) -> Dict[str, Provider]:
    """Return ``{name: provider}``; later directories override earlier ones."""
    registry: Dict[str, Provider] = {}
    for directory in provider_dirs():
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.py")):
            if path.name.startswith("_"):
                continue
            try:
                for provider in _load_file(path):
                    provider.source_path = str(path)  # type: ignore[attr-defined]
                    registry[provider.name] = provider
            except Exception as exc:  # one broken plugin must not break the others
                if warn:
                    warn(f"skip provider file {path}: {type(exc).__name__}: {exc}")
    return registry
