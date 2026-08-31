# Potential ADR: ChromaDB Embedded Client as Vector Store

**Module**: INFRA  
**Category**: Infrastructure Service  
**Priority**: Must Document (Score: 145)  
**Date Identified**: 2026-08-30

---

## What Was Identified

The project uses ChromaDB (v1.5.9, pinned) in embedded `PersistentClient` mode as the vector database for 5 collections: `sources`, `chunks`, `permanent_notes`, `mocs`, and `literature_notes`. All dense-vector retrieval operations (semantic search, graph expansion, hybrid RRF fusion) depend on ChromaDB, with embedding provider configurable (OpenAI, SentenceTransformers, Ollama) but the vector store choice fixed.

ChromaDB is invoked exclusively through the `VectorIndex` repository wrapper in `index.py`, never directly, maintaining a clean abstraction boundary. Embeddings are generated on-demand during harvest (chunks), extract (disabled until review approval), connect (permanent notes), and garden (MOCs) phases, then persisted indefinitely.

**Introduced**: Foundational; commit history suggests ChromaDB has been stable through multiple feature additions (initially dense-only, later augmented with hybrid retrieval in `2d6ff27`).

**Modified**: Stable; embedding provider logic evolved (Ollama + MRL dimensions added in `3964e11`), but core choice unchanged. The pinned version (==1.5.9) suggests prior CVE/stability concerns.

---

## Why This Might Deserve an ADR

- **Impact**: Every search-consuming operation (ask, article, connector RAG, sync suggestions) depends on ChromaDB hits. Also critical for duplicate detection during harvest (semantic layer-3).
- **Trade-offs Visible**:
  - Embedded mode (local, no external service) vs. client/server (scalable but requires additional deployment).
  - Vector store pinned to 1.5.9; attempting upgrades may break if collection format changed.
  - No off-the-shelf migration path when `embedding.provider` or `embedding.model` changes — manual `zettel reindex --force` required.
  - All vectors persist indefinitely; no cleanup strategy when sources are deleted (only references removed from metadata, vectors stay in DB).
- **Cost to Change**: Switching to a different vector store (e.g., Pinecone, Milvus, Weaviate) requires rewriting `VectorIndex` + re-embedding entire corpus + recalibrating similarity thresholds for all downstream logic (harvest dedupe at 0.88, retrieval floor at 0.70, etc.).
- **Team Knowledge**: Anyone working on retrieval, deduplication, or the garden pipeline must understand vector similarity semantics, embedding model differences, and the absolute relevance floor calibrated on the current embedding choice.
- **Security Note**: mapping.md flags a known CVE (CVSS 10.0 RCE) in ChromaDB's FastAPI server component, mitigated by embedded-client-only usage but still shipped; a candidate for security-review phase.

---

## Evidence Found in Codebase

### Key Files
- [`zettel/index.py`](../../../zettel/index.py) - Entire file (766 lines)
  - Pinned version: line 1 import comment `chromadb == 1.5.9`
  - Embedded client mode: `PersistentClient(path=...)` (line ~150)
  - 5 collections: COL_SOURCES, COL_CHUNKS, COL_PERMANENT, COL_MOCS, COL_LITERATURE (lines 14-21)
  - Embedding provider factories: OpenAI, SentenceTransformers, Ollama via LangChain adapters

### Code Evidence
```python
# From zettel/index.py (VectorIndex class):
import chromadb
from chromadb.config import Settings

# Embedded client, not client/server
client = chromadb.PersistentClient(
    path=str(self.chroma_path),
    settings=Settings(
        anonymized_telemetry=False,
        allow_reset=True,
    )
)

# Five collections
collections = {
    'sources': client.get_or_create_collection(...),
    'chunks': client.get_or_create_collection(...),
    'permanent_notes': client.get_or_create_collection(...),
    'mocs': client.get_or_create_collection(...),
    'literature_notes': client.get_or_create_collection(...),
}

# Embedding provider strategy (pluggable)
if provider == 'openai':
    embedding_fn = _OpenAIChromaEF(...)
elif provider == 'sentence-transformers':
    embedding_fn = _SentenceTransformersChromaEF(...)
elif provider == 'ollama':
    embedding_fn = _LangChainOllamaChromaEF(...)
```

### Impact Analysis
- **Introduced**: Foundational (~18 months based on stable history; embedded client has always been the choice per codebase).
- **Modified**: 6+ commits touching embedding logic (Ollama support, MRL dimensions, provider transitions), but core ChromaDB choice never questioned.
- **Last change**: `3964e11` (feat: embedding support MRL dimensions via langchain_ollama) — extending capabilities, not replacing.
- **Files affected**: index.py (wrapper), retrieval.py (uses hits), harvester.py (dedupe layer-3), connector.py (RAG), gardener.py (MOC clustering), sync.py (suggestions), ask.py (Q&A), article.py (article generation), web_app.py (job dispatch)
- **Scope**: Large (used by 9+ modules, 21 afferent dependencies in index.py per mapping)

### Known Issues Documented
- **CVE in FastAPI server**: Dependency audit flagged CVSS 10.0 RCE in ChromaDB's unused server component; mitigated by embedded-only usage but present in shipped code.
- **Vector invalidation**: Changing `embedding.provider` or `embedding.model` renders all vectors stale; requires manual full reindex.
- **No collection versioning**: If ChromaDB format changes between versions (e.g., 1.5.9 → 2.x), existing collections may become unreadable.
- **Vector persistence**: Deleted sources have metadata references cleaned but vectors remain in collections indefinitely (no compaction strategy visible).

---

## Questions to Address in ADR (if created)

- Why ChromaDB embedded instead of a managed service (Pinecone, Supabase pgvector) or self-hosted (Milvus, Qdrant)?
  - Answer likely: simplicity (no external service), local-only deployment model (Replit), privacy (vectors stay local).
- How does embedding-model switching work in production? (Currently: manual `reindex --force`; should there be a migration tool or automatic detection?)
- What happens to the 5 collections if a user loses the `data/chroma/` directory? (No backup/restore strategy visible; catastrophic data loss.)
- Why is ChromaDB pinned to exactly 1.5.9? (Likely: a known stable version; any upgrade risk assessment before moving to 2.x?)
- Should vector cleanup be automatic when `delete-source --delete-permanent` is run? (Currently: vectors orphaned in collections.)

## Related Potential ADRs
- SQLite with WAL + FTS5 (sister infrastructure service, no cross-store transaction guarantee)
- Hybrid Dense+BM25 Retrieval with RRF Fusion (uses ChromaDB for dense half)
- Absolute Relevance Floor for Retrieval (calibrated on ChromaDB similarity scores)
- Dual-Store Persistence (SQLite + ChromaDB with no cross-store transactions)

## Additional Notes
- Embedding provider is configurable but constrained to 3 options; no ability to swap in arbitrary providers at runtime.
- The `dimensions` config parameter enables MRL (Matryoshka Representation Learning) on compatible models; a nice feature but requires careful dimension tuning.
- No visible test coverage for ChromaDB upsert logic; reliance on fixture teardown/cleanup for test isolation.
