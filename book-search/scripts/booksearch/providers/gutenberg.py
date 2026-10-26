"""Project Gutenberg provider via the public Gutendex API (no login, public-domain books)."""

from __future__ import annotations

import urllib.parse
from pathlib import Path
from typing import List, Optional

from booksearch import Book, Provider, ProviderError, fetch_json, safe_filename, stream_download

API = "https://gutendex.com/books/"
EPUB_MIME = "application/epub+zip"

LANGUAGE_CODES = {
    "english": "en", "chinese": "zh", "japanese": "ja", "french": "fr", "german": "de",
    "spanish": "es", "italian": "it", "portuguese": "pt", "russian": "ru", "dutch": "nl",
}


def _language_code(language: Optional[str]) -> Optional[str]:
    if not language:
        return None
    value = language.strip().lower()
    return LANGUAGE_CODES.get(value, value if len(value) == 2 else None)


def _author_name(raw: str) -> str:
    # Gutendex uses "Austen, Jane"; show "Jane Austen".
    last, sep, first = raw.partition(", ")
    return f"{first} {last}".strip() if sep else raw


class GutenbergProvider(Provider):
    name = "gutenberg"
    label = "Project Gutenberg"
    homepage = "https://www.gutenberg.org"

    def search(self, query: str, *, language: Optional[str] = None, extension: Optional[str] = None, limit: int = 8) -> List[Book]:
        if extension and extension.lower() != "epub":
            return []  # this provider only serves EPUB
        params = {"search": query}
        code = _language_code(language)
        if code:
            params["languages"] = code
        data = fetch_json(f"{API}?{urllib.parse.urlencode(params)}", timeout=60)
        books = []
        for item in data.get("results", []):
            if EPUB_MIME not in (item.get("formats") or {}):
                continue
            books.append(
                Book(
                    provider=self.name,
                    id=str(item["id"]),
                    title=item.get("title") or "Unknown title",
                    authors=[_author_name(a.get("name", "")) for a in item.get("authors", []) if a.get("name")],
                    language=",".join(item.get("languages") or []),
                    extension="epub",
                    url=f"https://www.gutenberg.org/ebooks/{item['id']}",
                )
            )
            if len(books) >= limit:
                break
        return books

    def download(self, book_id: str, dest_dir: Path) -> Path:
        if not book_id.isdigit():
            raise ProviderError("Gutenberg id must be a number, as printed by `search`.")
        # The list endpoint (?ids=) is cached and fast; /books/<id>/ can take 40s+.
        try:
            results = fetch_json(f"{API}?ids={book_id}", timeout=60).get("results") or []
        except Exception:
            results = []
        item = results[0] if results else {}
        url = (item.get("formats") or {}).get(EPUB_MIME)
        if results and not url:
            raise ProviderError("this book has no EPUB edition")
        url = url or f"https://www.gutenberg.org/ebooks/{book_id}.epub3.images"
        authors = [_author_name(a.get("name", "")) for a in item.get("authors", []) if a.get("name")]
        stem = item.get("title") or f"gutenberg_{book_id}"
        if authors:
            stem = f"{stem} - {authors[0]}"
        return stream_download(url, dest_dir, safe_filename(stem, "epub"))
