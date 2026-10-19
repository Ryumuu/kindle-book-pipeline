# Third-party components and adoption record

This Skill combines narrowly scoped parts of mature projects; it does not vendor their source or model binaries inside the `.skill` archive.

| Project | Adopted part | License / boundary |
|---|---|---|
| [Calibre 9.13.0](https://github.com/kovidgoyal/calibre/releases/tag/v9.13.0) | Final conversion, `kindle_pw`, KF8/MOBI6, roundtrip | GPL-3.0; installed as an unmodified local app bundle |
| [Docling 2.119.0](https://github.com/docling-project/docling/tree/v2.119.0) | PDF layout, reading order, HTML+JSON export | MIT |
| [OcrMac 1.0.1](https://github.com/straussmaximilian/ocrmac) | Apple Vision local OCR | MIT |
| [EPUBCheck 5.3.0](https://github.com/w3c/epubcheck/releases/tag/v5.3.0) | EPUB conformance | BSD-3-Clause |
| [Temurin 21](https://github.com/adoptium/temurin21-binaries/releases/tag/jdk-21.0.12%2B8) | JRE for EPUBCheck | GPL-2.0 with Classpath Exception |
| [CPython 3.12.13 standalone build](https://github.com/astral-sh/python-build-standalone) | Private interpreter for the PDF route | CPython PSF license plus bundled dependency notices; build tooling is MPL-2.0; the selected archive retains its license metadata and is not shipped in the `.skill` |
| [uv](https://github.com/astral-sh/uv) | Creates the private venv and installs the exact dependency lock during confirmed bootstrap | Apache-2.0 OR MIT; uses the already-installed local executable and is not shipped in the `.skill` or runtime |
| [pypdfium2 5.12.1](https://github.com/pypdfium2-team/pypdfium2) | Full-page PDF probe/render support in the private PDF environment | Apache-2.0 OR BSD-3-Clause, with PDFium/dependency notices carried by its installed wheel; not shipped in the `.skill` |
| [OpenAI Codex CLI](https://developers.openai.com/codex/cli/) | Optional explicit footnote-only call to `gpt-5.3-codex-spark`; synthetic probe used host CLI `0.147.0` | External authenticated service/CLI, not bundled in the Skill or runtime; ordinary conversion never invokes it |
| [Layout Heron + ONNX](https://huggingface.co/docling-project/docling-layout-heron) | PDF layout weights pinned by per-file SHA-256 | Apache-2.0 model repository |
| [TableFormer fast/accurate](https://huggingface.co/docling-project/docling-models) | Table-structure weights pinned by per-file SHA-256 | CDLA-Permissive-2.0 model weights; repository also identifies Apache-2.0 components |
| [Noto Sans CJK / Noto Sans SC](https://github.com/notofonts/noto-cjk/blob/main/Sans/LICENSE) | Subset embedded only in the synthetic digital-PDF acceptance fixture | SIL OFL 1.1; copyright and full byte-preserved license ship at `evals/fixtures/LICENSES/NotoSansSC-OFL-1.1.txt`; no standalone font file is shipped |

Model weights are downloaded only by the confirmed installer and retained under the private runtime. The installer records and verifies the exact hashes for Layout Heron, its ONNX variant, and TableFormer fast/accurate weights. The links and license labels above are an adoption record, not a redistribution grant.

The PDF lock contains 109 direct and transitive Python package entries. They are installed only into the private runtime and excluded from the `.skill` archive. Redistributing that runtime requires a fresh SBOM and review of the license files carried by the exact wheels; this record does not grant runtime redistribution.

## Explicit exclusions

- Marker/Surya: excluded because the model output-license obligations and default cache/service paths do not fit clean ebook output and strict isolation.
- OCRmyPDF: not included because Docling + OcrMac already passed the same scan fixture; adding a Docker/Tesseract layer had no demonstrated net benefit.
- Pandoc: not included because Calibre won or matched the tested master-EPUB route and is already required for final Kindle formats.
- Whole-book cloud LLM translation, cloud OCR, DRM removal, book search/download, account upload, and automatic Kindle transfer: outside scope. The only cloud-content exception is the separately invoked, explicitly approved footnote-ledger translation described in `note-localization.md`.

The acceptance fixture embeds only the glyph subset needed by synthetic test text. Its source-font SHA-256 and generated-PDF SHA-256 are recorded in `evals/fixtures/README.md`; the prior Apple STHeiti subset was removed.

This is an engineering adoption record, not legal advice. Re-check licenses before redistributing the runtime or model binaries.
