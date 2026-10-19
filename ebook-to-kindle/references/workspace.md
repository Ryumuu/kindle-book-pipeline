# Private single-book workspace

## Why it is explicit

The workspace is a user-selected, private, per-book acceleration surface. It is not a hidden global cache and is never created unless `--workspace` is supplied. The same book may include its original source and approved derived EPUB revisions, but unrelated books should use different workspace roots.

The root cannot overlap the input, output directory, runtime, or installed/source Skill. It is identified by `workspace.json`, held under a nonblocking kernel lock, limited to 2 GiB, and uses 0700 directories plus 0600 regular files. Symbolic links, hard links, special files, unknown top-level entries, unknown node layers, corrupt hashes, and capacity-scan errors fail closed.

## Commands

```bash
python3 scripts/ebook_to_kindle.py convert INPUT \
  --output-dir OUTPUT_DIR \
  --workspace PRIVATE_BOOK_WORKSPACE \
  --reuse auto

python3 scripts/ebook_to_kindle.py convert INPUT \
  --output-dir OUTPUT_DIR \
  --workspace PRIVATE_BOOK_WORKSPACE \
  --reuse refresh

python3 scripts/ebook_to_kindle.py workspace inspect PRIVATE_BOOK_WORKSPACE
python3 scripts/ebook_to_kindle.py workspace purge PRIVATE_BOOK_WORKSPACE \
  --confirm PURGE_EBOOK_WORKSPACE
```

`auto` reads valid nodes and stores new accepted nodes. `refresh` ignores existing nodes but stores the newly accepted chain. `off`, or omitting the workspace entirely, performs a stateless conversion.

Purge is permanent and intentionally requires the exact token. It first verifies the root identity and complete tree contract while holding the workspace lock, atomically isolates that exact root, and then removes it. Never put the only copy of a source document inside a workspace; the boundary guard rejects that layout.

## Layers and invalidation

| Hit | Reused evidence | Still runs |
|---|---|---|
| exact | Accepted final artifact plus bound Q-gate evidence | input/object/container hashes, private-state checks, atomic publication |
| master | master EPUB, source reference text, baseline and route evidence | core runtime check, EPUBCheck, structure/content gates, final conversion/readback |
| semantic | Docling HTML/JSON/outline/assets and PDF route evidence | core runtime check, normalization, master/final conversion and every downstream gate |
| cold | nothing | exact input-required runtime plus the full route |

Keys bind the source SHA-256 and only the relevant generation recipe. PDF semantic keys bind PDF mode, Docling/OCR/model runtime, and semantic functions. Master keys additionally bind the source title, recursively hashed local HTML/Markdown resources, normalization, PW2 CSS, Calibre core, master options, structural gate functions, and the semantic key. Final keys additionally bind output format, PW2 final options, every quality-gate implementation, and the master key.

A changed input, PDF mode, relevant code, CSS, pinned runtime, model, format, or gate version produces a miss at the exact affected layer. Unrelated reports and documentation do not invalidate content nodes. A node under an otherwise matching key whose bytes or evidence do not match is corruption, not a miss, and returns `WORKSPACE_INVALID`.

## Performance truth

Stage timings are measured in-process and reported as aggregate seconds. An exact hit does not call deep runtime verification, Docling, Calibre, Java, or Spark. A non-PDF, master, or semantic route checks only the core runtime; a cold PDF also verifies the PDF lock and model hashes.

Master validation reads visible spine text directly from the generated EPUB instead of spawning a redundant EPUB-to-TXT Calibre process. Kindle validation converts each final file back to one EPUB and uses that same package for text, TOC, landmark, note, link, and resource gates. The former extra Kindle-to-TXT call is also removed; no quality threshold is removed.

Only exact reuse guarantees byte identity. Rebuilding from semantic or master inputs may yield a different container byte stream even when every semantic/content gate passes. A new hash requires the Paperwhite 2 manual gate.
