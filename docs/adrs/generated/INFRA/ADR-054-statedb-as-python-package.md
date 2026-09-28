# ADR-054: StateDB as Python Package

**Status**: Accepted (2026-09-28)  
**Depends on**: [ADR-001](./ADR-001-sqlite-wal-fts5-primary-persistence.md), [ADR-008](./ADR-008-repository-pattern-data-access.md)  
**Relates to**: [ADR-003](./ADR-003-hybrid-dense-bm25-retrieval.md), [ADR-032](../CLI/ADR-032-cli-as-python-package.md), [ADR-036](../RETRIEVAL/ADR-036-topic-index-routing-not-representation.md), [ADR-053](../CONNECT/ADR-053-connect-phase-as-python-package.md)

## Context

`zettel/state.py` had grown to ~2,780 lines. It held all of the following:

- the DDL
- a 60-entry `ALTER TABLE` migration list
- a one-time FTS backfill
- the PT-BR stopword list and the FTS MATCH builder
- about 140 `StateDB` methods covering thirteen domains, from the extract queue to the web dashboard

A close reading of the file turned up several problems the size had hidden:

- **Legacy code for a database that no longer exists.** Every migrated column was already in the `CREATE TABLE`, and the backfill only served databases created before FTS5. The vault is a development vault and is recreated at will.
- **Dead API.**
  - `count_notes` and `delete_topic_index_kind` had no caller.
  - `count_stale_chapter_summaries` and `search_chunks_fts` had only tests.
  - `get_pending_chunks` / `get_failed_chunks` duplicated `get_chunks_by_status`.
  - Two columns were never written by any code path: `notes.auto_checksum` and `mocs.embedding_input_hash`.
- **Duplicated definitions.**
  - `state._fold` and `topic_index.fold` were the same function.
  - `_fts_match_expr` re-tokenised the query instead of building on `fts_query_terms`, whose docstring claimed they were the same.
  - The per-edge weight rule (`origin='manual'` wins over the relation type) was written twice, in `graph.expand_notes` and in `get_weighted_note_degrees`.
- **Avoidable work.**
  - The web dashboard loaded every note title to label ten hubs.
  - The hub-MOC lookups loaded every MOC body to parse JSON in Python.
  - `get_stats` issued fifteen queries.
  - `delete_source_cascade` loaded chunk text only to collect ids.
  - `update_web_job` ran a `SELECT *` only to check that the row existed.

## Decision

Replace `zettel/state.py` with the package `zettel/state/`:

```
zettel/state/
├── __init__.py     re-exports StateDB
├── schema.py       SCHEMA_SQL (tables + indexes), FTS_SQL
├── base.py         StateBase: row helpers (_fetchone/_fetchall/_count/_now) + FTS sync writers
├── db.py           StateDB(*MIXINS): connection, pragmas, schema, FTS probe, close, vacuum
├── sources.py      files + sources
├── chapters.py     chapters, summaries, chapter <-> note aggregates
├── chunks.py       chunks, status, pages, granular-LIT pickers
├── concepts.py     concepts
├── notes.py        notes + web catalog
├── connections.py  note_connections + edge_weight
├── mocs.py         MOCs + topic_index_terms
├── assets.py       assets
├── llm_cache.py    llm_cache
├── runs.py         runs
├── fts.py          BM25 search + rebuild
└── web.py          web job queue, get_stats, dashboard
```

Query-term facts move to a new stdlib-only leaf, `zettel/search_terms.py`: `PT_STOPWORDS`, `fold`, `fts_query_terms` and `fts_match_expr`. The latter is now built from `fts_query_terms`, so there is one tokenisation by construction. The same rationale made `markdown_fences.py` a leaf. `state`, `retrieval` and `topic_index` all import from it, and `topic_index` no longer imports a private name from `state`.

`from zettel.state import StateDB` and every surviving method name stay the same.

### Rules

1. **No migration layer.** A schema change means `zettel init --reset`.
2. **Mixins may call each other through `self`.** `StateDB` composes all of them, and deleting a source deletes its chunks.
3. **No two mixins define the same attribute.** The MRO would silently shadow one of them.
4. **Siblings import by absolute path**, and nothing inside the package imports from `zettel.state` (the ADR-032 / ADR-053 rules).

`tests/test_state_package.py` enforces rules 3 and 4, and it pins the exact public method set, so a method lost in a refactor fails there rather than at a call site.

## Consequences

- The largest module is `sources.py`, at about 390 lines (most of it the `upsert_source` column list). The whole package is about 2,470 lines, the DDL included.
- `fts_match_expr` now emits lowercased, de-duplicated terms. FTS5 `unicode61` is case-insensitive, so what matches is unchanged. The one difference is a query that repeats a word: it no longer weighs that term twice in bm25.
- `get_chunks_by_status("pending")` orders by `source_id, chunk_index`, where the old order was insertion order. `extract` now walks the queue in document order.
- `get_web_dashboard` takes `relation_weights` explicitly, so `state` no longer imports `zettel.config`. The web app passes `DEFAULT_RELATION_WEIGHTS`, the same weights `garden --hubs` uses.
- `get_web_job` no longer returns the raw `result_json` alongside the decoded `result`.

## Alternatives

- **Clean up in place, keep one file.** Rejected: the result would still be about 2,300 lines mixing thirteen concerns.
- **One repository class per domain, injected separately.** Rejected: every consumer and test takes one `StateDB`, and ADR-008's single gateway is the point.

## References

* `zettel/state/db.py` — `StateDB`, `MIXINS`
* `zettel/state/base.py` — `StateBase`
* `zettel/state/connections.py` — `edge_weight`
* `zettel/search_terms.py`
* `tests/test_state_package.py`, `tests/test_state.py`
