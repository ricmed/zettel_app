# ADR-XXX: ChromaDB Embedded Client as Vector Store

**Status:** Accepted
**Date:** 2025-03-01 (approximate — foundational choice, no isolatable commit)
**Related to:** [ADR-XXX: Granular Per-Chunk Literature Notes with Readable Filenames](../EXTRACT/ADR-015-granular-literature-notes-readable-filenames.md)

## Context and Problem Statement

The system needs a vector database for dense semantic search across five distinct note/chunk categories (sources, chunks, permanent notes, MOCs, literature notes). This vector store underpins every search-consuming operation in the pipeline: hybrid retrieval (dense + BM25 fusion), graph expansion, connector RAG, sync suggestions, ask/article generation, and the semantic layer of harvest-time duplicate detection.

ChromaDB (pinned at `1.5.9`) was adopted in embedded `PersistentClient` mode rather than as a client/server deployment. It is accessed exclusively through a single repository wrapper (`VectorIndex`), which also abstracts a pluggable embedding provider (OpenAI, SentenceTransformers, or Ollama) independent of the vector store choice itself.

[NEEDS INPUT: Confirm whether embedded-only deployment was a deliberate infrastructure constraint (e.g., single-process/local-first hosting such as Replit) or simply the initial default that was never revisited as usage grew.]

## Decision Drivers

- The application targets local-only, single-process deployment, favoring a vector store with no separate service to install or operate.
- Keeping vectors on local disk avoids sending user content or embeddings to a third-party service.
- Retrieval-dependent operations (ask, article, connect, sync, harvest dedupe) need low-latency access to vectors from within the same process as the SQLite state store.
- Operating a standalone vector-database service would add deployment and monitoring overhead disproportionate to project scale.
- ChromaDB ships a FastAPI server component with a known CVSS 10.0 RCE; embedded-only usage avoids exposing that surface at all.
- The embedding provider needed to remain swappable (OpenAI/SentenceTransformers/Ollama) without coupling that choice to the vector store.

## Considered Options

1. ChromaDB embedded `PersistentClient` (chosen)
2. Self-hosted client/server vector database (e.g., ChromaDB server mode, Milvus, Qdrant)
3. Managed vector service (e.g., Pinecone, Supabase pgvector)

## Decision Outcome

Chosen option: **ChromaDB embedded `PersistentClient`**, because it satisfies the local-first, single-process deployment model with no additional infrastructure to run, keeps all vector data on local disk for privacy, and sidesteps the CVE surface of ChromaDB's own server component by never running it. The five collections (`sources`, `chunks`, `permanent_notes`, `mocs`, `literature_notes`) are all managed through one `VectorIndex` wrapper, keeping the rest of the codebase decoupled from ChromaDB's API.

[NEEDS INPUT: Confirm risk acceptance for the CVSS 10.0 RCE in ChromaDB's FastAPI server component — it ships as a dependency even though embedded-mode usage never invokes it.]

## Pros and Cons of the Options

### ChromaDB embedded `PersistentClient` (chosen)

- Good, because no external service needs to be deployed, configured, or monitored.
- Good, because all vector data stays local, avoiding third-party data exposure.
- Good, because it never invokes ChromaDB's networked server component, avoiding its known CVE.
- Bad, because there is no built-in migration path when the embedding provider or model changes — a full manual reindex is required.
- Bad, because collection format compatibility across ChromaDB versions is unverified, creating upgrade risk.

### Self-hosted client/server vector database

- Good, because the vector store can scale and be operated independently of the application process.
- Good, because a centralized service could serve multiple application instances.
- Bad, because it requires deploying and operating an additional service.
- Bad, because it reintroduces the networked server surface (and its CVE exposure) that embedded mode was chosen to avoid.

### Managed vector service

- Good, because the vendor handles scaling, availability, and often backup/versioning.
- Good, because there is no infrastructure to operate.
- Bad, because it requires sending embeddings/content to a third-party service, conflicting with the local-first, privacy-preserving design.
- Bad, because it requires network connectivity for every retrieval call and adds recurring usage cost.
- [NEEDS INPUT: Was a managed vector service formally evaluated and rejected, or excluded purely on local-first/privacy grounds without a cost or capability comparison?]

## Consequences

Embedding-provider or embedding-model changes render all existing vectors stale, with no automatic drift detection — operators must manually run a full reindex, and nothing today warns when configuration and stored vectors have diverged. Deleting a source removes its metadata references but leaves the underlying vectors in the collections indefinitely, so storage grows without a compaction path.

The `data/chroma/` directory is the sole copy of all embedded vectors, and no backup or restore procedure exists for it; losing that directory means re-embedding the entire corpus from source content. [NEEDS INPUT: Define a backup/restore strategy for `data/chroma/`, given that its loss is currently unrecoverable without a full reindex.]

Because the vector store choice is fixed while the embedding provider is pluggable, similarity thresholds calibrated elsewhere in the system (harvest duplicate detection, the absolute relevance floor) are tied to whichever embedding model is active, not to ChromaDB itself — changing embedding models requires recalibrating those thresholds independently of any vector-store decision.

## References

- `zettel/index.py:1` — pinned dependency (`chromadb == 1.5.9`)
- `zettel/index.py:14-21` — five collection definitions (sources, chunks, permanent_notes, mocs, literature_notes)
- `zettel/index.py:150` — `PersistentClient` instantiation (embedded mode)
- `zettel/retrieval.py` — consumes ChromaDB dense search results in hybrid RRF fusion
- `zettel/harvester.py` — semantic layer-3 duplicate detection queries ChromaDB directly
