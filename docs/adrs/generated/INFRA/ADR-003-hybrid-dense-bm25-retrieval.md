# ADR-XXX: Hybrid Dense+BM25 Retrieval with RRF Fusion and Absolute Relevance Floor
**Status:** Accepted
**Date:** 2024-08-30
**Used by:**
- [ADR-XXX: Retrieval Result Transparency (Hits vs Candidates)](../RETRIEVAL/ADR-010-retrieval-result-transparency-hits-vs-candidates.md)
- [ADR-XXX: Graph-Based Note Discovery with Weighted BFS Expansion](../RETRIEVAL/ADR-009-graph-based-note-discovery-weighted-bfs.md)

**Related to:**
- [ADR-XXX: Layered Hashing Strategy for Deterministic Caching and Drift Detection](./ADR-007-layered-hashing-strategy.md)
- [ADR-XXX: Three-Layer Duplicate Detection Strategy for Source Ingestion](../HARVEST/ADR-011-three-layer-duplicate-detection.md)
- [ADR-XXX: System+Human Prompt Split for Provider-Agnostic Prompt Caching](../LLM/ADR-025-prompt-caching-system-human-split.md)

## Context and Problem Statement

The retrieval layer needed a single lookup strategy for notes and chunks shared across every downstream consumer: connector RAG, sync suggestions, the `ask` Q&A command, and the `article` long-form command. Dense-vector search alone (ChromaDB embeddings) systematically underrates lexical matches such as acronyms and domain-specific jargon, since semantic similarity does not always track exact-term relevance. Lexical search alone (BM25) is brittle without semantic confirmation, since it cannot recognize a paraphrase or synonym.

A hybrid approach was implemented combining ChromaDB dense-vector search with SQLite FTS5 BM25, fused via Reciprocal Rank Fusion (RRF), and gated by an absolute relevance floor. This solved a real production issue: a purely positional fusion (RRF) always returns the N closest results in the corpus regardless of whether any are actually relevant, so an off-topic query could still receive a confidently-ranked "top" result. Harvest and extract deliberately do not use this pipeline; their duplicate-detection thresholds are calibrated on raw L2 distance and were left untouched.

The hybrid strategy was introduced first, then the relevance floor was added roughly a year and a half later after a production bug: weak BM25 matches were unconditionally bypassing the similarity check, letting low-relevance lexical hits pass as if they were strong matches.

## Decision Drivers

* Every downstream consumer (connector RAG, sync, ask, article) needs one trustworthy retrieval path, so the fusion strategy affects the whole system at once.
* Dense embeddings alone underrate lexical matches such as acronyms and domain jargon, while BM25 alone is brittle without vector confirmation.
* A positional fusion method (RRF) avoids training a learned ranker and stays agnostic to the different similarity scales of each retriever.
* RRF's fused score is purely positional, so an absolute relevance floor is needed to stop confidently-ranked but actually irrelevant results from reaching consumers.
* Threshold calibration is corpus- and embedding-model-specific, and a prior production bug showed that an incomplete floor can silently let irrelevant results through.
* Harvest/extract duplicate detection intentionally keeps its own raw L2 thresholds, so the two threshold systems cannot be unified into one universal setting.

## Considered Options

* Hybrid Dense+BM25 retrieval fused via RRF, gated by an absolute relevance floor (chosen)
* Vector-only dense search (legacy `mode: vector`, retained as a fallback)

## Decision Outcome

Chosen option: hybrid Dense+BM25 retrieval fused via Reciprocal Rank Fusion and gated by an absolute relevance floor, because it corrects the specific weakness of each retriever used alone. RRF combines rank position rather than raw score, avoiding the scale mismatch between cosine similarity and BM25 scores, while the floor prevents a purely positional fusion from confidently surfacing off-topic content. [NEEDS INPUT: Why was RRF chosen over a learned or weighted fusion of embedding similarity and BM25 confidence? No empirical comparison between the two approaches is documented.]

The relevance floor itself was added after a production incident in which weak BM25-only matches unconditionally bypassed the similarity check; the fix introduced a rank cutoff so only BM25 hits ranked within the top results can bypass the vector-similarity gate. This two-stage history (introduce hybrid, then harden the floor) indicates the original design was materially incomplete until real usage exposed the gap.

## Pros and Cons of the Options

### Hybrid Dense+BM25 with RRF + relevance floor (chosen)

* Good, because it rescues jargon/acronym queries that dense embeddings underrate, via the BM25 lexical half
* Good, because RRF is order-invariant and training-free, requiring no score normalization across retrievers
* Good, because the absolute relevance floor stops confidently-ranked but off-topic results across all four consumers
* Bad, because thresholds are empirically calibrated on this project's corpus and embedding model and may not transfer to a different model
* Bad, because RRF fusion is purely positional, so a very strong single-signal match can still under-fuse if the other retriever ranks it poorly
* Bad, because it already required one production bug fix (bypass rank cutoff) to close a gap where weak lexical hits passed the floor unconditionally

### Vector-only dense search (legacy fallback)

* Good, because it is simpler, with no fusion logic or relevance-floor gate sequence to maintain
* Good, because it avoids language-specific BM25 stopword tuning entirely
* Bad, because it systematically underrates lexical matches that a user's exact wording should catch
* Bad, because it has no mechanism analogous to the relevance floor's BM25 bypass to rescue a strong lexical match the embedding misses

## Consequences

All four retrieval consumers share the same relevance-floor thresholds, so a change tuned for one use case affects the others as well. [NEEDS INPUT: Should ask/article use different floor thresholds than connector, given that Q&A may tolerate broader retrieval than RAG context-building?] Harvest and extract remain on a separate, raw-L2 threshold system for duplicate detection, and that separation must be preserved during any future retrieval tuning.

Any change to the embedding model requires re-validating the similarity thresholds, since both were calibrated against the current model's similarity distribution and no calibration record was found. [NEEDS INPUT: What corpus size, embedding model, and test methodology were used to calibrate the 0.70 and 0.15 thresholds?] BM25 stopword filtering is PT-BR-specific; multilingual support would require conditional filtering logic rather than the current hardcoded list.

Graph expansion (BFS over note connections) is capped at one hop with 0.5 decay, keeping GraphRAG-style expansion conservative; deeper expansion was not adopted, reportedly due to memory cost, though this trade-off is not otherwise documented in the codebase.

## References

* `zettel/retrieval.py:59-77` — `Retriever.search_notes`, the hybrid fusion pipeline (dense search, BM25 search, RRF fusion, floor, graph expansion)
* `zettel/retrieval.py:80-95` — relevance floor gate sequence (`_apply_relevance_floor`)
* `zettel/config.py:204-227` — `RetrievalConfig` and `RelevanceFloorConfig` definitions
* `zettel/state.py:260` — SQLite FTS5 implementation backing the BM25 half
