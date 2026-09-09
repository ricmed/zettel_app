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

## Addendum (2026-09-09) — the BM25 bypass needs an absolute half

**Status:** Accepted amendment. Adds `relevance_floor.bm25_bypass_min_coverage`;
does not change the chosen option.

### The defect

This ADR records that the floor's rank cutoff was itself a fix for a production
bug in which *"weak BM25-only matches unconditionally bypassed the similarity
check"*. The cutoff narrowed that hole without closing it, because
**`bm25_bypass_max_rank` is a relative test**: it asks whether a hit ranked well
*among whoever matched*. `_fts_match_expr` joins the query's terms with `OR`, so
a note is returned for matching **any one** of them. When a query's match pool is
smaller than the cutoff — routine on a small corpus — "top 5" degenerates into
"everything that matched at all", and rank stops carrying information.

Measured 2026-09-09 on this repository's corpus (62 permanent notes,
`ollama/qwen3-embedding@1024d`), with off-domain questions:

| query | BM25 match pool | passed | via bypass |
|---|---|---|---|
| "como fazer risoto de cogumelos" | 4 | 4 | 4 |
| "manutencao preventiva do motor do carro" | 2 | 2 | 2 |
| "receita de bolo de cenoura…" | 4 | 4 | 4 |
| "escalacao do time … campeonato brasileiro" | 9 | 5 | 5 |
| "sintomas de deficiencia de vitamina D…" | 2 | 2 | 2 |
| "como podar uma roseira no inverno" | **0** | 0 | 0 |

Every one of those bypassing hits had similarity **below** the 0.70 floor
(0.58–0.68), and 15 of 17 shared exactly **one** word with the question — the
common verb *fazer*, or *carro*, or *final*. The last row is the control: with no
lexical overlap at all, the floor already behaves.

### Why this surfaced now

`ask` absorbs the noise, because `prompts/ask.md` makes the model triage the
context and discard what is irrelevant. `zettel catalog` (ADR-047) consumes the
same `search_notes` and calls **no LLM by design**, so it prints whatever the
floor admits. The defect was always there; a consumer without a triage step made
it visible.

### Decision: gate the bypass on term coverage, not only on rank

`bm25_bypass_min_coverage` (default **0.5**) requires a bypassing hit to contain
at least half of the query's FTS terms. Rank stays as the relative half;
coverage is the absolute one — *how much of what was asked is actually in this
note*.

`state.fts_query_terms` is the single definition of "the query's terms", shared
with `_fts_match_expr`, so the coverage denominator is exactly the term set BM25
searched (including its truncation at `max_tokens`). Coverage is computed in
`Retriever._attach_coverage` over the same surface FTS indexed (title + body for
notes, title + summary for chapters).

**Why coverage and not a minimum count of matched terms.** A count destroys the
very use case this ADR says the bypass exists for — *"rescuing jargon/acronyms
the embedding underrates"*. A one-word query (`ARIMA`, `sazonalidade`,
`benchmarks`) can only ever match one term, so `>= 2` would make the bypass
unreachable for it. Measured, those queries sit at coverage **1.00** and sail
through, while off-domain noise sits at 0.17–0.50:

| band (bypassing hits) | n | min | median | max |
|---|---|---|---|---|
| off-domain | 31 | 0.17 | 0.25 | **0.50** |
| in-domain | 56 | **0.50** | 1.00 | 1.00 |

| gate | off-domain blocked | in-domain kept |
|---|---|---|
| coverage >= 0.50 | **30/31 (96.8%)** | **56/56 (100%)** |
| coverage >= 0.51 | 31/31 (100%) | 45/56 (80.4%) |
| matched terms >= 2 | 25/31 (80.6%) | 33/56 (58.9%) |
| matched terms >= 3 | 31/31 (100%) | 27/56 (48.2%) |

### Effect on all four consumers

| consumer / band | hits before | hits after | delta |
|---|---|---|---|
| `ask` / `catalog`, off-domain | 31 | **1** | **−30 (−97%)** |
| `ask` / `catalog`, in-domain | 162 | **162** | **0** |
| `connect` / `sync` (paragraph query) | 60 | **60** | **0** |

`connect` and `sync` do not pass a question — they pass `thesis + definition` and
a whole note body, 20–27 distinct terms after truncation. Their coverage is
structurally low (0.20–0.70), so the gate denies most of their bypasses (49 → 14)
and **costs them nothing**: all 49 of those hits had similarity ≥ 0.734 and
passed the similarity check anyway. The bypass was doing no work there. That is
consistent with what the bypass is *for* — a rescue for short, term-like queries,
which is exactly where coverage is high and where the embedding is weakest.

The single surviving off-domain hit is not a false positive: the note's
`## Intuição` genuinely uses a car analogy ("carro esportivo… custo de
manutenção"), so it really does contain 2 of the 4 query terms. A query that is
off-domain while a note uses an off-domain *analogy* is a real limit of any
lexical signal, and is left alone rather than tuned away.

### Consequences

* Cost is bounded and does not grow with the corpus: only hits inside
  `bm25_bypass_max_rank` can consult coverage, so at most that many note bodies
  are folded per search. Measured `_bm25_notes`: ~1.0 ms → ~4.8 ms, flat in pool
  size, against ~407 ms for a full `search_notes` call dominated by the query
  embedding.
* `floor_reason` now names both steps when a bypass is denied
  (`"cobertura lexical 33% < 50% (bm25 rank 1), sem bypass; similaridade 0.64
  abaixo do piso (0.70)"`), so ADR-010's transparency contract still explains the
  whole chain.
* `bm25_bypass_min_coverage: 0.0` restores the previous rank-only behaviour.
* The same knob exists on `retrieval.chapter_floor` (ADR-047) for the same
  reason; both default to 0.5.
* **Still not fixed, and deliberately so:** `_PT_STOPWORDS` contains no common
  verbs, so *fazer* remains a matchable term. Extending the stopword list would
  change BM25 ranking for every consumer and needs its own measurement; the
  coverage gate makes the symptom harmless without touching that ranking.
* The 0.5 threshold is measured on 62 notes in a narrow corpus. The bands are
  wide apart (0.50 vs 0.50 at the edges, 0.25 vs 1.00 at the medians), but this
  motivates a value rather than settling one — re-measure on a broader vault.
