# Component Deep Analysis Report — `sync` (zettel/sync.py)

## 1. Executive Summary

`zettel/sync.py` implements **Sync Manual**, the vault-adoption mechanism exposed as `zettel sync-manual` (CLI) and the `sync` operation of the web pipeline (`POST /pipeline/sync`). It is explicitly **not part of the linear pipeline** (`harvest -> extract -> review -> connect -> garden`). Its purpose is to let a user hand-write or hand-edit Markdown notes directly in Obsidian — Sources (`10_Sources/`), Literature (`20_Literature/`, including citekey subfolders), Permanent notes (`30_Permanent/`), and MOCs (`40_MOCs/`) — and have the pipeline adopt them: assign missing identifiers (citekey/`source_id`, ULID `note_id`/`moc_id`), stamp `origin: manual` on newly-adopted files, persist their bodies into SQLite, embed the embeddable ones into ChromaDB, and integrate them into the note graph.

Two responsibilities are bundled in the module:

1. **Adoption/indexing** (`run_sync_manual` and its four per-type helpers `_sync_source`, `_sync_literature`, `_sync_permanent`, `_sync_moc`) — idempotent, checksum-gated upserts into `StateDB` and `VectorIndex`, plus MOC back-reference maintenance (`sync_moc_backrefs`).
2. **Graph-loop closure** (`_extract_body_edges` / `rebuild_manual_edges`) — reads `[[ZTL - ULID ...]]` wikilinks a human placed in a permanent note's body (outside auto-generated managed blocks) and persists them as `related` edges in `note_connections`, treating a manually placed link as an accepted connection (unlike the `auto-connections` block, which is only a suggestion).

Key findings:
- The module is idempotent by design: every sync path is gated by a semantic/content checksum comparison so re-running `sync-manual` on an unchanged vault is a no-op (`skipped`).
- It never overwrites body content outside YAML frontmatter and managed blocks (`safe_update_managed_blocks`), preserving hand-authorship.
- It draws on `harvester._generate_citekey`, `gardener._moc_embeddable`, `rebuild._moc_summary_from_body`, and `moc_backrefs.sync_moc_backrefs`, giving it non-trivial cross-module coupling for a "sync" component.
- The literature sync path (`_sync_literature`) carries the most implicit/underdocumented branching of the four handlers (source_id/citekey inference tiers) and has no direct unit test in `tests/test_sync.py` beyond one integration-style case in `test_manual_literature_links_and_persists_body`.
- `_extract_body_edges`'s "never downgrade" rule and self-link/unknown-target filters are well covered by dedicated tests.

---

## 2. Data Flow Analysis

### 2.1 `run_sync_manual` (adoption path)

```
1. CLI `zettel sync-manual` (optionally --rebuild-graph first) or web POST /pipeline/sync
2. run_sync_manual(cfg, db, idx) scans 4 vault folders in fixed order:
   10_Sources -> 20_Literature (recursive, incl. {Citekey}/ subfolders) -> 30_Permanent -> 40_MOCs
3. For each *.md file found: read content, parse_frontmatter() -> (meta, body)
4. Dispatch by note_type to one of:
     _sync_source      (10_Sources)
     _sync_literature   (20_Literature)
     _sync_permanent    (30_Permanent)
     _sync_moc          (40_MOCs)
5. Each handler:
     a. Assigns a missing identifier (citekey/source_id, note_id, moc_id) if absent
        -> _rewrite_frontmatter() writes the id + type + origin:manual back to the file
     b. Computes a checksum (semantic checksum for permanent/moc; raw body/lit_body
        equality for source/literature) and compares against StateDB
     c. If unchanged -> return "skipped"; caller does not touch the vector index
     d. If new/changed -> StateDB.upsert_*() (retention layer)
     e. If embeddable and changed (embedding_input_hash mismatch) -> VectorIndex.upsert_*()
        (permanent notes and MOCs only; sources are always upserted into the index)
6. Permanent notes additionally:
     _extract_body_edges()   -> persists manual [[ZTL-...]] links as `related` edges
     _suggest_connections()  -> Retriever.search_notes() -> writes `auto-connections`
                                  managed block (suggestions only, not graph edges)
7. MOCs additionally:
     sync_moc_backrefs()     -> diffs old vs new note-id set embedded in the MOC body,
                                  adds/removes `auto-moc-backrefs` blocks on linked ZTLs
8. Aggregate counters returned: {new, updated, skipped, sources, literature, permanent, mocs}
```

### 2.2 `rebuild_manual_edges` (graph backfill path)

```
1. CLI `zettel sync-manual --rebuild-graph` (runs BEFORE the adoption scan, same invocation)
2. db.list_notes() -> every note row already in SQLite (no filesystem access)
3. For each note with a non-empty body: _extract_body_edges(db, note_id, body)
4. Aggregate: {notes_scanned, edges_created}
```

### 2.3 `_extract_body_edges` (shared primitive)

```
1. _strip_auto_blocks(body) removes auto-connections / auto-backlinks / auto-moc-backrefs
   managed-block contents so only human-placed links survive
2. _ZTL_WIKILINK.findall() extracts candidate ULID targets, excluding a self-reference
3. db.get_note_connections(note_id) -> build a set of already-connected pairs
   (frozenset of {note_id, target} regardless of direction/type)
4. For each target:
     - skip if db.get_note(target) is None (target unknown to the pipeline)
     - skip if the pair is already connected in ANY relation type/direction (no downgrade)
     - otherwise db.upsert_note_connection(note_id, target, "related", "wikilink manual")
5. Returns count of edges actually created
```

---

## 3. Business Rules & Logic

### Overview of the business rules

| Rule Type | Rule Description | Location |
|-----------|-------------------|----------|
| Classification | `origin` field distinguishes pipeline-generated (`pipeline`) from human-authored/adopted (`manual`) content; sync only assigns `manual` when absent | sync.py:97-99 |
| Idempotency | A note whose content checksum is unchanged since last sync is skipped; the vector index and DB are not touched | sync.py:123-126, 220-226, 244-246, 297-299 |
| Identifier assignment | Missing `source_id`/citekey, `note_id`, or `moc_id` are generated and written back to the file's frontmatter on first sync | sync.py:108-121, 233-239, 284-290 |
| Immutable existing source | An SRC note whose `source_id` already exists in SQLite is always `skipped`, even if its body/frontmatter changed | sync.py:123-126 |
| Draft exclusion | LIT files under `00_Inbox/` or any `Review` path segment are never synced (they are extractor drafts, not approved literature) | sync.py:177-179 |
| Orphan literature auto-creates source | A LIT note lacking a resolvable `source_id` gets a brand-new manual source record generated for it | sync.py:189-198 |
| Granular vs index LIT branching | A LIT file with `type: literature` and a `chunk_id` updates the matching chunk row's review status/path; anything else is treated as a literature index/legacy snapshot into `sources.lit_body` | sync.py:200-226 |
| Chunk status coercion | When adopting a granular LIT tied to an existing chunk, an invalid/missing `status` value is coerced to `"approved"` | sync.py:204-208 |
| Embedding-cost avoidance | Permanent notes and MOCs are only re-embedded into ChromaDB when their embedding input hash (semantic checksum + model + provider) changed, not on every content change | sync.py:262-270 |
| Suggestion vs acceptance | Wikilinks inside `auto-connections`/`auto-backlinks`/`auto-moc-backrefs` managed blocks are never treated as accepted graph edges; only body wikilinks outside those blocks are | sync.py:349-362, 377 |
| No self-links | A wikilink from a note to itself is discarded and never becomes an edge | sync.py:378 |
| Closed-world graph targets | A wikilink to a ULID the pipeline has no note record for is discarded (not persisted, not retried later automatically) | sync.py:389-390 |
| No relation downgrade | If any connection (any type, any direction) already exists between two notes, a manual wikilink never creates a second, duplicate, or lower-priority edge | sync.py:391-392 |
| Suggestions are non-authoritative | `_suggest_connections` writes retrieval results to `auto-connections` but never calls `upsert_note_connection` — a suggestion never becomes a graph edge automatically | sync.py:317-346 |
| MOC edit detection by identity, not content hash of file | An edited MOC's "updated" status is detected by comparing to `cluster_signature` keyed on `moc_id`, deliberately not by re-deriving from a topic/cluster signature — so hand-edits are always detected as long as `moc_id` persists | sync.py:296-299 |
| Pipeline-authored notes are never re-labeled | Because `origin` is only defaulted with `meta.setdefault("origin", "manual")` when absent, a note already carrying `origin: pipeline` keeps that origin through every future sync pass | sync.py:99, 120, 238, 250, 289, 301 |

### Detailed breakdown of the business rules

---

### Business Rule: Manual vs Pipeline Origin Classification

**Overview**:
Every note synced by this component is tagged with an `origin` field that is either `manual` (hand-created/adopted) or `pipeline` (produced by harvest/extract/review/connect/garden). This tag is the load-bearing distinction the rest of the codebase (gardener's `--recreate` purge, purge_source, web dashboards) uses to decide what it is safe to regenerate or delete versus what it must never touch.

**Detailed description**:
`_manual_origin(meta)` returns `meta.get("origin", "manual")` — i.e., if a note's frontmatter has no `origin` key at all, it is assumed manual. When sync assigns a missing identifier to a freshly-discovered file, it calls `meta.setdefault("origin", "manual")`, which only writes the key if it is absent; a note that already declares `origin: pipeline` (for example a permanent note produced by `connect` that a user later hand-edited in Obsidian) is never relabeled to `manual` merely because it passed through sync-manual. This is deliberately asymmetric: sync can *discover* manual content and stamp it, but it can never demote pipeline content, because pipeline notes are expected to flow through sync-manual too whenever a human tweaks their body (their `note_id`/`moc_id` already exists, so the identifier-assignment branch is skipped entirely and `origin` is read as-is).

The practical effect is visible in `test_pipeline_note_stays_pipeline`: a ZTL file is seeded with `origin: pipeline` and an explicit `note_id`; after `run_sync_manual`, `db.get_note(...)["origin"]` is still `"pipeline"`. Conversely, `test_manual_permanent_gets_id_and_origin` shows a note with no `note_id` and no `origin` key ending up with `note_id` injected and `origin: manual` written into both the file and the DB row.

Because origin is read once per sync from the file's current frontmatter and passed straight through to `db.upsert_note`/`upsert_moc`/`upsert_source` (which write `origin=excluded.origin` unconditionally on every upsert, not `COALESCE`), a user who manually edits a pipeline note's frontmatter to set `origin: manual` (or vice versa) will have that override take effect on the very next sync pass — origin is not sticky once a value is present in the file.

**Rule workflow**:
```
read frontmatter
  origin key present?  -- yes --> use that value verbatim on every upsert
        |
        no
        v
  identifier already present? (source_id / note_id / moc_id)
        |                                   |
        no                                  yes
        v                                   v
  assign new id, set origin="manual",   origin defaults to "manual" via
  rewrite frontmatter to file           _manual_origin() fallback, but
                                         file is not modified
```

---

### Business Rule: Idempotent Re-Sync via Checksums

**Overview**:
Re-running `zettel sync-manual` on a vault where nothing changed must be a cheap no-op: no DB writes beyond the initial adoption, no re-embedding, and no wasted ChromaDB calls. Each note type uses a different equality check appropriate to what "unchanged" means for that type.

**Detailed description**:
Sources are the simplest and, notably, the most conservative: once a `source_id` exists in `StateDB` (`db.get_source(source_id)` returns a row), `_sync_source` returns `"skipped"` unconditionally — it does not compare any content hash. This means a hand-edited SRC file's bibliographic frontmatter (title, authors, publisher, etc.) is **not** re-synced into SQLite after the first adoption; only a brand-new SRC file is ever written to the `sources` table by this path. Literature index notes use full-body string equality (`existing.get("lit_body") == full`) rather than a hash, so a single trailing whitespace or formatting change is enough to trigger a re-sync of the LIT snapshot text — a coarser but exact comparison. Permanent notes and MOCs use a proper content hash: `extract_embeddable_text(body)` strips frontmatter and managed blocks first (so edits inside `auto-connections`/`auto-backlinks`/`auto-moc-backrefs` never trigger a resync), then `sha256_hex(normalize_text_for_hash(...))` produces a semantic checksum that is stored (`note_semantic_checksum` / `cluster_signature`) and compared on the next pass.

This checksum strategy is layered again for the embedding decision specifically: even when a permanent note's semantic checksum changed (so `db.upsert_note` runs and the body is persisted), `VectorIndex.upsert_permanent_note` is only invoked if `compute_embedding_input_hash(semantic_checksum, provider, model)` differs from what is stored — meaning a body edit that does not change the *embeddable* text (e.g. only frontmatter or a managed block changed, which is already excluded by `extract_embeddable_text`) will update the retention row but skip the costly re-embedding call. This double-gating (content changed? -> DB write; embedding input changed? -> vector upsert) is a deliberate cost-avoidance measure given embeddings are billed API calls.

MOC re-sync detection is keyed by `moc_id` identity rather than by title or path — `db.get_moc(moc_id)` — specifically so a user can rename the MOC's topic or move the file without breaking change detection, as long as the `moc_id` frontmatter field survives the edit.

**Rule workflow**:
```
SRC:          get_source(source_id) exists? --> skipped : new (always; no update path)
LIT (index):  lit_body == stored lit_body?  --> skipped : new/updated
LIT (chunk):  always applied to matching chunk row (status coerced); "updated" if chunk found
ZTL:          semantic_checksum == stored?  --> skipped : new/updated
                 (independently) embedding_input_hash changed? --> re-embed : skip embed
MOC:          semantic_checksum == cluster_signature? --> skipped : new/updated
                 (always) re-embed into idx.upsert_moc when not skipped
```

---

### Business Rule: Literature Note Source-ID / Citekey Resolution Tiers

**Overview**:
A LIT file may arrive in several shapes — a full monolithic index with an explicit `source_id`, a legacy note carrying only `citekey`, a granular chunk note tied to `chunk_id`, or a completely orphaned note with neither. `_sync_literature` resolves these into a single canonical `source_id` before anything else happens.

**Detailed description**:
The resolution order is: (1) if `source_id` is present and is not itself a chunk-shaped id (`"::"` not in it), the citekey is derived from it or kept as given; (2) else if only `citekey` is present, `source_id` is synthesized as `f"@{citekey}"`; (3) if after those two steps there is still no usable `source_id` (either missing entirely, or the `source_id` value actually contains `"::"`, which would indicate a malformed/chunk-shaped identifier leaking into the source_id field), a brand new citekey is generated via `harvester._generate_citekey` with no author list (`[]`) and whatever year/title is available, and a new source row may be created. This third branch is the "orphan adoption" rule: a `.md` file dropped into `20_Literature/` with only a title and no linkage to any source becomes its own manually-registered source.

Once `source_id` is settled, `db.get_source(source_id)` is checked and, if absent, a minimal `sources` row is inserted with `authors=[]` and `origin="manual"` — this is a lighter-weight registration than `_sync_source` performs (no bibliography JSON, no ABNT reference), since the literature note itself is assumed to carry the substantive content.

The function then branches a second time on note shape: if `type == "literature"` and a `chunk_id` is present, this is a **granular** chunk note — it does not touch `sources.lit_body` at all; instead it looks up the chunk by id and calls `db.update_chunk_review`, coercing `status` to one of `("approved", "persisted", "awaiting_review")` (defaulting to `"approved"` for anything else, including a typo or an unrecognized value). If the chunk id does not resolve (`db.get_chunk(chunk_id)` is `None`), the whole sync for that file is a no-op returning `"skipped"` — the file is not re-queued or flagged as an error. Otherwise (index/legacy monolithic shape), frontmatter is normalized (`type` forced to `"literature"` or `"literature_index"`) and the full file text is snapshotted into `sources.lit_body`.

**Rule workflow**:
```
source_id present and not chunk-shaped?
  yes -> citekey = citekey or source_id.lstrip('@')
  no:
    citekey present?
      yes -> source_id = f"@{citekey}"
      no / still missing or chunk-shaped -> generate new citekey (no authors) -> source_id = f"@{citekey}"

get_source(source_id) missing? -> insert minimal manual source row

type == "literature" and chunk_id present?
  yes -> update matching chunk's review fields (status coerced); "updated"/"skipped" based on chunk existing
  no  -> normalize frontmatter type; compare full file text to stored lit_body; "new"/"updated"/"skipped"
```

---

### Business Rule: Draft Exclusion for Literature Notes

**Overview**:
Extractor-produced draft LIT notes awaiting human approval must never be adopted by sync-manual as if they were finished, approved literature.

**Detailed description**:
Before any resolution logic runs, `_sync_literature` checks `"00_Inbox" in file_path.parts or "Review" in file_path.parts` and returns `"skipped"` immediately if true. This matters because Phase 2 of the pipeline (`extractor.py`) writes drafts to `00_Inbox/Review/{Citekey}/LIT - ... .md` — files that live inside the same broad vault tree that `run_sync_manual`'s glob (`"**/*.md"` under `20_Literature/`) could otherwise reach if drafts were ever placed under `20_Literature/`. Given the directory layout described in CLAUDE.md, drafts actually live under `00_Inbox/`, not `20_Literature/`, so in the current tree this guard is a defensive belt-and-suspenders check rather than one that fires in normal operation — but it directly prevents a draft that has not passed through `review.py`'s approval gate from being silently treated as a finished, citable literature note if a draft folder were ever nested under `20_Literature/Review/`.

**Rule workflow**:
```
file path contains "00_Inbox" OR "Review" segment?
  yes -> return "skipped" (no DB/vault writes at all)
  no  -> proceed with source_id/citekey resolution
```

---

### Business Rule: Body Wikilinks Become Accepted Graph Edges (Never Downgrading)

**Overview**:
This is the "graph loop closure" rule: a `[[ZTL - ULID - slug]]` wikilink a human types into a permanent note's own prose (not into an auto-generated block) is treated as the user's explicit acceptance of a connection between two notes, and is persisted as a `related` edge — but only if doing so cannot destroy more specific information already recorded.

**Detailed description**:
`_extract_body_edges` first calls `_strip_auto_blocks`, which repeatedly locates and deletes the `<!-- zettel:{name}:start -->...<!-- zettel:{name}:end -->` spans for `auto-connections`, `auto-backlinks`, and `auto-moc-backrefs` (looping until no more start tags are found, so multiple occurrences or malformed/unterminated blocks are both handled — an unterminated block truncates the body from that point on, which is a deliberately fail-safe choice: better to under-extract links than to accidentally read a suggestion as an acceptance). Only after this stripping does it regex-match `_ZTL_WIKILINK` (`\[\[ZTL - ([0-9A-HJKMNP-TV-Z]{26})`) against what remains, meaning links the *pipeline itself* wrote as suggestions can never be misread as user acceptances, no matter how they are phrased.

For each remaining target ULID (excluding a self-reference to `note_id`), two independent gates apply before an edge is created. First, `db.get_note(target)` must resolve — a link to a ULID the pipeline has never indexed (a typo, a note from a different vault, or a note deleted since) is silently dropped; this is a closed-world assumption that trades recall for correctness (no dangling edges pointing at nothing). Second, and this is the more subtle rule, the function fetches **all** existing connections touching `note_id` via `db.get_note_connections` and builds a `frozenset({source, target})` set that is direction- and type-agnostic; a wikilink is only turned into a new `related` edge if that undirected pair is not already present in *any* relation type in *either* direction. This means a `contradicts` or `extends` edge the connector (Phase 3) previously created between the same two notes is never silently overwritten or duplicated by a later manual wikilink — the richer, LLM-derived semantic relation always wins over the generic `related` fallback a plain wikilink implies. The description string persisted is always the literal `"wikilink manual"`, which lets later tooling distinguish these edges from LLM-derived ones with descriptive text.

`rebuild_manual_edges` exists purely to apply this same per-note logic retroactively across every note already in SQLite (via `db.list_notes()`), for vaults that accumulated hand-written wikilinks before this feature existed; it reads bodies already persisted in SQLite and touches no files on disk, so it is safe to run repeatedly and cannot regress content.

**Rule workflow**:
```
strip auto-connections / auto-backlinks / auto-moc-backrefs blocks from body
find all [[ZTL - <ULID> ...]] targets, excluding self
existing_pairs = { {source,target} for every existing connection touching note_id }
for each target:
  target note exists in DB?           no  -> skip
  {note_id, target} in existing_pairs? yes -> skip (never downgrade)
  otherwise -> upsert_note_connection(note_id, target, "related", "wikilink manual")
              add pair to existing_pairs (prevents duplicate inserts within the same body)
```

---

### Business Rule: Suggestions Are Never Auto-Accepted Connections

**Overview**:
`_suggest_connections` populates the `auto-connections` managed block with retrieval-based "you might also want to link these" suggestions, but this pathway never writes to `note_connections` — only a human placing the link in the note body (covered by the previous rule) turns a suggestion into a real edge.

**Detailed description**:
After a permanent note is adopted or updated, `_suggest_connections` runs a hybrid retrieval query (`Retriever.search_notes`) over the note's own embeddable text, excluding the note itself (`exclude_id=note_id`), bounded by `cfg.linking.topk` (default 5). It uses only `.hits` — the subset of results that cleared the retrieval system's absolute relevance floor — never `.candidates`, so a note with no sufficiently relevant neighbors gets no suggestions written at all rather than a list of weak/irrelevant matches. For each hit, it builds a wikilink via `vault.permanent_wikilink`, preferring the actual on-disk filename stem (via a fresh `db.get_note(n.note_id)` lookup) over a slug reconstructed from the title, so links stay valid even if the target file was renamed. The resulting bullet list is written via `safe_update_managed_blocks`, which is idempotent (a no-op write if content is unchanged) and bumps `updated_at` in frontmatter only when content actually changes.

This function is deliberately never called from `_sync_moc`, `_sync_source`, or `_sync_literature` — connection suggestions in this codebase are a permanent-note-only concept, consistent with `note_connections` being a graph over ZTL notes.

**Rule workflow**:
```
retriever.search_notes(embeddable_text, topk=linking.topk, exclude_id=note_id).hits
no hits -> return without touching the file
hits present -> build one wikilink per hit (using on-disk path when known)
              -> safe_update_managed_blocks(file, {"auto-connections": joined_links})
```

---

### Business Rule: MOC Back-Reference Reconciliation on Sync

**Overview**:
When a manually-created or hand-edited MOC is synced, the set of permanent notes it references is diffed against its previous state (if any) so that each affected permanent note's `auto-moc-backrefs` block reflects reality — links are added for newly-referenced notes and removed for notes that were dropped from the MOC.

**Detailed description**:
`_sync_moc` captures `previous_body = existing.get("body")` (the MOC's stored body **before** this sync's upsert overwrites it) so that `sync_moc_backrefs` can be called with both the old and new body for a true diff; for a brand-new MOC, `existing` is `None` so `previous_body` is `None`, and `sync_moc_backrefs` treats `old_ids` as an empty set (only additions, no removals). The diff itself is delegated entirely to `moc_backrefs.sync_moc_backrefs`/`extract_note_ids_from_moc_body`, which is outside this component's file but is a direct, load-bearing dependency: it computes `new_ids - old_ids` (notes to add a backref link to) and `old_ids - new_ids` (notes to strip the backref from), resolving each note's on-disk path via `db.get_note`, and skipping any note whose file cannot be found on disk (`_note_path_from_db` returns `None` if the path doesn't exist or isn't a file — a stale/moved permanent note silently loses its backref maintenance rather than raising).

Unlike `_extract_body_edges`, this rule does not create `note_connections` graph edges — `auto-moc-backrefs` is purely a documentation/navigation convenience block on the permanent note, separate from the graph.

**Rule workflow**:
```
existing_moc = db.get_moc(moc_id)
previous_body = existing_moc.body if existing_moc else None
db.upsert_moc(... new body ...)     # now `existing` is stale w.r.t. current body
sync_moc_backrefs(db, moc_id, topic, path, previous_body=previous_body, new_body=new_body):
  new_ids = extract_note_ids_from_moc_body(new_body)
  old_ids = extract_note_ids_from_moc_body(previous_body) if previous_body else {}
  for id in old_ids - new_ids: remove backref block entry on that note's file (if file exists)
  for id in new_ids - old_ids: add backref block entry on that note's file (if file exists)
```

---

## 4. Component Structure

```
zettel/
├── sync.py                       # THIS COMPONENT — manual note adoption + graph-edge backfill
│   ├── run_sync_manual()           # entry point: scans 4 vault folders, dispatches per type
│   ├── _sync_single_note()         # per-file dispatcher by note_type
│   ├── _manual_origin()            # origin fallback helper
│   ├── _sync_source()              # 10_Sources/*.md adoption
│   ├── _sync_literature()          # 20_Literature/**/*.md adoption (index + granular)
│   ├── _sync_permanent()           # 30_Permanent/*.md adoption + graph/suggestions
│   ├── _sync_moc()                 # 40_MOCs/*.md adoption + backref reconciliation
│   ├── _suggest_connections()      # writes auto-connections managed block (Retriever-based)
│   ├── _strip_auto_blocks()        # removes auto-* managed blocks before edge extraction
│   ├── _extract_body_edges()       # wikilink -> `related` note_connections edge
│   ├── rebuild_manual_edges()      # backfill _extract_body_edges over all stored notes
│   └── _rewrite_frontmatter()      # writes id/type/origin back into a note file
│
├── vault.py                       # parse_frontmatter, safe_update_managed_blocks,
│                                   #   permanent_wikilink, _block_pattern, compose_note (used by sync)
├── hashing.py                     # normalize_text_for_hash, sha256_hex,
│                                   #   compute_embedding_input_hash, extract_embeddable_text
├── retrieval.py                   # Retriever.search_notes() — used by _suggest_connections
├── moc_backrefs.py                # sync_moc_backrefs() — used by _sync_moc
├── gardener.py                    # _moc_embeddable() — used by _sync_moc (import-time, function-local)
├── rebuild.py                     # _moc_summary_from_body() — used by _sync_moc (import-time, function-local)
├── harvester.py                   # _generate_citekey() — used by _sync_source / _sync_literature
├── state.py                       # StateDB — all persistence
├── index.py                       # VectorIndex — all ChromaDB embedding
├── cli.py                         # `sync-manual` Typer command (entry point)
└── web_app.py                     # `sync` web pipeline operation (entry point)

tests/
├── test_sync.py                    # primary unit tests for this component
├── test_moc_backrefs.py            # covers sync + backref interaction (test_sync_manual_updates_moc_backrefs)
└── test_new_note.py                # covers new_note.py -> sync_manual adoption handoff
```

---

## 5. Dependency Analysis

```
Internal Dependencies:

cli.py (sync-manual command) ─┐
web_app.py (_dispatch "sync") ─┴─> sync.run_sync_manual(cfg, db, idx)
                                       │
                                       ├─> sync._sync_source ──> harvester._generate_citekey
                                       │                    └─> state.StateDB.{get_source, upsert_source}
                                       │                    └─> index.VectorIndex.upsert_source
                                       │
                                       ├─> sync._sync_literature ──> harvester._generate_citekey
                                       │                       └─> state.StateDB.{get_source, upsert_source,
                                       │                             get_chunk, update_chunk_review,
                                       │                             update_source_texts}
                                       │
                                       ├─> sync._sync_permanent ──> hashing.{extract_embeddable_text,
                                       │                             normalize_text_for_hash, sha256_hex,
                                       │                             compute_embedding_input_hash}
                                       │                       └─> state.StateDB.{get_note, upsert_note,
                                       │                             update_note_embedding}
                                       │                       └─> index.VectorIndex.upsert_permanent_note
                                       │                       └─> sync._extract_body_edges ──> vault._block_pattern
                                       │                                                    └─> state.StateDB.{
                                       │                                                          get_note_connections,
                                       │                                                          get_note,
                                       │                                                          upsert_note_connection}
                                       │                       └─> sync._suggest_connections ──> retrieval.Retriever
                                       │                                                     └─> vault.{permanent_wikilink,
                                       │                                                           safe_update_managed_blocks}
                                       │
                                       └─> sync._sync_moc ──> gardener._moc_embeddable
                                                          └─> rebuild._moc_summary_from_body
                                                          └─> state.StateDB.{get_moc, upsert_moc}
                                                          └─> index.VectorIndex.upsert_moc
                                                          └─> moc_backrefs.sync_moc_backrefs
                                                               └─> gardener_assign.extract_note_ids_from_moc_body
                                                               └─> vault.{note_filename, parse_frontmatter,
                                                                     read_managed_block, safe_update_managed_blocks}

sync.rebuild_manual_edges ──> state.StateDB.list_notes ──> sync._extract_body_edges (as above)

External Dependencies:
- ulid (python-ulid) — ULID generation for note_id/moc_id
- PyYAML (via zettel.vault's yaml usage in parse_frontmatter/compose_note) — frontmatter I/O
- SQLite (via StateDB) — retention layer (notes, sources, chunks, mocs, note_connections tables)
- ChromaDB (via VectorIndex) — permanent_notes / mocs / sources embedding collections
- Filesystem (pathlib.Path) — vault directory scanning and file read/write
```

Notably, three of `sync.py`'s dependencies (`harvester._generate_citekey`, `gardener._moc_embeddable`, `rebuild._moc_summary_from_body`) are **function-local imports** of module-private (underscore-prefixed) helpers from other pipeline phases, rather than a shared public API. This is a deliberate reuse of exact pipeline logic (so a manually-created source gets the same citekey scheme, and a manually-created MOC embeds with the exact same text shape as a pipeline-generated one) but it is a tight, non-obvious coupling across phase boundaries that an API consumer would not discover without reading `sync.py`'s function bodies.

---

## 6. Afferent and Efferent Coupling

Coupling is measured at function granularity (this module has no classes); afferent = number of distinct call sites elsewhere in the codebase/tests invoking the function, efferent = number of distinct external functions/methods each function calls.

| Component (function) | Afferent Coupling | Efferent Coupling | Critical |
|---|---|---|---|
| `run_sync_manual` | 4 (cli.py, web_app.py, test_sync.py, test_new_note.py, test_moc_backrefs.py) | 5 (`_sync_source/_sync_literature/_sync_permanent/_sync_moc` + Path.glob) | High |
| `_extract_body_edges` | 2 (`_sync_permanent`, `rebuild_manual_edges`) + direct test coverage (test_sync.py, 7 tests) | 4 (`_strip_auto_blocks`, `db.get_note_connections`, `db.get_note`, `db.upsert_note_connection`) | High |
| `_sync_permanent` | 1 (`_sync_single_note`) | 9 (hashing x4, `db.get_note`/`upsert_note`/`update_note_embedding`, `idx.upsert_permanent_note`, `_extract_body_edges`, `_suggest_connections`) | High |
| `_sync_moc` | 1 (`_sync_single_note`) | 8 (`ulid.ULID`, hashing x2, `db.get_moc`/`upsert_moc`, `idx.upsert_moc`, `gardener._moc_embeddable`, `rebuild._moc_summary_from_body`, `moc_backrefs.sync_moc_backrefs`) | Medium |
| `_sync_literature` | 1 (`_sync_single_note`) | 6 (`harvester._generate_citekey`, `db.get_source`/`upsert_source`/`get_chunk`/`update_chunk_review`/`update_source_texts`) | Medium |
| `_sync_source` | 1 (`_sync_single_note`) | 4 (`harvester._generate_citekey`, `db.get_source`/`upsert_source`, `idx.upsert_source`) | Medium |
| `_suggest_connections` | 1 (`_sync_permanent`) | 3 (`Retriever.search_notes`, `vault.permanent_wikilink`, `vault.safe_update_managed_blocks`) | Medium |
| `_strip_auto_blocks` | 1 (`_extract_body_edges`) | 1 (`vault._block_pattern`) | Low |
| `_sync_single_note` | 1 (`run_sync_manual`, loop body) | 4 (the four `_sync_*` handlers) | Low |
| `_manual_origin` | 4 (`_sync_source`, `_sync_permanent`, `_sync_moc`; `_sync_literature` does not use it) | 0 | Low |
| `_rewrite_frontmatter` | 3 (`_sync_source`, `_sync_permanent`, `_sync_moc`) | 1 (`vault.compose_note`) | Low |
| `rebuild_manual_edges` | 1 (cli.py `--rebuild-graph`) + test coverage | 2 (`db.list_notes`, `_extract_body_edges`) | Medium |

Observations: `_extract_body_edges` and `run_sync_manual` are the highest-risk functions to change — both have multiple independent callers/tests and non-trivial invariants (idempotency, edge-downgrade protection). `_sync_literature` has the lowest direct test coverage relative to its branching complexity (see Section 11), making it the function where a regression would be least likely to be caught automatically.

---

## 7. Integration Points

`sync.py` itself exposes no HTTP/RPC surface; it is invoked by two thin call sites and integrates with several storage/retrieval backends.

| Integration | Type | Purpose | Protocol | Data Format | Error Handling |
|---|---|---|---|---|---|
| `zettel sync-manual` CLI command | Internal entry point (cli.py) | User-triggered manual adoption / graph rebuild | In-process function call | Python objects | None specific in sync.py; exceptions propagate to Typer's default handling |
| `POST /pipeline/{operation}` with `operation=sync` | Internal entry point (web.py -> web_app.py) | Web-triggered background job | HTTP form POST -> in-process dispatch | Form-encoded (CSRF token) request; JSON-serializable dict response | Job queue guarantees single mutating job at a time (409 on conflict); worker thread catches exceptions per job (outside this file) |
| SQLite `StateDB` | Internal datastore | Retention of sources/chunks/notes/mocs/note_connections | Direct SQLite calls (no network) | Row dicts, JSON-encoded frontmatter/bibliography columns | Upserts are `INSERT ... ON CONFLICT DO UPDATE`; no explicit try/except in sync.py — a DB error propagates uncaught |
| ChromaDB `VectorIndex` | Internal datastore | Vector embeddings for sources/permanent_notes/mocs collections | In-process client (embedded ChromaDB) | Text + metadata dict (str/int/float/bool only, per project convention) | No explicit error handling in sync.py; embedding/provider errors propagate |
| `Retriever` (retrieval.py) | Internal service | Hybrid (dense+BM25+graph) similarity search for connection suggestions | In-process function call | `NoteSearchResult` dataclass | No error handling; retrieval failures propagate |
| Filesystem (vault directories) | Internal I/O | Source of truth for manually-authored Markdown notes | Direct file I/O (`pathlib`) | Markdown + YAML frontmatter | `scan_dir.exists()` guard skips missing folders silently; no handling for unreadable/corrupt files (`parse_frontmatter` swallows YAML errors and returns `{}`) |

---

## 8. Design Patterns & Architecture

| Pattern | Implementation | Location | Purpose |
|---|---|---|---|
| Strategy / Dispatch Table (via if/elif) | `_sync_single_note` routes to one of four handlers by `note_type` | sync.py:78-94 | Decouples the folder-scan loop from per-type adoption logic |
| Idempotent Upsert | Checksum comparison before every DB/index write across all four `_sync_*` handlers | sync.py:123-126, 220-226, 244-246, 297-299 | Makes repeated `sync-manual` runs cheap and safe |
| Managed Block / Safe Partial Update | `safe_update_managed_blocks`, `_strip_auto_blocks`, `_block_pattern` | sync.py:317-346, 349-362 (calling into vault.py) | Lets automated content coexist with hand-authored prose in the same file without clobbering either |
| Closed-World Graph Validation | `_extract_body_edges` only links to notes already known to `StateDB` | sync.py:389-390 | Prevents dangling/unverifiable graph edges |
| Non-Destructive Merge (never downgrade) | Undirected-pair existence check before inserting a `related` edge | sync.py:382-395 | Preserves higher-fidelity LLM-derived relations over generic manual links |
| Lazy/Local Imports for Cross-Phase Reuse | `from zettel.harvester import _generate_citekey`, `from zettel.gardener import _moc_embeddable`, `from zettel.rebuild import _moc_summary_from_body`, `from zettel.moc_backrefs import sync_moc_backrefs` | sync.py:106, 174, 281-282, 309 | Avoids import-time circular dependencies between sibling pipeline-phase modules while still reusing their exact logic |
| Snapshot-then-Diff | `_sync_moc` captures `previous_body` before overwriting, passes both to `sync_moc_backrefs` | sync.py:302-313 | Enables idempotent reconciliation of a derived side-effect (backref blocks) against a changing source of truth |
| Read-Modify-Write with Frontmatter Round-Trip | `_rewrite_frontmatter` -> `vault.compose_note` | sync.py:417-422 | Injects generated identifiers into hand-written files without disturbing body content |

---

## 9. Technical Debt & Risks

| Risk Level | Component Area | Issue | Impact |
|---|---|---|---|
| High | `_sync_source` | An SRC note whose `source_id` already exists is unconditionally `"skipped"` — bibliographic frontmatter edits made after first adoption (title, authors, publisher, ISBN, etc.) are never re-synced into SQLite | Vault and DB bibliographic metadata can silently drift apart for any manually-created source edited after its first sync |
| Medium | `_sync_literature` | Orphan LIT notes (no `source_id`/`citekey`) generate a citekey via `_generate_citekey(db, [], ...)` with an empty author list, always falling into the "no author" tiering branch — every orphan LIT with the same title prefix and no year could collide more readily than a properly-authored source, relying entirely on the citekey suffix-increment loop to disambiguate | Reduced citekey quality/readability for orphan literature notes; no correctness bug, but a data-quality risk |
| Medium | `_sync_literature` chunk-status coercion | An unrecognized `status` value in a granular LIT's frontmatter (e.g. a typo, or a future status value not yet in the allow-list) is silently coerced to `"approved"` rather than rejected or flagged | A note the user intended to mark, e.g., `"rejected"` or a future workflow status could be silently promoted to `approved`, changing pipeline behavior (Phase 3 `connect` reads `approved` concepts) without any warning |
| Medium | Error handling (module-wide) | No `try/except` around any file read, YAML parse follow-through, DB call, or index call inside `sync.py`; `parse_frontmatter` swallows `yaml.YAMLError` into `meta={}` silently, meaning a corrupt frontmatter block is treated as "no frontmatter" (whole file becomes body) rather than raising a visible warning specific to that file | A single malformed note can either (a) crash the whole `sync-manual` run if a downstream call fails on missing expected keys, or (b) be silently misclassified/mis-adopted with default values, with no per-file diagnostic surfaced to the user |
| Medium | `_extract_body_edges` via `_strip_auto_blocks` | An unterminated managed block (`start_tag` present, `end_tag` missing due to manual corruption) truncates the body from that point onward for wikilink extraction, silently dropping any real wikilinks placed after the corrupted block | A user's genuine body wikilinks located after a malformed managed block will never become graph edges, with no error or log message |
| Low | Cross-module coupling | `sync.py` imports private (`_`-prefixed) functions from `harvester`, `gardener`, and `rebuild` | Any refactor of those modules' private helpers (rename, signature change) silently breaks `sync.py` without a public-API contract to enforce compatibility |
| Low | `run_sync_manual` folder scan | The literature glob is `"**/*.md"` (recursive) while the other three folders use `"*.md"` (non-recursive); this asymmetry is intentional (citekey subfolders) but is not defended by any check that a subfolder actually corresponds to a citekey — any nested `.md` file under `20_Literature/` (e.g. a user's personal notes folder) will be scanned and potentially adopted | Unintended adoption of unrelated Markdown files placed inside `20_Literature/` subfolders |
| Low | `_suggest_connections` | Uses only `cfg.linking.topk` with no explicit relevance floor override, relying on `Retriever.search_notes` defaults; if those defaults change, suggestion behavior in sync silently changes with them | Behavior coupling to global retrieval config change, invisible from reading `sync.py` alone |

---

## 10. Test Coverage Analysis

| Component (function/behavior) | Unit Tests | Integration Tests | Coverage | Test Quality |
|---|---|---|---|---|
| `run_sync_manual` — source adoption | 1 (`test_manual_source_is_adopted`) | 1 (`test_new_note.py::test_scaffold_source_sync_manual_adopts`) | Good for the "new" path; no test for the always-`skipped`-on-existing-source rule's edge cases (e.g. re-sync with changed metadata) | Clear assertions on frontmatter injection, DB row, and index call; does not test document_type/bibliography_json payload assembly in `test_sync.py` itself (covered indirectly in `test_new_note.py`) |
| `run_sync_manual` — literature adoption | 1 (`test_manual_literature_links_and_persists_body`) | 0 dedicated | Weak — only the "index note with explicit source_id/citekey, orphan-creates source" path is exercised; the granular chunk_id branch, the "malformed source_id containing ::" branch, the draft-exclusion guard, and the resync/update path are all untested | Single happy-path assertion; several branches identified in Section 3 have zero direct coverage |
| `run_sync_manual` — permanent note adoption | 3 (`test_manual_permanent_gets_id_and_origin`, `test_pipeline_note_stays_pipeline`, `test_resync_unchanged_is_skipped`) | 0 additional | Good for id-assignment, origin-preservation, and idempotency; no test asserts on `_suggest_connections`' managed-block output (FakeIndex's `query_similar_notes` always returns `[]`, and `Retriever` is not mocked/exercised at all in test_sync.py, so `_suggest_connections`'s actual retrieval-driven branch is effectively untested here) | Solid coverage of the three core idempotency/origin rules; retrieval-suggestion behavior is an untested gap |
| `run_sync_manual` — MOC adoption | 1 (`test_edited_moc_returns_updated`) | 1 (`test_moc_backrefs.py::test_sync_manual_updates_moc_backrefs`) | Good — covers both new-MOC id assignment and edit-detection-by-checksum, plus backref reconciliation via the dedicated moc_backrefs test file | Assertions check both the `updated` status and (in test_moc_backrefs.py) the backref side effect; no test directly exercises `_sync_moc`'s embedding-skip path or malformed-topic edge cases |
| `_extract_body_edges` | 6 (`test_body_wikilink_creates_related_edge`, `test_wikilink_in_managed_block_is_ignored`, `test_self_link_ignored`, `test_link_to_unknown_note_ignored`, `test_existing_typed_edge_not_downgraded`, `test_existing_reverse_edge_not_duplicated`) | 0 additional | Excellent — every branch identified in Section 3's "no downgrade"/closed-world rule is independently tested, including both edge-direction cases | High quality: each test isolates exactly one invariant with a minimal fixture; strong negative-case coverage (self-link, unknown target, existing edge of another type, reverse-direction existing edge) |
| `rebuild_manual_edges` | 1 (`test_rebuild_manual_edges_backfills`) | 0 additional | Adequate for the basic backfill happy path; no test for a vault with zero eligible notes, notes with empty bodies (the `if not body: continue` skip), or a mix of already-connected and new-connection notes in the same run | Single scenario test; sufficient to prove the delegation to `_extract_body_edges` works, but does not stress multi-note aggregation |
| `_sync_source` bibliography payload assembly | 0 in test_sync.py | 1 (`test_new_note.py`, checks `document_type` only) | Weak — the large allow-list of ~25 bibliographic fields assembled into `biblio_payload` (sync.py:133-142) has no test asserting the JSON actually round-trips correctly for a document with multiple fields (e.g. journal + volume + issue + doi together) | Only one field (`document_type`) is asserted against; the JSON serialization itself (`bibliography_json`) is never inspected in any test found |
| Draft exclusion (`00_Inbox`/`Review` path guard) in `_sync_literature` | 0 | 0 | None found | Untested — a regression here (e.g. a path-matching typo) would not be caught by the existing suite |
| Chunk-status coercion in `_sync_literature` | 0 | 0 | None found | Untested — the fallback-to-`"approved"` behavior for invalid `status` values (a directly stated business rule) has no test |

Overall: the module's most safety-critical invariant — graph-edge non-downgrade and closed-world validation in `_extract_body_edges` — is the best-tested part of the component. The weakest-tested area is `_sync_literature`, whose source_id/citekey resolution has the most branches of any handler in this file and the thinnest direct test coverage relative to that complexity.
