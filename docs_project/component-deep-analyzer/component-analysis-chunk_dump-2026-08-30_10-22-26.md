# Component Deep Analysis Report: chunk_dump

## 1. Executive Summary

`zettel/chunk_dump.py` is a small, read-only diagnostic component in the Zettelkasten pipeline. Its sole purpose is to export the chunks that are already persisted in SQLite (`chunks` table) for a source into a single human-readable Markdown file, so a maintainer can visually inspect chunking quality (cut points, `section_path`, page mapping, and overlap between consecutive chunks) without re-running extraction or the LLM.

It is explicitly documented as **not a parallel chunker**: the text it renders is byte-for-byte the same text that `extract` (Phase 2) will later feed to the LLM. This makes it a trustworthy debugging aid for tuning `chunking.chunk_size` / `chunking.chunk_overlap` / `chunking.min_section_chars` in `config/config.yaml`.

The component is opt-in and has three entry points, all wired through `zettel/cli.py`:

- `zettel harvest --dump-chunks [--dump-dir DIR]` — dumps chunks for each newly harvested source immediately after chunking (via `zettel/harvester.py::_maybe_dump_chunks`).
- `zettel rechunk --dump-chunks [--dump-dir DIR]` — same, after re-chunking persisted extracted text.
- `zettel dump-chunks --source-id @Citekey | --all [--dump-dir DIR]` — a dedicated, standalone command that reexports already-harvested sources purely by reading SQLite, with no side effects on the pipeline state.

Key findings:

- The module is entirely **read-only with respect to the pipeline state** (SQLite/Chroma/vault notes) — its only write is the diagnostic Markdown file itself, which lives outside the Obsidian vault (`data/cache/chunk-dumps/` by default).
- It is a near-exact structural twin of `zettel/extraction_dump.py` (same `default_dump_dir`/`dump_filename`/`run_dump_*` shape), which is a deliberate, consistent pattern in this codebase for "opt-in diagnostic dump" features rather than duplicated ad hoc code.
- It reuses `zettel.vault.compose_note` (the same frontmatter+body composer used for real vault notes) purely for convenient YAML rendering — the output file is not a vault note and is never indexed, linked, or treated as `origin: manual`.
- Business logic is intentionally minimal and almost entirely presentational/diagnostic: filename sanitization for Windows safety, chunk ordering, and an overlap-detection heuristic are the only non-trivial rules.
- Test coverage of the module itself (`tests/test_chunk_dump.py`) is thorough for its public functions, but there is **no CLI-level test** covering the `dump-chunks` Typer command or the `--dump-chunks`/`--dump-dir` flags wired into `harvest`/`rechunk` in `zettel/cli.py`.

## 2. Data Flow Analysis

There are three distinct entry paths into this component. All three converge on the same rendering/writing primitives (`write_chunk_dump` → `render_chunk_dump`).

**Path A — `zettel harvest --dump-chunks` (inline, per newly harvested source)**

```
1. CLI: harvest command parses --dump-chunks/--dump-dir (zettel/cli.py:283-289)
2. CLI: _resolve_chunk_dump_dir() resolves the flags into an Optional[Path] (zettel/cli.py:391-399)
3. CLI: run_harvest(..., dump_dir=chunk_dump_dir) (zettel/cli.py:319-338)
4. harvester.py: run_harvest() finishes chunking+persisting one source (_process_file)
5. harvester.py: _maybe_dump_chunks(cfg, db, sid, dump_dir) called per new source (harvester.py:143, 439-446)
6. chunk_dump.py: dump_source_chunks(cfg, db, source_id, dump_dir)
7. chunk_dump.py: db.get_source(source_id) + db.get_chunks_for_source(source_id)
8. chunk_dump.py: write_chunk_dump() -> render_chunk_dump() -> vault.compose_note()
9. Filesystem: chunks-{sanitized-citekey}.md written under dump_dir (overwrite semantics)
```

**Path B — `zettel rechunk --dump-chunks` (inline, per re-chunked source)**

```
1. CLI: rechunk command parses --dump-chunks/--dump-dir (zettel/cli.py:802-809)
2. CLI: _resolve_chunk_dump_dir() (shared with harvest, zettel/cli.py:819)
3. CLI: run_rechunk(cfg, db, idx, source_id, dump_dir=chunk_dump_dir) (zettel/cli.py:821-825)
4. harvester.py: run_rechunk() rebuilds chapters/chunks from sources.extracted_text
5. harvester.py: _maybe_dump_chunks(cfg, db, sid, dump_dir) per source (harvester.py:208)
6. chunk_dump.py: dump_source_chunks(...) -> write_chunk_dump(...) -> render_chunk_dump(...)
7. Filesystem: chunks-{citekey}.md written/overwritten
```

**Path C — `zettel dump-chunks` (standalone, read-only reexport, no reprocessing)**

```
1. CLI: dump_chunks_cmd() requires --source-id or --all (zettel/cli.py:844-857)
2. CLI: resolves dest dir via default_dump_dir(cfg) or --dump-dir (zettel/cli.py:861-864)
3. chunk_dump.py: run_dump_chunks(cfg, db, source_id, dump_dir=dest)
4a. If source_id given: db.get_source(source_id); raises ValueError if not found
4b. If --all: db.list_sources() (all rows, no filtering)
5. For each source: db.get_chunks_for_source(source["source_id"])
6. write_chunk_dump(dest, src, chunks, cfg) -> render_chunk_dump(...) -> compose_note(...)
7. Filesystem: one chunks-{citekey}.md per source; stats {"sources": N} returned/printed
```

**Internal rendering pipeline (shared by all three paths)**

```
render_chunk_dump(source, chunks, cfg)
  1. overlap_cap = cfg.chunking.chunk_overlap
  2. _annotate_overlap(chunks, overlap_cap):
       a. sort_chunks(chunks)  — order by (chunk_index, chunk_id)
       b. for each chunk in order: overlap_prefix_len(prev_text, curr_text, cap)
  3. Compute stats: n_chunks, chars_total, chars_min/max/mean
  4. Build frontmatter dict: source identity, origin, chunking config snapshot, paging snapshot, stats
  5. Build "## Sumario" bullet list (one line per chunk, ordered)
  6. Build one "# Chunk NNN" section per chunk with full metadata + raw persisted text
  7. compose_note(meta, body) -> YAML frontmatter + "\n" + body
```

## 3. Business Rules & Logic

### Overview of the business rules

| Rule Type | Rule Description | Location |
|-----------|-------------------|----------|
| Read-only guarantee | The dump never mutates SQLite/Chroma/vault; it only reads sources/chunks and writes a diagnostic file | chunk_dump.py:180-218 (docstring "Read-only on the DB") |
| Fidelity guarantee | Rendered text is exactly `chunks.text` as persisted — not re-chunked or reformatted | chunk_dump.py:1-6 (module docstring), chunk_dump.py:159 |
| Filename safety | Citekeys are sanitized to a Windows-safe filename stem before being used in a path | chunk_dump.py:23, 30-33 |
| Fallback identity | If `citekey` is falsy, fall back to `source_id`, then to the literal string `"unknown"` | chunk_dump.py:174 (write_chunk_dump), 30-33 (sanitize_citekey) |
| Deterministic naming | Output filename is always `chunks-{sanitized_citekey}.md`, one file per source | chunk_dump.py:36-37 |
| Overwrite semantics | Writing a dump for the same citekey always overwrites the prior file, no versioning/append | chunk_dump.py:172-177 |
| Deterministic ordering | Chunks are always rendered in ascending `chunk_index` order, tie-broken by `chunk_id` string, regardless of input order or DB row order | chunk_dump.py:55-61 |
| Missing chunk_index fallback | A chunk with no `chunk_index` sorts to the very end (`10**9`) instead of erroring or sorting first | chunk_dump.py:57-58 |
| Overlap diagnostic | For each chunk after the first, compute the longest suffix-of-previous / prefix-of-current match, capped at `chunking.chunk_overlap`, as `overlap_prev` | chunk_dump.py:40-52, 64-78 |
| First-chunk overlap | The first chunk in sorted order always gets `overlap_prev = 0` (no previous text to compare) | chunk_dump.py:69, 73-75 |
| Empty-corpus rendering | A source with zero persisted chunks still produces a valid frontmatter + a `Sumario` placeholder line, never an error | chunk_dump.py:97-104, 127-128 |
| Config snapshot embedding | Every dump embeds the *current* `chunking.chunk_size` / `chunk_overlap` / `min_section_chars` values from the active `AppConfig`, not values captured at chunking time | chunk_dump.py:112-116 |
| Paging snapshot embedding | Every dump embeds the source's current paging fields (`content_start_file_page`, `content_start_book_page`, `page_offset`, `page_offset_confidence`) as persisted on the `sources` row | chunk_dump.py:117-122 |
| Aggregate stats | `n_chunks`, `chars_total`, and (when at least one chunk exists) `chars_min`/`chars_max`/`chars_mean` (rounded to 1 decimal) are always computed and embedded | chunk_dump.py:95-104 |
| Missing-source error (targeted mode) | `run_dump_chunks(source_id=...)` raises `ValueError("Fonte nao encontrada: {source_id}")` if the source does not exist, aborting the whole call | chunk_dump.py:206-208 |
| Missing-source silent skip (single-dump mode) | `dump_source_chunks()` (used by harvest/rechunk inline dumping) instead logs a warning and returns `None` — it never raises | chunk_dump.py:186-190 |
| All-sources mode has no filter | `run_dump_chunks(source_id=None)` dumps **every** row from `db.list_sources()` unconditionally — no status/date/type filtering | chunk_dump.py:210-211 |
| Directory auto-creation | The destination directory (and parents) is created on demand (`mkdir(parents=True, exist_ok=True)`) — the caller never needs to pre-create it | chunk_dump.py:173 |
| Opt-in only | Nothing is dumped unless a caller explicitly passes a `dump_dir` (harvest/rechunk) or invokes the dedicated `dump-chunks` command; default pipeline runs produce zero dump files | chunk_dump.py:443-444 (`_maybe_dump_chunks` in harvester.py returns immediately when `dump_dir is None`) |
| Default location | Absent an explicit `--dump-dir`, dumps land under `{cfg.cache_path}/chunk-dumps/` | chunk_dump.py:21, 26-27 |

### Detailed breakdown of the business rules

---

### Business Rule: Read-Only / Non-Invasive Diagnostic

**Overview**:
The entire component is designed so that invoking it — in any of its three entry paths — can never alter the state of the pipeline (SQLite rows, ChromaDB vectors, or vault `.md` notes). It only performs `SELECT`-equivalent reads (`db.get_source`, `db.get_chunks_for_source`, `db.list_sources`) and a single filesystem write to a diagnostic path outside the Obsidian vault tree.

**Detailed description**:
This matters architecturally because the rest of the pipeline (harvest → extract → review → connect → garden) is stateful: each phase transitions `chunks.status`, `concepts.status`, writes vault notes, and updates ChromaDB collections, and those transitions are the backbone of the pipeline's idempotency and resumability guarantees. A diagnostic tool that accidentally touched any of that state would risk corrupting the pipeline's bookkeeping (for example, accidentally marking a chunk as processed, or writing a stray note that `sync-manual` or `garden` would later pick up as manual content). By construction, `chunk_dump.py` never calls any `StateDB` mutator, `VectorIndex` upsert, or `vault` file-writer other than its own `write_chunk_dump`.

The read-only property is also what makes the dedicated `zettel dump-chunks` command safe to run at any time — including on a production/user vault — without a backup, and safe to run repeatedly in a loop (e.g. after each `chunk_size` tweak) purely for inspection.

The three call sites reinforce this differently: in `harvester.py`, `_maybe_dump_chunks` is called only *after* the chunk transaction for a source has already committed (`_finalize_source_chunking`), so the dump is a pure side observation of state that already exists; the standalone `dump-chunks` CLI command does not even instantiate a `VectorIndex` (`idx`) at all, only a `StateDB`, since it has no need to touch embeddings.

**Rule workflow**:
```
[pipeline commits chunk rows] -> [optional] dump_source_chunks/run_dump_chunks
                                        |
                                        v
                              db.get_source / db.get_chunks_for_source (SELECT only)
                                        |
                                        v
                              write_chunk_dump -> filesystem (cache dir, outside vault)
                                        |
                                        v
                        no SQLite/Chroma/vault-note mutation occurs
```

---

### Business Rule: Text Fidelity ("Same Payload Extract Will See")

**Overview**:
The Markdown body embedded for each chunk is the literal `chunks.text` column value, with no re-splitting, re-normalization, truncation, or markup transformation applied.

**Detailed description**:
The module docstring is explicit that this is "not a parallel chunker" — a deliberate design constraint to avoid the classic pitfall of a debugging tool drifting out of sync with production logic (e.g. a diagnostic script that re-implements chunking slightly differently and gives misleading confidence about what the LLM will actually receive). Because `extractor.py`'s Phase 2 LLM prompt reads chunks from the same `chunks` table via the same `text` column, whatever a maintainer sees in a `chunks-{citekey}.md` dump is guaranteed byte-identical to what `extract` will pass into `prompts/literature_note.md`.

This guarantee is what makes the tool useful for calibrating `chunking.chunk_size`, `chunking.chunk_overlap`, and `chunking.min_section_chars` in `config/config.yaml`: a maintainer can change these values, run `zettel rechunk --all --dump-chunks`, and directly inspect exactly how the new configuration would cut the corpus — without waiting for (or paying for) an LLM call in `extract`.

It also explains why the module intentionally avoids any Markdown-escaping or reformatting of the chunk text: escaping could mask exactly the artifacts (stray headers, dehyphenation errors, section leakage) that this tool exists to surface.

**Rule workflow**:
```
sources.extracted_text --(harvester chunking)--> chunks.text (persisted, unmodified further)
                                                        |
                                                        v
                                     chunk_dump.py reads chunks.text verbatim
                                                        |
                                                        v
                              rendered into "# Chunk NNN" body, no transformation
                                                        |
                                                        v
                    identical bytes later read by extractor.py Phase 2 LLM call
```

---

### Business Rule: Citekey-to-Filename Sanitization

**Overview**:
Citekeys (which can contain characters illegal or awkward in Windows filenames, such as `:`, `/`, `?`) are sanitized before being used to build `chunks-{citekey}.md`.

**Detailed description**:
`sanitize_citekey` replaces every run of characters outside `[A-Za-z0-9._-]` with a single underscore, then strips leading/trailing `.`, `_`, `-` from the result. If the result is empty (e.g. the citekey was composed entirely of unsafe characters, such as `"???"`), the sanitizer falls back to the literal string `"unknown"`. This guards specifically against Windows path restrictions (the project's CLAUDE.md explicitly flags Windows filename/console safety as a project-wide concern), since citekeys are user/LLM-derived (`AuthorYear` style) and not guaranteed to be filesystem-safe by construction elsewhere in the pipeline.

`write_chunk_dump` additionally guards against a missing citekey value entirely (`source.get("citekey") or source.get("source_id") or "unknown"`), so even a malformed or partially-migrated `sources` row can never crash the dump — it will just produce a less specific but still-valid filename.

Because the filename is derived purely from the (sanitized) citekey and not from `source_id` or a random suffix, this design implies that two distinct sources cannot share the exact same citekey without their dumps colliding — a scenario prevented upstream by the harvester/citekey-generation logic (out of scope for this component), not by `chunk_dump.py` itself.

**Rule workflow**:
```
citekey (raw, may contain ":", "/", etc.)
        |
        v
_UNSAFE_FILENAME.sub("_", citekey)         # collapse unsafe runs to "_"
        |
        v
.strip("._-")                               # trim boundary junk
        |
        v
empty? --yes--> "unknown"
   |no
   v
dump_filename() -> f"chunks-{safe}.md"
```

---

### Business Rule: Deterministic Chunk Ordering for Rendering

**Overview**:
Regardless of the order chunks are fetched from SQLite or passed into the renderer, the dump always presents them sorted by `chunk_index` ascending, with `chunk_id` as a string tie-breaker, and chunks lacking an index are pushed to the end.

**Detailed description**:
`db.get_chunks_for_source` has no `ORDER BY` clause (`SELECT * FROM chunks WHERE source_id=?`), so the raw row order is whatever SQLite happens to return (typically insertion order, but not contractually guaranteed). `sort_chunks` normalizes this by sorting on `(order, str(chunk_id))` where `order = int(chunk_index)` when present, or a large sentinel (`10**9`) when `chunk_index` is `None`. This guarantees the "Sumario" summary list and the "# Chunk NNN" sections always appear in reading order matching how the source's original text was segmented, which is essential for a human visually scanning the dump to judge chunk-boundary quality — an unordered or DB-order dump would make it much harder to spot overlap/boundary problems.

The `chunk_id` string tie-breaker is a secondary, deterministic disambiguator for the (presumably rare/anomalous) case of two chunks sharing the same `chunk_index` — it does not attempt any semantic ordering, just stable, reproducible output across repeated runs on the same data.

Pushing indexless chunks to the end (rather than the start, or raising an error) reflects a defensive design choice: such chunks are anomalous (every chunk should have a `chunk_index` after normal chunking), but the dump tool must not crash or hide data because of this — it surfaces them, just at the end and with `#???` shown as their index in the Sumario line (`idx_s = "???"` when `chunk_index` is `None`).

**Rule workflow**:
```
input chunks (arbitrary order)
        |
        v
sort_chunks(): key = (int(chunk_index) if present else 10**9, str(chunk_id))
        |
        v
stable ascending sort
        |
        v
ordered list used for both Sumario bullets and "# Chunk NNN" sections
```

---

### Business Rule: Overlap Diagnostic Heuristic (`overlap_prefix_len`)

**Overview**:
For each chunk after the first (in sorted order), the tool computes how many characters at the start of the current chunk's text match the end of the previous chunk's text, capped at the configured `chunking.chunk_overlap`, and reports it as `overlap_prev`.

**Detailed description**:
This is a purely diagnostic heuristic, not a correctness check enforced elsewhere in the pipeline. LangChain's recursive/character text splitter (used in `harvester.py` for chunking oversized sections) is expected to produce a shared boundary of up to `chunk_overlap` characters between consecutive pieces of the *same* chapter/section. `overlap_prefix_len(prev, curr, cap)` searches from `min(len(prev), len(curr), cap)` down to `1` for the longest `n` such that `prev` ends with `curr[:n]`, returning the first (i.e., longest) match found, or `0` if none exists or either string is empty or `cap <= 0`.

This gives a maintainer a quick visual signal, chunk-by-chunk, of whether the configured overlap is actually being honored end-to-end through chapter/section boundaries — since sections are chunked independently (each section may be merged with the next if under `min_section_chars`, per `harvester.py`), a `overlap_prev = 0` at a chapter/section boundary is expected and normal, whereas an unexpectedly low overlap *within* what should be one continuous long section may indicate a chunking misconfiguration.

Because the check is a **suffix-of-previous / prefix-of-current** string match rather than any chunk-identity or LangChain-internal introspection, it is a black-box, corpus-level heuristic: it works purely off the persisted `text` values and would flag a "coincidental" overlap the same way it flags a "designed" overlap. The comment in the code makes this limitation explicit: "diagnostic for `chunk_overlap`," not a hard invariant.

**Rule workflow**:
```
prev_text = "" (seed)
for chunk in sorted(chunks):
    curr_text = chunk.text
    overlap_prev = overlap_prefix_len(prev_text, curr_text, cap=chunking.chunk_overlap) if prev_text else 0
    annotate chunk with overlap_prev
    prev_text = curr_text   # becomes "prev" for the next iteration
```
```
overlap_prefix_len(prev, curr, cap):
    if prev empty or curr empty or cap <= 0: return 0
    for n from min(len(prev), len(curr), cap) down to 1:
        if prev.endswith(curr[:n]): return n
    return 0
```

---

### Business Rule: Config/Paging Snapshot Embedding (Reproducibility Metadata)

**Overview**:
Every rendered dump embeds, in its YAML frontmatter, a snapshot of the *currently active* `AppConfig.chunking` values and the *currently persisted* `sources` paging fields — not values frozen at the time the chunks were originally created.

**Detailed description**:
This is a deliberate choice with a specific implication: if a maintainer changes `chunk_size` in `config/config.yaml` and reruns `zettel dump-chunks` **without** first running `zettel rechunk`, the dump will show the *old* persisted chunk boundaries (since `dump-chunks` never re-chunks) but will report the *new* `chunk_size`/`chunk_overlap`/`min_section_chars` values in its frontmatter — a potential source of confusion if not understood. The three-path design (harvest/rechunk inline dump vs. standalone `dump-chunks`) matters here: only the inline dumps (Path A/B) are guaranteed to reflect chunks actually produced under the config values shown, because they run immediately after chunking with that exact config in effect. The standalone `dump-chunks` command is explicitly a "what's in the DB right now" reexport tool and does not claim the config values it displays produced the chunks it shows.

The paging block similarly always reflects the source's *current* `content_start_file_page` / `content_start_book_page` / `page_offset` / `page_offset_confidence` — these can be repaired independently via `zettel set-paging` without touching chunk text, so a dump taken after a paging repair will show updated paging metadata alongside unchanged chunk boundaries and unchanged `page_in_file`/`page_in_book` per-chunk values (those are only updated by `set-paging`, not derived at dump time).

This snapshot-at-render-time behavior is why the report treats `chunking`/`paging` blocks as *reproducibility metadata* for a human reader, not as an audit trail of what produced the specific chunks shown — there is no separate "chunking config used to create this chunk" column persisted per chunk.

**Rule workflow**:
```
render_chunk_dump(source, chunks, cfg):
    meta.chunking = {chunk_size, chunk_overlap, min_section_chars} <- cfg.chunking (LIVE, current AppConfig)
    meta.paging   = {content_start_file_page, content_start_book_page,
                      page_offset, page_offset_confidence} <- source (LIVE, current sources row)
    # Neither block is guaranteed to match the config/paging state
    # that was active when the chunk rows were originally persisted,
    # except in the harvest/rechunk inline-dump paths (A/B) where the
    # dump happens immediately after chunking under that same cfg.
```

---

### Business Rule: Error Handling Divergence Between Targeted and Bulk/Inline Dump Paths

**Overview**:
A missing source is handled differently depending on which function is called: `run_dump_chunks(source_id=...)` raises `ValueError`, while `dump_source_chunks(...)` (used by the inline harvest/rechunk paths) logs a warning and returns `None`.

**Detailed description**:
`run_dump_chunks` backs the standalone `zettel dump-chunks --source-id @X` command. When the user explicitly names a source that does not exist, the CLI (`zettel/cli.py:869-872`) catches the resulting `ValueError`, prints it in red, and exits with status `1` — appropriate for a direct, user-invoked CLI command where a typo'd citekey should be a visible, actionable error.

`dump_source_chunks`, by contrast, backs the internal `_maybe_dump_chunks` helper called automatically after each source is harvested or rechunked (`harvester.py:439-446`). In that context the caller (`harvester.py`) already knows the `source_id` is valid — it was just persisted in the same call — so a missing-source condition here would indicate an internal inconsistency rather than user error, and the design choice is to log-and-continue (`logger.warning`, return `None`) rather than raise and potentially abort an otherwise-successful harvest/rechunk run over a purely diagnostic side effect. This means a dump failure due to a missing source can *never* fail a harvest or rechunk run; it can only fail (loudly, by design) an explicit `dump-chunks` invocation.

`run_dump_chunks(source_id=None)` (the `--all` path) has no such per-source error path at all: `db.list_sources()` always returns a (possibly empty) list, and every returned row is assumed valid since it came directly from the `sources` table.

**Rule workflow**:
```
run_dump_chunks(source_id="@X"):
    src = db.get_source("@X")
    if src is None: raise ValueError("Fonte nao encontrada: @X")   # -> CLI prints red error, exit(1)
    ...

dump_source_chunks(cfg, db, "@X", dump_dir):   # internal, called from harvester.py
    src = db.get_source("@X")
    if src is None:
        logger.warning(...)
        return None                                                  # harvest/rechunk run continues normally
    ...
```

---

## 4. Component Structure

```
zettel/
└── chunk_dump.py                      # Entire component: single-file module, no package/subfolder
    ├── DEFAULT_DUMP_SUBDIR             # "chunk-dumps" constant
    ├── _UNSAFE_FILENAME                # compiled regex for filename sanitization
    ├── default_dump_dir(cfg)           # cfg.cache_path / "chunk-dumps"
    ├── sanitize_citekey(citekey)       # Windows-safe filename stem
    ├── dump_filename(citekey)          # "chunks-{safe}.md"
    ├── overlap_prefix_len(prev, curr, cap)   # suffix/prefix overlap heuristic
    ├── sort_chunks(chunks)             # order by (chunk_index, chunk_id)
    ├── _annotate_overlap(chunks, cap)  # attaches overlap_prev per chunk, in order
    ├── _fmt(value)                     # None -> "", else str(value)
    ├── render_chunk_dump(source, chunks, cfg) -> str   # builds frontmatter + Markdown body
    ├── write_chunk_dump(dump_dir, source, chunks, cfg) -> Path   # renders + writes file (overwrite)
    ├── dump_source_chunks(cfg, db, source_id, dump_dir) -> Path | None   # single-source, tolerant of missing source
    └── run_dump_chunks(cfg, db, source_id=None, dump_dir=None) -> dict[str, int]  # CLI-facing: one or all sources

External call sites (not part of the component, shown for boundary clarity):
zettel/harvester.py
    ├── run_harvest(..., dump_dir=...)      -> _maybe_dump_chunks() -> chunk_dump.dump_source_chunks()
    └── run_rechunk(..., dump_dir=...)      -> _maybe_dump_chunks() -> chunk_dump.dump_source_chunks()

zettel/cli.py
    ├── harvest command       --dump-chunks/--dump-dir -> _resolve_chunk_dump_dir() -> run_harvest(dump_dir=...)
    ├── rechunk command       --dump-chunks/--dump-dir -> _resolve_chunk_dump_dir() -> run_rechunk(dump_dir=...)
    └── dump_chunks_cmd (dump-chunks)  --source-id/--all/--dump-dir -> chunk_dump.run_dump_chunks()
```

There is no dedicated test subfolder structure beyond the single top-level test file:

```
tests/
└── test_chunk_dump.py    # unit tests for the module's public + one internal-adjacent behavior
```

## 5. Dependency Analysis

```
Internal Dependencies (compile-time imports):
chunk_dump.py -> zettel.config.AppConfig        (type-only: cfg.cache_path, cfg.chunking.*)
chunk_dump.py -> zettel.state.StateDB           (type-only + runtime: get_source, get_chunks_for_source, list_sources)
chunk_dump.py -> zettel.vault.compose_note      (runtime: frontmatter + body composition)

Internal Dependencies (runtime call graph, this component as callee):
zettel.harvester._maybe_dump_chunks -> chunk_dump.dump_source_chunks
zettel.cli.dump_chunks_cmd          -> chunk_dump.run_dump_chunks, chunk_dump.default_dump_dir
zettel.cli._resolve_chunk_dump_dir  -> chunk_dump.default_dump_dir

Internal Dependencies (data dependency, not import):
chunk_dump.py reads AppConfig.chunking.{chunk_size, chunk_overlap, min_section_chars} — schema
  owned by zettel/config.py::ChunkingConfig; any rename there breaks this module silently
  at attribute-access time (no explicit contract/interface).
chunk_dump.py reads sources.* and chunks.* dict keys — schema owned by zettel/state.py's
  SQL DDL (CREATE TABLE sources / chunks); relies on sqlite3.Row -> dict conversion in
  StateDB._fetchone/_fetchall.

External Dependencies:
- Python standard library only: logging, re, pathlib.Path, typing.Any
- No third-party package imports directly in chunk_dump.py
  (PyYAML is used transitively via zettel.vault.render_frontmatter, not imported here)
- Filesystem: writes to a local directory (default data/cache/chunk-dumps/), no network I/O,
  no external service calls, no database driver used directly (delegates all DB access to StateDB)
```

## 6. Afferent and Efferent Coupling

The codebase is not class-based for this component (module-level functions, plain dicts as data carriers), so the unit of coupling analysis is the function/module level rather than classes.

| Component (function/module) | Afferent Coupling (callers) | Efferent Coupling (calls out to) | Critical |
|---|---|---|---|
| `chunk_dump.run_dump_chunks` | 1 (cli.dump_chunks_cmd) | 3 (db.get_source, db.list_sources, write_chunk_dump) | Medium |
| `chunk_dump.dump_source_chunks` | 1 (harvester._maybe_dump_chunks) | 3 (db.get_source, db.get_chunks_for_source, write_chunk_dump) | Medium |
| `chunk_dump.write_chunk_dump` | 3 (dump_source_chunks, run_dump_chunks, tests) | 2 (render_chunk_dump, filesystem Path.write_text) | High |
| `chunk_dump.render_chunk_dump` | 1 (write_chunk_dump) | 3 (_annotate_overlap, compose_note, cfg.chunking access) | High |
| `chunk_dump._annotate_overlap` | 1 (render_chunk_dump) | 2 (sort_chunks, overlap_prefix_len) | Medium |
| `chunk_dump.sort_chunks` | 1 (_annotate_overlap) | 0 | Low |
| `chunk_dump.overlap_prefix_len` | 1 (_annotate_overlap) | 0 | Low |
| `chunk_dump.sanitize_citekey` | 1 (dump_filename) | 0 | Low |
| `chunk_dump.dump_filename` | 1 (write_chunk_dump) | 1 (sanitize_citekey) | Low |
| `chunk_dump.default_dump_dir` | 2 (cli._resolve_chunk_dump_dir, cli.dump_chunks_cmd) | 0 (reads cfg.cache_path) | Low |
| `chunk_dump._fmt` | many (used throughout render_chunk_dump for every field) | 0 | Low |

Notes on criticality: `render_chunk_dump`/`write_chunk_dump` are marked High because they are the single choke point through which all three entry paths (harvest, rechunk, dump-chunks) produce output — a defect there affects every consumer uniformly. `run_dump_chunks`/`dump_source_chunks` are Medium: each has exactly one caller today, but that caller is a distinct, user-facing pipeline surface (CLI command vs. inline harvest/rechunk hook), so a defect is visible but scoped to one path.

## 7. Endpoints

This component exposes no network/API endpoints (REST, GraphQL, gRPC, etc.) — it is a library module invoked in-process by the CLI and by `harvester.py`. Its externally observable "surface" is instead a set of CLI command/flag combinations, documented here for completeness in lieu of a network endpoint table:

| CLI Surface | Flags | Description |
|---|---|---|
| `zettel harvest` | `--dump-chunks`, `--dump-dir DIR` | Opt-in inline chunk dump for each newly harvested source |
| `zettel rechunk` | `--dump-chunks`, `--dump-dir DIR` | Opt-in inline chunk dump for each re-chunked source |
| `zettel dump-chunks` | `--source-id @Citekey` \| `--all`, `--dump-dir DIR` | Standalone reexport of already-persisted chunks, no reprocessing |

## 8. Integration Points

| Integration | Type | Purpose | Protocol | Data Format | Error Handling |
|---|---|---|---|---|---|
| SQLite (`StateDB`) | Internal data store | Read `sources` and `chunks` rows to render a dump | In-process (sqlite3 driver via StateDB) | Python dict rows (via `sqlite3.Row` -> `dict`) | `run_dump_chunks` raises `ValueError` on missing targeted source (propagates to CLI, exit 1); `dump_source_chunks` logs a warning and returns `None` on missing source (does not raise) |
| Local filesystem | Internal | Persist the rendered Markdown dump file | Direct file I/O (`Path.write_text`, UTF-8) | Markdown with YAML frontmatter | No explicit try/except around the write — an OS-level write failure (e.g. permissions, disk full) propagates as an uncaught exception up through the CLI command |
| `zettel.vault.compose_note` | Internal library call | Reuse the vault's frontmatter+body composer for consistent Markdown output | In-process function call | YAML (via PyYAML `yaml.dump`) + Markdown body | No error handling in `chunk_dump.py`; any YAML serialization failure (e.g. non-serializable value in `meta`) would propagate uncaught |
| `zettel.config.AppConfig` | Internal config object | Source of `cache_path` (default dump location) and `chunking.*` (rendered + used for overlap cap) | In-process attribute access | Pydantic model | No validation performed by this component; assumes `cfg.chunking.chunk_overlap` is coercible to `int` (`int(cfg.chunking.chunk_overlap)`), which could raise `TypeError`/`ValueError` on a malformed config, uncaught |

## 9. Design Patterns & Architecture

| Pattern | Implementation | Location | Purpose |
|---|---|---|---|
| Facade / thin adapter over `StateDB` | `dump_source_chunks`, `run_dump_chunks` wrap `db.get_source`/`get_chunks_for_source`/`list_sources` behind dump-specific functions | chunk_dump.py:180-218 | Isolates the CLI/harvester callers from needing to know SQLite fetch details |
| Pure function core / impure shell | `render_chunk_dump` (pure: dict/list in, str out) is separated from `write_chunk_dump` (impure: filesystem side effect) | chunk_dump.py:87-177 | Enables the extensive unit testing seen in `test_chunk_dump.py` without touching disk for most assertions |
| Opt-in feature flag / "None means off" | `dump_dir: Path | None` threaded through `run_harvest`/`run_rechunk`; `_maybe_dump_chunks` short-circuits when `None` | harvester.py:439-446 | Zero-cost, zero-risk default: normal pipeline runs are unaffected unless a caller opts in |
| Sibling-module symmetry (parallel diagnostic dumps) | `chunk_dump.py` mirrors `extraction_dump.py` almost function-for-function (`default_dump_dir`, `dump_filename`/naming, `run_dump_*`, `dump_source_*`) | chunk_dump.py vs. extraction_dump.py | Consistent mental model and CLI ergonomics across the two "inspect persisted X" diagnostic features (chunks vs. extracted text) |
| Idempotent overwrite (no append/versioning) | `write_chunk_dump` always writes to the same deterministic path per citekey | chunk_dump.py:172-177 | Dump always reflects the latest state; avoids unbounded accumulation of stale dump files across repeated runs |
| Snapshot-metadata-in-frontmatter | Live `cfg.chunking` + live `source` paging fields embedded in every render | chunk_dump.py:106-124 | Makes each dump file self-describing for later inspection without needing the exact `config.yaml` that produced it (with the caveat noted in the business rules section) |

## 10. Technical Debt & Risks

| Risk Level | Component Area | Issue | Impact |
|---|---|---|---|
| Medium | `run_dump_chunks` / `dump_source_chunks` | Two functions with near-identical purpose (fetch source+chunks, then `write_chunk_dump`) diverge in error handling (raise vs. log-and-skip) with no shared helper documenting *why* — the distinction is discoverable only by reading both call sites | A future refactor could unintentionally unify the behavior and break either the CLI's fail-fast UX or the harvester's fail-soft resilience |
| Low-Medium | `render_chunk_dump` | No exception handling around `int(cfg.chunking.chunk_overlap)`; a malformed/missing config value would raise an uncaught `TypeError`/`ValueError` all the way up through `write_chunk_dump` -> `dump_source_chunks`/`run_dump_chunks` -> CLI, potentially aborting a harvest/rechunk run over a purely diagnostic feature | Since `_maybe_dump_chunks` in harvester.py has no try/except around the call, a dump failure in Path A/B (harvest/rechunk) is not isolated from the primary pipeline operation it's attached to — an opt-in diagnostic could crash an otherwise-successful harvest run |
| Low | `write_chunk_dump` | No error handling around `Path.write_text` — a permissions error or full disk on the diagnostic dump would raise uncaught, again potentially interrupting harvest/rechunk (same isolation gap as above) | Same as above: failure blast radius extends beyond the diagnostic feature itself |
| Low | `sort_chunks` fallback ordering | Chunks without `chunk_index` are sorted using a large sentinel (`10**9`); if more than one indexless chunk exists, they fall back to `chunk_id` string ordering only among themselves, which is a reasonable but undocumented invariant (relies on `chunk_id`'s natural string sort correlating with intended order, which is not guaranteed by `chunk_id`'s format `source_id::chapter_id::short_hash`) | Purely cosmetic (dump ordering) — no functional/pipeline impact, but could mislead a maintainer inspecting anomalous chunk data |
| Low | Filename collision surface | `dump_filename` derives the output name solely from the sanitized citekey; the module performs no collision detection if two different `source_id`s sanitize to the same citekey-derived stem (should not happen given upstream citekey uniqueness invariants, but this module has no defense of its own) | A silent overwrite of one source's dump by another's, if the upstream uniqueness invariant is ever violated |
| Low | No dry-run / size guard on `--all` | `run_dump_chunks(source_id=None)` iterates every row from `db.list_sources()` with no count/size confirmation or progress reporting beyond per-source `logger.info` | On a very large vault, `dump-chunks --all` could write a large number of files with no upfront indication of scale to the user before it starts |
| Informational | Config/paging "snapshot" semantics | As detailed in the business rules section, the `chunking`/`paging` blocks reflect *live* config/DB state at render time, not necessarily the state that produced the persisted chunk boundaries, for the standalone `dump-chunks` path | Not a bug, but an easy source of misinterpretation if undocumented for a new team member; the module docstring does not call this nuance out explicitly (only inferable by reading `render_chunk_dump`'s implementation) |

## 11. Test Coverage Analysis

| Component | Unit Tests | Integration Tests | Coverage | Test Quality |
|---|---|---|---|---|
| `sanitize_citekey` / `dump_filename` | 1 (`test_sanitize_citekey_strips_unsafe_chars`) | 0 | Good | Covers unsafe-char collapsing and the all-unsafe -> "unknown" fallback |
| `overlap_prefix_len` | 1 (`test_overlap_prefix_len_finds_shared_boundary`) | 0 | Good | Covers a genuine overlap, cap-truncation, empty-prev, and cap=0 edge cases |
| `render_chunk_dump` | 3 (`test_render_includes_frontmatter_metadata_and_raw_text`, `test_render_orders_by_chunk_index_and_reports_overlap`, `test_render_empty_chunks_still_has_frontmatter`) | 0 | Good | Verifies frontmatter fields, chunk-index ordering independent of input order, overlap annotation in both Sumario and per-chunk sections, and the zero-chunk edge case |
| `write_chunk_dump` | 1 (`test_write_chunk_dump_creates_sanitized_file`) | 0 | Good | Verifies path construction from a citekey needing sanitization AND overwrite semantics (second write replaces first write's content) |
| `run_dump_chunks` | 2 (`test_run_dump_chunks_writes_from_sqlite`, `test_run_dump_chunks_missing_source_raises`) | 1 (uses a real `StateDB` against a temp SQLite file — arguably integration-level) | Good | Covers the happy path end-to-end from SQLite through the rendered file, and the `ValueError` on an unknown `--source-id` |
| `dump_source_chunks` | 1 (`test_dump_source_chunks_loads_db`) | 1 (real `StateDB`) | Good | Covers both the found-source and missing-source (`None` return) branches |
| `run_rechunk` + dump integration (harvester.py) | 0 dedicated | 1 (`test_run_rechunk_writes_dump_when_dump_dir_set`, located in `tests/test_chunk_dump.py`, exercises `zettel.harvester.run_rechunk`) | Good for this one path | Confirms `_maybe_dump_chunks` wiring works end-to-end for rechunk; does not exercise error paths (e.g., dump failure during rechunk) |
| `run_harvest` + dump integration (`--dump-chunks` on harvest) | 0 | 0 found | **Gap** | No test in `tests/test_chunk_dump.py`, `tests/test_harvester.py` (not inspected in depth here, but no match for `dump_dir`/`chunk_dump` in the tests directory outside `test_chunk_dump.py` / `test_extraction_dump.py`), confirms the harvest-path wiring (`harvester.py:143`) is untested at the integration level |
| CLI layer: `dump_chunks_cmd` (`zettel dump-chunks`) | 0 | 0 | **Gap** | No test exercises the Typer command itself — the `--source-id`/`--all` mutual-requirement check (`cli.py:855-857`), the `ValueError` -> red message -> `exit(1)` path (`cli.py:869-872`), or the default-dir resolution through `default_dump_dir` in the CLI context |
| CLI layer: `--dump-chunks`/`--dump-dir` flags on `harvest`/`rechunk` commands | 0 | 0 | **Gap** | `_resolve_chunk_dump_dir` (`cli.py:391-399`) has no direct unit test; its three branches (`--dump-dir` explicit, `--dump-chunks` flag only, neither -> `None`) are untested in isolation |
| Overall module (`zettel/chunk_dump.py`) public functions | 8 test functions across `tests/test_chunk_dump.py` | 3 of those use a real `StateDB` | High at the module level | The module's own logic is well covered including edge cases (empty chunks, missing source, unsafe citekey, overwrite). The main gap is at the CLI command layer and the `harvest --dump-chunks` integration path, both of which currently rely only on manual/README-documented verification rather than automated tests |

