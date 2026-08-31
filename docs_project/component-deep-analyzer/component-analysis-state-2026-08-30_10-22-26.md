# Component Deep Analysis Report — `state` (StateDB)

**Component**: `zettel/state.py` (`StateDB`)
**Analysis date**: 2026-08-30
**Scope**: Single file, single public class (`StateDB`) plus two module-level helpers (`_fts_match_expr`, module constants `_FTS_TOKEN_RE` / `_PT_STOPWORDS`) and three module-level SQL string constants (`_SCHEMA_SQL`, `_INDEX_SQL`, `_FTS_SQL`).
**Ignored per request**: `.venv`, `.git`, `node_modules`, `data`, `vault`, `attached_assets`, `.pytest_cache`.

---

## 1. Executive Summary

`zettel/state.py` implements `StateDB`, the single SQLite persistence layer for the entire Zettel pipeline. It is instantiated once per CLI invocation (`cli._get_db`) or once per web worker thread (`web_app.WebWorker`) and is passed by reference into nearly every other module in the codebase — harvesting, extraction, review, connection, gardening (taxonomy + hub MOCs), sync, retrieval/graph expansion, ask/article generation, asset description, bibliography, and the FastAPI web layer. It is, along with `VectorIndex` (ChromaDB), one of the two highest afferent-coupling hubs identified in the architectural report (`docs_project/architectural-analyzer/architectural-report-2026-08-30_10-22-26.md`).

Structurally, `StateDB` is a single ~1,430-line class (96 methods) organized by comment-delimited functional sections (Files, Sources, Chapters, Chunks, Concepts, Notes, MOCs, Assets, LLM Cache, Note Connections, Runs, Web job queue, FTS search, Stats). It owns:

- 12 core tables (`files`, `sources`, `chapters`, `chunks`, `concepts`, `notes`, `mocs`, `assets`, `llm_cache`, `note_connections`, `runs`, `web_jobs`, `web_job_events` — 13 counting both job tables) plus 2 FTS5 virtual tables (`fts_notes`, `fts_chunks`).
- An additive, non-destructive schema migration mechanism (`_migrate_schema`) that lets old databases evolve in place across releases without a migration framework.
- A graceful-degradation path for SQLite builds lacking the FTS5 extension (`fts_enabled` flag), so the hybrid retriever falls back to vector-only search rather than crashing.
- A durable, atomic single-job queue (`web_jobs`/`web_job_events`) that backs the FastAPI web UI's background worker, including a mutual-exclusion guarantee and crash recovery on restart.

Key finding: `StateDB` functions as a de-facto "God Object" / active-record-style repository for the whole application — it is not domain-partitioned into smaller repository classes, and it mixes two largely unrelated responsibilities (pipeline domain state, and an operational web-job queue) inside one class. This is architecturally consistent with the project's small-team, single-file-per-concern style, but it is the component most exposed to any future schema or behavior change, since a change to any table's shape or update semantics has to be manually verified against ~25 consuming modules.

---

## 2. Data Flow Analysis

`StateDB` has three distinct data-flow shapes, all converging on the same SQLite file (`config.state_db_path`, default `./data/state.db`, WAL mode):

**A. Pipeline write path (harvest → extract → review → connect → garden → sync)**

```
1. CLI/web command builds (AppConfig, StateDB, VectorIndex) via cli._load_deps/_get_db (state.py:53) or web_app.WebWorker._db_factory
2. Pipeline module (harvester.py, extractor.py, ...) calls StateDB.upsert_*() to persist a row
   (e.g. upsert_source -> INSERT ... ON CONFLICT DO UPDATE, state.py:524-592)
3. For chunks/notes, the write inline-refreshes the FTS5 shadow index
   (upsert_chunk -> _fts_index_chunk, state.py:862; upsert_note -> _fts_index_note, state.py:1070)
4. Every write self-commits (self.conn.commit() at the end of each method) — no cross-call transactions
5. Downstream phase reads back via get_*_by_status()/get_*_for_source() filters
   (e.g. connector.py loads approved, unlinked concepts via get_concepts_by_status("approved", without_notes=True), state.py:1022-1032)
6. Costs/usage are folded back onto the run and source rows (add_source_usage, finish_run, state.py:1430-1488)
```

**B. Retrieval / graph read path (ask, article, connect RAG, sync suggestions)**

```
1. Retriever (retrieval.py) calls search_notes_fts()/search_chunks_fts() (state.py:1102-1141)
   -> _fts_match_expr() sanitizes the query into a safe, stopword-filtered FTS5 MATCH expression (state.py:40-59)
2. Fused with ChromaDB dense results outside StateDB (in retrieval.py)
3. graph.expand_notes() BFS calls get_connections_for_notes() once per frontier (state.py:1188-1203)
   to batch-load edges instead of one query per note
```

**C. Web job queue path (FastAPI background worker)**

```
1. web.py enqueues a mutating operation -> StateDB.create_web_job() (state.py:1529-1550)
   uses BEGIN IMMEDIATE to atomically reject a second job while one is queued/running
2. WebWorker daemon thread polls/wakes, calls claim_web_job() (state.py:1552-1559) to transition queued->running
3. Progress checkpoints stream through update_web_job() + add_web_job_event() (state.py:1581-1625)
4. web.py's status endpoint polls get_web_job()/list_web_job_events() (state.py:1561-1631)
5. On process restart, recover_web_jobs() marks any still-"running" row "interrupted" (state.py:1515-1527);
   "queued" rows are left untouched so the new worker resumes them
6. Dashboard aggregates (get_web_dashboard, state.py:1633-1697) run ad-hoc read-only aggregate SQL
   over sources/chunks/notes/mocs/note_connections/runs for the web overview page
```

---

## 3. Business Rules & Logic

### Overview of the business rules

| Rule Type | Rule Description | Location |
|-----------|------------------|----------|
| Concurrency/Reliability | WAL journal mode + `foreign_keys=ON` enabled on every connection | state.py:299-300 |
| Resilience | FTS5 absence degrades to `fts_enabled=False` instead of raising | state.py:314-333 |
| Schema evolution | Additive-only migration via `ALTER TABLE`, duplicate-column errors swallowed | state.py:388-457 |
| Data integrity | Upsert `ON CONFLICT` clauses use `COALESCE(excluded.x, table.x)` to never clobber existing data with `NULL`/omitted arguments | state.py:563-581, 844-855, 1055-1064, 1225-1236 (and others) |
| Deduplication | Layer-1 duplicate detection: identical file bytes at a different path reuse `source_id` | state.py:506-520 |
| Deduplication | Layer-2 duplicate detection: identical normalized extraction checksum across formats reuses the source | state.py:773-792 |
| Workflow gate | `connect` only ever loads concepts that are `approved` **and** not yet linked to a note (`without_notes=True`) | state.py:1022-1032 |
| Workflow gate | Concept `status` defaults to `'pending'` unless explicitly supplied, even on update-in-place | state.py:988-1011 |
| Status rollup | `persisted` chunks are folded into the `chunks_approved` count for display purposes | state.py:1718-1724 |
| Cascade deletion | Deleting a source cascades to its chunks (+ concepts + FTS), chapters, orphan concepts, assets, and file-tracking rows, but never touches permanent notes | state.py:731-765 |
| Detachment | Permanent notes survive source deletion by default; `source_id` is nulled rather than the note being removed | state.py:722-729 |
| Graph topology matching | MOC-topic reuse is decided by a case-insensitive, bidirectional substring match | state.py:1249-1257 |
| Scoped bulk deletion | `garden --recreate` only deletes `origin='pipeline'` MOCs; `garden --hubs --recreate` only deletes `origin='hub_pipeline'` MOCs — the two MOC families never cross-delete each other | state.py:1259-1273 |
| Weighted graph metric | Hub ranking degree is computed as an undirected sum of per-relation-type weights (default weight `0.5` for unknown relation types) | state.py:1275-1290 |
| Validation | `record_duplicate` raises `ValueError` for any kind outside `{file, content, semantic}` | state.py:1490-1505 |
| Concurrency control | At most one `web_jobs` row may be `queued` or `running` at a time, enforced via `BEGIN IMMEDIATE` + a guard `SELECT` inside the same transaction | state.py:1529-1550 |
| Crash recovery | On restart, jobs left `running` are force-transitioned to `interrupted`; `queued` jobs are left alone for the new worker to resume | state.py:1515-1527 |
| Lexical safety | User-supplied search text is tokenized, stopword-filtered, and each token individually double-quoted before being used in an FTS5 `MATCH` expression, neutralizing FTS5 query-syntax injection | state.py:40-59 |
| Referential integrity for BFS | Graph expansion loads all edges touching a batch of note ids (source **or** target) in a single `IN (...)` query per hop | state.py:1188-1203 |

### Detailed breakdown of the business rules

---

### Business Rule: WAL Mode + Foreign Keys Enforcement

**Overview**:
Every `StateDB` connection is opened with `PRAGMA journal_mode=WAL` and `PRAGMA foreign_keys=ON` (state.py:299-300), before any schema statement runs.

**Detailed description**:
WAL (Write-Ahead Logging) mode is what makes the project's multi-consumer access pattern viable at all: the CLI, the FastAPI web worker thread, and potentially a concurrently-running `ask`/`article` command can all open independent `sqlite3.connect()` handles to the same `state.db` file (see Section 6/10 on concurrency) without the classic "database is locked" failure mode that plagues SQLite's default rollback-journal mode under concurrent readers + a writer. WAL allows readers to proceed against a consistent snapshot while a writer appends to the write-ahead log, which is essential given that `web_app.WebWorker` opens a brand-new `StateDB` per dispatched job (`web_app.py:180`) while the dashboard and status endpoints continue to poll the same file from other requests.

`foreign_keys=ON` activates SQLite's normally-off-by-default FK enforcement for the `FOREIGN KEY` declarations sprinkled through `_SCHEMA_SQL` (e.g. `chunks.source_id -> sources.source_id`, `web_job_events.job_id -> web_jobs.job_id ON DELETE CASCADE`). Without this pragma the constraints are parsed but silently unenforced, so this single line is what turns the cascading delete on `web_job_events` into a real guarantee rather than a no-op comment.

Because both pragmas are set unconditionally in `__init__` (not configurable), every `StateDB` instance in the process — CLI, web worker, or a test's `tmp_path` fixture — behaves identically with respect to isolation and referential integrity. There is no per-call override, and no `PRAGMA busy_timeout` is set (see Technical Debt), so the resilience WAL buys is not complete under sustained write contention.

**Rule workflow**:
```
StateDB.__init__(db_path)
  -> sqlite3.connect(db_path)
  -> PRAGMA journal_mode=WAL
  -> PRAGMA foreign_keys=ON
  -> _init_schema() (schema creation happens under these pragmas)
```

---

### Business Rule: Graceful FTS5 Degradation

**Overview**:
If the underlying SQLite build lacks the FTS5 extension module, `StateDB` catches the specific `sqlite3.OperationalError`, sets `self.fts_enabled = False`, logs a warning, and continues operating — every FTS-dependent method becomes a safe no-op instead of crashing the pipeline.

**Detailed description**:
`_init_fts` (state.py:314-333) wraps `_FTS_SQL` execution in a `try/except sqlite3.OperationalError`, inspecting the error message for `"fts5"` or `"no such module"`. Any other `OperationalError` is re-raised — the except is intentionally narrow so it does not mask unrelated SQL errors during table creation. This is the load-bearing mechanism behind the architecture's documented statement that "`retrieval.mode` ... degrades to vector-only when `StateDB.fts_enabled` is False": every write path that would touch FTS (`_fts_index_note`, `_fts_index_chunk`, `_fts_delete_chunk`) and every read path (`search_notes_fts`, `search_chunks_fts`, `rebuild_fts`) starts with an `if not self.fts_enabled: return` / `return []` guard.

The practical effect ripples well beyond `state.py` — it is the reason `Retriever` (retrieval.py) can unconditionally call `db.search_notes_fts(...)` without first checking capability; the capability check is centralized once, in the producer, rather than duplicated in every consumer. This is a deliberate defensive-programming choice: a portability concern (not all SQLite builds ship FTS5, notably some minimal/embedded builds) is fully absorbed by the persistence layer so the rest of the pipeline can treat hybrid search as best-effort rather than a hard requirement.

For a pre-existing database that already has FTS5 support but was created before the FTS tables existed, `_backfill_fts` (state.py:336-354) performs a one-time population by checking `COUNT(*)` on `fts_notes`/`fts_chunks` and only inserting when the count is zero — an idempotent backfill guard rather than a versioned migration flag.

**Rule workflow**:
```
_init_schema()
  -> executescript(_FTS_SQL)
     -> success: fts_enabled=True -> _backfill_fts() (only if FTS tables are empty but source tables are not)
     -> OperationalError containing "fts5"/"no such module": fts_enabled=False, log warning, return
     -> any other OperationalError: re-raise (real bug, not a missing-module case)
```

---

### Business Rule: Additive-Only Schema Migration

**Overview**:
`_migrate_schema` (state.py:388-457) applies a hardcoded, ordered list of `(table, column, coltype)` tuples via `ALTER TABLE ... ADD COLUMN`, swallowing the "duplicate column name" error SQLite raises when the column already exists.

**Detailed description**:
SQLite has no `ADD COLUMN IF NOT EXISTS` syntax, so the migration list is executed unconditionally on every `StateDB` construction, and idempotency is achieved purely by catching and inspecting the resulting `OperationalError`'s message text (`"duplicate column name" not in str(e).lower()` re-raises anything unexpected). There are 41 migration entries as of this analysis, spanning multiple historical schema phases documented inline by Portuguese comments (`# Fase 0 — retencao maxima no SQLite`, `# Metadados bibliograficos ABNT`, `# LIT granular — paginas, offset, checkpoint de processamento`, `# Custos LLM / embeddings`). This is effectively the project's entire schema-versioning strategy: there is no separate migrations directory, no schema-version table, and no rollback path — only forward, additive column grafting onto tables whose base shape is defined once in `_SCHEMA_SQL` for brand-new databases.

`test_migration_adds_new_columns_to_old_db` (tests/test_state.py:153-181) is the regression guard for this behavior: it hand-builds a pre-"Fase 0" schema (a much narrower `_OLD_SCHEMA_SQL`), inserts a row, reopens it through `StateDB`, and asserts the new columns exist with sane defaults while the original row's data (`title == "t"`) survives untouched. This test is the closest thing the component has to a schema-compatibility contract test, and it only covers one historical migration boundary — it does not (and structurally cannot, given the current design) verify every intermediate schema version a real long-lived vault database might have passed through.

Indexes (`_INDEX_SQL`) are deliberately created *after* migration completes (state.py:262-263 comment), because some indexed columns (e.g. `concepts.status`) do not exist in pre-migration databases and creating the index earlier would fail.

**Rule workflow**:
```
_init_schema()
  -> executescript(_SCHEMA_SQL)      # base tables, CREATE TABLE IF NOT EXISTS
  -> _migrate_schema()               # ALTER TABLE ADD COLUMN for every historical addition
  -> executescript(_INDEX_SQL)       # indexes, safe now that migrated columns exist
  -> _init_fts()
```

---

### Business Rule: COALESCE-Preserving Upserts

**Overview**:
Nearly every `upsert_*` method's `ON CONFLICT ... DO UPDATE SET` clause wraps optional columns in `COALESCE(excluded.column, table.column)` rather than unconditionally overwriting with `excluded.column`.

**Detailed description**:
This pattern appears in `upsert_source` (state.py:568-580), `upsert_chunk` (state.py:848-855), `upsert_note` (state.py:1056-1063), `upsert_moc` (state.py:1230-1234), `upsert_concept` (state.py:1005-1007), and `upsert_asset` (state.py:1358-1361). The intent is that pipeline phases legitimately call the same upsert method multiple times across the lifecycle of one entity, each time knowing only a subset of the full field set (e.g. `harvester.py` first creates a source row with structural metadata, then `review.py`/`sync.py` later call `update_source_texts`/`upsert_source` again with bibliographic fields filled in). If those partial calls used a plain `excluded.column` overwrite, a later call passing `None` for a field the caller doesn't currently have data for would silently erase previously-recorded data — for example, a re-harvest that doesn't recompute `docling_config_hash` would otherwise wipe out the paging checkpoint.

Not every column follows this rule uniformly: some are intentionally always overwritten unconditionally on conflict — e.g. `upsert_source`'s `title`, `authors`, `year`, `file_checksum`, `extraction_checksum`, `origin` (state.py:564-567) are always replaced with the new value, while `document_type`, `bibliography_json`, `abnt_reference`, and the paging fields are `COALESCE`-preserved (state.py:568-580). This is a deliberate two-tier design: core identity fields track the latest write; enrichment fields (filled in by a *later*, independent phase) are protected from being nulled out by an *earlier* phase's re-run. A reader of the SQL needs to check each column's clause individually — the rule is consistent within a table but not universal across all columns of a table, which increases the cost of introducing a new column correctly (see Technical Debt).

**Rule workflow**:
```
caller invokes upsert_X(id, ...partial-fields..., some_field=None)
  -> INSERT ... ON CONFLICT(id) DO UPDATE SET
       core_field = excluded.core_field                          (always replaced)
       enrichment_field = COALESCE(excluded.enrichment_field, X.enrichment_field)  (preserved if not supplied)
```

---

### Business Rule: Three-Layer Duplicate Detection Support

**Overview**:
`StateDB` provides the lookup primitives for the harvester's three-layer duplicate-detection pipeline (documented at the project level in CLAUDE.md): `get_file_by_checksum` for byte-identical files, and `get_source_by_extraction_checksum` for cross-format textually-identical sources. The third layer (semantic similarity) lives entirely in ChromaDB/`harvester.py` and does not touch `StateDB`.

**Detailed description**:
`get_file_by_checksum` (state.py:506-520) finds any `files` row sharing the same `file_checksum` at a *different* path (`exclude_path`), ordered by `last_seen_at ASC` so the earliest-seen copy — presumably the canonical one — wins ties. This lets the harvester recognize "the same PDF bytes dropped into the inbox under a new filename" and reuse the existing `source_id` instead of reprocessing.

`get_source_by_extraction_checksum` (state.py:773-792) performs the analogous lookup one level up the pipeline: after Docling/native extraction produces normalized text, its checksum is compared against every existing source's `extraction_checksum`. This catches the case where the *same underlying document* was saved twice in different container formats (e.g. a PDF and a hand-exported Markdown of the same paper) — the raw bytes differ, so layer 1 misses it, but the canonicalized text is identical.

Both methods return `Optional[dict]` (single row or `None`) rather than a list, and both support an `exclude_*` parameter specifically so a source/file can check "does anything *other than me* already have this fingerprint" during a re-harvest of itself. Test coverage: `test_get_file_by_checksum_detects_renamed_copy` and `test_get_source_by_extraction_checksum_cross_format` (tests/test_state.py:100-128) both assert the positive match, the `exclude_*` negative case, and the "nothing found" `None` case.

**Rule workflow**:
```
harvester._process_file(new_file)
  -> db.get_file_by_checksum(new_file_hash, exclude_path=new_file.path)
       found -> reuse existing source_id (Layer 1, no re-extraction)
       None  -> extract text, compute extraction_checksum
                -> db.get_source_by_extraction_checksum(extraction_checksum)
                     found -> reuse existing source (Layer 2)
                     None  -> proceed to Layer 3 (ChromaDB semantic check, outside StateDB)
```

---

### Business Rule: Approved-and-Unlinked Concept Gate for `connect`

**Overview**:
`get_concepts_by_status("approved", without_notes=True)` (state.py:1022-1032) is the exact query `connector.py` uses to decide which concepts are eligible for permanent-note generation — it is documented in CLAUDE.md as "the source of truth after review."

**Detailed description**:
A concept only becomes a candidate for the `connect` phase once two independent conditions hold simultaneously: `status='approved'` (set by `review.py` after a human or auto-approval gate) and `note_id IS NULL` (meaning no permanent note has been generated for it yet). This combination is what makes `connect` idempotent and safely re-runnable — running it twice does not regenerate notes for concepts that already have one, and running it after a partial failure picks up exactly where it left off, because `note_id` is only ever set once `connector.py` successfully writes the resulting `ZTL` note back via `upsert_concept`.

The `without_notes` flag is a boolean toggle on the same method rather than a separate query, which keeps the "give me everything in this status" and "give me the actionable subset" use cases co-located; `get_web_dashboard` and CLI status reporting call the same method with `without_notes` omitted (defaulting to `False`) to get raw status counts including already-linked concepts, while `connector.py`'s production code path always passes `without_notes=True`.

`test_concept_candidate_and_status` (tests/test_state.py:195-215) directly exercises the state transition this rule depends on: a concept starts `extracted`, moves to `approved` via `update_concept_status`, appears in the `without_notes=True` filter; then a second `upsert_concept` call sets `note_id="note1"` and status `"noted"`, after which the same filter returns an empty list — proving the gate closes exactly when a note is attached, independent of whatever the status string becomes afterward.

**Rule workflow**:
```
extractor.py: upsert_concept(status="extracted" or similar, note_id=None)
review.py:    update_concept_status(concept_id, "approved")   # after HITL/auto-approve
connector.py: get_concepts_by_status("approved", without_notes=True)
                -> for each: generate ZTL note, then upsert_concept(..., note_id=<new note id>)
                -> concept now excluded from future without_notes=True queries
```

---

### Business Rule: Source Deletion Cascade Preserves Permanent Notes by Default

**Overview**:
`delete_source_cascade` (state.py:731-765) removes a source and every row that structurally depends on it (chunks, concepts, chapters, assets, file-tracking rows) but deliberately never deletes rows from the `notes` table — permanent-note deletion is a separate, opt-in operation (`delete_note`, `clear_source_id_on_notes`).

**Detailed description**:
This split exists because a `ZTL` permanent note synthesizes and reinterprets ideas from a source — once written, its intellectual content is considered to outlive the source it was derived from (per the Zettelkasten methodology the whole project implements). `zettel delete-source` (`purge_source.py`, documented in CLAUDE.md) therefore defaults to keeping linked `ZTL` notes on disk and in the index, only calling `clear_source_id_on_notes` (state.py:722-729) to null out the now-dangling `notes.source_id` foreign reference, plus vault-level dead-wikilink stripping (handled outside `StateDB`, in `purge_source.py`/`vault.py`). Only when the caller explicitly passes `--delete-permanent` does `purge_source.py` additionally call `delete_note` (state.py:708-720) per note id, which does remove the row, its FTS shadow entry, and every `note_connections` edge touching it (both as source and target, via an `OR` clause) — a real content deletion, not a soft detach.

Within `delete_source_cascade` itself, the deletion order matters for referential correctness even though it is not wrapped in an explicit `BEGIN`/`COMMIT` transaction block (all statements execute on the same connection before one final `commit()` at state.py:757, so they are still atomic as a unit): chunks are deleted first via `delete_chunks` (which itself deletes each chunk's `concepts` rows and FTS shadow row before the chunk row, state.py:662-666), then chapters, then any concepts that might have been orphaned without a matching chunk (a defensive `DELETE FROM concepts WHERE source_id=?` in case a concept was created without ever reaching `chunk_id` linkage), then assets, then the `files` tracking rows, and finally the `sources` row itself. The method returns a per-table count dict so `purge_source.py` can report exactly what was removed.

**Rule workflow**:
```
purge_source.delete_source(citekey, delete_permanent=False)
  -> db.delete_source_cascade(source_id)
       -> delete_chunks(all chunk_ids for source)   # + their concepts + FTS rows
       -> DELETE chapters for source
       -> DELETE orphan concepts for source
       -> DELETE assets for source
       -> DELETE files rows for source
       -> DELETE sources row
  -> db.clear_source_id_on_notes(source_id)          # notes.source_id -> NULL (notes survive)
  -> [only if --delete-permanent] for each linked note_id: db.delete_note(note_id)
```

---

### Business Rule: Bidirectional Substring MOC-Topic Matching

**Overview**:
`find_moc_by_topic` (state.py:1249-1257) decides whether a newly-proposed MOC topic should reuse an existing MOC by checking, case-insensitively, whether either topic string is a substring of the other.

**Detailed description**:
This is a deliberately loose fuzzy-match rule rather than an exact-match lookup: `gardener.py`'s incremental-vs-new-MOC decision (documented in CLAUDE.md as "incremental preferred when note overlap >= `overlap_threshold` or category already has a MOC") uses this as one of its signals for topic continuity, and `article_graph.py`'s catalog/outline step reuses it to decide whether a proposed article topic already has a home MOC. Because the match is bidirectional (`existing_lower in topic_lower or topic_lower in existing_lower`), a broader newly-generated topic like "Aprendizado de Máquina Supervisionado" would match an existing narrower MOC titled "Aprendizado de Máquina", and vice versa — either direction of specialization/generalization is treated as the same topic family.

The method iterates `list_mocs()` (all MOCs, not scoped by origin) in application code rather than pushing the substring logic into SQL (`LIKE` both ways), which is a pragmatic but O(n) choice — acceptable given each vault realistically holds dozens to low hundreds of MOCs, not acceptable at a much larger scale. Because the check has no minimum topic length guard, a very short or generic topic string (e.g. a single common word) could match unrelated MOCs purely by substring coincidence — this is a soft ambiguity the caller (LLM-driven `gardener.py`) is expected to have already produced a reasonably specific topic string for.

**Rule workflow**:
```
gardener._process_cluster(candidate_topic)
  -> db.find_moc_by_topic(candidate_topic)
       for each moc in db.list_mocs():
         if moc.topic.lower() in candidate_topic.lower() or candidate_topic.lower() in moc.topic.lower():
           return moc   # first match wins, no ranking among multiple matches
  -> match found:    route through incremental MOC update path
  -> no match:        create new MOC (subject to categoria/strict_topics validation elsewhere)
```

---

### Business Rule: Scoped MOC Family Bulk Deletion

**Overview**:
`delete_pipeline_mocs` and `delete_hub_pipeline_mocs` (state.py:1259-1273) are separate methods that each delete only rows matching one `origin` value (`'pipeline'` vs `'hub_pipeline'`), so the two MOC-generation pipelines (taxonomy-based Phase 4 vs. hub-anchored Phase 4b) can each be recreated independently.

**Detailed description**:
`zettel garden --recreate` calls `delete_pipeline_mocs`, and `zettel garden --hubs --recreate` calls `delete_hub_pipeline_mocs` — the CLAUDE.md architecture notes explicitly call out that taxonomy pipeline MOCs (`origin='pipeline'`) are untouched by a hub-scoped recreate. Both methods return the deleted rows (not just a count) before committing the delete, because callers need the pre-deletion `frontmatter_json`/`path` values to call `moc_backrefs.clear_moc_backrefs` on the linked permanent notes *before* the MOC rows disappear — the row data is the payload the backref-cleanup step consumes, not just a log artifact.

Manually-created MOCs (`origin='manual'`, created via `zettel new-note --moc` and indexed via `sync-manual`) are excluded from both bulk-delete paths by construction, since neither `'pipeline'` nor `'hub_pipeline'` matches `'manual'` — a `--recreate` run can never destroy a hand-authored MOC.

**Rule workflow**:
```
garden --recreate
  -> deleted = db.delete_pipeline_mocs()          # only origin='pipeline'
  -> for moc in deleted: moc_backrefs.clear_moc_backrefs(moc)
  -> re-run the taxonomy clustering pipeline from scratch

garden --hubs --recreate
  -> deleted = db.delete_hub_pipeline_mocs()       # only origin='hub_pipeline'
  -> for moc in deleted: moc_backrefs.clear_moc_backrefs(moc)
  -> re-run hub-anchored MOC generation from scratch
```

---

### Business Rule: Weighted Undirected Graph Degree for Hub Ranking

**Overview**:
`get_weighted_note_degrees` (state.py:1275-1290) computes, for every note appearing in `note_connections`, an undirected weighted degree by summing a per-relation-type weight for every edge touching that note (as either source or target), defaulting unknown relation types to the `'related'` weight.

**Detailed description**:
This is the ranking signal `gardener_hub.py`'s Phase 4b uses to pick which permanent notes are "hub" candidates worth anchoring a MOC neighborhood expansion around (BFS via `graph.expand_notes`). The weights themselves are not owned by `StateDB` — they come from `config.DEFAULT_RELATION_WEIGHTS`, imported lazily inside `get_web_dashboard` (state.py:1676) and passed in as a parameter to `get_weighted_note_degrees` by its callers (`gardener_hub.py`, `get_web_dashboard`) — so `StateDB` stays agnostic to what the actual weight values are; it only performs the aggregation. Per CLAUDE.md, `contradicts` carries the highest default weight ("it's the signal embeddings miss"), meaning two notes connected by a `contradicts` edge contribute more to both notes' hub-worthiness scores than two notes merely `related`.

The computation itself is a single full-table scan of `note_connections` with in-Python aggregation via `collections.defaultdict(float)` rather than a SQL `GROUP BY` — reasonable at the vault sizes this project targets, but it means hub ranking is O(edges) on every call with no caching; `get_web_dashboard` calls it fresh on every dashboard load.

**Rule workflow**:
```
get_weighted_note_degrees(relation_weights)
  for each row in note_connections:
    weight = relation_weights.get(row.relation_type, relation_weights.get('related', 0.5))
    degrees[row.source_note_id] += weight
    degrees[row.target_note_id] += weight
  return degrees   # dict[note_id -> float]
```

---

### Business Rule: Web Job Mutual Exclusion via `BEGIN IMMEDIATE`

**Overview**:
`create_web_job` (state.py:1529-1550) guarantees at most one mutating pipeline operation is ever `queued` or `running` in the web UI at a time, by wrapping the check-then-insert in an explicit `BEGIN IMMEDIATE` transaction.

**Detailed description**:
`BEGIN IMMEDIATE` acquires SQLite's write lock immediately rather than lazily on the first write statement, which closes the race window that a plain `BEGIN` (deferred) would leave open: two near-simultaneous web requests could otherwise both read "no active job" before either had inserted its row. With `BEGIN IMMEDIATE`, the second caller's transaction blocks (or, without a busy timeout, could raise `sqlite3.OperationalError: database is locked` — see Technical Debt) until the first caller's transaction commits or rolls back, so the `SELECT ... WHERE state IN ('queued','running') LIMIT 1` guard inside the transaction is guaranteed to observe a consistent view. If an active job is found, the method explicitly rolls back and returns `False` (translated by `web.py` into an HTTP 409, per CLAUDE.md); otherwise it inserts the new job row and commits, returning `True`. Any unexpected exception during the block also triggers an explicit rollback before re-raising, so a failed insert can never leave the transaction half-open.

This is the concrete mechanism behind the CLAUDE.md-documented rule: "single Uvicorn worker, at most one mutating job (`queued`/`running`) at a time; second submit → 409." It is enforced entirely inside `StateDB`, not in `web.py` or `web_app.py`, which means the guarantee holds even if a caller forgot to check job state beforehand — the invariant is structurally impossible to violate through this code path.

**Rule workflow**:
```
create_web_job(job_id, operation, payload)
  -> BEGIN IMMEDIATE                                  # acquire write lock now, not lazily
  -> SELECT job_id FROM web_jobs WHERE state IN ('queued','running') LIMIT 1
       found    -> ROLLBACK, return False              # caller surfaces HTTP 409
       not found -> INSERT new row (state='queued'), COMMIT, return True
  -> (any exception) -> ROLLBACK, re-raise
```

---

### Business Rule: Web Job Crash Recovery

**Overview**:
`recover_web_jobs` (state.py:1515-1527) is called once at process startup and force-transitions every job still marked `running` (a leftover from a process that died mid-job) to `interrupted`, while leaving `queued` jobs untouched so the new worker instance picks them up normally.

**Detailed description**:
Because `web_jobs.state` is persisted (not held only in worker-thread memory), a server restart — deploy, crash, manual kill — would otherwise leave a job permanently stuck in `running`, which would in turn permanently block `create_web_job`'s mutual-exclusion guard from ever allowing a new job (since the guard checks for `state IN ('queued','running')`). `recover_web_jobs` is the compensating action that makes the queue self-healing on restart: it is a single `UPDATE` gated purely on `state='running'`, setting `phase='interrupted'` and a fixed Portuguese message (`"Interrompido pela reinicializacao da aplicacao"`), and stamps `finished_at`. `queued` jobs are correctly left alone — they were never claimed, so no work was lost, and the new worker's normal claim loop (`claim_web_job`) will pick them up.

`test_recovery_interrupts_running_but_keeps_queued` (tests/test_web_state.py:29-40) directly asserts both halves of this behavior in one test: a claimed (`running`) job becomes `interrupted` after `recover_web_jobs()`, while a separately-created `queued` job remains `queued` and is unaffected by the same call.

**Rule workflow**:
```
process startup (web_app.py)
  -> db.recover_web_jobs()
       UPDATE web_jobs SET state='interrupted', phase='interrupted',
              message='Interrompido pela reinicializacao da aplicacao', finished_at=now()
       WHERE state='running'
  -> queued jobs: untouched, resumed by the new WebWorker's normal claim loop
```

---

### Business Rule: FTS5 Query Sanitization and PT-BR Stopword Filtering

**Overview**:
`_fts_match_expr` (state.py:40-59) converts arbitrary user text into a safe FTS5 `MATCH` expression by tokenizing on `\w+`, dropping tokens shorter than 2 characters or present in a hardcoded PT-BR stopword list, double-quoting every surviving token, and OR-joining them (capped at 32 tokens).

**Detailed description**:
Double-quoting each token is what neutralizes FTS5's query-language operators (`-`, `*`, `NEAR`, `:`, `AND`/`OR`/`NOT`) — because raw user text (an `ask` question, a search box query) is never interpolated into the `MATCH` expression unescaped, a query containing FTS5 syntax characters cannot inject query semantics; it is always treated as a literal phrase search per token. Tokens are OR-joined rather than AND-joined deliberately: a natural-language question rarely has every one of its content words present verbatim in a single relevant note, so BM25 ranking is left to reward notes matching more terms, with RRF fusion (in `retrieval.py`) reconciling this against the dense vector ranking.

The stopword list (`_PT_STOPWORDS`, state.py:26-37) exists because the alternative — leaving high-frequency closed-class PT-BR words like "que", "de", "para", "não" unfiltered — would make the OR-joined MATCH expression match nearly the entire corpus on almost any query, which has two compounding effects documented in the code comments: it pollutes BM25 ranking generally, and it silently defeats `retrieval.py`'s `bm25_hit_bypasses_floor` relevance-floor bypass (a "hit" on a meaningless stopword would otherwise look like strong lexical evidence). This is called out in the CLAUDE.md architecture section as a production bug fix, not a hypothetical concern.

Three independent safety properties are unit-tested in isolation (tests/test_state.py:274-306): operator neutralization (`"deep-learning NEAR redes"` becomes 4 independently-quoted tokens, `NEAR` included as a literal word, never as an operator), short/symbol-only tokens are dropped (`"C++"` → `None`), and stopword-only queries return `None` so the caller can treat them as an empty query rather than issuing a MATCH that would return everything.

**Rule workflow**:
```
_fts_match_expr(user_text)
  -> tokens = [t for t in \w+ matches if len(t)>=2 and t.lower() not in _PT_STOPWORDS]
  -> no tokens survive -> return None (caller treats as empty search)
  -> tokens[:32] -> join(' OR ', f'"{t}"' for t in tokens)
  -> caller (search_notes_fts / search_chunks_fts) executes:
       SELECT ... WHERE fts_table MATCH <expr> ORDER BY rank LIMIT ?
```

---

## 4. Component Structure

`state.py` is a single file with no sub-package structure. Internal organization is by comment banner within one class:

```
zettel/state.py                                  (1,726 lines)
├── Module-level constants/helpers (lines 1-59)
│   ├── _FTS_TOKEN_RE                             # \w+ tokenizer regex
│   ├── _PT_STOPWORDS                             # PT-BR closed-class word filter set
│   └── _fts_match_expr()                         # safe FTS5 MATCH-expression builder
├── Module-level SQL constants (lines 61-288)
│   ├── _SCHEMA_SQL                                # CREATE TABLE IF NOT EXISTS x13
│   ├── _INDEX_SQL                                 # CREATE INDEX IF NOT EXISTS x8
│   └── _FTS_SQL                                   # CREATE VIRTUAL TABLE (fts5) x2
└── class StateDB                                  (lines 291-1726, 96 methods)
    ├── __init__ / _init_schema / _init_fts /
    │   _backfill_fts / _migrate_schema / close / vacuum   # lifecycle & schema evolution
    ├── ── Generic helpers ──            (473-484)  _now, _fetchone, _fetchall
    ├── ── Files ──                      (486-520)  upsert_file, get_file, get_file_by_checksum
    ├── ── Sources ──                    (522-795)  upsert_source, update_source_texts,
    │                                                update_source_paging, delete_source_cascade,
    │                                                get_source*, list_sources
    ├── ── Chapters ──                   (797-814)  upsert_chapter, get_chapters_for_source
    ├── ── Chunks ──                     (816-984)  upsert_chunk, delete_chunks(_for_chapter),
    │                                                get_pending/failed/by_status, update_chunk_*
    ├── ── Concepts ──                   (986-1032) upsert_concept, get/update_concept*,
    │                                                get_concepts_by_status
    ├── ── Notes ──                      (1034-1098) upsert_note, get_note, list_notes,
    │                                                 get_note_ids_for_source, delete_note,
    │                                                 clear_source_id_on_notes, update_note_embedding
    ├── ── Full-text (BM25) search ──    (1100-1164) search_notes_fts, search_chunks_fts,
    │                                                 rebuild_fts, _fts_index_note/_chunk (private)
    ├── ── Note Connections ──           (1166-1209) upsert_note_connection, get_note_connections,
    │                                                 get_connections_for_notes, count_note_connections
    ├── ── MOCs ──                       (1211-1338) upsert_moc, get_moc*, list_mocs,
    │                                                 find_moc_by_topic, delete_*_mocs,
    │                                                 get_weighted_note_degrees, hub-anchor lookups
    ├── ── Assets (images) ──            (1340-1400) upsert_asset, get_asset*, update_asset_*
    ├── ── LLM Cache ──                  (1402-1418) get_cached_llm_response, cache_llm_response
    ├── ── Runs ──                       (1420-1511) start_run, finish_run, add_source_usage,
    │                                                 record_duplicate, get_run, get_last_run
    ├── ── Web job queue ──              (1513-1697) create/claim/update/list web_job(s),
    │                                                 add/list web_job_events, recover_web_jobs,
    │                                                 get_web_dashboard
    └── ── Stats ──                      (1699-1725) get_stats
```

---

## 5. Dependency Analysis

```
Internal Dependencies:
zettel.state.StateDB --(lazy import, get_web_dashboard only)--> zettel.config.DEFAULT_RELATION_WEIGHTS

  (Everything else that "depends on state.py" is the reverse direction — see Section 6.
   state.py itself has essentially zero internal fan-out; it is a dependency sink, not
   a dependency source, which is exactly what a persistence layer should be.)

External Dependencies (all Python standard library — no third-party packages):
- sqlite3        - core persistence engine; relies on the optional FTS5 loadable module
                    for hybrid lexical search (gracefully degrades if unavailable)
- json           - serializes authors/frontmatter/payload/result fields into TEXT columns
- logging        - warns when FTS5 is unavailable or an FTS query fails
- re             - FTS5 MATCH-expression tokenization (_FTS_TOKEN_RE)
- datetime        - ISO-format timestamps for every created_at/updated_at/last_seen_at column
- pathlib        - db_path handling, parent-directory creation
- typing         - Any, Optional type hints
- collections.defaultdict - local import inside get_weighted_note_degrees only
```

---

## 6. Afferent and Efferent Coupling

`state.py` defines a single public class, so afferent/efferent coupling is broken out at two granularities: the class as a whole, and its internal functional method-groups (the natural "sub-components" within this one-class file, grouped by the table(s) each group owns). Afferent counts are the number of **distinct production modules** (excluding tests) observed calling at least one method in that group, found via `\bdb\.method_name\(` occurrences across `zettel/*.py`.

**Class-level:**

| Component | Afferent Coupling | Efferent Coupling | Critical |
|-----------|-------------------|--------------------|----------|
| `StateDB` (whole class) | 25 distinct production modules (329 total call sites) | 1 (`zettel.config.DEFAULT_RELATION_WEIGHTS`, lazy) + stdlib only | High |

**Method-group level** (sub-components within the class, by owned table domain):

| Method Group | Afferent Coupling (modules) | Efferent Coupling | Critical |
|--------------|------------------------------|--------------------|----------|
| Notes (`upsert_note`, `get_note`, `list_notes`, `get_note_ids_for_source`, `delete_note`, `clear_source_id_on_notes`, `update_note_embedding`, `list_permanent_note_ids`) | 13 (article, article_graph, ask, connector, gardener, gardener_hub, moc_backrefs, purge_source, retrieval, sync, web, rebuild, cli) | sqlite3 only | High |
| Sources (`upsert_source`, `get_source*`, `list_sources`, `update_source_texts`, `update_source_paging`, `delete_source_cascade`) | 13 (article, chunk_dump, cli, connector, extraction_dump, extractor, harvester, purge_source, review, sync, vault, web, rebuild) | sqlite3 only | High |
| Chunks (`upsert_chunk`, `get_pending/failed/by_status`, `get_chunk(s)_for_source`, `update_chunk_*`, `delete_chunks*`) | 11 (chunk_dump, cli, harvester, purge_source, rebuild, review, web, connector, sync, extractor, web_app) | sqlite3 only (+ inline FTS self-call) | High |
| Runs / usage (`start_run`, `finish_run`, `add_source_usage`, `record_duplicate`, `get_run`, `get_last_run`) | 11 (article_graph, ask, connector, extractor, gardener, gardener_hub, harvester, review, web_app, usage, cli) | sqlite3 only | Medium |
| MOCs (`upsert_moc`, `get_moc*`, `list_mocs`, `find_moc_by_topic`, `delete_*_pipeline_mocs`, hub-anchor lookups, `get_weighted_note_degrees`) | 7 (gardener, gardener_hub, sync, web, gardener_assign, rebuild, article_graph) | sqlite3, `zettel.config` (weights) | Medium |
| Assets (`upsert_asset`, `get_asset*`, `update_asset_*`, `reset_failed_assets`) | 7 (assets, article, extractor, purge_source, connector, cli, web_app) | sqlite3 only | Medium |
| Concepts (`upsert_concept`, `get_concept*`, `update_concept*`) | 6 (connector, extractor, cli, review, web, web_app) | sqlite3 only | Medium |
| LLM Cache (`get_cached_llm_response`, `cache_llm_response`) | 6 (article, ask, assets, bibliography, connector, extractor) | sqlite3 only | Medium |
| Note Connections (`upsert_note_connection`, `get_note_connections`, `get_connections_for_notes`, `count_note_connections`) | 6 (connector, sync, web, gardener_assign, graph, cli) | sqlite3 only | Medium |
| Files (`upsert_file`, `get_file`, `get_file_by_checksum`) | 3 (harvester, web, web_app) | sqlite3 only | Low |
| FTS search (`search_notes_fts`, `search_chunks_fts`, `rebuild_fts`) | 2 (retrieval, rebuild) | sqlite3 (FTS5 module) | Low |
| Stats (`get_stats`) | 2 (cli, web) | sqlite3 only | Low |
| Web job queue (`create/claim/update_web_job`, `get/list_web_job(s)`, `add/list_web_job_events`, `recover_web_jobs`, `get_web_dashboard`) | 1 (web_app.py exclusively) | sqlite3 only | Medium — single consumer, but that consumer is the entire web UI's operational backbone; failure here has no fallback |

**Interpretation**: Notes and Sources are `StateDB`'s highest-traffic sub-domains (13 distinct calling modules each), consistent with the pipeline's document-and-note-centric design. The Web job queue group has the *narrowest* afferent fan-in (only `web_app.py`) despite being architecturally significant — it is a cohesive sub-responsibility that shares no tables and almost no logic with the rest of the class, and is the clearest candidate the analysis surfaces for extraction into its own class (see Technical Debt, "Low cohesion / mixed responsibilities").

---

## 7. Integration Points

| Integration | Type | Purpose | Protocol | Data Format | Error Handling |
|-------------|------|---------|----------|-------------|-----------------|
| SQLite file (`config.state_db_path`, default `./data/state.db`) | Embedded database | Sole persistence store for all pipeline/domain state and the web job queue | Local file I/O via `sqlite3` DB-API, WAL mode | Relational rows; JSON-encoded TEXT columns for lists/dicts (`authors`, `frontmatter_json`, `payload_json`, `result_json`, `candidate_json`, `bibliography_json`) | Narrow `except sqlite3.OperationalError` around FTS5 module absence and FTS query failures (logged, degraded); duplicate-column errors swallowed during migration; all other SQLite errors propagate uncaught to the caller |
| SQLite FTS5 extension (optional, same file) | Embedded search index | BM25 lexical search backing hybrid retrieval | Same connection, `MATCH` queries | FTS5 virtual table rows (`unicode61 remove_diacritics 2` tokenizer) | Capability-gated via `fts_enabled`; absence degrades to no-op rather than error |
| `zettel.config.AppConfig` / `DEFAULT_RELATION_WEIGHTS` | Internal module | Supplies `state_db_path` (construction) and relation-type weights (hub degree scoring) | Direct Python import/call | Pydantic model / plain dict | None needed — pure in-process data access |
| 25 production `zettel/*.py` modules (harvester, extractor, review, connector, gardener, gardener_hub, gardener_assign, sync, purge_source, moc_backrefs, retrieval, graph, ask, article, article_graph, assets, bibliography, chunk_dump, extraction_dump, rebuild, vault, usage, cli, web, web_app) | Internal consumers | Every pipeline phase's incremental-processing state | Direct method calls on an injected `StateDB` instance | Python dicts (`sqlite3.Row` converted via `_fetchone`/`_fetchall`) | Callers are responsible for their own error handling; `StateDB` itself raises only `ValueError` (`record_duplicate` with an unknown kind) as an explicit domain-level validation error |

---

## 8. Design Patterns & Architecture

| Pattern | Implementation | Location | Purpose |
|---------|-----------------|----------|---------|
| Active Record / thin repository | `StateDB` methods map near 1:1 onto table CRUD operations, returning plain dicts rather than domain objects | zettel/state.py (whole class) | Keeps persistence logic centralized and simple; avoids an ORM dependency |
| Upsert-with-COALESCE (safe merge) | `ON CONFLICT ... DO UPDATE SET col=COALESCE(excluded.col, table.col)` | state.py:563-581, 844-855, 1055-1064, 1230-1234 | Lets independent pipeline phases progressively enrich the same row without clobbering fields they don't know about |
| Additive schema migration (poor-man's Alembic) | `_migrate_schema`'s ordered `ALTER TABLE ADD COLUMN` list with duplicate-column suppression | state.py:388-457 | Zero-dependency forward-only schema evolution across releases |
| Feature-capability flag | `self.fts_enabled` set once at construction, checked as a guard in every FTS-touching method | state.py:303, 314-333, and all `search_*_fts`/`_fts_index_*` methods | Graceful degradation when the runtime SQLite build lacks FTS5 |
| Optimistic write-lock transaction | `BEGIN IMMEDIATE` + guard `SELECT` + explicit `ROLLBACK`/`COMMIT` | state.py:1529-1550 (`create_web_job`) | Atomic single-active-job invariant without an external lock manager |
| Idempotent backfill | Count-then-populate guard (`if count==0: insert from source table`) | state.py:336-354 (`_backfill_fts`) | One-time population for databases created before a feature existed, safe to call unconditionally on every startup |
| Batch query for graph traversal | Single `IN (...)` query per BFS frontier instead of N per-node queries | state.py:1188-1203 (`get_connections_for_notes`) | O(hops) round-trips instead of O(nodes) for graph expansion |
| Sanitizing query builder | Tokenize → filter → quote → join, applied before any use of user text in a MATCH expression | state.py:40-59 (`_fts_match_expr`) | Prevents FTS5 query-syntax injection from user-controlled search text |
| Row-factory adapter | `sqlite3.Row` + `dict(row)` conversion helpers | state.py:298, 478-484 (`_fetchone`/`_fetchall`) | Presents SQL results as plain, JSON-serializable dicts to the rest of the codebase |
| Instance-per-thread connection (no shared handle) | Each thread/process that needs `StateDB` calls `StateDB(same_path)` independently | web_app.py:133, 180, 374 (callers), relying on state.py's WAL mode | Avoids `sqlite3`'s default `check_same_thread` restriction without disabling it, at the cost of relying entirely on WAL + file-level locking for cross-connection consistency |

---

## 9. Technical Debt & Risks

| Risk Level | Component Area | Issue | Impact |
|------------|-----------------|-------|--------|
| High | Concurrency (whole class) | No `PRAGMA busy_timeout` is ever set on any connection, despite WAL mode being relied upon for multi-connection access (CLI process + web worker thread + potential concurrent `ask`/`article` runs, each opening an independent connection to the same file) | Under write contention (e.g. a long-running `garden` job holding a write transaction while the web dashboard or another CLI command tries to write), a competing writer can receive an immediate `sqlite3.OperationalError: database is locked` instead of waiting, since SQLite's default busy behavior without a timeout is to fail fast |
| High | Overall class design | `StateDB` is a single ~1,430-line, 96-method class covering 13 structurally and semantically unrelated table domains, including an operational web-job queue that has no relation to the pipeline's domain tables | Any schema or behavioral change requires reasoning about a class with very low cohesion; the class cannot be unit-tested or reasoned about in isolated slices without loading the entire schema; a bug introduced in, say, the web-job-queue methods risks a merge/review blast radius across a file that also defines core domain persistence |
| Medium | `get_web_dashboard` (state.py:1654-1660) | The "alta"/"média"/"baixa" confidence-band SQL hardcodes the `0.85` boundary directly in the query, rather than reading the actual configured `literature_review.auto_approve_min_confidence` threshold used by `review.py`'s real approval gate | If an operator changes the auto-approve threshold in `config.yaml`, the web dashboard's confidence bands silently stop reflecting the real approval boundary, misleading anyone reading the dashboard about what "alta" actually means for their configuration |
| Medium | Schema migration (state.py:388-457) | Migrations are a flat, ever-growing list applied unconditionally on every startup with no schema-version tracking; correctness relies entirely on string-matching `"duplicate column name"` in the exception message | A SQLite version/build with a differently-worded duplicate-column error message would cause the migration to raise instead of silently no-op-ing; the list also has no way to represent a migration more complex than "add a nullable/defaulted column" (e.g. a rename, a type change, a backfill of derived data) without a bespoke one-off method like `_backfill_fts` |
| Medium | Redundant imports (state.py:1304, 1322) | `find_moc_by_hub_note_id` and `list_hub_anchor_note_ids` each locally `import json`, even though `json` is already imported at module scope (state.py:8) | Not a functional bug, but a maintenance smell — duplicated, unnecessary local imports; likely leftover from incremental edits |
| Medium | `find_moc_by_topic` (state.py:1249-1257) | Substring-based bidirectional topic matching with no minimum topic length and O(n) full-table scan (in Python, not SQL) over `list_mocs()` | A short or generic candidate topic string could match an unrelated MOC purely by substring coincidence; performance degrades linearly as the number of MOCs grows, with no index-assisted shortcut possible for a substring-both-ways comparison |
| Low | Transaction granularity | Nearly every public method calls `self.conn.commit()` at its own end; multi-step operations like `delete_source_cascade` are atomic only because they happen to run sequentially on one connection before one final commit — there is no explicit `BEGIN`/`COMMIT` boundary object, so a future refactor that splits such a method's statements across two calls would silently lose atomicity | Latent correctness risk if the class is refactored without this implicit constraint being documented anywhere outside code comments |
| Low | `vacuum()` (state.py:462-471) | Requires exclusive database access and can use significant temporary disk space; there is no built-in size/threshold check or lock-wait handling before running it | Called from `purge-rejected`/`delete-source` by default (opt-out via `--no-compact`); on a large vault this could stall other connections for longer than the caller anticipates, with no guardrail in `StateDB` itself |

---

## 10. Test Coverage Analysis

| Component | Unit Tests | Integration Tests | Coverage | Test Quality |
|-----------|------------|--------------------|----------|---------------|
| `StateDB` core CRUD (files, sources, chapters, chunks, concepts, notes, mocs, assets, llm_cache, runs) | ~26 direct test functions in `tests/test_state.py` (420 lines) | Indirectly exercised as the real (non-mocked) fixture in 23 other test files (`test_harvester_dedup.py`, `test_extractor.py`\*, `test_review.py`, `test_connector.py`, `test_gardener.py`, `test_gardener_hub.py`, `test_gardener_assign.py`, `test_sync.py`, `test_purge_source.py`, `test_rebuild.py`, `test_ask.py`, `test_article.py`, `test_article_graph.py`, `test_retrieval.py`, `test_graph.py`, `test_assets.py`, `test_bibliography.py`, `test_moc_backrefs.py`, `test_new_note.py`, `test_chunk_dump.py`, `test_extraction_dump.py`, `test_set_paging.py`, `test_set_paging_filter.py`) | High for the schema/CRUD surface used directly; no mocking layer for `StateDB` exists anywhere in the suite, so every test that touches the pipeline runs against a real temp-file SQLite DB | Good, direct assertions on actual DB state (`db.get_source(...)["field"]`); positive/negative/edge cases covered for checksum-lookup dedup (`get_file_by_checksum`, `get_source_by_extraction_checksum`); `record_duplicate`'s `ValueError` path is explicitly tested |
| FTS5 subsystem (`_fts_match_expr`, `search_notes_fts`, `search_chunks_fts`, `rebuild_fts`, backfill, reindex-on-update) | 9 dedicated tests (tests/test_state.py:274-386) | `test_retrieval.py` exercises it through `Retriever` | High — sanitization edge cases (operators, short tokens, stopword-only queries, token cap), diacritics-insensitive search, reindex-on-update, delete-on-chunk-removal, and pre-existing-DB backfill are all covered; every FTS test correctly `pytest.skip`s when `fts_enabled` is False, so the suite degrades gracefully on FTS5-less SQLite builds | Good; the skip-on-unsupported pattern is a notably careful piece of test design |
| Schema migration (`_migrate_schema`) | 1 test (`test_migration_adds_new_columns_to_old_db`, tests/test_state.py:153-181) | None additional | Partial — only one historical schema boundary (pre-"Fase 0") is exercised; the 41-entry migration list as a whole has no test verifying every individual column addition, nor a test simulating a database frozen at each intermediate historical schema version | Adequate for regression-catching the most disruptive migration boundary, but does not scale as a contract test if more migrations accumulate |
| `note_connections` / graph batch fetch | 2 tests (tests/test_state.py:391-421) | `test_graph.py`, `test_gardener_hub.py`, `test_gardener_assign.py` exercise `get_connections_for_notes`/`get_weighted_note_degrees` indirectly through BFS/hub-ranking logic | Good for the CRUD/roundtrip surface; MOC-adjacent graph methods (`get_weighted_note_degrees`, `list_permanent_note_ids`, `list_hub_anchor_note_ids`, `find_moc_by_hub_note_id`) are not directly unit-tested in `test_state.py` — coverage for those exists only via `gardener_hub`'s own test suite | Adequate, but the graph-metric methods lack an isolated unit test independent of the gardener pipeline's higher-level behavior |
| MOC methods (`upsert_moc`, `find_moc_by_topic`, `delete_pipeline_mocs`, `delete_hub_pipeline_mocs`) | 1 direct test (`test_moc_body_and_get_moc`, tests/test_state.py:232-237) | `test_gardener.py`, `test_gardener_hub.py` cover the bulk-delete and topic-matching rules indirectly | Partial — the substring bidirectional-match rule in `find_moc_by_topic` and the origin-scoped bulk deletes have no direct `test_state.py` unit test asserting their semantics in isolation from the gardener pipeline | Gap: a direct unit test for `find_moc_by_topic`'s bidirectional substring semantics (and its lack of a length guard) would be valuable given it is a genuine business rule, not incidental plumbing |
| `delete_source_cascade` / `delete_note` / `clear_source_id_on_notes` | None directly in `test_state.py` | `test_purge_source.py` exercises the cascade end-to-end through `purge_source.delete_source` | Adequate via integration, but no isolated unit test in `test_state.py` verifies the cascade's per-table row counts or that `notes` rows are untouched when only `delete_source_cascade` (not the higher-level `delete_source`) is called | Gap: the "notes survive by default" invariant is a business rule this component itself is responsible for; testing it only through the higher-level `purge_source` integration test means a future direct caller of `delete_source_cascade` alone has weaker regression protection |
| Web job queue (`create_web_job`, `claim_web_job`, `update_web_job`, `recover_web_jobs`, `get_web_dashboard`, job events) | 4 dedicated tests (tests/test_web_state.py:10-59) | `test_web.py` exercises the queue through the FastAPI routes | Good for the mutual-exclusion and crash-recovery invariants specifically (both are directly asserted); `get_web_dashboard` is only tested against an empty/zero-state database — the confidence-band, relations, origins, documents, `sources_cost`, and `hubs` aggregates are never asserted against non-trivial seeded data | Gap: dashboard aggregate correctness (especially the hardcoded `0.85` confidence-band boundary noted in Technical Debt) has no regression test that would catch it drifting out of sync with the real config value |
| `vacuum()` | None directly in `test_state.py` | Indirectly invoked via `test_purge_source.py`/`test_review.py`'s `--no-compact` flag paths (exercises the *skip* path more than the vacuum-runs path) | Low/partial — the actual `PRAGMA wal_checkpoint(TRUNCATE)` + `VACUUM` execution path itself has no test asserting the database remains functional and data-intact immediately afterward | Gap: a direct test opening a `StateDB`, deleting rows, calling `vacuum()`, and asserting the connection is still usable and prior data intact would close this |

\* `test_extractor.py` was found via directory listing but not independently re-verified in this pass beyond appearing among files importing `StateDB`-adjacent fixtures; treat as integration-level coverage consistent with its sibling pipeline-phase tests.

---

**Note**: This report is descriptive analysis only. No files in the codebase were modified in the course of this review.
