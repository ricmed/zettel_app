# Potential ADR: Dual-Store Persistence (SQLite + ChromaDB) with No Cross-Store Transactions

**Module**: INFRA (Persistence subsystem)  
**Category**: Infrastructure / Data Architecture  
**Priority**: Must Document (Score: 145)  
**Date Identified**: 2026-08-30

---

## What Was Identified

The project maintains two independent persistent stores with no transactional guarantee between them:
1. **SQLite** (`data/state.db`) — relational, normalized schema, FTS5 search
2. **ChromaDB** (`data/chroma/`) — vector embeddings, 5 collections, local filesystem

Every pipeline operation that modifies state must write to both independently. For example, during harvest:
- Chunk extracted → inserted into SQLite `chunks` table (pending status)
- Chunk embedded → upserted into ChromaDB `chunks` collection

If the process crashes between these two operations, a source is partially indexed: SQLite knows about the chunk (in DB), but ChromaDB has no embedding (missing from index). The inverse is also possible: an embedding exists in Chroma but no corresponding SQLite row.

This coupling is acknowledged in CLAUDE.md: "no cross-store transaction guarantee between SQLite and `index.py`" and flagged across multiple component analyses (review, index, state, purge_source, sync). The mapping.md explicitly lists this as a known risk: "no cross-store transaction guarantee" and "full-system single point of failure by design."

**Introduced**: Foundational architecture; both stores have existed for 18+ months. The coupling emerges from the design (two persistence layers), not a recent decision.

**Modified**: Evolved with pipeline additions (web_jobs/web_job_events added to SQLite; literature_notes collection added to ChromaDB), but the fundamental lack of cross-store transactions remains.

---

## Why This Might Deserve an ADR

- **Impact**: Every data-modifying operation (harvest, extract, review, connect, garden, sync, web jobs) depends on this. A crash during harvest chunks 1000 items means SQLite might have 800 + Chroma 600 in an inconsistent state. Recovery requires manual reconciliation.
- **Trade-offs Visible**:
  - Simplicity: Two independent stores are easier to understand than a unified transactional system (which would require either embedding-in-SQLite or vectors-in-Chroma).
  - Cost: No distributed transaction overhead; each store commits independently.
  - Risk: Inconsistency windows exist between SQLite and ChromaDB updates, especially during long-running operations (harvest can process 100+ chunks).
  - Recovery: No automatic consistency repair exists; manual tools (zettel reindex, zettel sync-manual) can detect/fix, but require human intervention.
- **Cost to Change**: Implementing cross-store transactions would require either:
  - Moving vectors into SQLite (BLOB column for embeddings) — loses ChromaDB's optimized search.
  - Implementing a 2-phase commit protocol (write to both, then confirm) — adds complexity and latency.
  - Using a unified store (PostgreSQL with pgvector) — changes deployment model, adds external dependency.
- **Team Knowledge**: Anyone working on pipeline operations must understand that SQLite writes and Chroma upserts are independent. If a crash occurs between them, the vault may be inconsistent and require `zettel reindex` or `zettel sync-manual --rebuild-graph` to repair.
- **Temporal Context**: Foundational, stable; no recent mitigation attempts visible in git history. The mapping.md notes this was "explicitly acknowledged in CLAUDE.md itself" — the project team is aware and has accepted the risk as part of the single-VM, local-disk design.

---

## Evidence Found in Codebase

### Key Files
- [`zettel/state.py`](../../../zettel/state.py) - SQLite persistence logic
- [`zettel/index.py`](../../../zettel/index.py) - ChromaDB persistence logic
- [`zettel/harvester.py`](../../../zettel/harvester.py) - Harvest phase (inserts to both stores)
- [`zettel/extractor.py`](../../../zettel/extractor.py) - Extract phase (inserts to both)
- [`CLAUDE.md`](../../../CLAUDE.md) - Explicitly acknowledges coupling

### Code Evidence
```python
# From zettel/harvester.py (example: harvest phase inserts to both stores independently):
def _process_chunk(chunk_text: str, ...):
    # Step 1: Write to SQLite (independent transaction)
    chunk_id = db.upsert_chunk(
        source_id=source_id,
        chapter_id=chapter_id,
        text=chunk_text,
        checksum=checksum,
        status="pending"
    )
    # SQLite committed here; if crash occurs next, Chroma is missing this chunk
    
    # Step 2: Embed and write to ChromaDB (independent transaction)
    embedding = embed(chunk_text)
    index.upsert_chunk(
        chunk_id=chunk_id,
        text=chunk_text,
        embedding=embedding
    )
    # Chroma committed here; SQLite and Chroma now consistent again
    # But: if process crashes between steps 1 and 2, inconsistent state

# From zettel/web_app.py (job dispatch shows the pattern):
def _enqueue_job(operation, ...):
    # Multiple independent persistence calls in sequence
    job = web_jobs.create(operation, payload)  # SQLite
    
    if operation == "harvest":
        # Harvest will write to both stores; if it crashes mid-operation,
        # no automatic rollback or recovery
        harvester.run(...)  # -> multiple db.upsert + index.upsert calls
```

### Explicit Acknowledgment
```python
# From zettel/web_app.py (comment explicitly noting the coupling):
# "_idx_kwargs must mirror `cli._idx_kwargs`" when opening VectorIndex
# — this is a workaround for the lack of cross-store consistency:
# if embedding config changes, both stores must be rebuilt manually
```

From CLAUDE.md:
> "no cross-store transaction guarantee between SQLite (`state.py`) and ChromaDB (`index.py`) — flagged independently across the `review`, `index`, `state`, `purge_source`, and `sync` component analyses, and explicitly acknowledged in CLAUDE.md"

### Impact Analysis
- **Introduced**: Foundational (both stores present from early architecture)
- **Modified**: Evolves additively (new tables in SQLite, new collections in ChromaDB), but transactional guarantee never added
- **Last observation**: Recent commits still follow the pattern (no mitigation visible); `web_jobs` table added independently to SQLite
- **Files affected**: harvester, extractor, review, connector, gardener, gardener_hub, sync, web_app — basically every phase writes to both
- **Scope**: Systemic (every data-modifying operation)
- **Known risks**: Reindex tool exists to repair SQLite/Chroma desync; `zettel reindex --force` re-embeds all notes and resyncs both stores

---

## Mitigation Strategies (Currently In Use)

1. **Crash-Tolerant Design**: Pipeline phases use SQLite `status` fields (pending, awaiting_review, approved) to track state. On restart, incomplete phases resume from their last checkpoint.
2. **Manual Reconciliation**: `zettel reindex` and `zettel sync-manual --rebuild-graph` can detect and repair inconsistencies.
3. **Single-Writer Queue**: Web UI enforces only one mutating job at a time (409 conflict on concurrent submit), reducing contention.
4. **Local Deployment Model**: Replit single-VM design means both stores are on the same filesystem; no network partition risk.

## Questions to Address in ADR (if created)

- What is the maximum acceptable inconsistency window? (Currently: unbounded; a crash at any point leaves both stores partly updated.)
- Should the project implement a crash-recovery protocol? (e.g., a "consistency check" phase before each pipeline stage)
- How often has the dual-store inconsistency actually caused issues in production? (Unknown; no bug reports visible, but single-user/local deployment may mask the issue.)
- Would embedding-in-SQLite (BLOB vectors) be acceptable if it enabled transactions? (Trade-off: loses ChromaDB's optimized search, gains ACID consistency.)
- Should `zettel doctor` automatically detect and repair inconsistencies, or remain passive (current)?

## Related Potential ADRs
- SQLite with WAL + FTS5
- ChromaDB Embedded Vector Store
- Hybrid Dense+BM25 Retrieval (depends on Chroma consistency)

## Additional Notes
- The project explicitly chose single-VM, local-disk deployment; cloud-native deployments (multi-instance) would require a different architecture (shared DB, coordination).
- No visible backup/disaster-recovery strategy (e.g., no automated snapshots of data/state.db + data/chroma/).
- The web UI's job queue (`web_jobs` table) is itself stored in SQLite, so a Chroma failure can still leave job records in SQLite.
