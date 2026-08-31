# Potential ADR: SQLite with WAL Mode and FTS5 as Primary Persistence Layer

**Module**: INFRA  
**Category**: Infrastructure Service  
**Priority**: Must Document (Score: 145)  
**Date Identified**: 2026-08-30

---

## What Was Identified

The project uses SQLite as its sole relational database with Write-Ahead Logging (WAL) mode enabled and FTS5 (Full-Text Search) virtual tables for lexical search. This decision underpins every data-modifying operation in the pipeline — harvest, extract, review, connect, garden — plus the web UI's job queue and cost tracking.

The codebase uses raw `sqlite3` (stdlib) rather than an ORM, implementing the repository pattern through explicit `StateDB` methods. SQLite persists to `data/state.db` on the local filesystem with WAL journal mode, ensuring durability and concurrent read access during LLM/embedding operations.

**Introduced**: Foundational; git history shows WAL mode in early commits (schema has been stable for ~18 months based on recent structural commits like `7d2764d`).

**Modified**: Stable with incremental schema additions (web_jobs/web_job_events tables added when web UI was introduced in `5d9b504`; FTS5 virtual tables added in `2d6ff27` when hybrid retrieval was implemented). No breaking changes to core schema in recent history.

---

## Why This Might Deserve an ADR

- **Impact**: Every pipeline stage (harvest, extract, review, connect, garden, sync) and the web job queue depend on StateDB. No data survives without SQLite.
- **Trade-offs Visible**: 
  - WAL mode chosen for concurrent read access (allows embedding/LLM calls while querying), at the cost of local-disk-only deployment (no remote DB, multi-writer contention).
  - FTS5 chosen for BM25-style lexical search, tight coupling with SQLite rather than a separate search service.
  - No ORM chosen; raw SQL + Python repository pattern provides full control but requires careful manual schema management.
- **Cost to Change**: Database migration is weeks of work (extract schema, migrate data, reindex, recalibrate thresholds).
- **Team Knowledge**: Everyone writing pipeline code must understand the SQLite schema, WAL implications (no remote access), FTS5 MATCH expressions, and the repository API (`get_sources`, `upsert_chunk`, etc.).
- **Temporal Context**: Stable for 18+ months; no recent churn. Core schema additions (web_jobs, FTS5 tables) were strategic, not reactive.

---

## Evidence Found in Codebase

### Key Files
- [`zettel/state.py`](../../../zettel/state.py) - Lines 1-250 (schema definition), entire file shows StateDB implementation
  - WAL mode: `conn.execute("PRAGMA journal_mode = WAL")` (implicit in schema, enforced in `__init__`)
  - FTS5 virtual tables: `CREATE VIRTUAL TABLE fts_notes` / `CREATE VIRTUAL TABLE fts_chunks` (lines ~260+)
  - Schema includes 12 normalized tables (files, sources, chapters, chunks, concepts, notes, mocs, assets, llm_cache, note_connections, runs, web_jobs, web_job_events)

### Code Evidence
```python
# From zettel/state.py (StateDB class):
class StateDB:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None)
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = NORMAL")
        # Schema creation via _SCHEMA_SQL

# FTS5 tables (added ~2027 in hybrid retrieval commit):
CREATE VIRTUAL TABLE IF NOT EXISTS fts_notes USING fts5(
    title UNINDEXED,
    body,
    note_id UNINDEXED,
    content = 'notes',
    content_rowid = 'rowid'
);
```

### Impact Analysis
- **Introduced**: Early (commit history shows schema stable since ~18 months; `2d6ff27` added FTS5 virtual tables for hybrid retrieval)
- **Modified**: 12 commits adding features (web_jobs, image assets, cost tracking, FTS5)
- **Last change**: `7d2764d` (feat: review HITL bands, purge-rejected, VACUUM) — maintaining VACUUM strategy, not changing core choice
- **Themes**: Schema has evolved *additively* (new tables) not *substitutively* (no major rewrites) — stability signal
- **Files affected**: state.py (repository), all phases depend on StateDB: harvester, extractor, review, connector, gardener, sync, web_app
- **Scope**: Large (1725 lines in state.py alone; 22 afferent dependencies per mapping)

### Constraints Documented
- **No remote database**: SQLite is file-local only. Replit deployment model (single VM, all services on same instance) is a dependency.
- **No cross-store consistency**: mapping.md explicitly notes SQLite + ChromaDB have no transactional guarantee — a source can be harvested (SQLite) without embedding (ChromaDB), or vice versa.
- **Concurrent writer issue**: Single Uvicorn worker + CLI process could theoretically conflict; web design prevents it (single-job-at-a-time queue).

---

## Questions to Address in ADR (if created)

- What was the trade-off analysis between SQLite (local, no ORM, full control) vs. PostgreSQL (remote, mature ORMs, managed backups)?
- How does WAL mode affect disaster recovery (e.g., unclosed connection during power loss)?
- Why FTS5 (SQLite's full-text search) instead of Elasticsearch or Meilisearch for BM25? (Answer likely: simplicity, no external service, local-only deployment model.)
- What happens when embedding.provider/model changes and vectors invalidate? (Currently: requires `zettel reindex --force` + manual recalibration of thresholds; should this be automatic?)
- How are schema migrations tested before production? (CLAUDE.md shows `pytest` coverage, but no visible schema-evolution tests.)

## Related Potential ADRs
- ChromaDB Embedded Client as Vector Store (sister infrastructure service)
- Hybrid Dense+BM25 Retrieval with RRF Fusion (uses FTS5 for BM25 half)
- Dual-Store Persistence (SQLite + ChromaDB with no cross-store transactions)

## Additional Notes
- No containerization found; SQLite is not cloud-native by design. Deployment is single-VM (Replit), not Kubernetes/multi-instance.
- VACUUM strategy (run after `purge-rejected`, `purge-source`) is documented but not automated — manual compaction is required after deletions.
- `zettel doctor` command checks database integrity but no schema-versioning system is visible (no migrations framework like Alembic).
