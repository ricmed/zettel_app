# Component Deep Analysis Report — `rebuild`

## 1. Executive Summary

The `rebuild` component is a single module, `zettel/rebuild.py`, that provides two **LLM-free recovery/reconstruction routines** for the Zettelkasten pipeline. It has no class, no persistent state of its own, and no external network calls — it is a pure "derive-from-source-of-truth" utility layer.

Its stated architectural role (per module docstring and `CLAUDE.md`) is: SQLite (`state.py`) is the durable source of truth for the pipeline; ChromaDB (vector index) and the Obsidian vault `.md` files are treated as **disposable, reconstructible caches** derived from SQLite. `rebuild.py` is the code that performs that reconstruction:

- **`run_reindex`** — rebuilds the five ChromaDB collections (`sources`, `chunks`, `permanent_notes`, `mocs`, `literature_notes`) and, when a full (non-partial) run is requested, also rebuilds the SQLite FTS5 lexical tables (`fts_notes`, `fts_chunks`) via `StateDB.rebuild_fts()`.
- **`run_rebuild_vault`** — recreates the Obsidian vault's `.md` files (SRC, LIT index, granular LIT, ZTL permanent notes, MOCs) from the bodies/frontmatter persisted in SQLite, without ever silently clobbering manually created/edited content.

The component is exposed **exclusively through the CLI** (`zettel reindex`, `zettel rebuild`) — it is explicitly listed in `CLAUDE.md` as "CLI only, not exposed in the web UI" — and is also invoked internally by `cli.py` as an automatic recovery path when an embedding-space mismatch is detected on `VectorIndex` open (`_get_idx`).

Key findings:
- The module is small (401 lines), has no classes, and consists of two public orchestration functions plus seven private/module-level helpers.
- It deliberately duplicates minimal MOC-summary parsing logic (`_moc_summary_from_body`) that is also imported by `sync.py`, creating a private-function cross-module coupling (see Technical Debt).
- `run_rebuild_vault`'s manual-note protection relies entirely on the `origin` field persisted per-record in SQLite (`"manual"` vs `"pipeline"`/other) plus a filesystem existence check — there is no content diffing or backup before any write.
- `run_reindex` is idempotent per-record (skips ids that already exist in the target collection) unless `force=True` resets the whole collection first — this is the central mechanism for safely recovering from partial failures without ever re-embedding unnecessarily.
- Both functions return a flat `dict[str, int]` of counters that CLI commands render directly into Rich tables — the component has no reporting/formatting responsibility beyond that.

---

## 2. Data Flow Analysis

### 2.1 `run_reindex` (ChromaDB + FTS5 rebuild)

```
1. CLI command `zettel reindex` (or `zettel rebuild --what chroma|all`) or internal
   `_get_idx()` embedding-mismatch recovery calls `run_reindex(cfg, db, idx, collection, force)`
2. Validate `collection` argument against the five known collection names (raise ValueError if unknown)
3. Determine target collection list: single collection, or all five if none specified
4. For each target collection:
   a. If force=True -> idx.reset_collection(name) (delete + recreate the Chroma collection)
   b. Dispatch to the matching per-collection reindexer:
      - COL_SOURCES     -> _reindex_sources(db, idx)
      - COL_CHUNKS      -> _reindex_chunks(db, idx)
      - COL_PERMANENT   -> _reindex_permanent(cfg, db, idx)
      - COL_MOCS        -> _reindex_mocs(db, idx)
      - COL_LITERATURE  -> _reindex_literature(cfg, db, idx)
5. Each per-collection reindexer:
   a. Reads all relevant rows from StateDB (db.list_sources() / db.list_notes() /
      db.list_mocs() / db.get_chunks_for_source())
   b. Skips ids already present in the Chroma collection (idx.existing_ids) to
      avoid re-embedding unchanged content (cost + time optimization)
   c. Builds embeddable text + sanitized metadata, calls idx.upsert_<type>()
   d. (Permanent notes only) computes a new embedding_input_hash and writes it
      back to SQLite via db.update_note_embedding() so future incremental runs
      can also skip it
6. If collection is None (full run) AND db.fts_enabled:
   db.rebuild_fts() truncates and repopulates fts_notes/fts_chunks from the
   notes/chunks tables
7. Return a dict of per-collection (and fts_notes/fts_chunks) counts of records
   actually written
8. Caller (CLI) renders the dict as a Rich table
```

### 2.2 `run_rebuild_vault` (vault .md reconstruction)

```
1. CLI command `zettel rebuild --what vault|all` calls
   run_rebuild_vault(cfg, db, force, dry_run)
2. For each source row (db.list_sources()):
   a. Rebuild SRC note: parse bibliography_json, call vault.build_source_note(),
      compute destination path via vault.source_note_filename(), _write()
   b. Rebuild LIT index note: if src["lit_body"] is present, write it verbatim
      to 20_Literature/<index filename>; else increment missing_body counter
   c. For each chunk with status in (approved, persisted):
      - If chunk.literature_note_path exists on disk: read that file verbatim,
        _write() it to the canonical destination path, and (if written and not
        dry_run) re-inject the raw source excerpt into the
        `auto-source-excerpt` managed block via vault.safe_update_managed_blocks
      - Else: reconstruct the note from chunk.summary_json (or fall back to
        raw chunk text) via vault.build_literature_chunk_note(), then _write()
3. For each permanent note row (db.list_notes()):
   a. Require both body and frontmatter_json to be present; else increment
      missing_body and skip
   b. Resolve destination path: stored `path` column if present, else compute
      a fresh path via vault.note_filename("ZTL", note_id, title)
   c. _write(path, compose_note(meta, body), origin)
4. For each MOC row (db.list_mocs()): same pattern as permanent notes, using
   note_filename("MOC", moc_id, topic)
5. `_write()` inner helper enforces the non-destructive policy:
   a. If path already exists and force=False -> skip (increment skipped)
   b. If path exists, force=True, but origin == "manual" -> still skip
      (never overwrite manual notes) + log
   c. If dry_run -> count as "written" but perform no I/O
   d. Otherwise -> mkdir parents, write file content, count as "written"
6. Return a dict of counters: sources, literature, permanent, mocs, written,
   skipped, missing_body
7. Caller (CLI) renders the dict as a Rich table
```

---

## 3. Business Rules & Logic

### Overview of the business rules

| Rule Type | Rule Description | Location |
|-----------|------------------|----------|
| Architectural invariant | ChromaDB and vault `.md` files are disposable caches; SQLite is the sole source of truth | rebuild.py:1-10 (module docstring) |
| Validation | `collection` argument (if given) must be one of `sources`/`chunks`/`permanent_notes`/`mocs`/`literature_notes` | rebuild.py:104-106 |
| Business Logic | `force=True` resets (deletes+recreates) the target Chroma collection before repopulating | rebuild.py:110-111 |
| Business Logic | Without `force`, records whose id already exists in the target Chroma collection are skipped (idempotent incremental reindex) | rebuild.py:137-139, 153-156 |
| Business Logic | Embedding-model/provider changes require `force=True`, otherwise old and new vector spaces mix silently | rebuild.py:99-102 (docstring); cli.py:1147-1180 |
| Business Logic | FTS5 lexical tables are only rebuilt on a *full* reindex run (`collection is None`), never on a single-collection reindex | rebuild.py:125-128 |
| Business Logic | FTS rebuild is a full truncate + repopulate, not incremental | state.py:1143-1164 |
| Validation | A permanent note with no persisted `body` cannot be reindexed; it is logged and skipped | rebuild.py:168-174 |
| Business Logic | Permanent-note reindex recomputes and persists a new `embedding_input_hash` keyed by (semantic checksum, embedding provider, embedding model) so subsequent runs can detect "already embedded under this exact space" | rebuild.py:176-186; hashing.py:67-73 |
| Business Logic | MOC embeddable text is the canonical `topic + summary` pair, summary extracted as the text between the H1 title and the first H2 heading | rebuild.py:54-67, 190-200; gardener.py:729-731 |
| Business Logic | Literature-note re-embedding only considers chunks whose status is `approved` or `persisted` — draft/awaiting_review/rejected chunks are never (re-)embedded | rebuild.py:208-209, 323-324 |
| Business Logic | Literature-note embedding text source priority: (1) the on-disk granular LIT file (first 3000 chars) if it exists, (2) `summary_json` (`summary` + `key_concepts`), (3) raw chunk text (first 1500 chars) as last resort | rebuild.py:211-222 |
| Validation | An empty/whitespace-only embedding text for a literature note is skipped entirely (never upserted) | rebuild.py:223-224 |
| Business Logic | `run_rebuild_vault` never overwrites an existing file unless `force=True` | rebuild.py:252-255 |
| Business Logic | Even with `force=True`, a record whose `origin == "manual"` is never overwritten — manual vault edits are permanently protected from pipeline reconstruction | rebuild.py:256-259 |
| Business Logic | `dry_run=True` performs no filesystem writes but still increments the `written` counter, so operators can preview scope before committing | rebuild.py:260-262 |
| Business Logic | Granular LIT notes prefer to restore the exact on-disk file content (`literature_note_path`) verbatim over regenerating from `summary_json` when both exist | rebuild.py:329-338 |
| Business Logic | When a granular LIT file is restored from its saved path, the raw source excerpt is re-injected into the `auto-source-excerpt` managed block using the chunk's persisted text — since the on-disk file's excerpt block may itself have been stripped/altered when it was written | rebuild.py:333-337 |
| Business Logic | If a granular LIT chunk has no persisted `literature_note_path` (or the file no longer exists), the note is fully reconstructed from `summary_json` fields via `build_literature_chunk_note`, always marked `status="approved"` | rebuild.py:339-364 |
| Business Logic | ZTL/MOC destination path prefers the SQLite-persisted `path` column (preserves the note's actual historical location); only falls back to computing a fresh canonical path when `path` is absent | rebuild.py:377-381, 393-395 |
| Validation | A permanent note or MOC missing either `body` or `frontmatter_json` cannot be rebuilt and is counted under `missing_body`, not silently dropped | rebuild.py:369-373, 388-391 |

### Detailed breakdown of the business rules

---

### Business Rule: SQLite as sole source of truth / disposable derived stores

**Overview**:
The entire component exists to enforce and operationalize one architectural invariant: ChromaDB collections and vault `.md` files carry no information that isn't also durably recorded in SQLite. Both can be deleted entirely and regenerated bit-for-bit-equivalent (modulo embedding vector values, which depend on the embedding provider) from `state.py` tables alone, with zero LLM calls.

**Detailed description**:
This rule underlies the design of every other rule in this report. It has two concrete implications baked into the schema: first, note/MOC/source bodies, frontmatter, and (for literature chunks) `summary_json` must always be persisted to SQLite at write time by the phases that create them (extractor, connector, gardener, sync) — `rebuild.py` itself only reads, it never derives content that wasn't already saved. Second, the Chroma vector store and FTS5 tables must be treated operationally as caches: they can legitimately be out of sync with SQLite (e.g., after an embedding-model swap, database corruption, accidental `rm -rf` of the Chroma directory, or FTS5 not being compiled into the SQLite build at initial setup) without that being a data-loss event.

This has a direct, practical use case documented in the module's own docstring and in `CLAUDE.md`'s embedding provider section: when an operator changes `embedding.provider` or `embedding.model` in `config.yaml`, the old vectors are no longer comparable to new ones in the same space. Rather than requiring a full re-harvest/re-extract/re-review/re-connect run (which would re-invoke the LLM and cost money), the operator runs `zettel reindex --force`, and every vector is regenerated purely from already-persisted text, at embedding cost only.

The same reasoning applies to the vault: if a user deletes or corrupts `.md` files, or wants to migrate the vault to a new machine, `zettel rebuild --what vault` regenerates every SRC/LIT/ZTL/MOC note from SQLite without touching the LLM providers at all — this is explicitly why the module docstring states "Two rebuilders, both LLM-free."

**Rule workflow**:
Any phase that would normally write a `.md` file or a Chroma vector must first (or simultaneously) persist the equivalent data into SQLite (`sources`, `notes`, `mocs`, `chunks` tables) — a precondition enforced by convention elsewhere in the codebase, not inside `rebuild.py` itself. `rebuild.py` then acts as the read-side of that contract: `run_reindex` walks `StateDB` list/get accessors to repopulate Chroma; `run_rebuild_vault` walks the same accessors to repopulate the filesystem. Because both directions read exclusively from `StateDB`, a corrupted or deleted Chroma/vault can be fully reconstructed by deleting it and re-running the corresponding `rebuild` operation — the only inputs are what is already in `state.db`.

---

### Business Rule: Incremental, idempotent reindexing via `existing_ids`

**Overview**:
`run_reindex` avoids redundant (and costly) re-embedding by checking, per record, whether the target Chroma collection already contains that exact id before upserting.

**Detailed description**:
Each `_reindex_*` helper calls `idx.existing_ids(collection_name, ids)` (for sources/chunks) or performs an equivalent presence check pattern before upserting, and skips any id already found. This means that calling `run_reindex` repeatedly with `force=False` on an already-fully-indexed vault is a cheap no-op pass (only new/never-seen ids trigger embedding calls) — it is safe to run defensively, e.g., after every `harvest`/`extract`/`connect`/`garden` run, without generating spurious cost. Since Chroma ids are content-addressed in several cases (chunk ids include a short hash of content — `source_id::chapter_id::short_hash`), identical content naturally maps to identical ids, so "already indexed" is a correct proxy for "no re-embedding needed."

This idempotency is what makes `force` a meaningful, separate flag rather than the default: `force=True` explicitly means "throw away the current vector space for this collection and rebuild everything from scratch," which is the only safe path when the *embedding function itself* changed (same content, different vector), since `existing_ids` cannot detect that the *meaning* of an existing id's stored vector is now stale — it only checks id presence, not vector freshness.

Permanent-note reindexing (`_reindex_permanent`) goes one step further: it always re-embeds every note that has a `body` (it does not check `existing_ids` for this collection), and instead separately records a per-note `embedding_input_hash` (a hash of the semantic content plus the exact provider+model pair) via `db.update_note_embedding`. This hash is written for downstream *incremental* embedding logic elsewhere in the pipeline to consult (skip re-embedding when the hash already matches), but `rebuild.py`'s own reindex path treats a full-collection reindex as a source-of-truth resync rather than a diff.

**Rule workflow**:
`run_reindex(force=False)`: for sources/chunks, gather candidate ids -> `existing_ids` -> subtract -> upsert only the delta. `run_reindex(force=True)`: `reset_collection` empties the target collection first, so `existing_ids` against the now-empty collection always returns an empty set, guaranteeing every record is regenerated. Literature notes and MOCs are always evaluated per source/chunk or per MOC row without an ids pre-filter in the current implementation — every call to `_reindex_literature`/`_reindex_mocs` re-embeds every eligible row unconditionally (see Technical Debt: inconsistent idempotency across collection types).

---

### Business Rule: Manual-note protection during vault rebuild

**Overview**:
`run_rebuild_vault` must never destroy content a human wrote or edited by hand in the vault, even when explicitly asked to overwrite (`force=True`).

**Detailed description**:
This is the single most safety-critical rule in the component, since `run_rebuild_vault` is a bulk filesystem-writing operation invoked from a CLI command that an operator might run without carefully checking every side effect. The rule is implemented at two layers of defense. First, by default (`force=False`), `_write()` refuses to touch any path that already exists on disk at all — this alone prevents accidental clobbering of *any* file, manual or pipeline-generated, in the common case. Second, when an operator explicitly opts into overwriting existing pipeline-generated files with `force=True` (e.g., because a SRC note's frontmatter schema evolved and old files are stale), the `_write()` helper still checks the record's `origin` field (persisted per source/note/MOC row in SQLite) and refuses to overwrite when `origin == "manual"`, logging an informational message and counting it under `skipped`.

The practical scenario this protects against: a user runs `zettel new-note` (which sets `origin: manual`) to hand-author a permanent note, or hand-edits vault content and later runs `zettel sync-manual` to index it (which also preserves `origin: manual` on the SQLite row, per `CLAUDE.md`'s sync.py description). If that same vault is later rebuilt with `zettel rebuild --what vault --force` — for instance during a bulk vault migration or after a filename-scheme change — the manual note's `origin` tag causes `_write()` to skip it regardless of the `force` flag, so the human's authored content is never replaced by whatever `rebuild.py` would have reconstructed for it from SQLite.

Note the asymmetry this rule creates: a manual note IS represented in SQLite (assuming `sync-manual` ran) and DOES have a body/frontmatter that `rebuild.py` could technically write, but the protection is unconditional regardless of whether the persisted body matches the current file content — `rebuild.py` makes no attempt to detect drift or merge; it purely refuses to act.

**Rule workflow**:
`_write(path, content, origin)`: `path.exists()` and `not force` -> skip. `path.exists()` and `force` and `origin == "manual"` -> skip (with a log message distinct from the generic skip). `path.exists()` and `force` and `origin != "manual"` -> proceed to overwrite. `not path.exists()` -> proceed to write regardless of `force`/`origin` (new file, nothing to protect). `dry_run` short-circuits the actual filesystem write in all "proceed" branches while still incrementing `written`.

---

### Business Rule: Verbatim restoration of granular literature notes with excerpt re-injection

**Overview**:
For a granular LIT chunk that still has a valid `literature_note_path` on disk, `run_rebuild_vault` restores that exact file content rather than regenerating it from structured fields, then separately re-applies the `auto-source-excerpt` managed block from the chunk's persisted raw text.

**Detailed description**:
This handles the case where the granular literature note's Markdown body may contain content beyond what `summary_json` captures — for example, any post-approval edits a human made to the note body outside the managed blocks (which, per `vault.py`'s "safe write" convention referenced in `CLAUDE.md`, are preserved during normal pipeline updates). Reading the file verbatim and re-writing it to its canonical destination (which may differ from its current path if, e.g., paging was corrected via `zettel set-paging` and the file needs to move) preserves those edits instead of regenerating a fresh note purely from `summary_json`, which would lose any such manual refinement.

However, the `auto-source-excerpt` managed block specifically is always re-injected from `chunk["text"]` (the chunk's canonical persisted source text in SQLite) rather than trusted from the restored file's own content. This is because the excerpt block's purpose is to always reflect the authoritative source text, and the file being restored may be a stale copy (e.g., from before a chunk was re-processed via `set-paging` or `rechunk`, or a copy from a different location). If the chunk has no text (`""` after strip), a placeholder Portuguese string `_Trecho nao disponivel._` is inserted instead of leaving the block empty or absent.

This dual mechanism — verbatim body restoration plus authoritative excerpt overwrite — is unique to the granular-LIT-with-existing-file code path; the fallback path (no `literature_note_path`, or the file is missing) instead calls `build_literature_chunk_note()` to construct the entire note (including its own excerpt section) fresh from `summary_json`/text fields, with no separate re-injection step needed since the excerpt is included in the initial build.

**Rule workflow**:
`chunk.get("literature_note_path")` truthy AND `Path(...).exists()` -> read full file text -> `_write(dest, content, origin)` -> if the write actually happened (`_write` returned True) AND not `dry_run` AND `dest.exists()` -> call `vault.safe_update_managed_blocks(dest, {"auto-source-excerpt": excerpt})` where `excerpt` is `chunk["text"].strip()` or the placeholder string. Otherwise (no path or file missing) -> fall through to `build_literature_chunk_note()` reconstruction from `summary_json`.

---

### Business Rule: Literature-note embedding text source priority (reindex path)

**Overview**:
When rebuilding the `literature_notes` Chroma collection, the text actually embedded for a given chunk is chosen from three possible sources in a strict priority order, since not every persisted chunk has the same fields populated.

**Detailed description**:
The most preferred source is the literal on-disk granular LIT `.md` file (`literature_note_path`), truncated to its first 3000 characters — this captures the fully-rendered, human-readable note including any post-approval manual edits, which is the richest available representation of "what this literature note actually says" for semantic search purposes. If that file is unavailable (deleted, moved without SQLite being updated, or the pipeline never wrote a granular file for this chunk under an older schema version), the fallback is `summary_json`, from which only the `summary` string and the `key_concepts` list are concatenated — a much shorter and more structured signal than the full note body, but still LLM-derived and topically focused. As a final fallback, when neither the file nor `summary_json` is available, the raw chunk source text itself (truncated to 1500 characters) is embedded — the least semantically refined option, essentially treating the chunk like a plain source excerpt for retrieval purposes.

If none of the three sources yield any non-whitespace text at all, the chunk is skipped entirely rather than upserting an empty/near-empty vector, which would otherwise pollute similarity search with a meaningless embedding.

This priority order reflects a general design principle visible elsewhere in the codebase (e.g., `retrieval.py`'s layered relevance floor): prefer the most information-dense, human-validated signal when available, and degrade gracefully rather than failing outright when it isn't.

**Rule workflow**:
`literature_note_path` set AND file exists -> read first 3000 chars. Else `summary_json` present and parseable -> `f"{summary}\n{' '.join(key_concepts)}"`. Else (or on `JSONDecodeError`) -> `chunk.text[:1500]`. If the resulting string is empty/whitespace-only after all fallbacks -> skip (no upsert, no counter increment).

---

## 4. Component Structure

`rebuild.py` is a flat, single-file module with no submodules or classes. Internal organization (annotated):

```
zettel/rebuild.py
├── Module docstring            # States the "two LLM-free rebuilders" contract
├── _moc_summary_from_body()    # Extracts MOC body text between H1 title and first H2
├── _tags_from_frontmatter()    # Parses `tags` out of a note's frontmatter JSON blob
├── _ALL_COLLECTIONS             # [sources, chunks, permanent_notes, mocs, literature_notes]
├── run_reindex()                 # PUBLIC: orchestrates ChromaDB + FTS5 rebuild
│   ├── _reindex_sources()        # Rebuild `sources` collection
│   ├── _reindex_chunks()         # Rebuild `chunks` collection
│   ├── _reindex_permanent()      # Rebuild `permanent_notes` collection + embedding_input_hash
│   ├── _reindex_mocs()           # Rebuild `mocs` collection (imports gardener._moc_embeddable)
│   └── _reindex_literature()     # Rebuild `literature_notes` collection (approved/persisted chunks only)
└── run_rebuild_vault()           # PUBLIC: orchestrates vault .md reconstruction
    └── _write() (local closure)  # Enforces overwrite/manual-protection/dry-run policy
```

There is no `__init__.py` export list specific to this module beyond normal Python module import (`from zettel.rebuild import run_reindex, run_rebuild_vault, _moc_summary_from_body`).

---

## 5. Dependency Analysis

```
Internal Dependencies:

cli.py (reindex, rebuild, _get_idx commands)
    └── zettel.rebuild.run_reindex / run_rebuild_vault

zettel.sync (_reindex-adjacent MOC sync path)
    └── zettel.rebuild._moc_summary_from_body   (private helper imported across module boundary)

zettel.rebuild.run_reindex
    ├── zettel.config.AppConfig               (embedding.provider / embedding.model read)
    ├── zettel.state.StateDB                  (list_sources, list_notes, list_mocs,
    │                                            get_chunks_for_source, update_note_embedding,
    │                                            rebuild_fts, fts_enabled)
    ├── zettel.index.VectorIndex              (reset_collection, existing_ids, upsert_source,
    │                                            upsert_chunk, upsert_permanent_note, upsert_moc,
    │                                            upsert_literature_note)
    ├── zettel.index (COL_SOURCES, COL_CHUNKS, COL_PERMANENT, COL_MOCS, COL_LITERATURE)
    ├── zettel.hashing (compute_embedding_input_hash, extract_embeddable_text,
    │                     normalize_text_for_hash, sha256_hex)
    └── zettel.gardener._moc_embeddable        (deferred/local import inside _reindex_mocs,
                                                  to avoid a module-level circular import)

zettel.rebuild.run_rebuild_vault
    ├── zettel.config.AppConfig                (vault_path)
    ├── zettel.state.StateDB                   (list_sources, get_chunks_for_source, list_notes, list_mocs)
    └── zettel.vault (build_literature_chunk_note, build_source_note, compose_note,
                        literature_chunk_filename_for_row, literature_index_filename,
                        literature_source_dirname, note_filename, safe_update_managed_blocks,
                        source_note_filename)

External Dependencies:
- Python stdlib: json (frontmatter/summary parsing), logging, re (unused directly in
  current file body beyond import — see Technical Debt), pathlib.Path, typing.Any
- No direct third-party libraries (ChromaDB/ORM access is fully encapsulated behind
  zettel.index.VectorIndex; SQLite access is fully encapsulated behind zettel.state.StateDB)
```

Note: `import re` at rebuild.py:16 is present but no direct `re.` call appears in the current file body — the module's own regex-like logic (`_moc_summary_from_body`) uses plain string operations, not `re`. This is flagged under Technical Debt as a likely-unused import.

---

## 6. Afferent and Efferent Coupling

Analysis unit: functions (this module has no classes). Afferent = number of distinct call sites across the codebase invoking the function; efferent = number of distinct external symbols (functions/classes from other modules) the function calls directly.

| Component (function) | Afferent Coupling | Efferent Coupling | Critical |
|-----------------------|--------------------|---------------------|----------|
| `run_reindex` | 4 (cli.py x3, called internally by `_get_idx`) | 8 (StateDB x2 methods used directly + 5 private `_reindex_*` helpers + `db.rebuild_fts`) | High |
| `run_rebuild_vault` | 2 (cli.py `rebuild` command) | 9 (StateDB x2 + 7 vault.py builders/writers) | High |
| `_moc_summary_from_body` | 2 (internal `_reindex_mocs` + external `sync.py`) | 0 | Medium |
| `_tags_from_frontmatter` | 1 (`_reindex_permanent`) | 0 (stdlib `json` only) | Low |
| `_reindex_sources` | 1 (`run_reindex`) | 2 (`db.list_sources`, `idx.existing_ids`/`idx.upsert_source`) | Low |
| `_reindex_chunks` | 1 (`run_reindex`) | 3 (`db.list_sources`, `db.get_chunks_for_source`, `idx.existing_ids`/`idx.upsert_chunk`) | Low |
| `_reindex_permanent` | 1 (`run_reindex`) | 5 (`db.list_notes`, `extract_embeddable_text`, `sha256_hex`/`normalize_text_for_hash`, `idx.upsert_permanent_note`, `db.update_note_embedding`, `compute_embedding_input_hash`) | Medium |
| `_reindex_mocs` | 1 (`run_reindex`) | 3 (`db.list_mocs`, `gardener._moc_embeddable`, `idx.upsert_moc`) | Medium (cross-module import) |
| `_reindex_literature` | 1 (`run_reindex`) | 3 (`db.list_sources`, `db.get_chunks_for_source`, `idx.upsert_literature_note`) | Low |

Interpretation: `run_reindex` and `run_rebuild_vault` are the two high-coupling "hub" functions expected of orchestration entry points — this is architecturally appropriate given their role as the single public surface of the component. `_moc_summary_from_body`'s afferent coupling crossing into `sync.py` is the one instance of unusual coupling for a module whose other helpers are strictly private/internal (see Technical Debt §10).

---

## 7. Endpoints

Not applicable — `rebuild.py` exposes no REST/GraphQL/gRPC/HTTP endpoints. It is invoked only via:
- CLI subcommands `zettel reindex` and `zettel rebuild` (Typer commands defined in `cli.py`)
- An internal function call from `cli.py::_get_idx()` (automatic embedding-mismatch recovery)

This section is otherwise omitted per report guidelines for components without exposed endpoints.

---

## 8. Integration Points

| Integration | Type | Purpose | Protocol | Data Format | Error Handling |
|-------------|------|---------|----------|-------------|-----------------|
| SQLite (`state.py` / `StateDB`) | Internal data store | Sole source of truth read for both rebuild paths | Direct Python API (sqlite3 under the hood) | Python dicts (`_fetchall` rows), JSON strings in columns (frontmatter_json, summary_json, bibliography_json, authors) | No explicit error handling for DB errors in rebuild.py itself; `json.JSONDecodeError`/`TypeError` from malformed JSON columns are caught locally and degrade to empty/fallback values rather than raising |
| ChromaDB (`index.py` / `VectorIndex`) | Internal vector store | Target of `run_reindex`'s upserts; source of `existing_ids` idempotency checks | Direct Python API (chromadb client wrapped by VectorIndex) | Raw text documents + sanitized metadata dicts (str/int/float/bool only) | No try/except around `idx.upsert_*`/`idx.reset_collection` calls in rebuild.py — failures propagate to the CLI caller uncaught |
| Vault filesystem (`.md` files under `cfg.vault_path`) | Internal file store | Target of `run_rebuild_vault`'s writes; source of verbatim-restoration reads for granular LIT | Local filesystem I/O (`pathlib.Path.read_text`/`write_text`, UTF-8) | `path.exists()` checked before every write decision; no exception handling around `write_text`/`read_text` — an OS-level failure (permissions, disk full) propagates uncaught |
| `zettel.gardener._moc_embeddable` | Internal cross-module function | Canonical MOC embeddable-text formatting, shared with the gardener phase | Direct Python import (deferred, inside `_reindex_mocs`, to avoid a circular import at module load time) | Plain string concatenation | N/A (pure function, no error paths) |

---

## 9. Design Patterns & Architecture

| Pattern | Implementation | Location | Purpose |
|---------|-----------------|----------|---------|
| Strategy / dispatch table | `run_reindex`'s `if/elif` chain mapping `_ALL_COLLECTIONS` names to per-collection reindex functions | rebuild.py:112-121 | Cleanly separates "which collections to touch" (orchestration) from "how to rebuild each one" (per-type logic) |
| Idempotent upsert / cache-aside | `idx.existing_ids()` pre-check before `upsert_source`/`upsert_chunk` | rebuild.py:137-139, 153-156 | Avoids redundant, costly re-embedding of unchanged content on repeated runs |
| Deferred/local import to break circular dependency | `from zettel.gardener import _moc_embeddable` inside `_reindex_mocs()` rather than at module top | rebuild.py:191 | `gardener.py` likely imports from modules that transitively depend on `rebuild.py` (or vice versa via cli.py), so a top-level import would create a cycle |
| Fail-safe / non-destructive write guard | `_write()` closure inside `run_rebuild_vault` | rebuild.py:251-266 | Centralizes the "never silently overwrite" and "never touch manual origin" policy in one place so every note-type branch reuses identical safety semantics |
| Graceful degradation / fallback chain | Literature-note embedding text source priority (file -> summary_json -> raw text -> skip) | rebuild.py:211-224 | Produces the richest available embeddable text without hard-failing when preferred sources are missing |
| Counter/statistics accumulator | Both public functions return a flat `dict[str, int]` mutated via closures/local accumulation rather than raising exceptions or returning rich objects | rebuild.py:108, 248-249 | Simple, directly renderable summary object for CLI table output; matches the pattern used by sibling CLI-facing modules (harvester, extractor stats) |
| Dry-run mode | `dry_run` flag short-circuits actual I/O while still tallying what *would* happen | rebuild.py:260-262 | Lets an operator preview the blast radius of a bulk vault rebuild before committing |

No class-based patterns (Repository, Factory, Builder classes, etc.) are used within this module itself — it is a pure functional/procedural orchestration layer over the `StateDB`/`VectorIndex`/`vault` abstractions, which themselves implement Repository-like patterns.

---

## 10. Technical Debt & Risks

| Risk Level | Component Area | Issue | Impact |
|------------|------------------|-------|--------|
| Medium | `_reindex_mocs`, `_reindex_literature` | No `existing_ids` idempotency check before upsert (unlike `_reindex_sources`/`_reindex_chunks`) — every call re-embeds every eligible MOC and every approved/persisted literature chunk unconditionally | Running `zettel reindex` (full, non-force) repeatedly re-embeds all MOCs and all literature notes every time, incurring avoidable embedding API cost/time proportional to corpus size, inconsistent with the stated "skip already-indexed" optimization applied to sources/chunks |
| Medium | `sync.py` -> `zettel.rebuild._moc_summary_from_body` | A single-underscore "private" helper is imported and used across a module boundary (`sync.py:282`) | Signals the function is really a shared utility, not private to `rebuild.py`; a future refactor of `rebuild.py` could silently break `sync.py` since Python does not enforce the privacy convention, and there is no shared "moc_utils" module making the contract explicit |
| Low | rebuild.py:16 | `import re` appears unused in the current file body (no `re.` usage found) | Minor lint/dead-import noise; no functional impact |
| Medium | `run_reindex` / `run_rebuild_vault` | Neither function wraps per-record processing in try/except — a single malformed row (e.g., unexpectedly-typed JSON column, an OS write failure partway through a large vault) aborts the entire batch, potentially leaving the target collection/vault in a partially-rebuilt state with no rollback | A large, otherwise-successful reindex/rebuild run can be fully interrupted by one bad record, and the returned stats dict is never produced/reported for the caller to know how far it got |
| Medium | `run_rebuild_vault` manual-note protection | Protection keys entirely on the `origin` column being correctly and consistently set to `"manual"` at every write site across the whole codebase (`new_note.py`, `sync.py`) | If any pipeline code path ever fails to persist/propagate `origin` correctly (e.g., a future code change to `sync.py` or `new_note.py`), `_write()` would silently allow a manual file to be overwritten under `force=True` with no independent safety net (no content hash comparison, no backup) |
| Low | `_reindex_literature` | Falls back to `chunk.get("text", "")[:1500]` inside the `except json.JSONDecodeError` branch for `summary_json`, but the *unconditional* fallback for "no summary_json at all" uses `(chunk.get("text") or "")[:1500]` — two textually different but behaviorally near-identical fallback expressions for what is conceptually the same "no better text available" case | Minor code duplication / drift risk if one branch is edited without updating the other |
| Low | `run_rebuild_vault` | The literature-index-snapshot branch (`lit_body`) increments `stats["missing_body"]` whenever `lit_body` is falsy, but that counter is shared with the unrelated "permanent note / MOC missing body+frontmatter" cases later in the function | The single `missing_body` counter conflates three semantically distinct situations (missing LIT index body vs. missing ZTL body/frontmatter vs. missing MOC body/frontmatter), making the CLI-rendered stats table ambiguous about which record type actually has the gap |
| Low | `_reindex_permanent` | Logs a warning (PT-BR: "Nota ... sem corpo persistido (anterior a Fase 0)") for notes with no body, implying pre-migration legacy rows may exist in some databases | Confirms there is at least one known historical schema-migration edge case that this component silently tolerates rather than actively repairs; operators must notice the log line themselves since it is not surfaced in the returned stats dict |

---

## 11. Test Coverage Analysis

Primary dedicated test file: `tests/test_rebuild.py` (205 lines, 10 test functions). One indirectly-related test in `tests/test_state.py` covers `StateDB.rebuild_fts()`, which `run_reindex` calls but which lives outside this component's boundary.

| Component (function) | Unit Tests | Integration Tests | Coverage (functional) | Test Quality |
|------------------------|------------|---------------------|--------------------------|----------------|
| `run_reindex` (all collections, no force) | 1 (`test_reindex_populates_all_collections`) | 0 | Good — asserts per-collection counts and that `embedding_input_hash` gets backfilled | Uses a hand-written `FakeIndex` test double rather than a real `VectorIndex`/Chroma instance, so metadata sanitization and real Chroma upsert semantics are not exercised by this test |
| `run_reindex` (single collection + force) | 1 (`test_reindex_single_collection_with_force`) | 0 | Adequate — confirms `stats.keys()` is restricted and `reset_collection` was called | Same `FakeIndex` limitation |
| `run_reindex` (unknown collection) | 1 (`test_reindex_unknown_collection_raises`) | 0 | Good — validation-error path fully covered | Simple `pytest.raises(ValueError)` assertion, no message content check |
| `run_reindex` (embedding-model swap, force) | 1 (`test_reindex_force_after_embedding_swap`) | 1 (uses a real `VectorIndex` against a temp Chroma dir) | Good — this is the most important real-world scenario (embedding drift) and is the only test exercising a real `VectorIndex`/`EmbeddingSpaceMismatch` roundtrip end-to-end | Solid; verifies both the mismatch-raising behavior and the successful force-reindex recovery, including collection counts post-swap |
| `_moc_summary_from_body` | 1 (`test_moc_summary_extraction`) | 0 | Adequate — single happy-path case (H1, one paragraph, then H2) | No negative/edge cases tested (empty body, no H1, multiple H1s, H2 immediately after H1 with no summary text, summary containing `## ` mid-line but not at line start) |
| `_reindex_sources` / `_reindex_chunks` / `_reindex_permanent` / `_reindex_mocs` | Covered only indirectly via `run_reindex`'s aggregate test | 0 | Adequate but indirect | No isolated unit tests target these private helpers directly; behavior is inferred from the aggregate stats dict only |
| `_reindex_literature` | 0 direct | 0 | **Not covered** | No test in `test_rebuild.py` seeds an `approved`/`persisted` chunk and calls `run_reindex` with `COL_LITERATURE` (or full run) to verify literature-note re-embedding, the three-tier text-source fallback, or the empty-text skip rule |
| `run_rebuild_vault` (basic write from DB) | 1 (`test_rebuild_vault_writes_from_db`) | 1 (writes real files under `tmp_path`, reads them back) | Good — verifies sources/literature/permanent/mocs counts and that written file content matches persisted bodies | Reasonable assertions on file existence and substring content |
| `run_rebuild_vault` (dry run) | 1 (`test_rebuild_vault_dry_run_writes_nothing`) | 1 | Good — confirms no filesystem side effects occur while `written` is still counted | Solid |
| `run_rebuild_vault` (skip existing without force) | 1 (`test_rebuild_vault_does_not_overwrite_existing_without_force`) | 1 | Good — direct verification of the primary non-destructive guarantee | Solid, clear assertion on preserved manual edit text |
| `run_rebuild_vault` (force + manual origin protection) | 1 (`test_rebuild_vault_force_preserves_manual_origin`) | 1 | Good — the most safety-critical rule in the component is directly tested | Solid; explicitly sets `origin="manual"` and confirms `force=True` still does not overwrite |
| `run_rebuild_vault` — granular LIT chunk restoration (file-verbatim path + excerpt re-injection) | 0 | 0 | **Not covered** | No test seeds a chunk with a `literature_note_path` pointing at an existing file to exercise the verbatim-restore + `safe_update_managed_blocks` excerpt re-injection branch (rebuild.py:329-338) |
| `run_rebuild_vault` — `missing_body` counting for LIT/ZTL/MOC | 0 | 0 | **Not covered** | No test asserts `stats["missing_body"]` increments when a source has no `lit_body`, or when a note/MOC lacks `body`/`frontmatter_json` |
| `_tags_from_frontmatter` | 0 direct | 0 | Not directly covered | Only exercised indirectly via `_reindex_permanent` in the aggregate test, and only with a well-formed `tags` list — the string-tags branch and the malformed-JSON branch (`json.JSONDecodeError`/`TypeError`) are untested |

**Overall assessment**: The two safety-critical rules (manual-note protection, embedding-space-mismatch recovery) are well covered by dedicated, meaningful tests. However, `_reindex_literature` — one of five collection rebuilders and the one with the most complex fallback logic (three-tier text source priority) — has **zero test coverage**, and the granular-LIT verbatim-restoration-with-excerpt-reinjection code path in `run_rebuild_vault` is likewise untested. Given `CLAUDE.md`'s emphasis on literature notes as a distinct, review-gated collection, this is the most significant coverage gap in the component.

---

**Component analyzed**: `rebuild` (`zettel/rebuild.py`)
**Report saved to**: `D:\projetos\zettel_app\docs_project\component-deep-analyzer\component-analysis-rebuild-2026-08-30_10-22-26.md`
