# Quality gates

> The thresholds below are implementation minimums enforced by the pipeline. Passing them proves the file is structurally sound; final reading comfort is still worth a quick look on a real Kindle.

All gates fail closed; a failed job does not publish staged books.

## Route and OCR

- TXT/Markdown visible text, HTML visible body text, DOCX document XML, and EPUB spine body text must contain at least 20 source-authored characters before Calibre runs; metadata titles, scripts, and styles do not count. UTF-8, BOM-marked UTF-16, and GB18030 are decoded strictly, and replacement-character ratio must stay at or below 1%.
- Auto-probe checks every PDF page for selectable text.
- A PDF is treated as digital when the aggregate text threshold passes, at least 90% of pages contain selectable text, and every non-selectable page is either a pixel-verified blank page or a limited edge cover page. An interior image-only page, a nonblank unexplained gap, or a predominantly scanned document routes the whole document through full-page OCR.
- Scans use OcrMac full-page OCR with `zh-Hans,en-US`.
- Docling PDF output must contain at least 20 visible body characters before Calibre runs.
- Scan OCR body text must have at least 20 non-space characters and no more than 1% Unicode replacement characters. Docling provenance is also counted page by page with furniture excluded. Normal nonblank pages require at least 15 attributable characters; pixel-verified blank pages use a zero threshold, and a page with sparse but valid selectable source text uses that observed count as its threshold. Every page must meet its assigned threshold and `page_coverage_ratio` must be 1.0. The report keeps only aggregate counts, thresholds and coverage metrics, never page text.
- HTML/Markdown local images, `srcset`, linked CSS, and CSS `url(...)` resources are preflighted before Calibre. Remote, missing, path-escaping, or symlinked resources fail closed.
- DOCX image relationships must be package-local and present. EPUB remote manifest resources are rejected; legal parent-relative ZIP members are normalized before lookup.
- Every Calibre route explicitly sets authored top/right/bottom/left margins to `0`; `pw2.css` also forces the reflow body margin and padding to `0`. The Kindle device may still reserve its own firmware reading inset.
- PDF bookmarks are mapped to body anchors in source order. A bookmark without a usable destination is searched only between its nearest known neighboring bookmark pages. If an adjacent missing run has no title match, its entries anchor immediately before the next known section rather than being scattered through the previous chapter; page-start and low-confidence mappings remain explicit in the report. It is never matched against an unrelated printed TOC near the front of the book.
- If a PDF has no outline, Docling level-1/2 section headings form the TOC; if no section heading exists, one deterministic `正文` entry is added. Every body table is checked for printed-TOC signals, regardless of its position: overlap with multiple outline labels, or dense monotonic page numbers together with chapter-like text. Classified printed-TOC tables are removed, ordinary data tables are retained, and both master and Kindle roundtrip must report `body_printed_toc_table_count: 0`.
- Dense, monotonic bracketed PDF endnotes are reconstructed only when at least 20 definition markers form a local sequence. Visible references are scanned across paragraphs, headings, preformatted blocks, tables, callouts and simple existing links; optional whitespace inside `[ n ]` is normalized. For a digital PDF with a detected dense endnote section, the source PDF and semantic HTML must have exactly the same per-number body-reference counts; extra Docling duplicates are removed and any mismatch fails before publication. A definition omitted by Docling may be restored only from a high-confidence line-start source-PDF segment. If the source PDF itself has no matching definition, a clickable footnote explicitly says that no recoverable source note was provided; it never invents note content.

## Master EPUB

- Valid ZIP EPUB mimetype and OPF.
- Non-empty spine with known manifest IDs.
- Every local manifest resource exists; remote manifest resources are forbidden during offline conversion.
- At least one navigation entry in EPUB3 nav or NCX. `toc_entries` is the larger of the nav and NCX counts, not a misleading sum of duplicate representations.
- Every nav, NCX, landmark, internal link, and fragment target must resolve to an existing packaged document and ID. A navigation document containing a `<table>` is rejected.
- At least one valid EPUB `bodymatter` or EPUB2 guide start location must resolve to the first readable body section, so Kindle “Go To” has a real start target.
- Source headings/bookmarks and source note counts are retained into the master EPUB. Every `noteref` must resolve to a semantic footnote/endnote and every linked note must contain a return link to the exact reference ID. Digital-PDF source and output note counts are compared per number, not merely by aggregate total.
- EPUBCheck must report exactly 0 fatal, 0 error, 0 warning.
- After removing the known generated input-title string, the master EPUB spine must expose at least 20 non-space visible characters. The same spine extractor used for final roundtrip validation replaces a redundant EPUB-to-TXT Calibre subprocess.
- Generated XHTML removes CJK-to-CJK layout spaces and spaces adjacent to Chinese punctuation. Adjacent paragraph, heading, and list fragments are merged only when the first block lacks terminal punctuation and both blocks share compatible prose semantics. Navigation, landmarks, tables, footnotes, furniture, preformatted or poetry-like text, anchored blocks, and linked TOC list items are never block-merged.
- Calibre filters source `font-family`, `font-size`, and `line-height` before applying the PW2 stylesheet. Master EPUB CSS must contain no fixed or out-of-range prose font size, publisher font family, inline typography override, or unsafe line height. Body and prose use relative `1em`; heading sizes are graded; only explicit note, noteref, caption, furniture, and hidden-anchor semantics may be smaller.
- Source body to master EPUB must retain 90% normalized length, 90% of 64 sampled markers, and 90% of those markers in original order. It may grow to at most 160%, and sampled-marker occurrence inflation may not exceed 1.6x.
- The classified source content-image count from HTML/Markdown, DOCX, EPUB, or Docling HTML must be retained as non-cover images in the master EPUB manifest. EPUB2 metadata/guide cover pages and EPUB3 `cover-image` properties are classified separately, so Calibre's generated cover cannot compensate for a lost content image. This automated gate proves local-reference integrity and classified image-count retention; it does not identify images by pixels or claim pixel-level fidelity after Calibre transforms an image or stylesheet.

## Kindle outputs

- AZW3 must be a PalmDB/BOOKMOBI container with MOBI file version 8.
- MOBI must be a PalmDB/BOOKMOBI container with MOBI file version 6.
- Roundtrip text must retain at least 90% and at most 150% of normalized length, 95% of 64 evenly sampled content markers, and 95% of those markers in their original order. Sampled-marker occurrence inflation may not exceed 1.5x, and both baseline and roundtrip replacement-character ratios must stay at or below 1%.
- Roundtrip EPUB must retain a valid manifest, spine, resources, and TOC. AZW3/MOBI roundtrip must expose at least the master TOC count through valid NCX entries so Paperwhite 2 “Go To → Table of Contents” remains functional.
- Each AZW3/MOBI is converted back to one EPUB. That same package supplies both visible spine text and structural/resource evidence, replacing the older separate Kindle-to-TXT subprocess without removing a content threshold.
- Master and Kindle roundtrip must retain all source-required resolved noterefs, semantic footnotes, and bidirectional note links. Broken internal targets and table-based TOCs are zero-tolerance failures.
- Master and Kindle roundtrip must retain a valid `bodymatter`/start landmark, and neither may contain a body table classified as a printed TOC.
- The classified source content-image count must also survive the AZW3/MOBI roundtrip EPUB; a lower count fails before publication.
- Kindle roundtrip visible text must retain zero CJK spacing anomalies, zero eligible prose hard breaks, and zero suspicious adjacent prose fragments. Typography is not re-judged from the reverse-converted EPUB because Calibre synthesizes new heading sizes and margins during reverse conversion; the master EPUB contract plus the explicit final `--filter-css`, relative CSS, profile, and zero-margin arguments are the typography authority.

Books and their JSON report are first copied to hidden files in the requested output directory. The private job directory, including source-derived text and OCR intermediates, must be removed successfully before those hidden files are atomically committed. Cleanup failure therefore leaves existing outputs untouched and publishes nothing.

## Stable error codes

| Code | Meaning |
|---|---|
| `UNSUPPORTED_INPUT` | Not a supported local file |
| `DEPENDENCY_MISSING` | Required runtime component is absent |
| `OUTPUT_EXISTS` | Refuses replacement without `--overwrite` |
| `OCR_QUALITY_FAILED` | Scan route or OCR density failed |
| `CONTENT_GATE_FAILED` | Master EPUB is an empty shell |
| `EPUB_INVALID` | Container, resources, TOC, or EPUBCheck failed |
| `KINDLE_ROUNDTRIP_FAILED` | Container version or content retention failed |
| `ISOLATION_FAILED` | Private state, cleanup, user-state, or job-hygiene isolation failed |
| `RUNTIME_BUSY` | Another conversion, doctor, install, acceptance, or uninstall owns the runtime lock |
| `WORKSPACE_INVALID` | Workspace identity, boundary, capacity, node, object, or hash failed |
| `WORKSPACE_BUSY` | Another operation owns the selected single-book workspace lock |
| `CLOUD_TRANSLATION_NOT_APPROVED` | Spark note text disclosure was not explicitly allowed |

These are deterministic automated gates, not a substitute for a real Paperwhite 2 display check.
