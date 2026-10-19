"""Template for your own book platform.

1. Copy this file to ~/.config/kindle-book-pipeline/providers/my_provider.py
   (or any folder listed in $BOOK_SEARCH_PROVIDER_PATH).
2. Change ``name`` and fill in ``search`` / ``download``.
3. Check it is picked up:  python3 book_search.py providers

Use the same ``name`` as a built-in provider to replace it.
Only the Python standard library is guaranteed to be available.

Security: a provider is ordinary Python that runs with your user rights. It can
read and write your files, read environment variables and use the network, and
it runs as soon as book_search.py lists providers. Only install provider files
you wrote yourself or have read and trust.
"""

from __future__ import annotations

import urllib.parse
from pathlib import Path
from typing import List, Optional

from booksearch import Book, Provider, ProviderError, fetch_json, safe_filename, stream_download


class MyProvider(Provider):
    name = "myshelf"                 # used as --provider myshelf and in refs like myshelf:123
    label = "My Shelf"
    homepage = "https://example.org"
    needs_login = False

    def is_ready(self):
        # Return (False, "why") if e.g. an API key is missing; search --provider all will skip it.
        return True, ""

    def search(self, query: str, *, language: Optional[str] = None, extension: Optional[str] = None, limit: int = 8) -> List[Book]:
        data = fetch_json("https://example.org/api/search?" + urllib.parse.urlencode({"q": query}))
        return [
            Book(
                provider=self.name,
                id=str(item["id"]),           # anything your download() understands
                title=item["title"],
                authors=item.get("authors", []),
                extension=item.get("format", "epub"),
            )
            for item in data.get("items", [])[:limit]
        ]

    def download(self, book_id: str, dest_dir: Path) -> Path:
        item = fetch_json(f"https://example.org/api/books/{urllib.parse.quote(book_id)}")
        if not item.get("download_url"):
            raise ProviderError("no download link")
        return stream_download(item["download_url"], dest_dir, safe_filename(item["title"], item.get("format", "epub")))
