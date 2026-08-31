# Component Deep Analysis Report: purge_source

## 1. Executive Summary

`zettel/purge_source.py` implements the **irreversible, complete removal of one harvested source** from the Zettelkasten pipeline — vault Markdown files, SQLite (`state.py`), and ChromaDB (`index.py`) — exposed exclusively through the CLI command `zettel delete-source` (`zettel/cli.py:547-639`). It is a maintenance/administrative operation, not part of the linear `harvest → extract → review → connect → garden` pipeline, and it is deliberately **not exposed in the web UI** (confirmed: no references to `purge_source`/`delete_source` exist in `zettel/web.py` or `zettel/web_app.py`).

The component's single public entry point, `purge_source()` (`zettel/purge_source.py:207-342`), performs a five-stage cascade for a given `@citekey`:

1. Resolve the source row and all dependent rows (chunks, granular literature ids, linked permanent notes).
2. Delete the corresponding vault files (SRC note, LIT index, granular LIT folder, Review drafts, linked assets).
3. Either strip `source_id` from linked permanent (ZTL) notes (default) or delete those ZTL notes outright (`--delete-permanent`).
4. Sweep the entire vault to strip now-dead `[[wikilinks]]` pointing at anything just deleted.
5. Delete the corresponding embeddings from three ChromaDB collections (`chunks`, `literature_notes`, `sources`), cascade-delete SQLite rows (`delete_source_cascade`), and optionally run `VACUUM` on both `state.db` and `chroma.sqlite3` to reclaim disk space.

The module explicitly documents that it "follows the same Chroma/SQLite cleanup patterns as `purge_rejected`" (`zettel/purge_source.py:222`, referring to `zettel/review.py:513`), making it the source-level sibling of that chunk-level irreversible cleanup operation. Key finding: the operation is well-covered by dedicated tests for its three primary code paths (full delete, keep-permanent, delete-permanent) but the `compact=True`/VACUUM branch is not exercised by any test — a pattern repeated from `purge_rejected`.

## 2. Data Flow Analysis

```
1.  CLI: `zettel delete-source @Citekey [--yes] [--delete-permanent] [--no-compact]`
    (zettel/cli.py:547 delete_source_cmd)
2.  cli._load_deps() / _get_db() / _get_idx() build (AppConfig, StateDB, VectorIndex)
3.  Pre-flight lookup: db.get_source(sid) -> abort with exit(1) if not found
    Preview counts: db.get_chunks_for_source(sid), db.get_note_ids_for_source(sid)
4.  Interactive confirmation gate: typer.confirm(...) unless --yes
5.  purge_source(cfg, db, idx, sid, delete_permanent, compact) called
      a. normalize_source_id() -> ensure "@" prefix
      b. db.get_source(source_id) -> if missing, return {"found": False}
      c. db.get_chunks_for_source(source_id) -> chunk_ids + literature_id per chunk
      d. db.get_note_ids_for_source(source_id) -> permanent_ids (notes.source_id + concepts.note_id)
      e. collect_link_targets(...) -> set of wikilink stems that will become dead
      f. _delete_vault_source_files(...) -> unlink/rmtree SRC, LIT index, LIT folder,
         Review drafts, and per-source assets from disk
      g. IF delete_permanent:
             unlink each ZTL file, db.delete_note(note_id) per id,
             idx.delete_permanent_notes(permanent_ids)  [Chroma, best-effort]
         ELSE:
             db.clear_source_id_on_notes(source_id)      [detach, keep ZTL]
      h. clean_wikilinks_in_vault(...) -> rglob "*.md" under 00_Inbox, 10_Sources,
         20_Literature, 30_Permanent, 40_MOCs; strip matching [[wikilinks]],
         clear stale source_id in frontmatter, re-sync FTS row via db.upsert_note
      i. idx.delete_chunks(chunk_ids)              [Chroma "chunks" collection]
      j. idx.delete_literature_notes(lit_ids)      [Chroma "literature_notes", best-effort]
      k. idx.delete_sources([source_id])           [Chroma "sources", best-effort]
      l. db.delete_source_cascade(source_id)       [SQLite: chunks, chapters, concepts,
         assets, files, sources rows]
      m. IF compact and (chunks removed OR sources removed OR delete_permanent):
             measure state.db / chroma.sqlite3 size before
             db.vacuum(); idx.vacuum()
             measure size after
6.  Result dict returned to CLI, printed as a sequence of Rich console summaries
    (vault counts, SQLite counts, Chroma counts, permanent-deleted count,
    wikilinks-cleaned count, before/after MB if compacted)
```

## 3. Business Rules & Logic

### Overview of the business rules

| Rule Type | Rule Description | Location |
|-----------|------------------|----------|
| Identity Normalization | Citekey is normalized to always carry a leading `@` before any lookup | purge_source.py:39-41 |
| Idempotent Not-Found Guard | Deleting an unknown source_id is a no-op that returns `{"found": False}` rather than raising | purge_source.py:225-227 |
| Default Preservation of Permanent Notes | By default, ZTL (permanent) notes stay in the vault; only their `source_id` link is severed | purge_source.py:272-273 |
| Opt-in Full Cascade to Permanent Notes | `--delete-permanent` additionally deletes ZTL files, SQLite `notes` rows, and their graph edges, and their Chroma embeddings | purge_source.py:255-271 |
| Dead-Wikilink Sanitation | Every surviving Markdown file across five vault roots is scanned and any `[[wikilink]]` pointing at something just deleted is stripped | purge_source.py:182-204 |
| Link-Target Completeness | The set of "dead" wikilink targets includes bare stems, path-qualified stems (`Citekey/LIT - ...`), literature_ids, and (conditionally) permanent-note ids/wikilinks/stems | purge_source.py:44-75 |
| SRC File Resolution Fallback | If the SRC file is not found at its conventionally derived filename, the code globs `10_Sources/SRC - *.md` and matches by `source_id` in frontmatter (handles a title changed after harvest) | purge_source.py:78-89 |
| Frontmatter `source_id` Detachment | When a surviving note's frontmatter `source_id` equals the deleted source and permanent notes are not being deleted, that field is removed and `updated_at` is refreshed | purge_source.py:157-159, 278 |
| Best-Effort Chroma Deletion | Literature-notes, sources, and (when applicable) permanent-notes Chroma deletions are wrapped in `try/except`, logging a warning but never aborting the overall purge | purge_source.py:268-271, 283-291 |
| Chunk-Level Chroma Deletion is Not Best-Effort | `idx.delete_chunks(chunk_ids)` is called unguarded (no try/except) — an exception here propagates and aborts the purge before SQLite cascade runs | purge_source.py:281-282 |
| Empty-List No-Op Deletes | All three Chroma delete methods (`delete_chunks`, `delete_literature_notes`, `delete_sources`) and `delete_permanent_notes` are no-ops when given an empty id list, so an "empty" source (no chunks/no lit ids) still safely reaches the source-level delete | index.py:529-533, 594-607 |
| Conditional Compaction | `VACUUM` on both `state.db` and `chroma.sqlite3` runs only when `compact=True` AND at least one of (chunks removed, sources removed, `delete_permanent`) is truthy | purge_source.py:313-317 |
| Synthetic Literature-Index Id | The per-source literature index is always scheduled for Chroma deletion under a synthetic id `f"{source_id}::index"`, in addition to each chunk's real `literature_id` | purge_source.py:233-239 |
| Note Re-Sync on Edit | Any surviving note file that is actually modified (wikilinks stripped or `source_id` cleared) is re-persisted to SQLite via `db.upsert_note(...)` only if it already has both a `note_id` in frontmatter and an existing DB row — this keeps FTS/graph state consistent with the on-disk edit | purge_source.py:168-178 |
| Excluding Self-Deleted Files from the Wikilink Sweep | Files already unlinked as part of `--delete-permanent` are excluded from the `clean_wikilinks_in_vault` rescan via `exclude_paths` (avoids a pointless read of an already-removed file / re-writing a file about to be deleted) | purge_source.py:253-261, 275-279 |
| CLI Confirmation Gate | Unless `--yes`/`-y` is passed, the CLI requires an explicit interactive "yes" (`typer.confirm`, default `False`) before calling `purge_source` | cli.py:599-602 |
| Not-Found is a Hard CLI Failure | At the CLI layer (distinct from the library layer), a missing source prints an error and exits with code 1 rather than proceeding | cli.py:575-579 |

### Detailed breakdown of the business rules

---

### Business Rule: Default Preservation vs. Opt-in Deletion of Permanent Notes

**Overview**:
The single most consequential decision `purge_source` embeds is that deleting a *source* (the raw PDF/Markdown ingest and its literature interpretation) does not, by default, delete the *permanent notes* (ZTL, the Zettelkasten's actual knowledge artifacts) that were derived from it. This reflects the project's core Zettelkasten philosophy: permanent notes are meant to be atomic, evergreen, and to outlive their originating source material once their ideas have been distilled and connected to other notes.

**Detailed description**:
When `delete_permanent=False` (the CLI default — the flag must be explicitly passed to change this), `purge_source` computes `permanent_ids = db.get_note_ids_for_source(source_id)` purely for reporting/warning purposes and passes `permanent_note_ids=[]` into `collect_link_targets(...)` (`purge_source.py:245`). This means the wikilink-stripping pass does **not** treat the ZTL notes' own titles/ids as dead targets — links pointing *at* the surviving ZTL notes are left completely intact throughout the vault. Instead, the code calls `db.clear_source_id_on_notes(source_id)` (`purge_source.py:273`, implemented at `state.py:722-729`), a single SQL `UPDATE notes SET source_id=NULL ... WHERE source_id=?` that detaches every permanent note whose `notes.source_id` column pointed at the now-deleted source, without touching the note's body, title, or any of its graph connections (`note_connections`). The `wikilinks_cleaned` counter reflects a different, narrower cleanup: dead references *within* those (and other) notes pointing at the deleted SRC/LIT/asset artifacts (e.g. a "Ref. literatura: [[Book2024/LIT - ...]]" line) — those links no longer resolve to anything, so they are stripped, but the ZTL note file itself survives.

When `delete_permanent=True`, the behavior inverts for the ZTL layer specifically: `permanent_note_ids=permanent_ids` is passed into `collect_link_targets`, which additionally computes, per note, its wikilink stem (via `permanent_wikilink()`, preferring the file's actual on-disk stem when a `path` is known — `purge_source.py:70-72`) and adds it to the dead-target set alongside the raw `note_id` and the derived filename stem (`Path(row["path"]).stem`). The purge then physically unlinks each ZTL file (`note_path.unlink()`, `purge_source.py:263`), calls `db.delete_note(note_id)` for each (which cascades to `note_connections` rows involving that note — `state.py:708-720`), and attempts `idx.delete_permanent_notes(permanent_ids)` against Chroma's `permanent_notes` collection, catching and logging (not propagating) any Chroma-side failure.

This rule has direct implications for graph integrity: because `delete_note` removes the ZTL's own `note_connections` rows (both directions), any *other* permanent note that had an edge to the just-deleted ZTL now has one fewer edge — but that other note's body-level `[[wikilink]]` reference to the deleted ZTL is separately cleaned by the vault-wide sweep (since the deleted note's id/stem is in `link_targets`). This is one of the only two operations in the codebase (the other being `purge_rejected`) that permanently removes graph edges and note files rather than soft-transitioning a status field.

**Rule workflow**:
```
delete_permanent == False (default):
  permanent_note_ids -> [] (excluded from dead-link targets)
  db.clear_source_id_on_notes(source_id)   # UPDATE notes SET source_id=NULL
  ZTL files: untouched on disk and in Chroma
  Only stale SRC/LIT/asset references inside ZTL bodies are stripped

delete_permanent == True (--delete-permanent):
  permanent_note_ids -> included in dead-link targets (id, wikilink stem, file stem)
  for each note_id in permanent_ids:
      unlink ZTL file from 30_Permanent/
      db.delete_note(note_id)              # cascades note_connections + FTS row
  idx.delete_permanent_notes(permanent_ids)  # Chroma "permanent_notes", best-effort
  Vault-wide sweep also strips wikilinks TO the now-deleted ZTL notes from every
  remaining note and every MOC that referenced them
```

---

### Business Rule: Three-Collection Chroma Cleanup with Mixed Failure Tolerance

**Overview**:
A source's embeddings live in up to three (or four, with `--delete-permanent`) separate ChromaDB collections. The purge deliberately treats these deletions with different failure tolerances: the raw-chunk deletion is allowed to fail loudly (and abort the whole operation before SQLite is touched), while the higher-level `literature_notes`, `sources`, and `permanent_notes` deletions are deliberately best-effort.

**Detailed description**:
`idx.delete_chunks(chunk_ids)` (`purge_source.py:281-282`) is invoked with no exception handling. Since it runs *before* `db.delete_source_cascade(source_id)`, an unguarded ChromaDB failure here (e.g. a locked `chroma.sqlite3`, a corrupted HNSW segment) will raise out of `purge_source()` entirely, leaving the vault files already deleted (step 2) and the ZTL detachment/deletion already applied (step 3), but the SQLite rows for the source/chunks/chapters/concepts still present. This creates a real, acknowledged inconsistency window: vault state has moved past SQLite state. There is no rollback or transactional wrapping across these heterogeneous stores (vault filesystem, SQLite, ChromaDB) — the design assumes chunk deletion essentially never fails in practice (it is a straightforward `collection.delete(ids=...)` call) and prioritizes not silently succeeding when the primary dense-vector store did not actually shed the chunk data that a caller might otherwise re-embed as if it were new content.

By contrast, `idx.delete_literature_notes(lit_ids)` (`purge_source.py:283-287`), `idx.delete_sources([source_id])` (`purge_source.py:288-291`), and (conditionally) `idx.delete_permanent_notes(permanent_ids)` (`purge_source.py:268-271`) are each wrapped in their own `try/except Exception as e: logger.warning(...)`. A failure in any of these is logged but does not stop the purge from proceeding to the SQLite cascade and eventual VACUUM. The rationale, inferred from the pattern mirrored in `purge_rejected` (`zettel/review.py`), is that these three collections are lower-frequency, lower-volume, and less critical to keep byte-for-byte synchronized than the `chunks` collection, which feeds ongoing dedupe/FTS logic across every future harvest run.

The practical consequence is an asymmetric guarantee: after a `delete-source` call returns successfully, SQLite is guaranteed fully cascaded and the vault is guaranteed clean, but Chroma's `literature_notes`/`sources`/`permanent_notes` collections may still contain orphaned embeddings if any of those three delete calls threw. Nothing in the codebase currently re-reconciles that drift automatically (no equivalent of a "garbage collect Chroma against SQLite" pass was found for `purge_source`'s three best-effort collections).

**Rule workflow**:
```
idx.delete_chunks(chunk_ids)              -> unguarded; exception propagates, aborts purge
try: idx.delete_literature_notes(lit_ids) -> except: logger.warning(...); continue
try: idx.delete_sources([source_id])      -> except: logger.warning(...); continue
try: idx.delete_permanent_notes(ids)      -> except: logger.warning(...); continue  [only if delete_permanent]
db.delete_source_cascade(source_id)       -> always attempted after the above
```

---

### Business Rule: Vault-Wide Dead-Wikilink Sanitation

**Overview**:
Rather than trying to track every note that references a deleted source at write time, the purge takes a brute-force but correctness-preserving approach: after building a complete set of "targets that will no longer exist," it re-scans every Markdown file in five vault roots and strips any wikilink matching those targets.

**Detailed description**:
`collect_link_targets()` (`purge_source.py:44-75`) assembles a `set[str]` covering every string form a wikilink to the doomed content could take: the SRC note's stem (`source_note_stem`), the LIT index's stem (`literature_index_stem`), the per-source literature folder name (`literature_source_dirname`, used for path-qualified links like `Citekey/LIT - ...`), and for every chunk, both its bare filename stem and its `dirname/stem` path-qualified form, plus its raw `literature_id` if present. When permanent notes are included (delete_permanent case), it additionally resolves each note's DB row, adds the raw `note_id`, the `permanent_wikilink(...)` result stripped of its `[[...]]` brackets, and (if a `path` is recorded) the file's actual on-disk stem — covering the case where the wikilink alias/format doesn't exactly match the note_id.

`clean_wikilinks_in_vault()` (`purge_source.py:182-204`) then walks `00_Inbox`, `10_Sources`, `20_Literature`, `30_Permanent`, and `40_MOCs` (the `_VAULT_SCAN_DIRS` constant, `purge_source.py:30-36` — notably `90_Assets` is excluded, since it holds non-Markdown binary attachments) via `rglob("*.md")`, skipping any path already in `exclude_paths` (files already deleted earlier in this same purge). For each remaining file, `_clean_note_file()` (`purge_source.py:144-179`) parses frontmatter/body, calls `strip_matching_wikilinks(body, link_targets)` (in `vault.py:58-85`), and — importantly — also clears a stale `source_id` frontmatter key matching the deleted source when not in delete-permanent mode. A file is only rewritten to disk (`path.write_text(...)`) if either the body actually changed or the frontmatter changed; unaffected files are left with their original mtime.

`strip_matching_wikilinks` itself (`vault.py:58-85`) is more than a blind regex removal: after stripping matched `[[...]]` occurrences, it also cleans up now-empty leftover list bullets (lines that reduce to `-`, `- ()`, `←`, or `← `) and rewrites a now-dangling `- Ref. literatura:` line to explicitly read `- Ref. literatura: _fonte removida_` (Portuguese for "source removed") rather than leaving a bare, confusing label. This means the sweep is content-aware for at least one specific structural pattern used across granular literature-chunk notes, not a purely mechanical string replace.

Because this sweep runs over the *entire* vault (bounded by the five root directories) on every single-source deletion, its cost scales with total vault size, not with the size of the deleted source — a scalability characteristic worth noting for large vaults (see Technical Debt).

**Rule workflow**:
```
link_targets = collect_link_targets(citekey, title, chunks, permanent_ids_or_empty, db)
for subdir in (00_Inbox, 10_Sources, 20_Literature, 30_Permanent, 40_MOCs):
    for md_file in subdir.rglob("*.md"):
        if md_file in exclude_paths: skip
        meta, body = parse_frontmatter(md_file)
        cleaned = strip_matching_wikilinks(body, link_targets)
        if meta.source_id == deleted_source_id and not delete_permanent:
            meta.pop("source_id")
        if cleaned != body or meta changed:
            rewrite file, bump updated_at
            if note_id in meta and db.get_note(note_id): db.upsert_note(...)  # keep FTS/graph in sync
```

---

### Business Rule: Layered SQLite Cascade Delete

**Overview**:
`db.delete_source_cascade(source_id)` (`state.py:731-765`) removes every SQLite row that depends on the source, in an order that respects the schema's implicit foreign-key relationships, even though SQLite foreign keys are not declared with `ON DELETE CASCADE` in this schema (deletes are done procedurally, table by table).

**Detailed description**:
The cascade first fetches and deletes chunks via the existing `self.delete_chunks(chunk_ids)` helper (`state.py:656-669`), which itself deletes each chunk's `concepts` rows, the `chunks` row, and its FTS shadow row (`fts_chunks`) — reusing the same method that services routine re-chunking (`delete_chunks_for_chapter`). It then deletes `chapters` rows one by one (no bulk `DELETE ... WHERE source_id=?` — an explicit loop over fetched rows), followed by three single bulk `DELETE` statements scoped by `source_id`: `concepts` (catching any concepts not already removed via their chunk, e.g. legacy/orphaned rows), `assets`, and `files`. Finally the `sources` row itself is deleted. All deletes happen inside one implicit transaction, committed once at the end (`self.conn.commit()`, `state.py:757`) — so a mid-cascade exception (e.g. a locked database) would leave the whole cascade uncommitted rather than partially applied, unlike the cross-store (vault/Chroma/SQLite) operation as a whole, which has no such atomicity.

Notably, `delete_source_cascade` explicitly does **not** touch the `notes` table — its docstring says "Remove a source and all dependent rows (**not** permanent notes)" (`state.py:733`) — reinforcing that permanent-note lifecycle is deliberately handled by the caller (`purge_source`) via the separate `delete_permanent`/`clear_source_id_on_notes` branch, not baked into the generic cascade helper. This separation of concerns lets `delete_source_cascade` be reused safely by any future caller that wants pure source-data cleanup without an opinion on permanent-note retention.

The returned `dict[str, int]` (`chunks`, `chapters`, `concepts`, `assets`, `files`, `sources`) is threaded straight into the top-level `purge_source()` result under the `"sqlite"` key and echoed by the CLI as three of those six counts (`chunks`, `chapters`, `concepts` — `cli.py:617-622`; `files`/`assets` counts are not printed at the CLI layer even though they're present in the dict).

**Rule workflow**:
```
chunk_ids = [c.chunk_id for c in get_chunks_for_source(source_id)]
removed_chunks = delete_chunks(chunk_ids)          # also deletes per-chunk concepts + FTS
for chapter in get_chapters_for_source(source_id): DELETE FROM chapters WHERE chapter_id=?
DELETE FROM concepts WHERE source_id=?             # catches any concepts without a live chunk
DELETE FROM assets   WHERE source_id=?
DELETE FROM files    WHERE source_id=?
DELETE FROM sources  WHERE source_id=?
commit()
return {chunks, chapters, concepts, assets, files, sources}   # each an int row count
```

---

### Business Rule: Conditional VACUUM / Compaction

**Overview**:
Reclaiming physical disk space via `VACUUM` is expensive (rewrites the entire database file) and is therefore gated both by an explicit `compact` flag and by a heuristic that skips it when nothing meaningful was actually removed.

**Detailed description**:
The CLI exposes compaction as opt-out (`--no-compact` disables it; the default is to compact) via `compact=not no_compact` (`cli.py:608`). Inside `purge_source`, compaction additionally requires `sqlite_removed.get("chunks") or sqlite_removed.get("sources") or delete_permanent` to be truthy (`purge_source.py:313-317`) — i.e., it will still run whenever a source row was actually deleted (which is true any time `purge_source` reaches this point, since a missing source already returned early), so in practice this guard mostly matters for skipping VACUUM only when `compact=False` was explicitly requested, or in the theoretical case where `sqlite_removed["sources"]` came back `0` (e.g., a race where another process removed it first) and no chunks were removed and permanent notes weren't targeted.

When compaction proceeds, the function captures the pre-VACUUM file sizes of `state.db` (`Path(db.db_path)`) and `chroma.sqlite3` (`Path(cfg.chroma_path) / "chroma.sqlite3"`, guarding for non-existence), calls `db.vacuum()` (`state.py:462-471`: `PRAGMA wal_checkpoint(TRUNCATE)` then `VACUUM`, with an explicit comment noting VACUUM recreates the file so a subsequent commit is still needed) and `idx.vacuum()` (`index.py:265-291`: closes the Chroma client entirely, forces `gc.collect()` — a documented Windows-specific workaround so the SQLite file isn't held open — then runs the same checkpoint+VACUUM sequence via a raw `sqlite3.connect`). It records before/after sizes in MB (rounded to two decimals) into the result dict and logs a single info-level summary line.

This mirrors, line-for-line in spirit, the same compaction block in `purge_rejected` (`zettel/review.py`), reinforcing the project's stated convention ("same pattern as purge-rejected," per `CLAUDE.md`).

**Rule workflow**:
```
if compact and (chunks_removed OR sources_removed OR delete_permanent):
    state_mb_before  = size(state.db)
    chroma_mb_before = size(chroma.sqlite3) if exists else 0.0
    db.vacuum()     # WAL checkpoint + VACUUM on state.db
    idx.vacuum()    # close Chroma client, gc.collect(), WAL checkpoint + VACUUM on chroma.sqlite3
    state_mb_after   = size(state.db)
    chroma_mb_after  = size(chroma.sqlite3) if exists else 0.0
    result.compacted = True
else:
    result.compacted = False   # sizes remain their zero-initialized defaults
```

---

### Business Rule: Idempotent Handling of Unknown Sources

**Overview**:
Calling `purge_source` for a `source_id` that does not exist is a safe, side-effect-free no-op at the library level, but a hard failure at the CLI level.

**Detailed description**:
Inside `purge_source()`, the very first action after normalizing the id is `source = db.get_source(source_id)`; if `None`, the function returns `{"found": False}` immediately (`purge_source.py:225-227`) without touching the vault, Chroma, or any other SQLite table. This makes the function safe to call speculatively or to retry. `test_purge_source_not_found` (`tests/test_purge_source.py:246-249`) explicitly locks in this contract.

The CLI wraps this differently: `delete_source_cmd` performs its own `db.get_source(sid)` lookup *before* calling `purge_source`, and if missing, prints a red error message and raises `typer.Exit(1)` (`cli.py:575-579`) — never actually reaching the library function's own not-found branch in that flow. This means the `{"found": False}` path is really an API-level defensive contract for other/future callers (e.g. scripts or tests) rather than something the shipped CLI currently exercises end-to-end via `purge_source` itself.

**Rule workflow**:
```
CLI layer:   db.get_source(sid) is None -> print error, exit(1)   [never calls purge_source]
Library API: purge_source(...) called directly with unknown id -> returns {"found": False}, no side effects
```

---

## 4. Component Structure

```
zettel/
├── purge_source.py                      # The component under analysis (343 lines)
│   ├── normalize_source_id()            # @-prefix normalization
│   ├── collect_link_targets()           # builds the dead-wikilink target set
│   ├── _resolve_src_path()              # locates the SRC file (with citekey-mismatch fallback)
│   ├── _remove_tree()                   # unlink-or-rmtree helper
│   ├── _delete_vault_source_files()     # removes SRC, LIT index, LIT folder, Review drafts, assets
│   ├── _clean_note_file()               # per-file wikilink strip + source_id detach + FTS resync
│   ├── clean_wikilinks_in_vault()       # vault-wide sweep orchestrator
│   └── purge_source()                   # top-level orchestrator / public API
│
├── cli.py                               # only external caller (Typer command)
│   └── delete_source_cmd()              # `zettel delete-source` (lines 547-639)
│
├── state.py                             # SQLite persistence used by purge_source
│   ├── get_source / get_chunks_for_source / get_chapters_for_source
│   ├── get_note_ids_for_source / get_note / get_note_connections
│   ├── get_assets_for_source
│   ├── delete_note / clear_source_id_on_notes / delete_source_cascade / delete_chunks
│   ├── upsert_note (re-sync after in-place edits)
│   └── vacuum()
│
├── index.py                             # ChromaDB (VectorIndex) used by purge_source
│   ├── delete_chunks / delete_literature_notes / delete_sources / delete_permanent_notes
│   └── vacuum()
│
└── vault.py                             # Obsidian I/O helpers reused by purge_source
    ├── parse_frontmatter / compose_note
    ├── strip_matching_wikilinks
    ├── permanent_wikilink
    └── source_note_filename / source_note_stem / literature_index_filename /
        literature_index_stem / literature_source_dirname / literature_chunk_filename_for_row

tests/
└── test_purge_source.py                 # Dedicated unit tests for this component (250 lines)
```

## 5. Dependency Analysis

```
Internal Dependencies:

cli.delete_source_cmd
  -> cli._load_deps / cli._get_db / cli._get_idx      (constructs AppConfig, StateDB, VectorIndex)
  -> purge_source.normalize_source_id
  -> purge_source.purge_source
       -> state.StateDB.get_source
       -> state.StateDB.get_chunks_for_source
       -> state.StateDB.get_note_ids_for_source
       -> purge_source.collect_link_targets
            -> vault.source_note_stem
            -> vault.literature_index_stem
            -> vault.literature_source_dirname
            -> vault.literature_chunk_filename_for_row
            -> state.StateDB.get_note
            -> vault.permanent_wikilink
       -> purge_source._delete_vault_source_files
            -> purge_source._resolve_src_path
                 -> vault.source_note_filename
                 -> vault.parse_frontmatter
            -> vault.literature_index_filename
            -> vault.literature_source_dirname
            -> purge_source._remove_tree
            -> state.StateDB.get_assets_for_source
       -> state.StateDB.get_note (per permanent note, when delete_permanent)
       -> state.StateDB.delete_note
       -> index.VectorIndex.delete_permanent_notes
       -> state.StateDB.clear_source_id_on_notes
       -> purge_source.clean_wikilinks_in_vault
            -> purge_source._clean_note_file
                 -> vault.parse_frontmatter
                 -> vault.strip_matching_wikilinks
                 -> vault.compose_note
                 -> state.StateDB.get_note
                 -> state.StateDB.upsert_note
       -> index.VectorIndex.delete_chunks
       -> index.VectorIndex.delete_literature_notes
       -> index.VectorIndex.delete_sources
       -> state.StateDB.delete_source_cascade
            -> state.StateDB.delete_chunks (SQLite variant)
       -> state.StateDB.vacuum
       -> index.VectorIndex.vacuum

External Dependencies:
- Python standard library: json, logging, shutil, datetime, pathlib, typing (no third-party
  runtime dependency introduced directly by this module)
- ChromaDB (indirectly, via index.VectorIndex) — vector store whose collections
  ("chunks", "literature_notes", "sources", "permanent_notes") are mutated
- SQLite (indirectly, via state.StateDB) — relational store holding sources/chunks/
  chapters/concepts/assets/files/notes/note_connections
- Obsidian-compatible Markdown vault on the local filesystem (read/write/delete via
  pathlib.Path and shutil)
- Typer / Rich (only at the CLI boundary, zettel/cli.py, not imported by purge_source.py itself)
```

Note: `purge_source.py` itself has **zero third-party imports** — its only external-facing dependencies are transitively pulled in through `AppConfig`, `StateDB`, and `VectorIndex`, all of which are passed in by the caller (dependency injection via function parameters, not internal instantiation). This makes the module a pure orchestration layer over already-abstracted persistence interfaces.

## 6. Afferent and Efferent Coupling

Analysis unit: module-level functions within `purge_source.py` (the codebase is function/module-oriented for this component rather than class-oriented — there are no classes defined in `purge_source.py`).

| Component | Afferent Coupling | Efferent Coupling | Critical |
|-----------|-------------------|--------------------|----------|
| `purge_source()` | 1 (cli.delete_source_cmd) | 9 (StateDB x6 methods, VectorIndex x4 methods, collect_link_targets, _delete_vault_source_files, clean_wikilinks_in_vault) | High |
| `collect_link_targets()` | 1 (purge_source) | 6 (vault.source_note_stem, vault.literature_index_stem, vault.literature_source_dirname, vault.literature_chunk_filename_for_row, vault.permanent_wikilink, StateDB.get_note) | Medium |
| `_delete_vault_source_files()` | 1 (purge_source) | 6 (_resolve_src_path, vault.literature_index_filename, vault.literature_source_dirname, _remove_tree x3 call sites, StateDB.get_assets_for_source) | Medium |
| `clean_wikilinks_in_vault()` | 1 (purge_source) | 1 (_clean_note_file, called once per file) | Medium |
| `_clean_note_file()` | 1 (clean_wikilinks_in_vault) | 4 (vault.parse_frontmatter, vault.strip_matching_wikilinks, vault.compose_note, StateDB.get_note/upsert_note) | Medium |
| `_resolve_src_path()` | 1 (_delete_vault_source_files) | 2 (vault.source_note_filename, vault.parse_frontmatter) | Low |
| `_remove_tree()` | 3 (_delete_vault_source_files call sites) | 0 (stdlib pathlib/shutil only) | Low |
| `normalize_source_id()` | 2 (purge_source, cli.delete_source_cmd) | 0 (pure string logic) | Low |

`purge_source()` is the clear architectural chokepoint: it has the highest efferent coupling in the module (it is the only function that talks to all three storage layers — vault filesystem, SQLite, ChromaDB — directly or via its own helpers) while also being the sole afferent target from the CLI. This concentration is appropriate for an orchestrator function but means any regression here has outsized blast radius across all three stores simultaneously; it is also the function exercised, directly or indirectly, by every test in `tests/test_purge_source.py`.

## 7. Endpoints

Not applicable — `purge_source` exposes no network endpoints (REST/GraphQL/gRPC). It is invoked exclusively as an in-process Python function call from `zettel/cli.py`, itself surfaced as a single Typer CLI subcommand:

| Command | Arguments / Options | Description |
|---------|---------------------|--------------|
| `zettel delete-source SOURCE_ID` | `SOURCE_ID` (positional, e.g. `@Citekey`); `--config/-c`; `--yes/-y`; `--delete-permanent`; `--no-compact` | Irreversibly delete a harvested source from vault, SQLite, and Chroma |

## 8. Integration Points

| Integration | Type | Purpose | Protocol | Data Format | Error Handling |
|-------------|------|---------|----------|-------------|-----------------|
| Obsidian vault (filesystem) | Local filesystem | Delete SRC/LIT/Review/asset files; rewrite surviving notes to strip dead wikilinks | Direct file I/O (`pathlib`, `shutil`) | Markdown + YAML frontmatter | No try/except around file I/O — an `OSError` (e.g. file locked by another process/editor) propagates and aborts the purge mid-flight |
| SQLite `state.db` (via `StateDB`) | Internal datastore | Source-of-truth for sources/chunks/chapters/concepts/assets/files/notes/note_connections | In-process SQLite calls (`sqlite3` under `StateDB`) | Rows / dict cursors | Cascade delete runs inside one implicit transaction with a single commit; no explicit try/except in `purge_source.py` around SQLite calls — a raised `sqlite3.Error` propagates |
| ChromaDB `chroma.sqlite3` + HNSW segments (via `VectorIndex`) | Internal datastore (vector store) | Remove embeddings for the source's chunks, literature notes, source-level entry, and (optionally) permanent notes | In-process ChromaDB client API (`collection.delete(ids=...)`) | Vector documents + metadata dicts | Mixed: `delete_chunks` unguarded (propagates); `delete_literature_notes`, `delete_sources`, `delete_permanent_notes` each wrapped in `try/except Exception: logger.warning(...)` (best-effort, never blocks) |
| Typer CLI / Rich console | User-facing | Confirmation gate, progress/result reporting | stdin/stdout (interactive prompt) | Plain text / Rich markup | `typer.confirm(default=False)` requires explicit "yes" unless `--yes`; a not-found source is a hard `typer.Exit(1)` before `purge_source` is ever invoked |

No external network services, third-party APIs, message queues, or webhooks are involved in this component.

## 9. Design Patterns & Architecture

| Pattern | Implementation | Location | Purpose |
|---------|-----------------|----------|---------|
| Facade / Orchestrator | `purge_source()` is a single function that coordinates three distinct subsystems (vault, SQLite, Chroma) behind one call | purge_source.py:207-342 | Gives the CLI (and any future caller) one atomic-looking entry point instead of requiring callers to sequence three subsystems themselves |
| Dependency Injection | `cfg`, `db`, `idx` are passed as parameters rather than constructed inside `purge_source.py` | purge_source.py:207-215 | Testability (the test suite injects a `_FakeIndex` in place of real ChromaDB — `tests/test_purge_source.py:23-43`) and decoupling from concrete infrastructure |
| Best-Effort / Graceful Degradation | `try/except Exception as e: logger.warning(...)` around non-critical Chroma deletes | purge_source.py:268-271, 283-291 | Prevents a secondary-store failure (Chroma) from blocking a primary-store success (SQLite/vault), at the cost of possible drift |
| Idempotent Guard Clause | Early `if not source: return {"found": False}` | purge_source.py:225-227 | Makes repeated/defensive calls safe without special-casing by the caller |
| Template/Convention-Based Filename Resolution with Fallback | `_resolve_src_path()` tries the conventional filename first, then globs and matches by frontmatter `source_id` | purge_source.py:78-89 | Tolerates vault drift where a file was renamed (e.g. title edited) after harvest, without requiring a rename-tracking mechanism |
| Set-Based Target Matching | `collect_link_targets()` returns a `set[str]` consumed by `strip_matching_wikilinks` | purge_source.py:44-75; vault.py:58-85 | O(1) average membership checks per wikilink match across a large vault scan |
| Sibling/Parallel Implementation ("same pattern as X") | Explicit docstring cross-reference to `purge_rejected` in `zettel/review.py` | purge_source.py:222 | Deliberate consistency of irreversible-deletion UX/behavior across the two purge operations in the codebase, at the cost of near-duplicated compaction logic (no shared helper extracted) |
| Command Pattern (CLI layer) | Typer `@app.command(name="delete-source")` wrapping the library call | cli.py:547-639 | Standard CLI-framework separation between user interaction (confirmation, printing) and business logic (`purge_source`) |

## 10. Technical Debt & Risks

| Risk Level | Component Area | Issue | Impact |
|------------|-----------------|-------|--------|
| High | `purge_source()` cross-store ordering | No transactional/rollback boundary across vault filesystem, SQLite, and ChromaDB; vault files and ZTL detachment/deletion happen *before* the SQLite cascade and the unguarded `idx.delete_chunks()` call. An exception at or before that call leaves vault state ahead of SQLite state with no automated recovery path | A failed run can leave a source vanished from disk/Chroma-chunks but still fully present in SQLite (or vice-versa for Chroma-only failures on lit/source/permanent), silently corrupting the pipeline's assumption that SQLite is authoritative |
| Medium | Best-effort Chroma cleanup (`literature_notes`, `sources`, `permanent_notes`) | Failures are only logged (`logger.warning`), never surfaced back in the returned result dict as a `success`/`error` field, and never retried automatically | Long-lived orphaned embeddings in three Chroma collections with no built-in reconciliation/garbage-collection pass specific to `purge_source`'s scope |
| Medium | Vault-wide wikilink sweep (`clean_wikilinks_in_vault`) | Every `delete-source` call performs a full `rglob("*.md")` over five vault roots regardless of the deleted source's size, and rewrites (parses YAML + regex + re-serializes) every touched file | Cost scales with total vault size, not deleted-source size; on very large vaults this could make routine single-source deletions noticeably slow, and it re-parses/re-writes files with no batching or short-circuiting once no more targets remain to be found |
| Medium | `mocs` table / MOC managed content not explicitly reconciled | `--delete-permanent` deletes ZTL notes and their `note_connections`, and the generic wikilink sweep (since `40_MOCs` is in `_VAULT_SCAN_DIRS`) will strip plain `[[wikilink]]` references to the deleted notes from MOC bodies — but there is no explicit call from `purge_source.py` into `moc_backrefs.sync_moc_backrefs`/`clear_moc_backrefs`, nor any update of the `mocs` SQLite table's own bookkeeping (e.g. `cluster_signature`) to reflect a shrunk membership | Risk of MOC content drifting out of sync with the graph/cluster model that `gardener.py` maintains, dependent entirely on the generic text-substitution sweep behaving correctly for whatever structure `gardener.py` currently emits |
| Low | `_resolve_src_path()` fallback scan | On fallback, iterates every `SRC - *.md` file in `10_Sources/` reading and YAML-parsing each until a `source_id` match is found (or exhausts the directory) | O(n) full-directory scan + YAML parse per file on the (expected to be rare) title-mismatch path; acceptable for the current expected vault sizes but not O(1) |
| Low | Unprinted cascade counts at CLI layer | `delete_source_cascade` returns `files` and `assets` counts, but `cli.delete_source_cmd`'s console output only prints `chunks`, `chapters`, `concepts` from the `sqlite` dict (`cli.py:617-622`) | Minor observability gap — an operator running the CLI cannot see from console output alone how many `files`/`assets` SQLite rows were removed, even though the data is present in the returned dict |
| Low | No dry-run mode | There is no `--dry-run`/preview mode that reports what *would* be deleted (files, rows, embeddings) without executing the deletion; the only pre-commit visibility is the CLI's brief chunk/permanent-note count printed before the `typer.confirm()` prompt | Operators must trust the summary counts and the interactive confirmation; there is no way to inspect the full `link_targets` set or file list before committing to an irreversible action |

## 11. Test Coverage Analysis

Dedicated test file located at `tests/test_purge_source.py` (250 lines, 6 test functions). No other test file in the project references `purge_source`, `normalize_source_id` (from this module), or `collect_link_targets`/`clean_wikilinks_in_vault` by name.

| Function / Path | Unit Tests | Integration-style Coverage | Coverage Assessment | Test Quality |
|------------------|------------|------------------------------|----------------------|---------------|
| `normalize_source_id()` | 1 (`test_normalize_source_id`) | N/A (pure function) | Full — both branches (with/without leading `@`) asserted | Simple, direct assertions; adequate |
| `strip_matching_wikilinks()` (vault.py, exercised here) | 1 (`test_strip_matching_wikilinks_path_qualified`) | N/A | Covers the path-qualified target case only; does not test bare-stem matching, literature_id matching, or the "Ref. literatura" rewrite / empty-bullet cleanup branches from within this test file (those may be covered elsewhere, e.g. `tests/test_vault.py`, not confirmed in this analysis pass) | Adequate for the one case it targets; a slightly loose assertion (`... or ...replace("  "," ")`) suggests some uncertainty about exact whitespace output |
| `purge_source()` — full delete, no permanent notes involved | 1 (`test_purge_source_removes_vault_sqlite_chroma`) | Yes — builds a real `StateDB` (SQLite) + real vault directory tree + a `_FakeIndex` test double for Chroma | Good: asserts SQLite rows gone (source, chunks, chapters, concepts), vault files gone (SRC, LIT index, LIT folder, Review drafts), and correct ids passed to the fake Chroma's `delete_chunks`/`delete_literature_notes`/`delete_sources` | Solid end-to-end assertions across all three stores for the default path |
| `purge_source()` — default keep-permanent path with wikilink cleanup | 1 (`test_purge_source_keeps_permanent_cleans_wikilinks`) | Yes | Good: verifies the ZTL file survives, its dead wikilinks (to the deleted LIT chunk and SRC note) are stripped, its `source_id` is cleared in both file frontmatter and DB row, and a second unrelated note's dead wikilink is also cleaned (`wikilinks_cleaned >= 2`) | Strong assertions on the harder-to-verify cross-cutting sweep behavior; uses `>=` rather than an exact count, slightly loosening the check |
| `purge_source()` — `delete_permanent=True` | 1 (`test_purge_source_delete_permanent`) | Yes | Good: verifies the ZTL file is deleted, its SQLite `notes` row is gone, its `note_connections` are gone (checked via `db.get_note_connections("OTHER")`), and its id was passed to the fake Chroma's `delete_permanent_notes` | Directly exercises the graph-edge cascade (`delete_note`) via the connections table, a meaningful assertion beyond simple existence checks |
| `purge_source()` — not-found source | 1 (`test_purge_source_not_found`) | N/A (short-circuit path) | Full — confirms `{"found": False}` and (implicitly, by not raising) no side effects | Minimal but sufficient for a guard-clause branch |
| `purge_source()` — `compact=True` / VACUUM branch | 0 | None | **Gap** — every test in the file passes `compact=False` explicitly; the before/after MB measurement, `db.vacuum()`, and `idx.vacuum()` invocation inside `purge_source` are never exercised through `purge_source()` itself (mirrors the same known gap documented for the sibling `purge_rejected` in `docs_project/component-deep-analyzer/component-analysis-review-...md`) | Not tested — `StateDB.vacuum()` may be tested in isolation elsewhere (`test_state.py`, not confirmed in this pass), but the wrapping logic in `purge_source.py:313-335` (conditional trigger, size measurement, result-dict population, `idx.vacuum()` call) has no dedicated test |
| CLI layer (`delete_source_cmd`) | 0 | None found | **Gap** — no test file was found exercising the Typer command itself (confirmation prompt, `--yes`/`--delete-permanent`/`--no-compact` flag wiring, not-found exit code, or console output formatting) | Not tested directly; only the underlying library function is covered |
| `_resolve_src_path()` fallback (glob + frontmatter match on title mismatch) | 0 explicit | Indirectly plausible only if a fixture used a mismatched title (not the case in the current fixtures — `_seed_source` always writes the SRC file at its conventionally-derived filename) | **Gap** — the fallback branch (`purge_source.py:82-89`) is not exercised by any test in the file | Untested edge case for vaults where the SRC filename drifted from its convention |

Overall: the three primary business-logic paths (full delete, keep-permanent, delete-permanent) and the not-found guard are well covered with real SQLite + real filesystem fixtures and a lightweight Chroma test double, giving good confidence in the core cascade correctness. The two most significant gaps are (1) the compaction/VACUUM branch, and (2) the CLI command wrapper itself (confirmation gating, flag plumbing, and error/exit-code behavior), neither of which has any automated test coverage in this codebase as currently structured.
