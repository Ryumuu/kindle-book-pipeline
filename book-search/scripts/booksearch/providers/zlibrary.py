"""Z-Library provider (uses your own account and daily download quota).

Credentials come from, in order:
  1. env ``ZLIB_REMIX_USERID`` / ``ZLIB_REMIX_USERKEY`` (and optional ``ZLIB_BASE_URL``)
  2. ``~/.config/zlibrary/config.json`` (written by ``book_search.py zlib-sync-cookie``)
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from booksearch import Book, Provider, ProviderError, fetch_json, safe_filename, stream_download

CONFIG_FILE = Path(os.path.expanduser("~/.config/zlibrary/config.json"))
DEFAULT_BASE_URL = "https://z-lib.by"


def load_config() -> Dict[str, Any]:
    config: Dict[str, Any] = {"base_url": DEFAULT_BASE_URL, "remix_userid": "", "remix_userkey": ""}
    if CONFIG_FILE.is_file():
        try:
            config.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    for key, env in (("remix_userid", "ZLIB_REMIX_USERID"), ("remix_userkey", "ZLIB_REMIX_USERKEY"), ("base_url", "ZLIB_BASE_URL")):
        if os.environ.get(env):
            config[key] = os.environ[env]
    config["base_url"] = str(config.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
    return config


def save_config(config: Dict[str, Any]) -> None:
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(CONFIG_FILE), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2, ensure_ascii=False)
    os.chmod(CONFIG_FILE, 0o600)


def sync_cookie_from_chrome() -> Tuple[bool, str]:
    """macOS only: read remix_userid/remix_userkey from the active Chrome tab.

    Requires Chrome > View > Developer > "Allow JavaScript from Apple Events",
    and the active tab must be a logged-in Z-Library page.
    """
    if sys.platform != "darwin":
        return False, "sync-cookie only works on macOS with Google Chrome."
    script = 'tell application "Google Chrome" to execute active tab of first window javascript "document.cookie"'
    try:
        proc = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, check=True, timeout=15)
    except (subprocess.SubprocessError, OSError) as exc:
        return False, f"Could not read cookies from Chrome: {type(exc).__name__}"
    userid = re.search(r"remix_userid=([0-9]+)", proc.stdout)
    userkey = re.search(r"remix_userkey=([a-zA-Z0-9]+)", proc.stdout)
    if not (userid and userkey):
        return False, "remix_userid / remix_userkey not found. Open a logged-in Z-Library tab first."
    config = load_config()
    config["remix_userid"], config["remix_userkey"] = userid.group(1), userkey.group(1)
    save_config(config)
    return True, f"Saved credentials to {CONFIG_FILE} (mode 600)."


class ZLibraryProvider(Provider):
    name = "zlibrary"
    label = "Z-Library"
    homepage = DEFAULT_BASE_URL
    needs_login = True

    def _config(self) -> Dict[str, Any]:
        return load_config()

    def _headers(self, config: Dict[str, Any]) -> Dict[str, str]:
        return {"Cookie": f"remix_userid={config['remix_userid']}; remix_userkey={config['remix_userkey']}"}

    def is_ready(self) -> Tuple[bool, str]:
        config = self._config()
        if config.get("remix_userid") and config.get("remix_userkey"):
            return True, ""
        return False, "not logged in: set ZLIB_REMIX_USERID/ZLIB_REMIX_USERKEY or run `zlib-sync-cookie`"

    def _require(self) -> Dict[str, Any]:
        ready, reason = self.is_ready()
        if not ready:
            raise ProviderError(reason)
        return self._config()

    def info(self) -> Dict[str, Any]:
        config = self._require()
        data = fetch_json(f"{config['base_url']}/eapi/user/profile", headers=self._headers(config), timeout=15)
        if data.get("success") != 1:
            raise ProviderError(f"API error: {data.get('error') or 'unknown'}")
        user = data.get("user", {})
        used = int(user.get("downloads_today") or 0)
        limit = int(user.get("downloads_limit") or 10)
        return {
            "account": user.get("name", ""),
            "downloads_today": used,
            "downloads_limit": limit,
            "downloads_remaining": max(0, limit - used),
            "premium": bool(user.get("isPremium")),
        }

    def search(self, query: str, *, language: Optional[str] = None, extension: Optional[str] = None, limit: int = 8) -> List[Book]:
        config = self._require()
        payload = {"message": query, "limit": limit}
        if language:
            payload["languages[]"] = language
        if extension:
            payload["extensions[]"] = extension
        headers = self._headers(config)
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        data = fetch_json(
            f"{config['base_url']}/eapi/book/search",
            data=urllib.parse.urlencode(payload).encode("utf-8"),
            headers=headers,
        )
        if data.get("success") != 1:
            raise ProviderError(f"search error: {data.get('error') or 'unknown'}")
        books = []
        for item in data.get("books", [])[:limit]:
            if not item.get("id") or not item.get("hash"):
                continue
            books.append(
                Book(
                    provider=self.name,
                    id=f"{item['id']}/{item['hash']}",
                    title=item.get("title") or "Unknown title",
                    authors=[a.strip() for a in str(item.get("author") or "").split(";") if a.strip()],
                    year=str(item.get("year") or ""),
                    language=str(item.get("language") or ""),
                    extension=str(item.get("extension") or "").lower(),
                    size=str(item.get("filesizeString") or ""),
                    publisher=str(item.get("publisher") or ""),
                    isbn=str(item.get("identifier") or ""),
                    url=f"{config['base_url']}{item['href']}" if item.get("href") else "",
                )
            )
        return books

    def download(self, book_id: str, dest_dir: Path) -> Path:
        config = self._require()
        book_number, _, book_hash = book_id.partition("/")
        if not (book_number.isdigit() and re.fullmatch(r"[A-Za-z0-9]+", book_hash or "")):
            raise ProviderError("Z-Library id must look like <number>/<hash>, as printed by `search`.")
        meta = fetch_json(f"{config['base_url']}/eapi/book/{book_number}/{book_hash}/file", headers=self._headers(config), timeout=15)
        if meta.get("success") != 1:
            raise ProviderError(f"could not get download link: {meta.get('error') or 'unknown'}")
        info = meta.get("file", {})
        url = info.get("downloadLink")
        if not url:
            raise ProviderError("download link missing (daily quota used up?)")
        filename = safe_filename(info.get("description") or f"book_{book_number}", info.get("extension") or "bin")
        return stream_download(url, dest_dir, filename)
