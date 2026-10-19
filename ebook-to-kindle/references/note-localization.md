# Explicit Chinese footnote localization

This is an optional post-processing route for an EPUB that already has valid `noteref → footnote → backlink` semantics. It does not translate the whole book and it never runs inside ordinary `convert`.

## Privacy and approval boundary

`extract` and `apply` are local. `translate` sends the selected footnote source text to the configured cloud model through the local Codex CLI. Before a real-book run, show the exact book/ledger scope and obtain user approval for that external disclosure/account use. The command still requires `--allow-cloud-translation`; there is no automatic local or cloud fallback and no silent destination change.

The default model is `gpt-5.3-codex-spark`. Prompts are passed on stdin rather than the command line, the Codex run is ephemeral/read-only, user/project rules are ignored for the child translation turn, and only schema-constrained JSON is accepted. Translation ledgers contain private note text and are written as 0600 files; do not publish them.

## Workflow

1. Produce or select the semantic EPUB to localize.

2. Extract an exact per-ID ledger:

   ```bash
   python3 scripts/localize_notes.py extract BOOK.epub --ledger PRIVATE/notes.json
   ```

3. After explicit cloud approval, translate only wholly-unapproved unique source groups:

   ```bash
   python3 scripts/localize_notes.py translate \
     --ledger PRIVATE/notes.json \
     --model gpt-5.3-codex-spark \
     --allow-cloud-translation
   ```

4. Review the ledger/sample diff. Only after the user accepts the translations, freeze every note ID:

   ```bash
   python3 scripts/localize_notes.py approve \
     --ledger PRIVATE/notes.json \
     --confirm APPROVE_NOTE_TRANSLATIONS
   ```

5. Apply approved text while preserving note IDs and backlinks, then run semantic verification:

   ```bash
   python3 scripts/localize_notes.py apply BOOK.epub \
     --ledger PRIVATE/notes.json \
     --output BOOK.zh-notes.epub
   ```

6. Convert the localized EPUB to AZW3 through the ordinary offline command and its private workspace.

## Non-negotiable translation semantics

- Each ledger is bound to the exact source EPUB SHA-256 and each note's member, ID, normalized source text, and source hash.
- Approved translations are authoritative per ID. They may differ even when the English source text repeats. Translate/fan-out is allowed only while every note in that duplicate group is still pending.
- Model output is `model-reviewed`, not approved. `apply` accepts only explicit `approved` entries with matching translation hashes.
- Applying translations preserves every note ID and backlink, repackages EPUB with a valid stored mimetype, then re-extracts all notes and rechecks semantic retention. A mismatch publishes nothing.
- An already published, approved localization must not be retranslated or deduplicated. Its byte-identical final artifact remains the authority unless a separately versioned book passes every quality gate again and is checked on a real Kindle.
