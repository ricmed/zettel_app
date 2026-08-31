# Component Deep Analysis Report: extraction_dump

## 1. Executive Summary

`zettel/extraction_dump.py` is a small, read-mostly diagnostic component in the `harvest` phase of the Zettelkasten pipeline. Its sole purpose is to let a user inspect the raw Markdown that the extraction layer (Docling for PDF, native pass-through for Markdown) produced and persisted to `sources.extracted_text`, **before** that text is chapter-split and chunked. It answers the question "what did Docling/the MD reader actually see, and where did it think the headings were?" without re-running extraction or touching the chunker.

The component is opt-in and side-effect-light: it never mutates SQLite or ChromaDB, only writes Markdown files to a cache directory (`data/cache/extraction-dumps/` by default, or a caller-supplied directory). It is wired into the pipeline at two points:

1. **Inline during `zettel harvest --dump-extraction`**: as soon as `harvester._process_file` persists `extracted_text` for a brand-new source (`db.update_source_texts`), it immediately calls `_maybe_dump_extraction`, which delegates to `extraction_dump.dump_source_extraction`.
2. **Standalone via `zettel dump-extraction --source-id @Citekey` / `--all`**: re-exports already-harvested sources' persisted text without re-extracting, by reading straight from SQLite (`run_dump_extraction`).

Key findings:
- The component is a thin, single-file module (150 lines) with no classes, only functions — high cohesion, low complexity.
- It reuses `sanitize_citekey`/filename conventions from the sibling `chunk_dump.py` component and `compose_note`/frontmatter rendering from `vault.py`, rather than duplicating that logic — a deliberate code-reuse decision (see Design Patterns).
- It is strictly read-only against `StateDB` (only calls `get_source` / `list_sources`), matching its own docstring guarantee ("Read-only on the DB").
- A structural gap exists: the harvester's "complete an already-persisted-but-incompletely-chunked source" code path (`_complete_incomplete_source`) does **not** call `_maybe_dump_extraction`, so `--dump-extraction` silently produces no file for sources recovered through that path (see Technical Debt).
- Test coverage is thorough for the module's own pure functions and its two public entry points, plus one harvester integration test, but there is no test exercising the CLI command `dump-extraction` itself or the `--all` flag end-to-end.

## 2. Data Flow Analysis

Two independent entry paths converge on the same rendering/writing logic.

**Path A — inline dump during harvest (`--dump-extraction` / `--dump-extraction-dir`)**

```
1. CLI: `zettel harvest --dump-extraction` (zettel/cli.py:291-298, harvest_cmd)
2. cli._resolve_extraction_dump_dir() resolves the target Path (or None if flag absent)
   (zettel/cli.py:403-412)
3. harvester.run_harvest(..., extraction_dump_dir=path) (zettel/harvester.py:65)
4. harvester._process_file() extracts text via _extract_text() (Docling/native MD)
5. db.upsert_source(...) + db.update_source_texts(source_id, extracted_text=text)
   (zettel/harvester.py:689-704)  <- extraction persisted to SQLite FIRST
6. harvester._maybe_dump_extraction(cfg, db, source_id, extraction_dump_dir)
   (zettel/harvester.py:450-456)
7. extraction_dump.dump_source_extraction(cfg, db, source_id, dump_dir)
   -> re-reads the just-persisted row via db.get_source(source_id)
8. extraction_dump.render_extraction_dump() builds frontmatter + heading outline + raw text
9. extraction_dump.write_extraction_dump() writes extraction-{citekey}.md (overwrite)
10. cli prints "Dump de extracao gravado em: {dir}" (zettel/cli.py:347-348)
```

**Path B — standalone re-export (`zettel dump-extraction`)**

```
1. CLI: `zettel dump-extraction --source-id @Citekey` or `--all`
   (zettel/cli.py:883-920, dump_extraction_cmd)
2. Validates that exactly one of --source-id/--all was requested
3. extraction_dump.run_dump_extraction(cfg, db, source_id_or_None, dump_dir)
4. For a single source_id: db.get_source(source_id); raises ValueError if missing
   For --all: db.list_sources() (every row in `sources`, no filtering)
5. For each source: skip (log warning, count) if extracted_text is empty/None
6. Otherwise: write_extraction_dump(dest, src, text, cfg) per source
7. Returns {"sources": written, "skipped": skipped} tally
8. CLI prints success count + skipped-count warning
```

**Shared rendering step (both paths funnel here)**

```
render_extraction_dump(source, extracted_text, cfg)
  -> list_headings(text): regex-scan ATX headings H1-H6, in document order
  -> build metadata dict (source_id, citekey, title, origin_path, origin_type,
     pdf_extractor from cfg, chars=len(text))
  -> build body: "## Headings detectados" bullet list (or empty-state message)
                 + "## Texto extraido" + raw extracted_text verbatim
  -> compose_note(meta, body) from vault.py: YAML frontmatter + blank line + body
write_extraction_dump(dump_dir, source, text, cfg)
  -> dump_dir.mkdir(parents=True, exist_ok=True)
  -> filename = extraction-{sanitize_citekey(citekey or source_id or "unknown")}.md
  -> path.write_text(rendered, encoding="utf-8")  # always overwrites
```

## 3. Business Rules & Logic

### Overview of the business rules

| Rule Type | Rule Description | Location |
|-----------|------------------|----------|
| Precondition | Command is opt-in: no dump is produced unless `--dump-extraction`/`--dump-extraction-dir` (harvest) or the standalone `dump-extraction` command is invoked | zettel/cli.py:291-298, 883-896 |
| Precondition | `dump-extraction` CLI requires exactly one of `--source-id` or `--all`; neither given -> error exit 1 | zettel/cli.py:894-896 |
| Validation | Standalone dump on a missing `--source-id` raises `ValueError("Fonte nao encontrada: {id}")`, caught by the CLI and turned into exit code 1 | zettel/extraction_dump.py:126-129; zettel/cli.py:904-911 |
| Skip rule | A source whose `extracted_text` is empty/None is skipped (not an error) — logged as a warning and counted separately from "written" | zettel/extraction_dump.py:101-108, 136-145 |
| Data source | The dump body is exactly `sources.extracted_text` as already persisted — never a fresh Docling/MD extraction call | zettel/extraction_dump.py (module docstring), zettel/harvester.py:704-705 |
| Filename rule | Output file is always named `extraction-{sanitized citekey}.md`; falls back to `source_id` then the literal string `"unknown"` if citekey is absent | zettel/extraction_dump.py:32-33, 82-83 |
| Filename sanitization | Citekey is sanitized to `[A-Za-z0-9._-]` (unsafe chars -> `_`, then strip leading/trailing `._-`), matching the sibling chunk-dump convention, for Windows filename safety | zettel/chunk_dump.py:23,30-33 (reused) |
| Idempotency / overwrite | Re-running a dump for the same citekey silently overwrites the previous file at the same path — there is no versioning or append-only history | zettel/extraction_dump.py:80-87 (`write_extraction_dump` docstring: "overwrites") |
| Heading extraction | Headings shown in the outline are ATX-only (`^#{1,6}\s+...`), scanned in multiline mode, in document order — the exact same regex family the harvester uses to split H1-H2 chapters / H3-H6 `section_path` | zettel/extraction_dump.py:25, 36-38 |
| Empty-state rule | When no ATX heading is found in the text, the outline shows a literal placeholder line instead of an empty list | zettel/extraction_dump.py:63-64 |
| Metadata rule | `pdf_extractor` in the dump's frontmatter reflects the **current** `cfg.pdf_extractor` config value, not necessarily the extractor that actually produced this source's text (no per-source extractor is stored) | zettel/extraction_dump.py:55 |
| Directory rule | Output directory defaults to `cfg.cache_path / "extraction-dumps"` when no explicit directory is supplied; is created recursively on first write | zettel/extraction_dump.py:28-29, 81 |
| Pipeline coupling | Inline dumping only fires on the "new source" code path of `_process_file`; the "complete an incompletely-chunked existing source" path does not trigger a dump | zettel/harvester.py:450-456 vs. 484-499 (`_complete_incomplete_source`) |
| Read-only guarantee | The component never calls any `StateDB` write method — `run_dump_extraction`/`dump_source_extraction` only call `get_source`/`list_sources` | zettel/extraction_dump.py:90-149 (docstring: "Read-only on the DB") |

### Detailed breakdown of the business rules

---

### Business Rule: Opt-in, non-invasive diagnostic dump

**Overview**: The entire component only produces output when a user explicitly asks for it, either via harvest flags or the dedicated CLI command. It never runs as an implicit side effect of any other pipeline phase.

**Detailed description**: `zettel harvest` by default does not create any extraction dump; `_resolve_extraction_dump_dir` (cli.py:403-412) returns `None` unless `--dump-extraction` or `--dump-extraction-dir` was passed on the command line, and `harvester._maybe_dump_extraction` is a no-op guard that returns immediately when `dump_dir is None` (harvester.py:453-454). This mirrors the sibling `--dump-chunks` feature exactly, reinforcing a project-wide convention that diagnostic exports are always opt-in and never silently slow down or clutter a normal harvest run. Passing `--dump-extraction-dir <path>` implicitly also enables the dump (the presence of an explicit directory is itself the "on" switch), independent of whether `--dump-extraction` was also passed — this is handled by `_resolve_extraction_dump_dir` checking `dump_extraction_dir` before falling back to checking the boolean flag.

The standalone `zettel dump-extraction` command carries the same philosophy one step further: it is a fully separate CLI verb, invoked independently of any harvest run, that only ever reads what has already been persisted. This lets a user re-inspect old sources' extraction quality (e.g. after a Docling version bump, or when debugging why a chapter split looks wrong) without re-running the (potentially slow, potentially costly) extraction step. Because the command validates that at least one of `--source-id`/`--all` is present and exits with a red error message plus exit code 1 otherwise, there is no ambiguous "no-op" invocation of the command — a user always either gets output or an explicit error.

This rule shapes the entire component's design: no caching layer, no automatic invalidation, no scheduling — every dump is a deliberate, on-demand snapshot triggered by a human.

**Rule workflow**:
```
IF harvest invoked WITHOUT --dump-extraction AND WITHOUT --dump-extraction-dir:
    extraction_dump_dir = None -> _maybe_dump_extraction is a no-op for every source
IF harvest invoked WITH --dump-extraction-dir <path>:
    extraction_dump_dir = Path(path).expanduser().resolve()  (flag alone implies dumping)
ELIF harvest invoked WITH --dump-extraction (no explicit dir):
    extraction_dump_dir = cfg.cache_path / "extraction-dumps"
IF `dump-extraction` CLI command invoked WITHOUT --source-id AND WITHOUT --all:
    print error, exit(1) -- no dump attempted
```

---

### Business Rule: Dump content is the persisted extraction, never a re-extraction

**Overview**: The Markdown written to disk is byte-for-byte `sources.extracted_text` as already stored in SQLite — the component never invokes Docling, PyMuPDF, or the native Markdown reader itself.

**Detailed description**: This is stated explicitly in the module's docstring ("not a re-run of Docling") and is structurally enforced by the function signatures: `render_extraction_dump` and `write_extraction_dump` both take `extracted_text: str` as a plain string parameter supplied by the caller, and both call sites (`dump_source_extraction`, `_maybe_dump_extraction` in harvester.py) source that string exclusively via `db.get_source(source_id)["extracted_text"]`. There is no import of `docling`, `pymupdf`/`fitz`, or the harvester's `_extract_text` function anywhere in `extraction_dump.py`.

This guarantees two important properties for users debugging extraction quality. First, the dump is guaranteed to be **exactly** what the rest of the pipeline (chapter splitting, chunking, and eventually the LLM extractor) will see, because it is read from the same column those downstream phases read from — there is no risk of the dump showing a "fresher" or "different" extraction than what was actually chunked. Second, it makes the dump essentially free performance-wise: no OCR, no PDF parsing, no LLM call — just a SQLite read and a file write — which is exactly why `zettel dump-extraction --all` can be run against an entire vault of hundreds of sources without material cost, unlike a full `harvest --force` re-run.

The corollary is that this component cannot detect or repair problems that occurred *during* extraction itself (e.g. Docling mis-reading a scanned page) — it can only reveal what was captured. If the persisted text is already wrong, the dump will faithfully reproduce that wrongness; the fix in that case is `zettel rechunk` or a fresh harvest with a different `pdf_extractor`, not this component.

**Rule workflow**:
```
text = db.get_source(source_id)["extracted_text"]   # never re-extracted
IF text is empty/None:
    skip source, log warning, count as "skipped"
ELSE:
    render_extraction_dump(source_row, text, cfg)  # pure string transform of `text`
    write file
```

---

### Business Rule: Sources without persisted extraction text are skipped, not errored

**Overview**: A source row that exists in SQLite but has no `extracted_text` (e.g., harvested before extraction-text retention was added, or corrupted/incomplete) is silently counted as "skipped" with a warning log, rather than raising an exception or aborting a batch `--all` run.

**Detailed description**: Both `dump_source_extraction` (single-source path, used inline by the harvester and by `zettel dump-source-extraction`-style lookups) and `run_dump_extraction` (used by the `--all` batch path and the `--source-id` CLI path) apply the identical guard: `text = src.get("extracted_text") or ""`, and if falsy, log a warning referencing "Fase 0" (the migration point at which extraction-text retention was introduced) and return `None` (single-source) or increment a `skipped` counter and `continue` (batch). This design choice means a `dump-extraction --all` invocation across a mixed-vintage vault (some sources harvested before extraction-text retention existed, some after) will not abort partway through — it degrades gracefully, producing dumps for every source it can and reporting exactly how many it could not.

The warning message is actionable rather than purely diagnostic: it tells the operator to "Reprocesse o arquivo original via harvest" (single-source path) — i.e., the fix for a missing extraction dump is not a special-case repair tool, but simply re-running harvest on the original file so `extracted_text` gets backfilled. This keeps the component's contract simple: it has exactly one recovery path (re-harvest), and it always names that path in the log rather than leaving the operator to guess.

The distinction between the single-source function's return value (`None` = "could not produce a dump") and the CLI-facing `run_dump_extraction`'s return value (a stats dict distinguishing "written" vs. "skipped") reflects the two different call-site needs: the harvester's inline call site only cares whether a dump happened (used for `if dump_dir is None: return` short-circuiting and doesn't otherwise branch on outcome), while the CLI needs an aggregate count across potentially many sources to report a meaningful summary line to the user.

**Rule workflow**:
```
FOR EACH source IN (single [source] list OR db.list_sources()):
    text = source.get("extracted_text") or ""
    IF NOT text:
        log.warning("... sem texto extraido persistido ... Pulando.")
        skipped += 1
        CONTINUE
    write_extraction_dump(dest, source, text, cfg)
    written += 1
RETURN {"sources": written, "skipped": skipped}
```

---

### Business Rule: Missing `--source-id` in single-source mode is a hard error, not a skip

**Overview**: Unlike a source that exists but lacks extracted text (skipped gracefully), a `--source-id` that does not resolve to any row in `sources` at all is treated as an operator error and raises `ValueError`.

**Detailed description**: `run_dump_extraction` distinguishes two failure modes for a single-source request. If `source_id` is provided but `db.get_source(source_id)` returns nothing (the citekey/ID simply does not exist in the database), the function raises `ValueError(f"Fonte nao encontrada: {source_id}")` immediately, before any file I/O — this is different in kind from the "text is empty" case, which is a data-quality issue on an otherwise-valid source. The CLI command (`dump_extraction_cmd` in cli.py:904-911) catches specifically `ValueError`, prints the message in red, closes the DB connection, and exits with status code 1 — giving the operator a clear, fast failure for a typo'd citekey rather than a silent no-op or a stack trace.

This same `ValueError` contract is also exercised directly by `dump_source_extraction` for the harvester's own inline call path, though in that context a missing source is logged as a warning and the function returns `None` rather than raising — because at that call site (immediately after `db.upsert_source`/`db.update_source_texts` for the very source_id just written), a "not found" condition would indicate an internal consistency bug in the harvester itself rather than a user typo, so it is treated as a soft warning to avoid crashing an otherwise-successful harvest run over a diagnostic feature.

**Rule workflow**:
```
run_dump_extraction(source_id="@X"):
    src = db.get_source("@X")
    IF src is None:
        RAISE ValueError("Fonte nao encontrada: @X")   # propagates to CLI -> exit(1)

dump_source_extraction(source_id="@X"):   # harvester's inline / direct-call path
    src = db.get_source("@X")
    IF src is None:
        log.warning(...); RETURN None    # does not raise, does not abort caller
```

---

### Business Rule: Filename derivation and Windows-safe sanitization

**Overview**: Every dump file is named deterministically from the source's citekey, sanitized to characters safe for a Windows filename, with a documented fallback chain.

**Detailed description**: `dump_filename(citekey)` returns `f"extraction-{sanitize_citekey(citekey)}.md"`. The `sanitize_citekey` function is not defined in `extraction_dump.py` itself but imported from `zettel/chunk_dump.py`, where it replaces any run of characters outside `[A-Za-z0-9._-]` with a single underscore, then strips leading/trailing `._-`, and returns the literal string `"unknown"` if the result is empty. This is a deliberate reuse decision: rather than reimplementing filename sanitization, `extraction_dump.py` imports the exact same function the chunk-dump component uses, guaranteeing the two diagnostic dump features name their output files under an identical, single-source-of-truth convention (e.g., a citekey `Smith:2020/foo` becomes `Smith_2020_foo` in both `chunks-Smith_2020_foo.md` and `extraction-Smith_2020_foo.md`).

At the call site, `write_extraction_dump` computes `citekey = source.get("citekey") or source.get("source_id") or "unknown"` before sanitizing — a three-level fallback chain (citekey, then the `@`-prefixed source_id, then a hardcoded literal) that ensures a filename is always produced even for a malformed or partially-populated source row, rather than raising a `KeyError` or writing a file with an empty stem.

Because the filename depends only on the citekey (not on a timestamp, run ID, or content hash), calling the dump twice for the same source at different times **always** targets the same path, which is what enables the overwrite behavior described in the next rule.

**Rule workflow**:
```
citekey = source["citekey"] OR source["source_id"] OR "unknown"
safe = sanitize_citekey(citekey)     # non-[A-Za-z0-9._-] runs -> "_", strip "._-" ends
filename = f"extraction-{safe}.md"
```

---

### Business Rule: Overwrite semantics (no history, no versioning)

**Overview**: Writing a dump for a citekey that already has a dump file replaces its content entirely; there is no append, no backup-of-previous-version, and no warning that a prior file is being discarded.

**Detailed description**: `write_extraction_dump` calls `path.write_text(...)` unconditionally — Python's `Path.write_text` always truncates and rewrites the target file. This is confirmed by `test_write_extraction_dump_creates_sanitized_file`, which writes once with `RAW_MD`, then writes again with different content ("# Outro") to the same citekey, and asserts the second write's content ("# Outro") is present while the first write's content ("Titulo do paper") is completely gone from the file. This is intentional and consistent with the sibling `chunk_dump.py` component's documented behavior, and matches the diagnostic, point-in-time nature of the feature: the dump is meant to represent "what does the extraction look like *right now*", not an audit trail of every extraction attempt.

A practical consequence is that a user who runs `dump-extraction --all` twice in a row (e.g., once before and once after a `pdf_extractor` config change followed by a fresh `harvest --force`) will lose the ability to diff the two extraction snapshots from the filesystem alone, unless they manually copy the output directory between runs — the component provides no built-in comparison or retention mechanism.

**Rule workflow**:
```
write_extraction_dump(dump_dir, source, text, cfg):
    dump_dir.mkdir(parents=True, exist_ok=True)
    path = dump_dir / dump_filename(citekey)
    path.write_text(render(...), encoding="utf-8")   # unconditional overwrite
    RETURN path
```

---

### Business Rule: Heading outline reflects the harvester's own chapter/section-split regex

**Overview**: The "Headings detectados" section of every dump lists ATX Markdown headings (`#` through `######`) in document order, using the identical heading pattern family the harvester itself relies on to split content into H1-H2 chapters and H3-H6 `section_path` metadata.

**Detailed description**: `list_headings` uses `_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)` to scan the full extracted text, returning a list of `(level, title)` tuples for every match, preserving document order (Python's `finditer` yields matches left-to-right, top-to-bottom under `MULTILINE`). This lets a user visually verify, before any chunking happens, exactly which lines in the extracted Markdown the pipeline will treat as structural boundaries — e.g., to catch a Docling artifact where a body paragraph was mis-rendered with a leading `#` (which would spuriously become a "chapter"), or conversely to catch a real section heading that Docling failed to mark as an ATX heading (and would therefore be invisible to the harvester's splitter too).

Because this regex is intentionally the *same family* used elsewhere in the harvest phase (per the module docstring: "same regex harvest uses to split"), the outline is not merely descriptive — it is diagnostic in the strong sense of being a faithful preview of how the harvester's own `_split_into_chapters` logic will interpret the same text. If a heading appears in this dump's outline but the resulting source's chapters/`section_path` don't reflect it (or vice versa), that is evidence of a genuine harvester bug rather than a difference in heading-detection logic between the two components.

When no headings are found at all (e.g., a plain-prose document with no Markdown structure), the outline renders a single literal line, `"- (nenhum heading # a ######)"`, rather than an empty section — ensuring the "Headings detectados" heading is never followed by a confusing blank gap.

**Rule workflow**:
```
headings = [(len(m.group(1)), m.group(2).strip())
            for m in re.finditer(r"^(#{1,6})\s+(.+)$", text, re.MULTILINE)]
IF headings:
    FOR (level, title) IN headings: emit "- H{level} {title}"
ELSE:
    emit "- (nenhum heading # a ######)"
```

---

### Business Rule: Frontmatter metadata is a fixed, minimal projection of the source row

**Overview**: Every dump's YAML frontmatter carries exactly seven fields — `source_id`, `citekey`, `title`, `origin_path`, `origin_type`, `pdf_extractor`, `chars` — regardless of what other columns exist on the `sources` row.

**Detailed description**: `render_extraction_dump` builds a `meta` dict with a fixed key set, defaulting any missing source field to an empty string via `source.get(key) or ""`, except `pdf_extractor` (always taken live from `cfg.pdf_extractor`, not from the source row — the `sources` table does not persist which extractor was used per-source) and `chars` (always `len(text)`, computed fresh from the actual dumped text, guaranteeing it can never drift from the body even if the source row's own metadata is stale). This is a deliberate minimalism: the dump is meant to answer "what extractor config is active, how long is this text, what file did it come from, what heading structure does it have" — not to be a full mirror of the `sources` table (which also carries bibliographic JSON, paging offsets, cost tracking, checksums, etc., none of which are relevant to an extraction-quality review and would add noise).

One subtlety with real operational impact: `pdf_extractor` reflects the **current** value of `cfg.pdf_extractor` at dump time, not necessarily the extractor that historically produced this particular source's `extracted_text`. If a vault's config was changed from `docling` to a different extractor after some sources were harvested, re-running `dump-extraction --all` on old sources would label their dumps with the new extractor name even though their text was actually produced by the old one — a potential source of confusion during a Docling migration or extractor comparison exercise (see also Technical Debt).

**Rule workflow**:
```
meta = {
  source_id:   source.get("source_id") or "",
  citekey:     source.get("citekey") or "",
  title:       source.get("title") or "",
  origin_path: source.get("origin_path") or "",
  origin_type: source.get("origin_type") or "",
  pdf_extractor: cfg.pdf_extractor,      # from CURRENT config, not source history
  chars:       len(text),                # always fresh, tied to actual dumped text
}
```

---

## 4. Component Structure

`extraction_dump.py` is a standalone module (no package/subdirectory of its own); it lives alongside its sibling diagnostic-dump module and the modules it depends on:

```
zettel/
├── extraction_dump.py         # THIS COMPONENT — extraction Markdown dump (harvest --dump-extraction / dump-extraction CLI)
├── chunk_dump.py               # sibling diagnostic dump (persisted chunks); supplies sanitize_citekey() reused here
├── harvester.py                 # pipeline caller: _maybe_dump_extraction() wired into _process_file()
├── cli.py                        # CLI wiring: harvest_cmd flags + dedicated `dump-extraction` command
├── config.py                     # AppConfig: cache_path, pdf_extractor read by this component
├── state.py                       # StateDB: get_source/list_sources read-only access
└── vault.py                        # compose_note()/render_frontmatter() reused for output formatting

tests/
└── test_extraction_dump.py     # unit + integration tests for this component
```

Internal organization of `extraction_dump.py` itself (150 lines, no classes):

```
extraction_dump.py
├── DEFAULT_DUMP_SUBDIR = "extraction-dumps"       # constant
├── _HEADING_RE                                     # module-level compiled regex
├── default_dump_dir(cfg) -> Path                   # cfg.cache_path / DEFAULT_DUMP_SUBDIR
├── dump_filename(citekey) -> str                   # "extraction-{sanitized}.md"
├── list_headings(text) -> list[(level, title)]      # pure function, no I/O
├── render_extraction_dump(source, text, cfg) -> str # pure function, builds full Markdown string
├── write_extraction_dump(dir, source, text, cfg) -> Path   # I/O: mkdir + write_text
├── dump_source_extraction(cfg, db, source_id, dir) -> Path|None   # single-source, DB-backed
└── run_dump_extraction(cfg, db, source_id?, dir?) -> {sources, skipped}  # single-or-all, DB-backed
```

## 5. Dependency Analysis

```
Internal Dependencies:
extraction_dump.dump_filename          -> chunk_dump.sanitize_citekey
extraction_dump.render_extraction_dump -> vault.compose_note -> vault.render_frontmatter
extraction_dump.write_extraction_dump  -> extraction_dump.render_extraction_dump, extraction_dump.dump_filename
extraction_dump.dump_source_extraction -> state.StateDB.get_source, extraction_dump.write_extraction_dump
extraction_dump.run_dump_extraction    -> state.StateDB.get_source / list_sources, extraction_dump.write_extraction_dump
extraction_dump.default_dump_dir       -> config.AppConfig.cache_path

Callers (inbound, not part of this component):
harvester._maybe_dump_extraction -> extraction_dump.dump_source_extraction
harvester._process_file          -> harvester._maybe_dump_extraction (after db.update_source_texts)
cli.harvest_cmd                  -> cli._resolve_extraction_dump_dir -> extraction_dump.default_dump_dir
cli.dump_extraction_cmd          -> extraction_dump.default_dump_dir, extraction_dump.run_dump_extraction

External Dependencies:
- Python standard library: `re` (heading regex), `pathlib.Path` (filesystem), `logging`
- No third-party packages imported directly by this module
- Transitively via vault.compose_note: `PyYAML` (yaml.dump for frontmatter rendering)
- Transitively via state.StateDB: SQLite (stdlib `sqlite3`, WAL mode) — read-only from this component's perspective
```

The component has **zero direct external (third-party) dependencies** of its own; everything beyond the standard library is inherited indirectly through `vault.py` (YAML) and `state.py` (SQLite).

## 6. Afferent and Efferent Coupling

Coupling analyzed at function granularity (the module has no classes).

| Function | Afferent Coupling (called by) | Efferent Coupling (calls out to) | Critical |
|----------|-------------------------------|-----------------------------------|----------|
| `render_extraction_dump` | 2 (`write_extraction_dump`, `test_extraction_dump.py` directly) | 2 (`list_headings`, `vault.compose_note`) | Medium |
| `write_extraction_dump` | 3 (`dump_source_extraction`, `run_dump_extraction`, tests) | 3 (`render_extraction_dump`, `dump_filename`, `Path.mkdir`) | High |
| `dump_source_extraction` | 2 (`harvester._maybe_dump_extraction`, tests) | 2 (`StateDB.get_source`, `write_extraction_dump`) | High |
| `run_dump_extraction` | 2 (`cli.dump_extraction_cmd`, tests) | 3 (`StateDB.get_source`, `StateDB.list_sources`, `write_extraction_dump`) | High |
| `list_headings` | 2 (`render_extraction_dump`, tests) | 1 (`_HEADING_RE.finditer`) | Low |
| `dump_filename` | 2 (`write_extraction_dump`, tests) | 1 (`chunk_dump.sanitize_citekey`) | Low |
| `default_dump_dir` | 3 (`cli.harvest_cmd` via `_resolve_extraction_dump_dir`, `cli.dump_extraction_cmd`, `run_dump_extraction`) | 0 (pure path arithmetic on `cfg.cache_path`) | Low |

`write_extraction_dump`, `dump_source_extraction`, and `run_dump_extraction` are the highest-risk functions: they sit at the intersection of the most inbound callers (both CLI paths and the harvester) and the most outbound dependencies (StateDB, filesystem, and the rendering pipeline), making them the functions most likely to need coordinated changes if the `sources` schema, frontmatter format, or filename convention changes.

## 7. Endpoints

Not applicable — this component exposes no REST/GraphQL/gRPC endpoints. It is exposed exclusively through two CLI surfaces:

| Interface | Command | Description |
|-----------|---------|--------------|
| Typer CLI flag | `zettel harvest --dump-extraction` | Enables inline extraction dump for every newly-harvested source in the run, into the default directory |
| Typer CLI flag | `zettel harvest --dump-extraction-dir <path>` | Same as above, into an explicit directory (implies enabling the dump) |
| Typer CLI command | `zettel dump-extraction --source-id @Citekey [--dump-dir <path>]` | Re-export one already-harvested source's persisted extraction, without re-extracting |
| Typer CLI command | `zettel dump-extraction --all [--dump-dir <path>]` | Re-export every source in `sources` that has persisted `extracted_text` |

## 8. Integration Points

| Integration | Type | Purpose | Protocol | Data Format | Error Handling |
|-------------|------|---------|----------|-------------|-----------------|
| SQLite (`sources` table via `StateDB`) | Internal data store | Read `extracted_text`, `citekey`, `title`, `origin_path`, `origin_type` per source | Direct function calls (`get_source`/`list_sources`) | Python `dict` rows | Missing source_id -> `ValueError` (single) or entry skipped (batch, via empty-text guard for existing-but-empty rows) |
| Local filesystem (`cache/extraction-dumps/` or caller dir) | Internal storage | Persist one Markdown file per source | `pathlib.Path` write | UTF-8 Markdown with YAML frontmatter | No explicit error handling — a filesystem error (permissions, disk full) propagates as an unhandled `OSError` to the CLI, which has no dedicated catch for it (only `ValueError` is caught in `dump_extraction_cmd`) |
| `AppConfig` (`config.yaml` / defaults) | Internal configuration | Reads `cache_path` (default dump location) and `pdf_extractor` (frontmatter label) | In-process object attribute access | Pydantic model | N/A — config is always fully resolved before this component runs (fails earlier in `_load_deps` if invalid) |
| `harvester.py` (Phase 1 pipeline) | Internal pipeline hook | Triggers the dump immediately after `extracted_text` is persisted for a new source | Direct function call (`_maybe_dump_extraction`) | In-memory `source_id: str` | Silent no-op if `dump_dir is None`; any exception from `dump_source_extraction` would propagate up into the harvest run (not separately caught) |

## 9. Design Patterns & Architecture

| Pattern | Implementation | Location | Purpose |
|---------|-----------------|----------|---------|
| Opt-in diagnostic export (project convention) | `dump_dir: Path | None` parameter threaded through `run_harvest` -> `_process_file` -> `_maybe_dump_extraction`; `None` = disabled | zettel/harvester.py:65, 450-456 | Keeps normal harvest runs free of side effects; mirrors `chunk_dump.py`'s identical convention |
| Pure function / I/O separation | `list_headings` and `render_extraction_dump` are pure (no I/O, deterministic given inputs); `write_extraction_dump` is the sole I/O boundary | zettel/extraction_dump.py:36-71 vs. 74-87 | Testability — the bulk of the test suite exercises rendering logic without touching the filesystem or a DB |
| Cross-module reuse over duplication | Imports `sanitize_citekey` from `chunk_dump.py` instead of re-implementing filename sanitization; imports `compose_note` from `vault.py` instead of re-implementing frontmatter rendering | zettel/extraction_dump.py:16,19 | Single source of truth for filename-safety rules and note-composition format shared with the sibling chunk-dump feature and the main vault writer |
| Read-only façade over `StateDB` | Only `get_source`/`list_sources` are called; no `upsert_*` or other mutating method is ever invoked | zettel/extraction_dump.py:90-149 | Lets the feature be safely re-run at any time (including on a live/production vault) without risk of corrupting pipeline state |
| Graceful batch degradation | `run_dump_extraction`'s `--all` loop uses `continue` on a per-source skip condition rather than aborting the whole batch | zettel/extraction_dump.py:136-149 | Lets `--all` produce partial, useful output across a vault with mixed harvest vintages instead of an all-or-nothing failure |
| Dual entry point for one capability (inline vs. standalone) | Same rendering/writing core (`write_extraction_dump`) is reached both from harvester's inline hook and the CLI's standalone re-export command | zettel/harvester.py:455-456; zettel/extraction_dump.py:126-149 | Avoids needing a full harvest re-run just to regenerate a diagnostic dump for an already-processed source |

## 10. Technical Debt & Risks

| Risk Level | Component Area | Issue | Impact |
|------------|-----------------|-------|--------|
| Medium | `harvester._complete_incomplete_source` | This alternate "resume chunking for an existing source" code path never calls `_maybe_dump_extraction`, unlike the main new-source path | A source recovered via the incomplete-chunking-repair path produces **no** extraction dump even when `--dump-extraction` is active, which is inconsistent and could confuse an operator relying on the flag to always capture every processed source in a run |
| Low | `render_extraction_dump` / `pdf_extractor` field | The frontmatter's `pdf_extractor` value is always the **current** `cfg.pdf_extractor`, not the extractor actually used to produce that specific source's persisted text (no per-source extractor column exists in `sources`) | Re-dumping older sources after switching `pdf_extractor` in config produces dumps mislabeled with the new extractor name — misleading during extractor-comparison/migration work |
| Low | `write_extraction_dump` overwrite behavior | No versioning/backup of a previous dump for the same citekey; every write silently truncates the prior file | A user cannot diff "extraction before vs. after a config/Docling version change" from the dump directory alone without manually renaming/copying files between runs |
| Low | Filesystem error handling | Neither `write_extraction_dump` nor the CLI's `dump_extraction_cmd` explicitly catches filesystem errors (e.g. permission denied, disk full, invalid `--dump-dir` on a read-only mount) | Such failures surface as raw, unhandled Python tracebacks to the CLI user rather than a friendly message consistent with the `ValueError` handling already in place for a missing source |
| Low | `run_dump_extraction` on very large vaults | `--all` calls `db.list_sources()` (unbounded `SELECT * FROM sources ORDER BY created_at DESC`) and loops synchronously, writing one file per source with no batching, progress reporting, or parallelism | On a vault with very many sources this could be slow with no interim feedback beyond per-skip log lines; no test covers behavior at scale |
| Informational | `pdf_extractor` metadata field naming | The field is named `pdf_extractor` even though it also describes non-PDF (native Markdown) sources' dumps, since `cfg.pdf_extractor` is a single global config value applying regardless of `origin_type` | Not a functional bug, but a naming choice that can read oddly in a dump for an `origin_type: md` source (e.g. `pdf_extractor: docling` on a source that never touched Docling) |

## 11. Test Coverage Analysis

Test file located at `tests/test_extraction_dump.py` (228 lines, 9 test functions, no test classes). No other test file in the repository (outside the excluded `.venv`/`data`/`vault` folders) references `extraction_dump` symbols directly, other than one integration assertion inside this same file that drives the real `harvester._process_file`.

| Function / Area | Unit Tests | Integration Tests | Coverage (functional) | Test Quality |
|-------------------|------------|---------------------|--------------------------|---------------|
| `dump_filename` / `sanitize_citekey` reuse | 1 (`test_dump_filename_sanitizes_citekey`) | 0 | Full (happy path + special-char sanitization) | Good — asserts exact sanitized output for a citekey containing `:` and `/` |
| `list_headings` | 1 (`test_list_headings_collects_h1_to_h6`) | 0 | Full (all 6 heading levels + no-heading case) | Good — precise expected tuple list; also covers the empty-input edge case |
| `render_extraction_dump` | 2 (`test_render_includes_frontmatter_outline_and_raw_text`, `test_render_no_headings_notes_empty_outline`) | 0 | Full (frontmatter fields, heading outline ordering, raw text preservation, empty-outline placeholder) | Good — checks structural ordering ("Texto extraido" section starts with the original H1) not just substring presence |
| `write_extraction_dump` | 1 (`test_write_extraction_dump_creates_sanitized_file`) | 0 | Full (path correctness, sanitized filename, overwrite behavior explicitly verified) | Good — the overwrite assertion (old content must be absent after a second write) is a meaningful regression guard |
| `run_dump_extraction` | 3 (`test_run_dump_extraction_writes_from_sqlite`, `test_run_dump_extraction_missing_source_raises`, `test_run_dump_extraction_skips_source_without_text`) | 0 (uses a real, temp-file-backed `StateDB`, but not the CLI layer) | Good — covers success, hard-error (`ValueError`), and soft-skip paths | Good assertions on the returned stats dict; does not cover the `--all` multi-source aggregation path (only ever exercises a single source via `db.list_sources()` implicitly having one row, never explicitly tests 2+ sources mixed written/skipped in one `--all` call) |
| `dump_source_extraction` | 1 (`test_dump_source_extraction_loads_db`) | 0 | Good — covers success, missing source (returns `None`), and empty-text source (returns `None`) in one test | Reasonably thorough but combines three assertions in a single test function rather than separating them |
| `harvester._process_file` inline dump wiring | 0 | 1 (`test_process_file_writes_extraction_dump_when_dir_set`) | Partial | Good use of a `_FakeIdx` test double and a real `StateDB`/vault directory; verifies the dump file is produced with correct headings and body during a real (non-mocked) harvest call. Does **not** cover: `_complete_incomplete_source` path (see Technical Debt — this path never dumps, and no test documents/locks that behavior either as intended or as a bug), nor a PDF-origin source (only exercises a native `.md` file) |
| `cli.dump_extraction_cmd` (the actual Typer command) | 0 | 0 | None | Gap — no test invokes the CLI command itself (e.g. via `typer.testing.CliRunner`); the CLI-level behaviors (mutually-exclusive `--source-id`/`--all` validation, red-error/exit-1 on `ValueError`, success/skip message formatting) are entirely untested at that layer, though the underlying `run_dump_extraction` logic they wrap is well covered |
| `cli._resolve_extraction_dump_dir` | 0 | 0 | None | Gap — the flag-resolution logic (`--dump-extraction-dir` implying enablement even without `--dump-extraction`) has no dedicated test; only exercised indirectly and incompletely through the harvester integration test, which passes `extraction_dump_dir` directly rather than going through the CLI resolver |

Overall: the module's own pure logic and its two public DB-backed entry points are well tested with clear, focused assertions. The two coverage gaps worth flagging are (1) the Typer CLI command layer (`dump_extraction_cmd`) and the `--dump-extraction`/`--dump-extraction-dir` flag-resolution helper in `cli.py`, and (2) the `_complete_incomplete_source` harvester branch's interaction (or lack thereof) with the dump feature.
