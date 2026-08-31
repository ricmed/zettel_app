# ADR-XXX: SQLite with WAL Mode and FTS5 as Primary Persistence Layer
**Status:** Accepted
**Date:** 2025-02 (approximate — foundational decision; schema stable ~18 months as of identification)
**Used by:** [ADR-XXX: SQLite-Backed Persistent Job Queue with Single Worker Thread](../WEB/ADR-023-sqlite-backed-job-queue-single-worker.md)
**Related to:** [ADR-XXX: Repository Pattern for Data Access (StateDB and VectorIndex)](./ADR-008-repository-pattern-data-access.md)

## Context and Problem Statement

The pipeline (harvest, extract, review, connect, garden) and the web UI's job queue need a single relational store for pipeline state, metadata, and cost tracking, plus a lexical search mechanism to complement dense vector retrieval. The deployment target is a single-VM environment (Replit) with no managed remote database service available.

The project chose SQLite as its sole relational database, with Write-Ahead Logging (WAL) journal mode enabled for concurrent read access during long-running LLM and embedding calls, and FTS5 virtual tables for BM25-style lexical search. Access is implemented through raw `sqlite3` (stdlib) via an explicit repository pattern (`StateDB`) rather than an ORM, giving full control over schema and queries at the cost of manual schema management.

This decision underpins every data-modifying operation in the system: no pipeline stage or web job can persist state without it, and the schema has evolved only additively (new tables) over roughly 18 months, without a substitutive rewrite — a strong stability signal for the choice.

## Decision Drivers

- Every pipeline stage and the web job queue require durable, transactional state; nothing survives without this store.
- Concurrent read access is needed while LLM/embedding calls are in flight, without blocking other pipeline reads.
- Lexical (BM25-style) search must be available for hybrid retrieval without introducing a separate search service.
- The deployment model targets a single VM with no managed remote database.
- Full control over schema and query behavior was preferred over ORM abstraction overhead.

## Considered Options

1. SQLite with WAL mode and FTS5 (chosen)
2. PostgreSQL as a remote, managed relational database
3. SQLite for state, paired with an external search service (e.g., Elasticsearch/Meilisearch) for lexical search

## Decision Outcome

Chosen option: SQLite with WAL mode and FTS5, because it provides a single embedded store that requires no separate service to operate, matches the single-VM deployment model, supports concurrent reads during long-running LLM operations via WAL, and keeps lexical search co-located with the same store used for pipeline state — avoiding a second infrastructure dependency for search.

[NEEDS INPUT: Was a formal cost/effort comparison against PostgreSQL performed at the time, or was SQLite an implicit default given the single-VM deployment target?]

## Pros and Cons of the Options

### SQLite with WAL Mode and FTS5 (chosen)

- Good, because no separate database server needs to be provisioned or operated.
- Good, because WAL mode allows concurrent reads while writes are in progress.
- Good, because FTS5 provides BM25-style search without adding new infrastructure.
- Bad, because the database is file-local only — no remote or multi-instance access is possible.
- Bad, because a single-writer model requires coordination (single Uvicorn worker, one mutating job at a time) to avoid contention between CLI and web processes.

### PostgreSQL (remote, managed)

- Good, because of a mature ORM and tooling ecosystem.
- Good, because managed backups and replication reduce operational risk.
- Bad, because it requires a managed service or separate process, adding operational overhead misaligned with the single-VM deployment model.
- Bad, because migrating from SQLite would require weeks of work (schema extraction, data migration, reindexing, threshold recalibration).

### SQLite + External Search Service

- Good, because a dedicated search engine may offer more advanced ranking and scale further for large corpora.
- Bad, because it introduces a second infrastructure dependency, breaking the no-external-service deployment model.
- Bad, because keeping the search index in sync with SQLite state adds operational complexity.

## Consequences

Pipeline state and lexical search live in one auditable, file-based store, so search queries require no network calls to a separate service, and backup is a matter of copying a single file plus its WAL/SHM sidecars. VACUUM after `purge-rejected` and `purge-source` keeps the file compact, though this compaction is manual rather than scheduled.

Horizontal scaling or multi-instance deployment is not possible without a future migration, and the current design enforces a single Uvicorn worker with a single mutating job at a time to avoid write contention between the CLI and the web UI. There is no cross-store transactional guarantee between SQLite and ChromaDB: a source can exist in one store without being reflected in the other, which requires manual reconciliation rather than atomic commits.

[NEEDS INPUT: What is the disaster-recovery/backup procedure for `data/state.db` in production, particularly around WAL checkpoint handling on an unclean shutdown?]

[NEEDS INPUT: How are schema changes validated before deployment, given no visible migration-testing framework (e.g., an Alembic equivalent) for the additive schema evolution?]

## References

- `zettel/state.py:47-52` — WAL mode and PRAGMA initialization in `StateDB.__init__`
- `zettel/state.py:55-61` — FTS5 virtual table definition (`fts_notes`)
- `zettel/state.py:1-250` — Full schema definition (12 normalized tables) and `StateDB` implementation
