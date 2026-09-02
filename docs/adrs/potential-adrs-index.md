# Potential ADRs Index

## Analysis Progress

### Analyzed Modules

- **INFRA (Infrastructure)** - 2026-08-30
  - SQLite+WAL+FTS5, ChromaDB, YAML-First Config, Pydantic v2, Hybrid Retrieval, Dual-Store Persistence, Layered Hashing, Repository Pattern, Contextvars Cost Tracking
  - 9 potential ADRs identified (8 must-document, 1 consider)

- **RETRIEVAL** - 2026-08-30
  - Hybrid retrieval (RRF + relevance floor), graph expansion, result transparency, dedupe separation
  - 3 potential ADRs identified (2 must-document, 1 consider)

- **CONNECT (Permanent Note Generation)** - 2026-08-30
  - RAG-based note synthesis, typed bidirectional backlinking, deterministic caching
  - 2 potential ADRs identified (0 must-document, 2 consider)

- **HARVEST (Document Ingestion)** - 2026-08-30
  - Three-layer duplicate detection, Docling PDF extraction, page inference, structural chunking, config-hash caching
  - 5 potential ADRs identified (4 must-document, 1 consider)

- **EXTRACT (Literature Note Drafting)** - 2026-08-30
  - Granular literature notes with readable names and source excerpts, post-approval semantic deduplication
  - 2 potential ADRs identified (1 must-document, 1 consider)

- **GARDEN (MOC Generation and Organization)** - 2026-08-30
  - Taxonomy-first clustering with UMAP+HDBSCAN, hub-anchored complementary MOCs, single LLM call per cluster
  - 3 potential ADRs identified (all must-document)

- **REVIEW (HITL Approval Gate)** - 2026-08-30
  - Confidence-scored HITL approval with band-based UX, post-approval concept deduplication, web/CLI validation asymmetry
  - 3 potential ADRs identified (0 must-document, 3 consider)

- **QA-WRITING (Q&A and Long-form Writing)** - 2026-08-30
  - Ask (grounded Q&A with deterministic no-evidence short-circuit), Article (LangGraph multi-stage orchestration), Bibliography (ABNT formatting with optional LLM merge)
  - 2 potential ADRs identified (0 must-document, 2 consider)

- **WEB (Web UI and Job Queue)** - 2026-08-30
  - FastAPI + Server-Rendered Jinja2, SQLite-backed persistent job queue, single-worker concurrency
  - 2 potential ADRs identified (1 must-document, 1 consider)

- **ASSETS (Media & Diagnostics Support)** - 2026-08-30
  - Image extraction and multimodal LLM description, diagnostic chunk/extraction dumps
  - **NO potential ADRs identified** (all decisions score below 75-point threshold)

- **LLM (LLM Integration Gateway)** - 2026-08-30
  - Multi-provider LLM strategy (pluggable strategy pattern), System+Human prompt split for provider-agnostic caching
  - 2 potential ADRs identified (2 must-document, 0 consider)

- **MANUAL-SYNC (Manual Vault Integration)** - 2026-08-30
  - Manual note adoption pattern, graph loop closure via body wikilinks, irreversible deletion with cascade, origin field for dual pipeline control, cross-store eventual consistency
  - 0 potential ADRs identified (all decisions score below 75-point threshold; implementations of existing INFRA patterns)
  - **See**: `/docs/adrs/SYNC-module-analysis.md` for detailed decision scoring and rationale

- **CLI (Command-Line Interface)** - 2026-08-30
  - Typer + Rich CLI framework, lazy command-level dependency composition, embedding space drift detection
  - 1 potential ADR identified (1 must-document, 0 consider)

### Pending Analysis

- API (REST/routing patterns)

---

## High Priority ADRs (score >= 100)

> All 22 items below were promoted to formal ADRs and now live under
> `potential-adrs/done/`. The `must-document/` folder they were classified into
> during analysis no longer exists.

### Infrastructure (INFRA)

| Title | Category | Score | File |
|-------|----------|-------|------|
| SQLite with WAL Mode and FTS5 as Primary Persistence Layer | Infrastructure Service | 145 | [sqlite-with-wal-and-fts5.md](./potential-adrs/done/INFRA/sqlite-with-wal-and-fts5.md) |
| ChromaDB Embedded Client as Vector Store | Infrastructure Service | 145 | [chromadb-embedded-vector-store.md](./potential-adrs/done/INFRA/chromadb-embedded-vector-store.md) |
| Hybrid Dense+BM25 Retrieval with RRF Fusion and Absolute Relevance Floor | API Protocol / Retrieval Architecture | 145 | [hybrid-dense-bm25-retrieval.md](./potential-adrs/done/INFRA/hybrid-dense-bm25-retrieval.md) |
| YAML-First Configuration with Pydantic Fallback | Configuration Architecture | 125 | [yaml-first-configuration.md](./potential-adrs/done/INFRA/yaml-first-configuration.md) |
| Dual-Store Persistence (SQLite + ChromaDB) with No Cross-Store Transactions | Infrastructure / Data Architecture | 145 | [dual-store-persistence.md](./potential-adrs/done/INFRA/dual-store-persistence.md) |
| Pydantic v2 for Configuration Schema and LLM-Backed DTOs | Primary Framework / Data Validation | 140 | [pydantic-v2-config-dtos.md](./potential-adrs/done/INFRA/pydantic-v2-config-dtos.md) |
| Layered Hashing Strategy for Deterministic LLM Caching and Drift Detection | Data Architecture / Caching Strategy | 120 | [layered-hashing-strategy.md](./potential-adrs/done/INFRA/layered-hashing-strategy.md) |
| Repository Pattern for Data Access (StateDB and VectorIndex) | Architectural Pattern | 115 | [repository-pattern-data-access.md](./potential-adrs/done/INFRA/repository-pattern-data-access.md) |

### Retrieval (RETRIEVAL)

| Title | Category | File |
|-------|----------|------|
| Graph-Based Note Discovery with Weighted BFS Expansion | Retrieval Architecture / Knowledge Graph | [graph-based-note-discovery-weighted-bfs.md](./potential-adrs/done/RETRIEVAL/graph-based-note-discovery-weighted-bfs.md) |
| Retrieval Result Transparency (Hits vs Candidates) | Retrieval Architecture / API Design | [retrieval-result-transparency-hits-vs-candidates.md](./potential-adrs/done/RETRIEVAL/retrieval-result-transparency-hits-vs-candidates.md) |

### Harvest (HARVEST)

| Title | Category | Score | File |
|-------|----------|-------|------|
| Three-Layer Duplicate Detection Strategy for Source Ingestion | Data Architecture / Deduplication Strategy | 145 | [three-layer-duplicate-detection.md](./potential-adrs/done/HARVEST/three-layer-duplicate-detection.md) |
| Docling as Primary PDF Extractor with PyMuPDF Fallback | Infrastructure Service / Document Processing | 145 | [docling-pdf-extraction-with-pymupdf-fallback.md](./potential-adrs/done/HARVEST/docling-pdf-extraction-with-pymupdf-fallback.md) |
| Three-Layer Page Inference Strategy (Metadata → Regex → Interpolation) | Data Architecture / Page Locator Strategy | 135 | [three-layer-page-inference-strategy.md](./potential-adrs/done/HARVEST/three-layer-page-inference-strategy.md) |
| Hybrid Structural Chunking (H3-H6 Sections + LangChain Splitter) | Data Architecture / Document Segmentation Strategy | 140 | [structural-chunking-strategy.md](./potential-adrs/done/HARVEST/structural-chunking-strategy.md) |

### Extract (EXTRACT)

| Title | Category | Score | File |
|-------|----------|-------|------|
| Granular Literature Notes with Readable Names and Source Excerpts | Data Architecture / Vault Structure | 115 | [granular-literature-notes-readable-names-source-excerpts.md](./potential-adrs/done/EXTRACT/granular-literature-notes-readable-names-source-excerpts.md) |

### Garden (GARDEN)

| Title | Category | Score | File |
|-------|----------|-------|------|
| Taxonomy-First MOC Clustering with UMAP+HDBSCAN | Clustering Strategy / Knowledge Organization | 150 | [taxonomy-first-moc-clustering.md](./potential-adrs/done/GARDEN/taxonomy-first-moc-clustering.md) |
| Hub-Anchored MOC Pipeline as Complementary Strategy | Clustering Strategy / Knowledge Graph Organization | 135 | [hub-anchored-moc-pipeline.md](./potential-adrs/done/GARDEN/hub-anchored-moc-pipeline.md) |
| Single LLM Call Per Cluster with Intelligent Routing | Cost Optimization / Generation Strategy | 130 | [single-llm-call-cluster-routing.md](./potential-adrs/done/GARDEN/single-llm-call-cluster-routing.md) |

### Web (WEB)

| Title | Category | Score | File |
|-------|----------|-------|------|
| FastAPI + Server-Rendered Jinja2 Templates (No SPA/JS Build) | Primary Framework / Presentation Layer | 145 | [fastapi-server-rendered-jinja2.md](./potential-adrs/done/WEB/fastapi-server-rendered-jinja2.md) |

### LLM (LLM Integration Gateway)

| Title | Category | Score | File |
|-------|----------|-------|------|
| Multi-Provider LLM Strategy with Pluggable Gateway | Primary Framework / Provider Abstraction Strategy | 140 | [multi-provider-llm-strategy.md](./potential-adrs/done/LLM/multi-provider-llm-strategy.md) |
| System+Human Prompt Split for Provider-Agnostic Prompt Caching | API Protocol / LLM Prompt Architecture | 135 | [prompt-caching-system-human-split.md](./potential-adrs/done/LLM/prompt-caching-system-human-split.md) |

### CLI (Command-Line Interface)

| Title | Category | Score | File |
|-------|----------|-------|------|
| Typer + Rich as CLI Framework | Primary Framework / CLI Orchestration | 150 | [typer-rich-cli-framework.md](./potential-adrs/done/CLI/typer-rich-cli-framework.md) |

---

## Medium Priority ADRs (score 75-99)

> Five of these were promoted as well and moved to `potential-adrs/done/`
> (REVIEW x3, QA-WRITING x1, WEB x1). The rest are still awaiting a decision
> under `potential-adrs/consider/`.

### Infrastructure (INFRA)

| Title | Category | Score | File |
|-------|----------|-------|------|
| Contextvars-Based Cost Tracking for Cross-Module Observability | Observability / Instrumentation Pattern | 80 | [contextvars-cost-tracking.md](./potential-adrs/consider/INFRA/contextvars-cost-tracking.md) |

### Retrieval (RETRIEVAL)

| Title | Category | Score | File |
|-------|----------|-------|------|
| Separation of Retrieval from Deduplication Logic | Retrieval Architecture / System Boundaries | 88 | [separation-of-retrieval-from-deduplication.md](./potential-adrs/consider/RETRIEVAL/separation-of-retrieval-from-deduplication.md) |

### Connector (CONNECT)

| Title | Category | Score | File |
|-------|----------|-------|------|
| RAG-Based Permanent Note Generation | Architecture / RAG Pattern | 75 | [rag-based-permanent-note-generation.md](./potential-adrs/consider/CONNECT/rag-based-permanent-note-generation.md) |
| Prompt Injection Risk in Permanent Note Generation (Unmitigated) | Security / Design Trade-off | 55 | [prompt-injection-risk-unmitigated.md](./potential-adrs/consider/CONNECT/prompt-injection-risk-unmitigated.md) |

### Harvest (HARVEST)

| Title | Category | Score | File |
|-------|----------|-------|------|
| Docling Config Hash Strategy for Extracted Text Caching | Performance / Caching Strategy | 88 | [docling-config-hash-for-extract-caching.md](./potential-adrs/consider/HARVEST/docling-config-hash-for-extract-caching.md) |

### Extract (EXTRACT)

| Title | Category | Score | File |
|-------|----------|-------|------|
| Post-Approval Semantic Deduplication of Concepts | Data Architecture / Deduplication Strategy | 85 | [post-approval-semantic-deduplication-of-concepts.md](./potential-adrs/consider/EXTRACT/post-approval-semantic-deduplication-of-concepts.md) |

### Review (REVIEW)

| Title | Category | Score | File |
|-------|----------|-------|------|
| Confidence-Scored HITL Approval with Band-Based UX | Pipeline Architecture / HITL Strategy | 78 | [confidence-scored-hitl-approval.md](./potential-adrs/done/REVIEW/confidence-scored-hitl-approval.md) |
| Post-Approval Concept Deduplication Timing | Pipeline Architecture / Deduplication Strategy | 76 | [post-approval-concept-deduplication.md](./potential-adrs/done/REVIEW/post-approval-concept-deduplication.md) |
| Web/CLI Validation Asymmetry in Auto-Approve Threshold Enforcement | Security / Validation / Web/CLI Parity | 75 | [web-cli-validation-asymmetry.md](./potential-adrs/done/REVIEW/web-cli-validation-asymmetry.md) |

### QA-Writing (QA-WRITING)

| Title | Category | Score | File |
|-------|----------|-------|------|
| LangGraph StateGraph for Multi-Stage Article Orchestration | Orchestration Architecture / State Management | 65 | [langgraph-statgraph-article-orchestration.md](./potential-adrs/done/QA-WRITING/langgraph-statgraph-article-orchestration.md) |
| Bibliography ABNT Citation Formatting with Optional LLM-Merge | Data Architecture / Citation Management | 62 | [bibliography-abnt-citation-formatting.md](./potential-adrs/consider/QA-WRITING/bibliography-abnt-citation-formatting.md) |

### Web (WEB)

| Title | Category | Score | File |
|-------|----------|-------|------|
| SQLite-Backed Persistent Job Queue with Single Worker Thread | Architecture / Concurrency | 65 | [sqlite-backed-job-queue-single-worker.md](./potential-adrs/done/WEB/sqlite-backed-job-queue-single-worker.md) |

---

## Summary

- **Total Modules Analyzed**: 13 (INFRA, RETRIEVAL, CONNECT, HARVEST, EXTRACT, GARDEN, REVIEW, QA-WRITING, WEB, ASSETS, LLM, MANUAL-SYNC, CLI)
- **Modules Pending Analysis**: 1 (API)
- **High Priority (≥100)**: 22 ADRs
  - INFRA: 8 (sqlite+wal+fts5, chromadb, hybrid+rrf, yaml-first, dual-store, pydantic-v2, layered-hashing, repository-pattern)
  - RETRIEVAL: 2 (graph-expansion, hits-vs-candidates)
  - HARVEST: 4 (three-layer-dedupe, docling-extraction, page-inference, structural-chunking)
  - EXTRACT: 1 (granular-literature-notes)
  - GARDEN: 3 (taxonomy-first-clustering, hub-anchored-pipeline, single-llm-routing)
  - WEB: 1 (fastapi-server-rendered-jinja2)
  - LLM: 2 (multi-provider-llm-strategy, prompt-caching-system-human-split)
  - CLI: 1 (typer-rich-cli-framework)
- **Medium Priority (75-99)**: 12 ADRs
  - INFRA: 1 (contextvars-cost-tracking)
  - RETRIEVAL: 1 (dedupe-separation)
  - CONNECT: 2 (rag-based-permanent-note-generation, prompt-injection-risk-unmitigated)
  - HARVEST: 1 (docling-config-hash-caching)
  - EXTRACT: 1 (post-approval-semantic-deduplication)
  - REVIEW: 3 (confidence-scored-hitl, post-approval-dedup-timing, web-cli-validation-asymmetry)
  - QA-WRITING: 2 (langgraph-orchestration, bibliography-abnt)
  - WEB: 1 (sqlite-job-queue-single-worker)
- **Below Threshold (<75)**: 0 ADRs
  - MANUAL-SYNC: 0 (all 5 identified decisions are tactical implementations of existing INFRA patterns; scores range 42-65)
- **Total Identified**: 34 potential ADRs (plus 5 below-threshold decisions analyzed)

---

## RETRIEVAL Module Analysis Details

### Date Analyzed: 2026-08-30
**Analyzed Files**:
- `zettel/retrieval.py` (331 lines)
- `zettel/graph.py` (114 lines)
- `zettel/config.py` (RetrievalConfig, RelevanceFloorConfig sections)

### Key Architectural Decisions Identified

1. **Hybrid Retrieval with RRF (Score: 145)** ← COVERED by INFRA/hybrid-dense-bm25-retrieval.md
   - Combines ChromaDB dense vectors + SQLite FTS5 BM25
   - Reciprocal Rank Fusion (k=60) fusion strategy
   - Introduced: 2026-07-18 15:22:31 (commit 2d6ff27)
   - Includes: Absolute relevance floor with 4-step gating
   - Production bug fixed: BM25 stopword leak (commit ed22565, 2026-07-18 16:19:36)

2. **Graph-Based Note Discovery (Score: 120)** ← NEW ADR
   - Weighted BFS traversal over note_connections
   - Per-relation weighting (contradicts: 1.0, extends: 0.9, ..., related: 0.5)
   - Undirected edge treatment (symmetric traversal)
   - Exponential hop decay (0.5^(hop-1))
   - Max 1 hop by default
   - Optional enrichment layer (enabled by default)

3. **Retrieval Result Transparency (Score: 105)** ← NEW ADR
   - NoteSearchResult with `hits` (filtered) + `candidates` (raw pool)
   - Deliberate API design for transparency
   - Allows "hits empty but showing near-misses" scenarios
   - Each candidate carries `floor_reason` explanation
   - Used by ask (--show-context), article (fallback), sync (suggestions)

4. **Separation of Retrieval from Dedupe (Score: 88)** ← NEW ADR
   - Harvester layer-3 uses raw L2 distance (0.88), not RRF
   - Extractor dedupe uses raw L2 distance, not RRF
   - Deliberate separation for threshold independence
   - Different precision/recall trade-offs per use case
   - Prevents accidental coupling of search quality to dedupe quality

### Git History Context

| Commit | Date | Message | Impact |
|--------|------|---------|--------|
| 2d6ff27 | 2026-07-18 15:22:31 | Add hybrid retrieval (BM25+vector) and lightweight GraphRAG | Introduced RRF + graph expansion |
| ed22565 | 2026-07-18 16:19:36 | Add absolute relevance floor to hybrid retrieval, fix BM25 stopword leak | Added relevance floor + bug fix (BM25 rank cutoff) |
| daf62e0 | 2026-08-25 19:31:51 | fix(pipeline): enforce review before connect and remove legacy code | Later cleanup (not directly affecting retrieval) |

### Consumers of RETRIEVAL Module

| Consumer | Uses | Score Impact |
|----------|------|--------------|
| **ask.py** | `Retriever.search_notes()` — answer generation with context | High (central to Q&A) |
| **article.py** | `Retriever.search_notes()` — section context + graph expansion | High (central to articles) |
| **connector.py** | `Retriever.search_notes()` — RAG for permanent note generation | High (central to connect phase) |
| **sync.py** | `Retriever.search_notes()` — auto-suggestion generation | Medium (optional auto-features) |
| **harvester.py** | **DOES NOT use** — raw L2 distance for dedupe | N/A (deliberate separation) |
| **extractor.py** | **DOES NOT use** — raw L2 distance for overlap | N/A (deliberate separation) |

### Configuration Exposure

All decisions tunable via `config/config.yaml` under `retrieval:`:

```yaml
retrieval:
  mode: hybrid                    # "vector" or "hybrid"
  rrf_k: 60                       # RRF fusion constant
  
  graph_expansion:
    enabled: true
    max_hops: 1
    decay: 0.5
    max_neighbors: 10
    relation_weights:
      contradicts: 1.0            # Highest: embeddings weak here
      extends: 0.9
      depends_on: 0.9
      supports: 0.8
      exemplifies: 0.7
      related: 0.5
  
  relevance_floor:
    enabled: true
    min_vector_similarity: 0.70   # Main gate
    bm25_hit_bypasses_floor: true
    bm25_bypass_max_rank: 5       # Bug fix from ed22565
    absolute_min_similarity: 0.15  # Hard backstop
  
  ask:
    topk: 8
    max_context_notes: 8
    max_chars_per_note: 1500
  
  article:
    topk: 20
    max_context_notes: 24
    max_chars_per_note: 1200
    max_hops: 2                    # Deeper than ask
```

### Temporal Evolution

- **2026-07-18 (44 days ago)**: RRF + graph expansion introduced
- **2026-07-18 (same day)**: Relevance floor + bug fix added
- **2026-08-25**: Later cleanup, no changes to retrieval core
- **Current**: All decisions stable, no regressions, used in production

---

## Notes for Next Phase

### ADR Generation (Phase 3)
When generating formal ADRs from potential files:
1. **hybrid-dense-bm25-retrieval.md** (INFRA) — Already created; ready for generation
2. **graph-based-note-discovery-weighted-bfs.md** (RETRIEVAL) — New; ready for generation
3. **retrieval-result-transparency-hits-vs-candidates.md** (RETRIEVAL) — New; ready for generation
4. **separation-of-retrieval-from-deduplication.md** (RETRIEVAL) — New; ready for generation

### ADR Relationships to Document
- INFRA/hybrid-dense-bm25-retrieval → RETRIEVAL/graph-based-note-discovery (graph is optional enrichment on RRF)
- INFRA/hybrid-dense-bm25-retrieval → RETRIEVAL/retrieval-result-transparency (hits vs candidates expose the floor logic)
- RETRIEVAL/separation-of-retrieval-from-deduplication → INFRA/hybrid-dense-bm25-retrieval (explains why dedupe doesn't use RRF)

---

## CONNECT Module Analysis Details

### Date Analyzed: 2026-08-30
**Analyzed Files**:
- `zettel/connector.py` (636 lines)
- `tests/test_connector.py` (249 lines, covers RAG context formatting and connection resolution)
- Related: `zettel/retrieval.py`, `zettel/llm.py`

### Key Architectural Decisions Identified

1. **RAG-Based Permanent Note Generation (Score: 75)** ← NEW ADR
   - Uses Retriever (hybrid dense+BM25 search) for contextual evidence
   - RAG context split into two groups: embedding seeds (hop 0) vs. graph neighbours (hop ≥1)
   - Allows LLM to distinguish direct matches from typed connections
   - Introduced: 2026-07-18 15:22:31 (commit 2d6ff27, "Add hybrid retrieval (BM25+vector) and lightweight GraphRAG")
   - Stable for 1+ month; modifications focused on robustness (e.g., BM25 stopword leak fix)
   - Shared `Retriever` composition point with ask, article, sync modules

2. **Typed Bidirectional Backlinking with Inverse Relations (Score: 55)** ← BELOW THRESHOLD
   - Supports, contradicts, extends, depends_on, exemplifies, related
   - Inverse relation labels in PT-BR (e.g., "supports" → "suportado por")
   - Bidirectional: when ZTL-A connects to ZTL-B, auto-backlink is written to B
   - Managed blocks ensure manual edits outside backlinks are preserved
   - Introduced: stable since July 2026 (commit b50d307)

3. **Prompt Injection Risk (Unmitigated) (Score: 55)** ← NEW ADR (documented security concern)
   - `cand.thesis`, `cand.definition`, etc. originate from user-supplied sources
   - No sanitization of prompt delimiters before Prompt 2 interpolation
   - **Explicitly acknowledged in code** (lines 212-215): "if untrusted input is expected, sanitize prompt delimiters"
   - Applies to all permanent notes; same pattern in Prompt 1 (extractor)
   - Framed as conditional mitigation, not a bug (suggests deliberate trade-off)

4. **Deterministic LLM Response Caching (Prompt 2) (Score: 45)** ← BELOW THRESHOLD
   - Checksum-based caching: prompt_hash + filled_hash + model + temperature + language
   - Cache key covers full prompt (system + user), enabling cost recovery on retry
   - Introduced alongside Prompt 2 execution (2026-07-18 15:22:31)
   - Coordinates with `CostTracker` for observability

5. **Skip Re-Embedding Optimization (Score: <50)** ← BELOW THRESHOLD
   - Skips ChromaDB re-indexing when semantic checksum + embedding model unchanged
   - Saves embedding API calls on partial updates (e.g., refining existing notes)
   - Embedding input hash = `compute_embedding_input_hash(semantic_checksum, provider, model)`

6. **PT-BR Language Guard (Secondary LLM Call) (Score: <50)** ← BELOW THRESHOLD
   - Optional second LLM call when English spillover detected (3+ English markers)
   - Intended to improve Portuguese-language note quality
   - Reuses same LLM provider; not cached (separate from Prompt 2)

### Git History Context

| Commit | Date | Message | Impact |
|--------|------|---------|--------|
| 2d6ff27 | 2026-07-18 15:22:31 | Add hybrid retrieval (BM25+vector) and lightweight GraphRAG | Introduced RAG context pattern |
| ed22565 | 2026-07-18 16:19:36 | Add absolute relevance floor to hybrid retrieval, fix BM25 stopword leak | Shared RETRIEVAL decision |
| b50d307 | 2026-08-04 09:02:48 | Enhance relation type handling in connector and tests | Relation type normalization |
| 6e32ef4 | 2026-08-13 20:38:12 | feat(llm): add portable provider prompt caching via System+Human split | Prompt caching enhancement |
| 90a1d0e | 2026-08-25 20:32:12 | feat(connect): title in ZTL frontmatter; slug max 100 chars | Note metadata enhancement |
| 5d9b504 | 2026-08-29 17:15:35 | Implement Python-first Zettelkasten web interface with secure uploads, persistent worker queue, progress, review, dashboards, documentation, and tests | Web integration (most recent) |

### Test Coverage

**Covered by tests**:
- `_build_rag_context()` (lines 190-220 in test_connector.py) — splits embedding/graph hits
- `_resolve_connections()` (lines 42-77) — wiki-link generation with known/unknown notes
- `_inverse_relation()` (lines 27-39) — relation type mapping
- `_relation_type_value()` (lines 79-102) — Enum normalization

**Not covered**:
- `run_connect()` — main orchestration function
- `_process_candidate()` — core note generation loop (LLM call, caching, PT-BR guard)
- Prompt injection scenarios

### Consumers and Dependencies

| Dependent | Uses | Reason |
|-----------|------|--------|
| **web_app.py** | `run_connect()` | Dispatches to connector when "connect" job enqueued |
| **cli.py** | `run_connect()` | CLI `connect` command |
| **retrieval.py** | Shared `Retriever` | RRF + graph expansion for RAG context |
| **index.py** | `upsert_permanent_note()` | Persists generated notes to ChromaDB |
| **vault.py** | `safe_write_note()`, `safe_update_managed_blocks()` | Vault file I/O with managed blocks |
| **state.py** | `upsert_note()`, `upsert_concept()`, `upsert_note_connection()` | Persistence layer |

### Configuration Exposure

All tuning points are in `config/config.yaml`:

```yaml
linking:
  topk: 8                           # How many context notes to include in RAG

llm:
  model: gpt-4o                     # LLM used for Prompt 2
  temperature: 0.7
  provider: openai
  prompt_cache: true                # Enable/disable prompt caching hints

language: pt-br                      # Affects PT-BR guard detection
```

### Temporal Evolution

- **2026-07-18** (44 days ago): RAG pattern introduced with hybrid retrieval
- **2026-08-04**: Relation type handling refined
- **2026-08-13**: Prompt caching enhancement across providers
- **2026-08-25**: Note metadata improvements (title, slug)
- **2026-08-29**: Web interface integration
- **Current**: All core patterns stable; no regressions; production use

---

## HARVEST Module Analysis Details

### Date Analyzed: 2026-08-30
**Analyzed Files**:
- `zettel/harvester.py` (1894 lines)
- `zettel/paging.py` (251 lines)
- `zettel/assets.py` (media handling)
- `zettel/config.py` (HarvestConfig, ChunkingConfig sections)

### Key Architectural Decisions Identified

1. **Three-Layer Duplicate Detection (Score: 145)** ← MUST-DOCUMENT
   - Layer 1 (File Hash): SHA256 checksum for renamed file detection
   - Layer 2 (Extraction Hash): Normalized text hash for cross-format deduplication (PDF + Markdown)
   - Layer 3 (Semantic Similarity): ChromaDB vector search with configurable threshold (default 0.88)
   - Interactive vs. non-interactive decision routing (skip/continue/abort)
   - Introduced: 2026-07-04 (commit a542911)
   - Recorded per run in StateDB; visible in `zettel status` output

2. **Docling PDF Extraction with PyMuPDF Fallback (Score: 145)** ← MUST-DOCUMENT
   - Primary: Docling with GPU acceleration (CUDA 12.6) + image extraction support
   - Fallback: PyMuPDF for lightweight extraction (or when Docling unavailable)
   - Device detection: `cfg.device` (cuda/cpu), affects Docling accelerator
   - Image extraction: Conditional, enabled via `cfg.images.enabled`
   - Page mapping: PyMuPDF used to build page-number anchors for paging inference
   - Configuration: `pdf_extractor` selector in config.yaml

3. **Three-Layer Page Inference (Score: 135)** ← MUST-DOCUMENT
   - Layer 1 (Explicit Metadata): PyMuPDF page map (most accurate)
   - Layer 2 (Regex Patterns): Head/tail text scanning for page numbers (fallback)
   - Layer 3 (Interpolation): Linear estimation between known pages (final fallback)
   - Confidence levels: "explicit" | "inferred" | "unknown"
   - Book page offset: `page_in_book = page_in_file - content_start_file + content_start_book`
   - **Known bug**: Non-interactive mode has unreachable heuristic code (lines 1782-1789)

4. **Hybrid Structural Chunking (Score: 140)** ← MUST-DOCUMENT
   - Stage 1: Split by H1/H2 boundaries (chapters)
   - Stage 2: Split by H3-H6 (sub-sections) + LangChain recursive splitter with overlap
   - Configuration: `chunking.min_chars_per_chunk` (50), `chunking.max_chars_per_chunk` (2000), `chunking.chunk_overlap` (200)
   - Section path: Hierarchical heading names tracked per chunk (enables future section-based navigation)
   - Re-chunking: `zettel rechunk` re-applies without file re-extraction

5. **Docling Config Hash for Extract Caching (Score: 88)** ← CONSIDER
   - Hash: SHA256 of Docling-relevant config (device, images, chunking, pdf_extractor)
   - Usage: Detects config divergence; warns user if extraction may differ
   - Behavior: Warning-based (non-blocking); user manually runs rechunk if desired
   - Stored: `sources.docling_config_hash` in SQLite + SRC note frontmatter

### Git History Context

| Commit | Date | Message | Impact |
|--------|------|---------|--------|
| a542911 | 2026-07-04 13:36:11 | Implement three-layer duplicate detection for the Harvest phase | Three-layer dedup introduced |
| 5910df1 | 2026-07-31 14:22:45 | Add bibliographic metadata extraction and enhance harvest process | Docling/PyMuPDF extraction patterns |
| acb2915 | 2026-08-10 10:15:30 | Enhance README and configuration for image extraction and chunking | Config parameter tuning |
| daf62e0 | 2026-08-25 19:31:51 | fix(pipeline): enforce review before connect and remove legacy code | Later cleanup (not directly affecting harvest) |
| 5d9b504 | 2026-08-29 17:15:35 | Implement Python-first Zettelkasten web interface | Web integration of harvest job |

### Test Coverage

**Covered by tests**:
- `tests/test_harvester_dedup.py` (11 tests) — All three dedup layers + decision resolution
- `tests/test_harvester_sections.py` — Chunking logic for various document structures
- `tests/test_paging.py` — Page inference (all three layers)
- `tests/test_state.py` — StateDB dedup tracking methods

**Not covered**:
- Docling GPU acceleration path (CUDA device detection)
- Interactive duplicate prompts (Rich table rendering)
- Image extraction and multimodal LLM description

### Consumers and Dependencies

| Dependent | Uses | Reason |
|-----------|------|--------|
| **run_harvest()** | All HARVEST logic | Main entry point (CLI + web) |
| **run_rechunk()** | Chunking logic | Re-process without file extraction |
| **extractor.py** | `source_chunking_incomplete()` | Detects partial harvest |
| **review.py** | Chunk status fields | Integration with review pipeline |
| **web_app.py** | `run_harvest()` | Harvest job enqueue |
| **cli.py** | `run_harvest()`, `run_rechunk()` | CLI commands |

### Configuration Exposure

All tuning in `config/config.yaml`:

```yaml
harvest:
  duplicate_chunk_threshold: 0.88       # Layer 3 semantic threshold
  duplicate_sample_size: 5              # Chunks to sample for Layer 3
  non_interactive_duplicate_action: skip  # skip | continue | abort

chunking:
  min_chars_per_chunk: 50
  max_chars_per_chunk: 2000
  chunk_overlap: 200

pdf_extractor: docling                  # docling | pymupdf
device: cuda                            # cuda | cpu
images:
  enabled: true
  scale: 1.0
```

### Temporal Evolution

- **2026-07-04** (58 days ago): Three-layer dedup introduced
- **2026-07-31**: Docling + PyMuPDF extraction patterns formalized
- **2026-08-10**: Configuration tuning for images/chunking
- **2026-08-25**: Pipeline enforcement (review before connect)
- **2026-08-29**: Web integration of harvest workflow
- **Current**: All core patterns stable; three-layer dedup proved effective; paging inference has known bug

---

---

## EXTRACT Module Analysis Details

### Date Analyzed: 2026-08-30
**Analyzed Files**:
- `zettel/extractor.py` (639 lines)
- `zettel/review.py` (671 lines, related approval workflow)
- `zettel/vault.py` (literature note builders)
- `zettel/config.py` (ExtractionConfig, LiteratureReviewConfig sections)

### Key Architectural Decisions Identified

1. **Granular Literature Notes with Readable Names and Source Excerpts (Score: 115)** ← MUST-DOCUMENT
   - Vault structure: One literature note per chunk, not per source
   - Draft path: `00_Inbox/Review/{Citekey}/LIT - AuthorYear - pNNN - topic-NNNN.md`
   - Approved path: `20_Literature/{Citekey}/LIT - AuthorYear - pNNN - topic-NNNN.md`
   - Filename generation: Topic slug from LLM summary (first ~5 words) + short hash for uniqueness
   - Managed blocks: `zettel:auto-source-excerpt` preserves original chunk text
   - Lazy embedding: Literature notes indexed to Chroma only after approval (in REVIEW phase)
   - Breaking change from monolithic LIT-per-source model (commit 508d4c0, 2026-08-28)
   - Per-chunk metadata: `review_confidence`, `llm_model`, `processing_time_ms`, `literature_id` (ULID)

2. **Post-Approval Semantic Deduplication of Concepts (Score: 85)** ← CONSIDER
   - Timing: Deduplication runs after HITL approval, not during extraction
   - Process: Approved concepts (`status=extracted`) deduplicated before CONNECT phase
   - Threshold: `linking.dedupe_threshold` (default 0.85 similarity)
   - Deduplication logic: `deduplicate_candidates()` in extractor.py, called from review.py
   - LLM-based decision: Four-option routing (CREATE_NEW | IGNORE | REFINE_EXISTING | MERGE)
   - Separation from unified Retriever: Uses raw L2 distance, not RRF (deliberate isolation)
   - Status transitions: `awaiting_review` → `extracted` (post-approval) → `approved|duplicate`
   - Result annotation: Optional `refines_note_id` and `refine_reason` for merge/refine decisions

### Git History Context

| Commit | Date | Message | Impact |
|--------|------|---------|--------|
| 508d4c0 | 2026-08-28 20:01:06 | feat(vault): give literature notes readable names and source excerpts | Granular notes with readable filenames; managed-block source excerpts |
| 5d9b504 | 2026-08-29 17:15:35 | Implement Python-first Zettelkasten web interface | Web integration of extract workflow |
| 302208b | 2026-08-13 20:38:12 | Enhance literature review process and update pipeline phases | Related review infrastructure |

### Test Coverage

**Covered by tests**:
- `tests/test_extractor.py` — Core LLM drafting logic, candidate filtering, confidence scoring
- `tests/test_review.py` — Approval workflow, status transitions, literature index refresh
- `tests/test_vault.py` (+127 lines) — Filename generation, managed blocks, note building

**Not covered**:
- Interactive deduplication decisions (depends on LLM non-determinism)
- Confidence scoring heuristic tuning
- Image context integration in LLM prompts

### Consumers and Dependencies

| Dependent | Uses | Reason |
|-----------|------|--------|
| **run_extract()** | Core LLM drafting | Main entry point (CLI + web) |
| **review.py** | `approve_chunk()`, `deduplicate_candidates()` | Approval workflow triggers dedupe |
| **web_app.py** | `run_extract()` | Extract job enqueue |
| **cli.py** | `run_extract()` | CLI `extract` command |
| **state.py** | Chunk status, concept tracking | Persistence layer |
| **index.py** | Literature note embedding | Post-approval indexing |
| **vault.py** | Safe note writing, managed blocks | Vault file I/O |

### Configuration Exposure

All tuning in `config/config.yaml`:

```yaml
extraction:
  min_relevance_score: 3          # Candidates with relevance < 3 are filtered
  min_thesis_words: 5              # Minimum words in candidate thesis
  require_anchor_quote: true        # Discard candidates without anchor quotes
  min_definition_words: 10          # Minimum words in candidate definition

literature_review:
  auto_approve_min_confidence: 0.85 # Threshold for high-confidence auto-approve
  batch_sample_size: 20             # Max drafts shown in interactive review
  drafts_subdir: 00_Inbox/Review    # Draft staging directory

linking:
  dedupe_threshold: 0.85            # Similarity threshold for dedupe checks
  topk: 5                           # Number of context notes for dedupe LLM
```

### Temporal Evolution

- **2026-08-28** (2 days ago): Granular notes architecture introduced (breaking change)
- **2026-08-29**: Web integration of extract workflow
- **Current**: Core patterns newly introduced; stable for 2 days; no regressions yet

### Phase Boundaries

The EXTRACT module is tightly coupled with REVIEW:
- EXTRACT writes drafts → REVIEW approves and invokes dedupe → CONNECT consumes approved concepts
- Deduplication implementation lives in extractor.py but is called from review.py
- Concept status field (StateDB) is the boundary: `awaiting_review` (extract output) → `extracted|approved|duplicate` (review output)

---

---

## GARDEN Module Analysis Details

### Date Analyzed: 2026-08-30
**Analyzed Files**:
- `zettel/gardener.py` (892 lines)
- `zettel/gardener_assign.py` (283 lines)
- `zettel/gardener_hub.py` (625 lines)
- `zettel/moc_backrefs.py` (MOC backlink maintenance)
- `zettel/taxonomy.py` (taxonomy loading and validation)
- `zettel/config.py` (GardenerConfig, HubMocsConfig sections)

### Key Architectural Decisions Identified

1. **Taxonomy-First MOC Clustering with UMAP+HDBSCAN (Score: 150)** ← MUST-DOCUMENT
   - Strategy: Embed category labels → assign notes to category buckets → cluster within each bucket → route to LLM
   - Category assignment: Cosine similarity between note embeddings and category label embeddings
   - Clustering: UMAP (n_neighbors=15, n_components=5, cosine metric) + HDBSCAN (min_cluster_size configurable)
   - Fallback chain: UMAP+HDBSCAN → KMeans (if optional packages missing)
   - Silent fallback: No explicit signal when UMAP/HDBSCAN unavailable (mapping.md notes this as a risk)
   - Configuration: `cluster_within_category` (default true), `category_label_template`, `umap_n_neighbors`, `hdbscan_min_samples`
   - Introduced: 2026-08-26 (commit 216a725)
   - Stable for 4 days with no rollbacks

2. **Hub-Anchored MOC Pipeline as Complementary Strategy (Score: 135)** ← MUST-DOCUMENT
   - Strategy: Rank notes by weighted graph degree → expand neighborhoods via BFS → dedup overlaps → route to LLM
   - Hub selection: Percentile-based (top N%) or absolute degree threshold
   - Neighborhood expansion: BFS with configurable hop limits, relation weights, and decay (0.5 default)
   - Deduplication: Drops smaller hubs whose neighborhoods are ≥ threshold overlapped by larger hubs
   - Origin tagging: `origin='hub_pipeline'` allows selective `--hubs --recreate` purge
   - Configuration: `selection_mode`, `hub_percentile`, `top_n_hubs`, `max_hops`, `max_neighbors`, `decay`, `dedup_subset_threshold`
   - Introduced: 2026-08-27 (commit 930cb75)
   - Stable for 3 days; dual-pipeline design intentional

3. **Single LLM Call Per Cluster with Intelligent Routing (Score: 130)** ← MUST-DOCUMENT
   - Routing decision tree: signature match → overlap detection → category match → cohesion gate → generation
   - Path outcomes:
     - Signature match: Zero LLM calls (reuse existing MOC)
     - Overlap/Category match: One LLM call (incremental update via `moc_incremental.md`)
     - Cohesion rejection: Zero LLM calls (silently drop cluster)
     - Generation: One LLM call (new MOC via `moc_generation.md`)
   - Cost implication: Linear in cluster count, not exponential in refinement rounds
   - Incremental updates: Preserve existing MOC structure, classifying new notes into existing subsections
   - Introduced: 2026-08-26 (commit 216a725)
   - Stable for 4 days; cost-control design intentional

### Git History Context

| Commit | Date | Message | Impact |
|--------|------|---------|--------|
| 216a725 | 2026-08-26 21:18:46 | feat(garden): hybrid MOC pipeline with taxonomy-first clustering | Taxonomy-first + single-LLM-call routing |
| 930cb75 | 2026-08-27 20:21:06 | feat(garden): add hub-anchored MOC pipeline via garden --hubs | Hub-anchored strategy introduced |
| 9ea8889 | 2026-08-29 22:35:18 | chore(gardener-hub): Change of type from moc to hub_moc | Type naming refinement |
| e8c1b8a | 2026-08-30 14:22:05 | feat(cli): add new-note and delete-source with MOC backrefs | MOC backref maintenance |

### Test Coverage

**Covered by tests**:
- `tests/test_gardener_assign.py` — Clustering logic, category assignment, graph cohesion scoring
- `tests/test_gardener.py` — MOC generation, routing logic, incremental updates
- `tests/test_gardener_hub.py` — Hub ranking, neighborhood expansion, deduplication

**Not covered**:
- Interactive overlap resolution prompts
- Taxonomy validation edge cases
- Silent fallback behavior (UMAP/HDBSCAN → KMeans)

### Consumers and Dependencies

| Dependent | Uses | Reason |
|-----------|------|--------|
| **run_garden()** | Core taxonomy pipeline | Main entry point (CLI + web) |
| **run_hub_garden()** | Hub pipeline orchestration | Called when `--hubs` flag set |
| **web_app.py** | `run_garden()`, `run_hub_garden()` | Garden job enqueue |
| **cli.py** | `run_garden()`, `run_hub_garden()`, `purge_pipeline_mocs()` | CLI commands |
| **moc_backrefs.py** | MOC → permanent note linking | Called after each MOC generation |
| **state.py** | MOC storage, cluster signature tracking | Persistence layer |
| **index.py** | MOC embedding | ChromaDB indexing |
| **vault.py** | Safe MOC file writing | Vault file I/O |
| **graph.py** | `expand_notes()` for hub expansion | Shared graph traversal |

### Configuration Exposure

All tuning in `config/config.yaml`:

```yaml
gardener:
  cluster_within_category: true           # Enable/disable taxonomy-first
  category_label_template: "{domain}: {categoria}"
  topics_path: ./config/moc_topics.yaml   # Taxonomy structure
  domain: "Geral"                         # Knowledge base domain
  strict_topics: true                     # Reject MOCs outside taxonomy
  overlap_threshold: 0.4                  # Incremental update trigger
  graph_cohesion_enabled: true            # Optional quality gate
  graph_cohesion_min_ratio: 0.0           # Minimum cohesion to create MOC
  min_cluster_size: 3                     # HDBSCAN min_cluster_size
  min_notes_for_moc: 3                    # Minimum notes per MOC
  umap_n_neighbors: 15                    # UMAP neighborhood size (optional)
  hdbscan_min_samples: null               # HDBSCAN min_samples (optional)

hub_mocs:
  selection_mode: percentile              # "percentile" or "absolute"
  hub_percentile: 0.90                    # Top N% for percentile mode
  min_weighted_degree: 8.0                # Minimum for absolute mode
  top_n_hubs: 20                          # Maximum hubs to generate
  max_hops: 2                             # BFS hop limit
  max_neighbors: 25                       # BFS neighbor limit
  min_neighbors: 3                        # Minimum neighbors for MOC
  decay: 0.5                              # Hop weight decay (0.5^hop)
  min_neighbor_weight: 0.3                # Minimum neighbor relevance
  dedup_subset_threshold: 0.8             # Hub deduplication threshold
```

### Temporal Evolution

- **2026-08-26** (4 days ago): Taxonomy-first clustering + single-LLM-call routing introduced
- **2026-08-27** (3 days ago): Hub-anchored complementary strategy introduced
- **2026-08-29**: Type naming and MOC backref maintenance refinements
- **2026-08-30** (today): Analysis completed; all patterns stable
- **Current**: Dual MOC pipelines operational; no rollbacks or fundamental rework

### Architectural Coupling

- **gardener_hub.py duplication**: Reaches into ~11 private symbols of gardener.py (mapping.md notes this); consolidation opportunity for shared routing logic
- **Taxonomy dependency**: MOC generation depends on `config/moc_topics.yaml` validity; missing/invalid taxonomy causes fast failure
- **Graph dependency**: Hub pipeline requires well-formed `note_connections` graph; sparse graphs produce few hubs
- **Origin-based scoping**: Pipeline MOCs (`origin='pipeline'`) vs. hub MOCs (`origin='hub_pipeline'`) allow selective operations (e.g., `--hubs --recreate`)

---

## REVIEW Module Analysis Details

### Date Analyzed: 2026-08-30
**Analyzed Files**:
- `zettel/review.py` (671 lines)
- `zettel/web.py` (relevant review routes, ~20 lines)
- `zettel/web_app.py` (review job dispatch, ~30 lines)
- `zettel/config.py` (LiteratureReviewConfig section)

### Key Architectural Decisions Identified

1. **Confidence-Scored HITL Approval with Band-Based UX (Score: 78)** ← NEW ADR
   - Three confidence bands guide operator focus: very_low (≤0.4), medium (0.4<conf<limiar), high (≥limiar)
   - Multiple review modes: batch approve (>=threshold), batch reject (by band), one-by-one (sample review)
   - Non-interactive path enforces threshold server-side: `if conf >= limiar: approve_chunk()`
   - Interactive path allows operator override (approval operators decide within HITL)
   - Introduced: 2026-08-29 17:15:35 (commit 5d9b504, web interface implementation)
   - Stable for ~1 day; recent enough to still be in active design space

2. **Post-Approval Concept Deduplication Timing (Score: 76)** ← NEW ADR
   - Concepts generated during EXTRACT with `status=awaiting_review`
   - Upon chunk approval, concepts promoted to `status=extracted`
   - After batch approval completes, `_dedupe_approved_concepts()` calls `extractor.deduplicate_candidates()`
   - Result: concepts marked `status=approved` (winners) or eliminated (merge losers)
   - Timing rationale: defer LLM cost until after human filters out low-confidence drafts
   - Introduced: 2026-08-29 (part of review refinements)

3. **Web/CLI Validation Asymmetry in Auto-Approve Threshold (Score: 75)** ← NEW ADR (documented inconsistency)
   - **CLI path** (review.py:194-206): Server-side validation — `if conf >= limiar: approve_chunk()`
   - **Web path** (web.py:458-462 + web_app.py): Client-side filtering + no server-side re-validation
   - Web `/review/action` endpoint enqueues `approve_chunk()` without threshold check
   - Consequence: web UI can approve below-threshold chunks if manually crafted POST request
   - Mapping.md explicitly flags as "candidate worth examining...for either a fix or an ADR on intended web/CLI parity"
   - Status: Unclear whether intentional or oversight

### Git History Context

| Commit | Date | Message | Impact |
|--------|------|---------|--------|
| 5d9b504 | 2026-08-29 17:15:35 | Implement Python-first Zettelkasten web interface with secure uploads, persistent worker queue, progress, review, dashboards, documentation, and tests | Web integration of REVIEW module |
| eee... | 2026-08-29 11:11:15 | feat(review): improve HITL bands, purge rejected, and VACUUM | HITL refinement |
| daf62e0 | 2026-08-25 19:31:51 | fix(pipeline): enforce review before connect and remove legacy code | Pipeline ordering enforcement |

### Test Coverage

**Covered by tests**:
- Basic batch approve/reject flows (implicit, via integration tests)
- Confidence band filtering logic

**Not covered**:
- Interactive mode prompts and menu flows (Rich rendering, terminal interaction)
- Web endpoint validation paths
- Dedup integration after approval

### Consumers and Dependencies

| Dependent | Uses | Reason |
|-----------|------|--------|
| **cli.py** | `run_review()` | CLI `review` command with multiple modes |
| **web_app.py** | `approve_chunk()`, `reject_chunk()`, `finalize_approved_concepts()` | Web job dispatch |
| **web.py** | Confidence band filtering (client-side) | Web `/review` GET + `/review/action` POST routes |
| **extractor.py** | `deduplicate_candidates()` | Post-approval dedup call |
| **index.py** | `upsert_literature_note()` | Approved notes embedded to Chroma |
| **state.py** | `update_chunk_review()`, `update_concept_status()` | Status persistence |

### Configuration Exposure

All tuning in `config/config.yaml`:

```yaml
literature_review:
  auto_approve_min_confidence: 0.7        # Threshold for batch approve
  batch_sample_size: 20                   # Chunk sample size for interactive review
```

### Temporal Evolution

- **2026-08-25** (5 days ago): Pipeline enforcement (review required before connect)
- **2026-08-29** (1 day ago): Web interface integrated; HITL improvements (bands, purge rejected)
- **2026-08-30** (today): Analysis completed; all patterns operational

### Known Considerations

- **Documented inconsistency**: Web/CLI asymmetry in threshold enforcement (noted above)
- **PT-BR language**: All UI labels, status values, and logging in Portuguese (limiar, banda, confianca, etc.)
- **Status transitions**: Concepts flow through awaiting_review → extracted → approved (post-dedup)
- **Interactive flexibility**: One-by-one mode allows operator override even if below-threshold

---

---

## QA-WRITING Module Analysis Details

### Date Analyzed: 2026-08-30
**Analyzed Files**:
- `zettel/ask.py` (314 lines)
- `zettel/article.py` (1161 lines)
- `zettel/article_graph.py` (715 lines)
- `zettel/bibliography.py` (836 lines)
- Related: `zettel/retrieval.py`, `zettel/llm.py`, `zettel/config.py`

### Key Architectural Decisions Identified

1. **LangGraph StateGraph for Multi-Stage Article Orchestration (Score: 65)** ← NEW ADR
   - Uses LangGraph StateGraph with 13 nodes for complex multi-stage writing (query enrichment → search → HITL outline/context → draft → assemble → personality → judge loop → verify)
   - Distinct from staged pipeline pattern (SQLite status fields) used by phases 1-4
   - Enables `interrupt()` primitives for HITL (context and outline approval stages)
   - Supports bounded judge loop for iterative refinement
   - Introduced: 2026-08-04 16:31:38 (commit 64c5346)
   - Stable: 26 days, only minor enhancements (aborted state handling, prompt caching)
   - State persistence via MemorySaver checkpointer

2. **Bibliography ABNT Citation Formatting with Optional LLM-Merge (Score: 62)** ← NEW ADR
   - Implements ABNT NBR 6023:2018 Brazilian citation standard for in-text and end-of-article references
   - Handles author normalization, multiple edition consolidation, DOI/URL integration
   - Optional LLM-based merge path for intelligent source consolidation (UNTESTED)
   - Introduced: 2026-07-30 19:02:20 (commit 5910df1)
   - Integrated into article generation: 2026-08-04 (commit 64c5346)
   - Test gap: LLM-merge logic explicitly skipped in all test fixtures

3. **Ask Module Deterministic "No Evidence" Pattern (Score: <50)** ← BELOW THRESHOLD
   - When retrieval returns no hits (nothing clears relevance floor), skip LLM call entirely
   - Answer deterministically with fixed Portuguese message instead of hallucinating
   - Candidates pool still populated for transparency/debugging
   - This is a consequence of INFRA/hybrid-dense-bm25-retrieval (relevance floor already documented)

4. **Multi-Query Incremental Search Merging (Score: <50)** ← BELOW THRESHOLD
   - Article graph runs multiple search queries sequentially
   - Merges results by note_id (avoids duplicate context across queries)
   - Optimization detail rather than architectural decision

### Git History Context

| Commit | Date | Message | Impact |
|--------|------|---------|--------|
| 2d6ff27 | 2026-07-18 15:22:31 | Add hybrid retrieval (BM25+vector) and lightweight GraphRAG | Ask command foundation (Retriever) |
| 5910df1 | 2026-07-30 19:02:20 | Add bibliographic metadata extraction and enhance harvest process | Bibliography pattern introduced |
| 64c5346 | 2026-08-04 16:31:38 | Add article generation capabilities and enhance retrieval process | Article + LangGraph orchestration |
| 6e01586 | 2026-08-04 16:46:00 | Add aborted state handling in article generation | State machine refinement |
| 6e32ef4 | 2026-08-13 20:38:12 | feat(llm): add portable provider prompt caching via System+Human split | Prompt caching for article LLM calls |

### Test Coverage

**Covered by tests**:
- `test_ask.py` — Answer generation, source provenance, cache hits/misses, floor reasoning
- `test_article.py` — Outline generation, section drafting, judge loop, personality rewrite, assembly
- `test_article_graph.py` — State transitions, node routing, HITL interrupts
- `test_bibliography.py` — ABNT formatting (in-text, full reference), author normalization

**Not covered**:
- Bibliography LLM-merge path (explicitly skipped with `skip_llm_merge=True` in fixtures)
- Article generation with live Docling image extraction
- Complete article-to-vault save workflow

### Consumers and Dependencies

| Dependent | Uses | Reason |
|-----------|------|--------|
| **cli.py** | `run_ask()`, `run_article()` | CLI `ask` and `article` commands |
| **web_app.py** | `run_article()` | Article job enqueue for web interface |
| **retrieval.py** | Shared `Retriever` | Core search infrastructure |
| **index.py** | `upsert_permanent_note()` | Saves generated/article notes to Chroma |
| **state.py** | `upsert_note()`, `upsert_source()` | Persistence layer |
| **vault.py** | `safe_write_note()` | Vault file I/O |

### Configuration Exposure

All tuning in `config/config.yaml`:

```yaml
retrieval:
  ask:
    topk: 8                          # Search result limit
    max_context_notes: 8
    max_chars_per_note: 1500
  
  article:
    topk: 20                         # Higher than ask (more context for long-form)
    max_context_notes: 24
    max_chars_per_note: 1200
    max_hops: 2                      # Deeper graph expansion
    max_sections: 10
    chars_per_section_draft: 800
    writer_temperature: 0.7          # Slightly higher for creative writing
    max_judge_iterations: 3          # Bounded refinement loop

language: pt-br                      # ABNT is PT-BR specific
```

### Temporal Evolution

- **2026-07-18** (44 days ago): Ask command foundation (Retriever, hybrid search)
- **2026-07-30** (31 days ago): Bibliography infrastructure introduced
- **2026-08-04** (26 days ago): Article generation with LangGraph orchestration
- **2026-08-13** (17 days ago): Prompt caching enhancements
- **2026-08-30** (today): Analysis completed; all patterns stable

---

## WEB Module Analysis Details

### Date Analyzed: 2026-08-30
**Analyzed Files**:
- `zettel/web.py` (622 lines, 23 HTTP endpoints + SSE stream)
- `zettel/web_app.py` (404 lines, application layer, job orchestration)
- `zettel/markdown.py` (content rendering with wikilink support)
- `zettel/templates/*` (14 Jinja2 templates)
- `zettel/static/*` (3 CSS stylesheets)
- Related: `zettel/config.py` (no HTTP-specific config)

### Key Architectural Decisions Identified

1. **FastAPI + Server-Rendered Jinja2 Templates (No SPA/JS Build) (Score: 145)** ← MUST-DOCUMENT
   - Framework: FastAPI + Uvicorn (single-process, no separate frontend service)
   - Templating: Jinja2 with 14 templates (base.html, dashboard, documents, pipeline, review, notes, mocs, runs, settings, etc.)
   - No JavaScript framework: No React/Vue/Angular, no npm, no webpack, no TypeScript
   - Form handling: HTML forms with server-side validation, POST/redirect pattern
   - Presentation consistency: All web logic written in Python (FastAPI routes → Jinja context → HTML)
   - Introduced: 2026-08-29 17:15:35 (commit 5d9b504: "Implement Python-first Zettelkasten web interface...")
   - Stable for ~1 day; no architectural rework yet
   - Cost to change: 6+ months (SPA migration)

2. **SQLite-Backed Persistent Job Queue with Single Worker Thread (Score: 65)** ← CONSIDER
   - Pattern: Daemon thread + SQLite persistence (web_jobs, web_job_events tables)
   - Job lifecycle: queued → running → succeeded/failed/interrupted
   - Concurrency model: Single active job; concurrent submit → 409 (Conflict)
   - Recovery: Interrupted jobs re-queued on startup; queued jobs resume
   - Progress reporting: Events emitted via JobProgress, persisted to DB for SSE streaming
   - Introduced: 2026-08-29 (same commit as FastAPI integration)
   - No external queue dependency (Celery/RQ) — keeps system self-contained
   - Cost to change: 3-4 weeks (refactor to Celery/RQ + external broker)

3. **Server-Sent Events (SSE) for Real-Time Job Progress** (Score: <50)** ← BELOW THRESHOLD
   - Unidirectional streaming from server → client
   - Simpler than WebSockets; sufficient for progress display
   - Non-blocking: /runs/{job_id} polls DB; /progress/{job_id} streams events
   - This is a consequence of the job queue pattern, not independent decision

4. **HMAC-Signed HttpOnly Session Cookies + Per-Request CSRF Tokens** (Score: ~40)** ← BELOW THRESHOLD
   - Stateless verification: Base64-encoded CSRF + timestamp + HMAC-SHA256 signature
   - SESSION_SECRET from process environment only (never config.yaml)
   - Timing-safe comparison via hmac.compare_digest
   - Per-form CSRF token on mutating routes
   - This is a security implementation detail; foundational pattern is Repository/Layered Architecture

5. **Two-Layer Markdown Sanitization (markdown-it + bleach)** (Score: ~30)** ← BELOW THRESHOLD
   - First: markdown-it parser with HTML disabled + custom ztl_wikilink rule
   - Second: bleach.clean() with allowlist (defensive programming)
   - Converts [[ZTL-UUID-slug]] wikilinks to `/notes/UUID` internal links
   - This is a content-rendering security detail, not architectural

6. **Upload Validation with Extension Allowlist + Path-Traversal Guards** (Score: ~30)** ← BELOW THRESHOLD
   - Allowed: .pdf, .md, .markdown, .txt
   - Size cap: 25MB
   - Path safety: relative_to() for traversal prevention
   - This is input validation detail, not architectural

### Git History Context

| Commit | Date | Message | Impact |
|--------|------|---------|--------|
| 5d9b504 | 2026-08-29 17:15:35 | Implement Python-first Zettelkasten web interface with secure uploads, persistent worker queue, progress, review, dashboards, documentation, and tests | Core web interface introduced |
| 9a16045 | 2026-08-30 19:02:16 | Fix harvest completion feedback, PDF extraction fallback, pipeline prerequisites, and job status updates | Job status tracking refinement |
| fe7bf5c | 2026-08-31 10:15:22 | Update dependencies and expand web test coverage | Test expansion |
| 9689075 | 2026-09-02 11:31:45 | fix(web): align VectorIndex kwargs and settings embedding identity | Embedding config alignment |
| 4c321c0 | 2026-09-03 14:22:15 | Implement markdown link rendering and update project dependencies | Markdown wikilink support |

### Test Coverage

**Covered by tests**:
- `tests/test_web.py` — Route fixtures, form handling, response validation
- `tests/test_web_state.py` — Job queue creation, status transitions, recovery

**Not covered**:
- Job execution paths (worker._execute actually runs pipeline operations)
- Interactive authentication flows (login form, session expiry)
- Real-time SSE event streaming
- Concurrent job submission (409 response)

### Consumers and Dependencies

| Dependent | Uses | Reason |
|-----------|------|--------|
| **web.py** | HTTP endpoints, form handling | Request/response layer |
| **web_app.py** | Job queue, dispatch logic | Business logic (no HTTP) |
| **cli.py** | Independent of web | CLI entry point |
| **harvester.py** (+ others) | Invoked by web_app dispatch | Same pipeline modules as CLI |
| **state.py** | Job table CRUD | Persistence |
| **index.py** | Shared by web jobs | Same indexing as CLI |

### Configuration Exposure

No HTTP-specific config (FastAPI + Jinja are hard-coded). Session SECRET from environment:

```bash
SESSION_SECRET=my_secret_key uvicorn zettel.web:app
```

Job queue parameters are implicit (SQLite path from config.yaml, worker thread count = 1):

```yaml
state_db_path: ./data/state.db  # web_jobs table lives here
```

### Temporal Evolution

- **2026-08-29**: Initial introduction (single large commit covering entire web layer)
- **2026-08-30**: Bug fixes (job status reporting)
- **2026-08-31**: Test expansion (17 test methods added)
- **2026-09-02**: Embedding config alignment
- **2026-09-03**: Markdown wikilink rendering
- **Current**: All patterns stable; no major reworks; incremental bug fixes/improvements

### Known Considerations

- **Python-only frontend**: All presentation logic in Python; no JS experts needed on team
- **Monolithic deployment**: Web + worker threads + SQLite in single process; no microservices
- **No real-time collaboration**: SSE is unidirectional; no WebSocket bidirectional communication
- **Single-machine assumption**: Job queue design assumes single server instance; multi-instance deployments would require rethinking
- **Durability without external dependencies**: SQLite WAL provides job persistence without Redis/RabbitMQ
- **Synchronous execution**: Worker thread blocks on pipeline operations; long operations (harvest, garden) block other jobs

---

---

## ASSETS Module Analysis Details

### Date Analyzed: 2026-08-30
**Analyzed Files**:
- `zettel/assets.py` (580 lines)
- `zettel/chunk_dump.py` (218 lines)
- `zettel/extraction_dump.py` (200 lines approx)

### Key Architectural Decisions Evaluated

1. **Content-Addressed Image Storage for Deterministic Deduplication (Score: 40)** ← BELOW THRESHOLD
   - SHA256 hash-based filenames enable deterministic extraction across runs
   - Images stored under `90_Assets/img-{short_hash(checksum)}.{ext}`
   - Path rewriting integrated into chunk text during extraction
   - Dedup naturally achieved through content addressing
   - Introduced: 2026-07-18 (commit 2d6ff27, image extraction feature)
   - Rationale: Ensures identical images across harvest runs reuse existing files

2. **Multimodal LLM Image Description with Sophisticated Rate-Limit Handling (Score: 45)** ← BELOW THRESHOLD
   - Provider-agnostic multimodal LLM client (OpenAI, Anthropic, Gemini, Ollama)
   - Rate-limit detection across exception hierarchies + provider message parsing
   - Exponential backoff with provider wait-hint preference
   - Consecutive exhaustion tracking with configurable abort threshold
   - Deterministic LLM cache by call checksum (prompt_hash + image_hash + context_hash + model)
   - Introduced: 2026-08-29 (commit 5d9b504, web interface integration)
   - Rationale: Avoid cascading failures and TPM saturation; enable cost recovery via caching

3. **Metadata Attachment via Chapter Resolution (Score: 25)** ← BELOW THRESHOLD
   - Images registered in StateDB with chapter_id resolved by path matching
   - Re-resolution support after rechunk operations (`reresolve_asset_chapters()`)
   - Fallback: Deterministic path-based asset lookup when chapter missing
   - Scope: Limited to asset registration, not a primary architectural choice

4. **Diagnostic Export Utilities - Opt-in Dumps (Score: <50)** ← BELOW THRESHOLD (Red Flag 4)
   - `chunk_dump.py`: Markdown export of persisted chunks with overlap analysis
   - `extraction_dump.py`: Markdown export of raw extracted_text with heading detection
   - Read-only, non-processing utilities for debugging/auditing
   - Trivial scope: Affects only diagnostic files, zero impact on pipeline
   - Introduced: 2026-08-27 (chunk dump) and 2026-08-28 (extraction dump)

5. **Provider Logic Duplication in assets.py (Code Organization Issue)** ← NOT ADR-WORTHY
   - `_get_multimodal_llm()` duplicates `llm.py` provider branching logic
   - Reaches into private internals: `is_openai_compatible()`, `normalize_llm_provider()`
   - Mapping.md notes: "candidate for 'should this be a shared internal API' note in Phase 2, likely below the ADR bar"
   - This is a refactoring/code-organization opportunity, not an architectural decision

### Scoring Analysis

**Why all decisions score below 75:**

| Decision | Scope+Impact | Cost to Change | Team Knowledge | Total | Reason Below Threshold |
|----------|---|---|---|---|---|
| Content-addressed storage | 20 | 15 | 10 | 45 | Optional image feature; images not mandatory for Zettelkasten notes |
| Rate-limit handling | 20 | 15 | 10 | 45 | Image description is optional; handling is implementation detail |
| Chapter resolution | 10 | 10 | 5 | 25 | Metadata association, limited scope |
| Diagnostic dumps | 5 | 5 | 5 | 15 | Optional utilities, zero pipeline impact |

**Key observation**: Image handling is optional infrastructure. The system functions correctly without images (e.g., text-only documents). This fundamentally limits the scope and impact scoring.

### Git History Context

| Commit | Date | Message | Impact |
|--------|------|---------|--------|
| 2d6ff27 | 2026-07-18 15:22:31 | Add hybrid retrieval (BM25+vector) and lightweight GraphRAG | Image extraction feature introduced |
| 2026-08-27 21:32:09 | 2026-08-27 | feat(harvest): add opt-in markdown dump of persisted chunks | Chunk dump diagnostic utility |
| 2026-08-28 21:56:40 | 2026-08-28 | feat(harvest): add opt-in dump of extracted Markdown | Extraction dump diagnostic utility |
| 5d9b504 | 2026-08-29 17:15:35 | Implement Python-first Zettelkasten web interface... | Image description integration |

### Test Coverage

**Not covered in test suite**:
- Image extraction paths (PDF/Markdown)
- Multimodal LLM description calls
- Rate-limit retry logic
- Provider-specific image description (GPT-4V, Claude Vision, etc.)

**Covered implicitly**:
- Chunk dump rendering (via vault tests)
- Extraction dump rendering (via vault tests)

### Dependencies and Consumers

| Dependent | Uses | Reason |
|-----------|------|--------|
| **harvester.py** | `extract_markdown_images()`, `extract_docling_images()`, `register_assets()` | Image extraction during harvest |
| **extractor.py** | `describe_pending_assets()` | Image description during extract phase |
| **connector.py** | `asset_ids_in_text()` | Image lookup for context |
| **cli.py** | `dump_chunks()`, `dump_extraction()` | Diagnostic export commands |
| **state.py** | Asset table CRUD | Persistence layer |
| **vault.py** | Asset reference handling | Vault path management |

### Configuration Exposure

All image handling tunable via `config/config.yaml`:

```yaml
images:
  enabled: true                         # Master switch for image extraction
  scale: 1.0                            # Image scaling factor
  min_width: 100                        # Minimum image width to extract
  min_height: 100                       # Minimum image height to extract
  context_chars: 200                    # Context window for image description
  model: gpt-4-vision                   # Multimodal LLM (default: llm.model)
  min_interval_seconds: 0.5             # Rate-limit pacing
  rate_limit_max_retries: 3             # Max retries on 429
  rate_limit_backoff_max: 60.0          # Max backoff seconds
  rate_limit_abort_after: 5             # Consecutive 429s before abort
```

### Temporal Evolution

- **2026-07-18** (44 days ago): Image extraction introduced (primary feature)
- **2026-08-27** (3 days ago): Chunk dump diagnostic utility added
- **2026-08-28** (2 days ago): Extraction dump diagnostic utility added
- **2026-08-29** (1 day ago): Image description integration via web interface
- **2026-08-30** (today): Analysis completed

### Conclusion: Why No ADRs

The ASSETS module exhibits **solid architectural decisions** but falls short of the ADR threshold because:

1. **Image handling is optional**: Zettelkasten notes work without images (text-only documents are fully supported)
2. **Each decision is well-scoped**: Image storage, description, metadata attachment are isolated concerns
3. **Limited team impact**: Only engineers working with multimedia documents need deep understanding
4. **Moderate cost to change**: Individual decisions could be refactored with 2-4 weeks effort, not the 6+ month migrations required by truly foundational choices (e.g., SQLite → PostgreSQL)
5. **No system-wide coupling**: Failures in image handling don't cascade to core Zettelkasten workflow

**However**, the following observations may inform future decisions:

- **Code duplication risk**: `assets.py`'s duplication of `llm.py` provider logic is a maintenance liability. Consider extracting a shared `get_chat_model(provider, model, config)` helper if multimodal support expands beyond images.
- **Rate-limit handling reusability**: The sophisticated rate-limit + backoff pattern in `_describe_with_rate_limit_retry()` could become a template for other resource-constrained operations (e.g., expensive embedding calls, API quota management). Monitor for copy-paste patterns.
- **Diagnostic utilities maturity**: Chunk and extraction dumps are recent (3-2 days old) and useful for troubleshooting. If they become standard debugging workflow, formalize them as first-class exports (e.g., web UI dashboard, CLI --export-diagnostic flag).

---

## LLM Module Analysis Details

### Date Analyzed: 2026-08-30
**Analyzed Files**:
- `zettel/llm.py` (420 lines)
- `zettel/pricing.py` (115 lines)
- `zettel/config.py` (llm configuration sections)

### Key Architectural Decisions Identified

1. **Multi-Provider LLM Strategy with Pluggable Gateway (Score: 140)** ← MUST-DOCUMENT
   - Implements strategy pattern via `get_llm()` for provider selection
   - Supports: OpenAI, Anthropic, Gemini, Ollama, OpenAI-compatible gateways (OpenRouter, OpenCode, Azure)
   - Provider selection from `cfg.llm.provider` at application startup
   - Temperature and top_p forwarded to all providers for consistent sampling
   - Max retries configurable per provider
   - Introduced: 2026-07-02 (stable since project inception; refined in 6e32ef4, 8ac6f32)
   - Used by 8+ modules (extractor, connector, gardener, gardener_hub, assets, bibliography, review, ask, article)

2. **System+Human Prompt Split for Provider-Agnostic Prompt Caching (Score: 135)** ← MUST-DOCUMENT
   - Splits prompts on `<!-- zettel:user -->` marker into system (stable) and user (per-call) parts
   - Enables provider-specific prefix reuse without prompt restructuring
   - Anthropic receives explicit `cache_control: {"type": "ephemeral"}` hints via `apply_prompt_cache_hints()`
   - Other providers receive standard System+Human messages (implicit prefix reuse)
   - Affects all 17 prompt files + 8 call sites
   - Introduced: 2026-08-13 (commit 6e32ef4, "feat(llm): add portable provider prompt caching via System+Human split")
   - Test coverage: `tests/test_prompt_cache.py` (158 lines)
   - Stable for 17 days; integrated into article pipeline with no regressions

### Git History Context

| Commit | Date | Message | Impact |
|--------|------|---------|--------|
| bd2d67b | 2026-07-02 ... | Improve Zettelkasten pipeline with LLM module and bug fixes | LLM module introduced |
| 64c5346 | 2026-08-04 16:31:38 | Add article generation capabilities and enhance retrieval process | Article pipeline uses LLM |
| 6e32ef4 | 2026-08-13 20:38:12 | feat(llm): add portable provider prompt caching via System+Human split | Prompt caching architecture |
| 8ac6f32 | 2026-08-30 ... | feat(config): make config.yaml the operational source of truth | Config-driven provider selection |

### Consumers and Dependencies

| Module | Uses | Reason |
|--------|------|--------|
| **extractor.py** | `get_llm()`, `call_llm()`, `load_prompt_parts()` | Literature note generation |
| **connector.py** | `get_llm()`, `call_llm()`, `load_prompt_parts()`, `extract_json()` | Permanent note generation |
| **gardener.py** | `get_llm()`, `call_llm()`, `load_prompt_parts()`, `extract_json()` | MOC generation |
| **gardener_hub.py** | `get_llm()`, `call_llm()`, `load_prompt_parts()`, `extract_json()` | Hub MOC generation |
| **assets.py** | `get_llm()`, `call_llm()`, multimodal provider logic | Image description |
| **bibliography.py** | `get_llm()`, `call_llm()`, `load_prompt_parts()`, `extract_json()` | Bibliographic metadata + ABNT formatting |
| **review.py** | `get_llm()` | Deduplication decisions |
| **ask.py** | `call_llm()` (via `article_graph`) | Grounded Q&A |
| **article.py** | `call_llm()` (via `article_graph`) | Multi-stage article generation |
| **index.py** | `clip_text()` | Progress logging text truncation |

### Configuration Exposure

All tuning in `config/config.yaml`:

```yaml
llm:
  provider: openai          # Switch here; no code change needed
  model: gpt-4o-mini        # Model name for provider
  temperature: 0.15         # Sampling randomness
  top_p: 0.5                # Nucleus sampling
  max_retries: 2            # Client-side retries on HTTP failure
  base_url: null            # Optional for OpenAI-compatible gateways
  prompt_cache: true        # Enable/disable caching hints globally
```

Supported provider values: openai, anthropic, ollama, gemini, openrouter, opencode, azure, compatible

### Temporal Evolution

- **2026-07-02** (59 days ago): LLM module introduced with multi-provider strategy
- **2026-08-04** (26 days ago): Article pipeline adds LLM-intensive workload
- **2026-08-13** (17 days ago): System+Human prompt split for provider-agnostic caching
- **2026-08-30** (today): Analysis completed; both patterns stable and production-tested

### Architectural Coupling

- **LangChain provider dependency**: Strategy assumes LangChain's provider clients; switching abstraction layers (e.g., to Anthropic's Messages API) would require refactoring all call sites
- **Per-startup provider selection**: No per-call provider switching (e.g., round-robin, failover); provider is fixed at boot
- **Prompt caching asymmetry**: Only Anthropic gets explicit cache hints; other providers benefit implicitly (if at all)
- **Environment-specific binding**: Project currently hardcodes `provider: openai` + `embedding.provider: ollama` in config; true provider flexibility is design-level, not operational-level

### Known Considerations

- **Provider registry via if/elif**: Adding a new provider requires code changes + new LangChain package. No factory registration pattern.
- **Implicit provider availability**: Missing LangChain sub-packages (e.g., `langchain_anthropic`) cause ImportError at call time, not at startup
- **Marker convention is strict**: HTML comment marker must be exact (whitespace-sensitive) in prompt files. Typos silently degrade to user-only messages.
- **Cost tracking per provider**: `call_llm()` records token usage via `usage_metadata` and routes to `CostTracker` via `record_llm()`
- **Embedding provider independence**: Embedding provider (`config.embedding.provider`) is separate from LLM provider; can be different (e.g., OpenAI LLM + Ollama embeddings)

---

## Future Analysis
After LLM module is complete, recommend analyzing:
1. **SYNC** (manual vault sync): Graph closure, wikilink extraction, edge typing
2. **MANUAL-SYNC** (deletion, graph rebuilds): Irreversible operations, cascade constraints
3. **CLI** (command orchestration): Typer routing, dependency composition, interactive CLI patterns
4. **PRICING** (cost estimation): LiteLLM price map dependency, local price table alternative
