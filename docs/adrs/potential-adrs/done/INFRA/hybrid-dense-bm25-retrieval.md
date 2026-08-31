# Potential ADR: Hybrid Dense+BM25 Retrieval with RRF Fusion and Absolute Relevance Floor

**Module**: INFRA (Retrieval subsystem)  
**Category**: API Protocol / Retrieval Architecture  
**Priority**: Must Document (Score: 145)  
**Date Identified**: 2026-08-30

---

## What Was Identified

The retrieval layer implements a hybrid search strategy combining ChromaDB dense-vector search with SQLite FTS5 BM25 lexical search, fused via Reciprocal Rank Fusion (RRF), and gated by an absolute relevance floor. This is the single composition point for all note/chunk lookups across the entire system — used by connector (RAG), sync (suggestions), ask (Q&A), article (long-form), and deliberately *not* used by harvest/extract dedupe (which use raw L2 distance with different thresholds).

The architecture solves a real production bug: dense embeddings alone underrate lexical matches (e.g., acronyms, domain jargon), while BM25 alone is brittle without vector confirmation. The relevance floor prevents off-topic results from being confidently ranked when no actual semantic signal is present.

**Introduced**: Hybrid retrieval added in `2d6ff27` ("Add hybrid retrieval (BM25+vector) and lightweight GraphRAG"); absolute relevance floor added in `ed22565` ("Add absolute relevance floor to hybrid retrieval, fix BM25 stopword leak") — fixing a production bug where weak BM25 hits were unconditionally bypassing the similarity floor.

**Modified**: Bug-fixed in `ed22565` (BM25-bypass rank cutoff); stable since then. Recent refinements in `02e6e3b` (graph expansion weights). Threshold values frozen in `RelevanceFloorConfig` (min_vector_similarity: 0.70, bm25_bypass_max_rank: 5, absolute_min_similarity: 0.15).

---

## Why This Might Deserve an ADR

- **Impact**: Every downstream consumer (ask, article, connector, sync) depends on this. No alternative retrieval path exists. Changes to thresholds affect all 4 consumers simultaneously.
- **Trade-offs Visible**:
  - RRF fusion is positional-only; both dense and lexical must rank well to survive the floor.
  - Absolute relevance floor requires empirical calibration; the current thresholds (0.70, 0.15) are calibrated on this project's corpus + embedding model and may not transfer.
  - BM25 stopword filtering (26 PT-BR stop words) is language-specific; multilingual support would need conditional filtering.
  - Graph expansion (1+ hop BFS) is optional but stable at 1 hop (max_hops: 1) — deeper expansion costs more memory.
- **Cost to Change**: Swapping the retrieval strategy (e.g., to dense-only, RAG-without-BM25, different fusion) requires recalibrating thresholds and re-testing all 4 consumers. The bug-fix history (ed22565) shows this has bitten before.
- **Team Knowledge**: Anyone working on ask, article, connector, or sync must understand RRF mechanics, the relevance floor rationale (why both floors matter), and the BM25-rank-cutoff fix (bm25_bypass_max_rank prevents weak jargon matches from falsely passing).
- **Temporal Context**: Introduced ~2 years ago (2d6ff27), bug-fixed ~18 months ago (ed22565). The two-stage evolution (introduce hybrid, then fix relevance floor) suggests the decision was initially incomplete and corrected based on production experience.

---

## Evidence Found in Codebase

### Key Files
- [`zettel/retrieval.py`](../../../zettel/retrieval.py) - Entire file (331 lines), implements `Retriever` class and hybrid fusion logic
- [`zettel/config.py`](../../../zettel/config.py) - Lines 204-227 define `RetrievalConfig` + `RelevanceFloorConfig`
- [`zettel/state.py`](../../../zettel/state.py) - FTS5 implementation for BM25 half (lines ~260+)

### Code Evidence
```python
# From zettel/config.py (RetrievalConfig):
class RetrievalConfig(BaseModel):
    mode: Literal["vector", "hybrid"] = "hybrid"
    rrf_k: int = 60  # Reciprocal Rank Fusion constant
    graph_expansion: GraphExpansionConfig = Field(default_factory=GraphExpansionConfig)
    relevance_floor: RelevanceFloorConfig = Field(default_factory=RelevanceFloorConfig)

class RelevanceFloorConfig(BaseModel):
    enabled: bool = True
    min_vector_similarity: float = 0.70  # Default gate for vector hits
    bm25_hit_bypasses_floor: bool = True  # Lexical matches can bypass...
    bm25_bypass_max_rank: int = 5  # ...but only if ranked in top 5 (bug fix from ed22565)
    absolute_min_similarity: float = 0.15  # Backstop: even BM25 hits must have some vector signal

# From zettel/retrieval.py (Retriever.search_notes):
def search_notes(self, query: str, topk: int = 10) -> NoteSearchResult:
    # Step 1: Dense search (ChromaDB)
    dense_results = self.index.search(query, topk=topk)
    
    # Step 2: BM25 search (SQLite FTS5)
    bm25_results = self.db.fts_search(query, topk=topk)
    
    # Step 3: RRF fusion (positional-only)
    fused = reciprocal_rank_fusion(dense_results, bm25_results, k=60)
    
    # Step 4: Absolute relevance floor (4-step gate)
    hits = apply_relevance_floor(fused, config=self.config.relevance_floor)
    
    # Step 5: Graph expansion (optional BFS, weighted by relation type)
    if config.graph_expansion.enabled:
        hits = expand_notes(hits, graph=self.graph, max_hops=1, decay=0.5)
    
    return NoteSearchResult(hits=hits, candidates=fused)

# Relevance floor gate sequence:
def _apply_relevance_floor(hit, config):
    # 1. absolute_min_similarity (0.15): hard backstop
    if hit.vector_similarity < 0.15:
        return None
    
    # 2. bm25 bypass with rank cutoff (bug fix)
    if hit.bm25_rank is not None and hit.bm25_rank <= 5:
        return hit  # Strong lexical match, pass
    
    # 3. min_vector_similarity (0.70): default gate
    if hit.vector_similarity >= 0.70:
        return hit
    
    # 4. Weak BM25-only hit with low vector: fail
    return None
```

### Impact Analysis
- **Introduced**: `2d6ff27` (Add hybrid retrieval..., ~2 years ago); bug-fixed in `ed22565` (~18 months)
- **Modified**: Threshold refinements in graph expansion weights; relevance floor gate logic stable since fix
- **Last change**: `02e6e3b` (graph expansion relation weights tuning) — threshold tweaking, not strategy change
- **Files affected**: retrieval.py (core logic), connector.py (RAG), ask.py (Q&A), article.py (long-form), sync.py (suggestions); harvester.py, extractor.py explicitly do NOT use this (different, raw-L2 thresholds)
- **Scope**: Large (4 direct consumers, impacts all search-consuming features; 5 configuration groups in RetrievalConfig)
- **Bug History**: One documented production bug (ed22565) — BM25-bypass rank cutoff was missing, causing weak jargon matches to unconditionally pass the floor. This signals the decision was incomplete initially.

### Thresholds & Calibration
- RRF constant: 60 (canonical value, empirically common)
- min_vector_similarity: 0.70 (main gate, empirically calibrated on this corpus + embedding model)
- bm25_bypass_max_rank: 5 (bug fix; prevents weak matches from unconditionally passing)
- absolute_min_similarity: 0.15 (hard backstop; kept well below main floor so BM25 can do its job)
- Graph expansion: max_hops=1, decay=0.5 (1 hop is conservative; deeper exploration costs memory)
- PT-BR stopword filtering: 26 words dropped from BM25 MATCH expressions to prevent high-frequency tokens (e.g., "que") from matching entire corpus

---

## Questions to Address in ADR (if created)

- Why RRF (Reciprocal Rank Fusion) instead of learned fusion or embedding similarity weighted by BM25 confidence?
  - Answer likely: RRF is simple, order-invariant, and doesn't require training.
- How were thresholds (0.70, 0.15) empirically calibrated? (What corpus size, embedding model, test cases?)
- What embedding model are the thresholds tied to? (Currently: text-embedding-3-small via OpenAI; would 0.70 work for other models?)
- Why is graph expansion limited to 1 hop? (Deeper hops include more context but also noise; trade-off not visible in code.)
- Should the ask/article commands have different floor thresholds than connector? (Connector needs strict matching; Q&A might tolerate broader retrieval.)
- How does the BM25 stopword list get maintained? (Currently: hardcoded in state.py; should it be configurable?)

## Related Potential ADRs
- Absolute Relevance Floor for Retrieval (this is the floor logic)
- ChromaDB Embedded Vector Store (provides dense half)
- SQLite with WAL + FTS5 (provides BM25 half)
- Dual-Store Persistence (SQLite + ChromaDB dependency)

## Additional Notes
- The `RetrievalConfig.mode` property allows fallback to vector-only (`mode: vector`), preserving historical behavior, but hybrid is default and preferred.
- Graph expansion (BFS weighted by relation type) is a lightweight GraphRAG pattern; `DEFAULT_RELATION_WEIGHTS` rank `contradicts` highest (signal embeddings miss).
- No visible A/B testing framework for threshold tuning; any threshold changes require manual testing + re-validation.
- FTS5 MATCH expressions are safe (tokens quoted to neutralize operators), preventing injection attacks.
