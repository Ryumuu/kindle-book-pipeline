#!/usr/bin/env python3
"""Offline tests for the book-search plugin core (no real book platform is contacted)."""

from __future__ import annotations

import contextlib
import functools
import http.server
import io
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import book_search  # noqa: E402
import booksearch  # noqa: E402


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = b"x" * 1000
        self.send_response(200)
        self.send_header("Content-Length", "5000" if self.path == "/short" else str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class StreamDownloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def test_never_overwrites_and_numbers_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "Book.epub").write_bytes(b"mine")
            saved = booksearch.stream_download(self.base + "/ok", Path(tmp), "Book.epub")
            self.assertEqual(saved.name, "Book (2).epub")
            self.assertEqual((Path(tmp) / "Book.epub").read_bytes(), b"mine")
            self.assertEqual(saved.stat().st_size, 1000)

    def test_truncated_download_fails_and_leaves_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(Exception):
                booksearch.stream_download(self.base + "/short", Path(tmp), "Book.epub")
            self.assertEqual(os.listdir(tmp), [])

    def test_rejects_file_names_that_leave_the_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("../evil.epub", "/tmp/evil.epub", "a/b.epub", "..", ".", "", "a\\b.epub"):
                with self.assertRaises(booksearch.ProviderError, msg=name):
                    booksearch.stream_download(self.base + "/ok", Path(tmp), name)
            self.assertEqual(os.listdir(tmp), [])


class PluginLoadingTests(unittest.TestCase):
    def _registry(self, files):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for name, code in files.items():
            Path(tmp.name, name).write_text(code, encoding="utf-8")
        warnings = []
        with mock.patch.dict(os.environ, {"BOOK_SEARCH_PROVIDER_PATH": tmp.name}):
            registry = booksearch.load_providers(warn=warnings.append)
        return registry, warnings

    def test_user_plugin_overrides_builtin_and_broken_or_private_files_are_skipped(self):
        registry, warnings = self._registry({
            "gutenberg.py": "from booksearch import Provider\nclass P(Provider):\n    name = 'gutenberg'\n    label = 'mirror'\n",
            "broken.py": "raise RuntimeError('boom')\n",
            "_off.py": "from booksearch import Provider\nclass P(Provider):\n    name = 'off'\n",
        })
        self.assertEqual(registry["gutenberg"].label, "mirror")
        self.assertNotIn("off", registry)
        self.assertIn("zlibrary", registry)
        self.assertTrue(any("broken.py" in str(w) for w in warnings))

    def test_crashing_is_ready_does_not_abort_listing(self):
        registry, _ = self._registry({
            "flaky.py": "from booksearch import Provider\nclass P(Provider):\n    name = 'flaky'\n    def is_ready(self):\n        raise RuntimeError('bad config')\n",
        })
        out = io.StringIO()
        args = mock.Mock(json=True)
        with contextlib.redirect_stdout(out):
            self.assertEqual(book_search.cmd_providers(args, registry), 0)
        rows = {row["name"]: row for row in __import__("json").loads(out.getvalue())}
        self.assertIn("is_ready() failed", rows["flaky"]["reason"])
        self.assertTrue(rows["gutenberg"]["ready"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
