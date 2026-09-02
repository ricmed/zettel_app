# ADR-XXX: Dual-Store Persistence Without Cross-Store Transactions

**Status:** Accepted
**Date:** 2025-03-01
**Related to:**
- [ADR-XXX: Granular Per-Chunk Literature Notes with Readable Filenames](../EXTRACT/ADR-015-granular-literature-notes-readable-filenames.md)
- [ADR-XXX: Web/CLI Auto-Approve Threshold Validation Asymmetry](../REVIEW/needs-input/ADR-XXX-web-cli-auto-approve-threshold-validation-asymmetry.md)
- [ADR-XXX: Confidence-Band Human-in-the-Loop Approval Gate](../REVIEW/needs-input/ADR-017-confidence-band-hitl-approval-gate.md)

## Context and Problem Statement

The pipeline persists state across two independent stores: SQLite (`data/state.db`) for relational metadata, pipeline status tracking, and FTS5 lexical search, and ChromaDB (`data/chroma/`) for vector embeddings across five collections. Every data-modifying phase (harvest, extract, review, connect, garden, sync, and web job dispatch) writes to both stores, but the two writes are not part of a single transaction. During harvest, for example, a chunk is first inserted into SQLite with a `pending` status and only afterward embedded and upserted into ChromaDB (`chunk_and_persist` in `zettel/harvester/chunking.py`). If the process crashes between these two calls, the stores diverge: SQLite may reference chunks with no corresponding embedding, or vice versa.

This coupling is architectural rather than incidental — it exists because SQLite and ChromaDB serve different, non-overlapping needs (normalized relational state plus BM25 search versus approximate nearest-neighbor vector search) and neither store natively provides both. The two stores have coexisted since the project's early architecture (18+ months prior to this writing) and have only grown additively (new SQLite tables such as `web_jobs`, new ChromaDB collections such as `literature_notes`) without a cross-store transactional guarantee ever being added.

The same two-step pattern repeats across every phase that mutates state, not just harvest: extraction, review approval, connection, gardening, and manual sync each perform a SQLite write and a ChromaDB write as separate steps, so a partial failure in any of them leaves the vault in a state where the two stores disagree about what has been processed. No incident log or monitoring currently confirms how often this divergence actually manifests in practice; the project's single-user, single-VM, local-disk deployment model may simply mask an otherwise-visible failure mode.

## Decision Drivers

* SQLite and ChromaDB each provide a capability (relational/FTS5 search vs. optimized vector similarity) the other does not, so a single unified store would sacrifice one of the two.
* Adopting a store with native cross-domain transactions (e.g., an external database with vector support) would replace the self-contained, single-VM local-disk deployment with one requiring an external managed dependency.
* Two independently-committing local stores avoid the latency and implementation cost of a distributed or two-phase commit protocol.
* The pipeline's existing checkpoint model (SQLite `status` fields such as `pending`/`awaiting_review`/`approved`) already gives phases a way to resume after a crash, reducing (but not eliminating) the practical impact of a missed cross-store write.
* Any accepted design must leave an operable path to detect and repair drift, since no automatic reconciliation exists today.
* The stores have grown additively for 18+ months (new SQLite tables, new ChromaDB collections) without incident reports forcing a redesign, which weighs against a disruptive migration in the absence of demonstrated harm.

## Considered Options

* Keep two independent stores (SQLite for relational/FTS state, ChromaDB for vectors), accept the inconsistency window as a known risk, and mitigate it with status-field checkpointing plus manual reconciliation tooling.
* Move vector embeddings into SQLite as BLOB columns, unifying persistence under one transactional engine.
* Migrate to a single store with native vector support (e.g., a server-based database with a vector extension), enabling true cross-store ACID transactions.

## Decision Outcome

Chosen option: "Keep two independent stores, mitigate via checkpointing and manual reconciliation," because it preserves each store's specialized strength (SQLite's relational integrity and FTS5 search, ChromaDB's vector search) without introducing an external database dependency or reworking the single-VM local-disk deployment model. The pipeline's phase-based status tracking already tolerates partial progress and resumes from the last checkpoint, and existing tools (`zettel reindex`, `zettel sync-manual --rebuild-graph`) provide a manual path to detect and repair drift when it occurs.

[NEEDS INPUT: The maximum acceptable inconsistency window (how long or how large a divergence between stores is tolerable before it is considered a defect) has not been formally defined by the team.]

### Positive Consequences

* No added infrastructure dependency; both stores run locally within the existing single-VM deployment.
* Each store is used for what it is best at, avoiding a compromise implementation of vector search inside SQLite or of relational integrity inside ChromaDB.
* Existing checkpoint fields and reconciliation commands provide a working, if manual, recovery path.
* The single-writer job queue in the web UI limits how many concurrent operations can be mutating both stores at once, narrowing (though not closing) the practical exposure window.

### Negative Consequences

* A crash between a SQLite write and its corresponding ChromaDB upsert (or the reverse order elsewhere in the pipeline) leaves the two stores in a divergent state with no automatic detection.
* Recovery currently depends on a human running `zettel reindex` or `zettel sync-manual --rebuild-graph`; there is no proactive check that flags drift on its own.
* The risk scales with the size of a run — a harvest processing hundreds of chunks has a correspondingly larger window of exposure per crash.
* There is no automated backup or disaster-recovery strategy covering both stores together, so a corrupted or divergent pair of stores has no independent snapshot to fall back to.

## Pros and Cons of the Options

### Keep two independent stores (chosen)

* Good, because it requires no new infrastructure or deployment changes.
* Good, because each store is used for its strongest capability (FTS5 vs. vector search).
* Bad, because there is no atomic guarantee across the two writes.
* Bad, because reconciliation after a crash is manual, not automatic.

### Embed vectors in SQLite (BLOB column)

* Good, because it would provide full transactional consistency between relational state and vector data.
* Bad, because it loses ChromaDB's optimized approximate nearest-neighbor search.
* Bad, because vector similarity search would need to be reimplemented or emulated inside SQLite, with unclear performance at scale.

### Migrate to a unified store with native vector support

* Good, because it would enable true cross-store ACID transactions.
* Bad, because it introduces an external managed database dependency, replacing the current self-contained deployment.
* Bad, because it requires a data migration and changes the operational model away from single-VM local-disk simplicity.
* [NEEDS INPUT: This option's feasibility and cost were not evaluated against a specific target technology or migration plan.]

## Consequences

Every pipeline phase that mutates state must continue to treat the SQLite write and the ChromaDB write as two separate, non-atomic operations, and any new phase added to the pipeline inherits this same obligation. The existing status-field checkpointing pattern (`pending`, `awaiting_review`, `approved`) must be preserved in any future phase so that a crash mid-run remains resumable rather than silently corrupting state.

Because there is no automated detection of divergence, the team currently relies on the absence of visible symptoms as an (unverified) signal that inconsistency is not a practical problem. [NEEDS INPUT: There is no monitoring or incident log confirming how frequently dual-store drift has actually occurred in production use, which makes it difficult to judge whether the current manual-reconciliation approach is sufficient going forward.]

The single-writer job queue in the web UI (rejecting a second mutating job with a 409 while one is running) reduces concurrent-write contention but does not address the crash-during-a-single-job scenario. [NEEDS INPUT: Whether `zettel doctor` should be extended to proactively detect and repair store drift, versus remaining a passive diagnostic, has not been decided.]

## References

* `zettel/harvester/chunking.py` — `chunk_and_persist`, sequential and independent SQLite and ChromaDB writes during chunk ingestion
* `zettel/state.py` — SQLite-side `upsert_chunk`
* `zettel/index.py` — ChromaDB-side `upsert_chunk`
* `CLAUDE.md` — documents the dual-store architecture and the absence of a cross-store transaction guarantee
