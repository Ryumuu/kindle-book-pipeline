# Runtime and operations

## Supported host

- Apple Silicon Mac, macOS 14 or newer.
- Default runtime: `~/.local/share/ebook-to-kindle` (0700).
- Persistent cap: 6 GiB. Verified 2026-08-11 installation: about 3.05 GiB.
- No writes to `/Applications`, Homebrew, accounts, browser state, or Kindle devices.

## Install in two gates

Show the plan first:

```bash
python3 scripts/bootstrap_runtime.py --component core --print-plan
python3 scripts/bootstrap_runtime.py --component pdf --print-plan
```

After explicit approval for downloads and persistent writes:

```bash
python3 scripts/bootstrap_runtime.py --component core --accept-download
python3 scripts/ebook_to_kindle.py doctor --deep

# First prove TXT/AZW3 works, then install the heavier PDF route.
# The PDF route needs uv on PATH (https://docs.astral.sh/uv/); the installer does not install it.
python3 scripts/bootstrap_runtime.py --component pdf --accept-download
python3 scripts/ebook_to_kindle.py doctor --deep
```

The installer accepts only a dedicated `ebook-to-kindle` runtime directory that is empty or already has this runtime's exact name/schema/version identity manifest. Its plan prints the exact target path. Fixed binary archives are SHA-256 checked before extraction; Python packages come from the forced official PyPI index at exact locked versions; the four selected Docling model weight files are verified at exact relative paths and hashes. Installer subprocesses receive runtime-private disposable `HOME`, `CFFIXED_USER_HOME`, `TMPDIR`, and XDG directories; these caches are strictly removed before commit. `hdiutil` creates a short-lived read-only mount while copying the pinned Calibre app. A failed `all` install rolls back only components and empty job/package directories created by that attempt. For a brand-new root, a zero-byte fixed lock plus an identified empty manifest may remain; this retryable shell contains no downloaded component or user document and can be removed by the guarded uninstaller.

Deep doctor verifies the manifest name/schema/version, exact Calibre/Temurin/EPUBCheck versions, parity of the saved 109-entry source/runtime Python locks, live Python/Docling/OcrMac/pypdfium2 probes, all four selected model hashes, symlink containment, an empty/cleanable job root, and the live 6 GiB cap. It does not dynamically re-enumerate and hash every installed wheel on each run. An existing component is reused only after the same integrity contract passes.

Conversion verification is input- and hit-aware. A cold PDF requires the full core+PDF contract. A direct document, master hit, or semantic hit requires the exact core contract and skips unrelated PDF model hashes. An exact final hit verifies the source/workspace/artifact/container bindings and does not launch runtime probe subprocesses. `doctor --deep` always remains the full authoritative installation check; `doctor` without `--deep` is only a non-authoritative file-readiness snapshot and does not take the runtime operation lock.

## Versions

| Component | Version | Purpose |
|---|---:|---|
| Calibre | 9.13.0 | EPUB, AZW3, MOBI6 conversion and roundtrip |
| EPUBCheck | 5.3.0 | EPUB conformance gate |
| Temurin JRE | 21.0.12+8 | Bundled EPUBCheck runtime |
| CPython | 3.12.13+20260807 | Isolated PDF runtime |
| Docling | 2.119.0 | PDF layout and reading order |
| OcrMac | 1.0.1 | Local Apple Vision OCR |

## State isolation

Each job starts from a small environment allowlist (locale, timezone, and terminal only) plus a fixed system `PATH=/usr/bin:/bin:/usr/sbin:/sbin`, so API keys, cloud credentials, proxies, private package indexes, inherited executables, `PYTHONPATH`, `JAVA_TOOL_OPTIONS`, and tool-specific injection variables are not passed through. Third-party subprocesses receive job-private `HOME` and `CFFIXED_USER_HOME`; `CALIBRE_CONFIG_DIRECTORY`, `CALIBRE_CACHE_DIRECTORY`, `CALIBRE_TEMP_DIR`, `TMPDIR`, all XDG state, `DOCLING_CACHE_DIR`, `HF_HOME`, and `TORCH_HOME` are also redirected into the random 0700 job directory. This changes subprocess environment values, not the user's real HOME data. Hugging Face and Transformers are offline after installation, PDF Python runs in isolated mode, and every conversion-time third-party command is wrapped by the supported macOS `sandbox-exec` profile `(deny network*)`.

Publication is cleanup-gated: final books and the report are staged through one `O_DIRECTORY|O_NOFOLLOW` directory descriptor anchored to the chosen output directory, the source-derived private job is strictly removed and verified absent, and only then are outputs committed. The runtime tree is forbidden as an output surface. A pre-commit cleanup or directory-identity error returns `ISOLATION_FAILED` and leaves prior outputs unchanged; a post-commit cleanup warning is persisted in the JSON report instead of contradicting an already committed book.

The runtime is intentionally single-operation. Conversion, deep doctor, runtime installation, acceptance verification, and uninstall share a nonblocking kernel `flock` on the fixed private `.operation.lock`; a second process gets `RUNTIME_BUSY` before it can mutate state. Skill install/replace and Skill uninstall also share a separate fixed lock in the allowed Skill parent. The zero-byte files persist, while kernel locks are released automatically when their descriptors or processes exit. A stale entry under `jobs/` separately fails closed with `ISOLATION_FAILED`. Same-UID lock-path replacement after an open is treated as a residual local-adversary risk; the 0700 parent, no-follow open, owner/type/link checks, and kernel lock are the enforced boundary.

## Commands

```bash
python3 scripts/ebook_to_kindle.py doctor --deep
python3 scripts/ebook_to_kindle.py convert INPUT --output-dir OUTPUT_DIR
python3 scripts/ebook_to_kindle.py convert INPUT --formats azw3,mobi6,epub --output-dir OUTPUT_DIR
python3 scripts/ebook_to_kindle.py workspace inspect PRIVATE_BOOK_WORKSPACE
python3 scripts/uninstall.py --dry-run
```

The uninstall command performs no deletion without the exact confirmation token printed by `--dry-run`.
