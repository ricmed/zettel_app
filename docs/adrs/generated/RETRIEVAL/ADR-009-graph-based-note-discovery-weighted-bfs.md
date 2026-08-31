# ADR-XXX: Graph-Based Note Discovery with Weighted BFS Expansion
**Status:** Accepted
**Date:** 2026-07-18
**Depends on:** [ADR-XXX: Hybrid Dense+BM25 Retrieval with RRF Fusion and Absolute Relevance Floor](../INFRA/ADR-003-hybrid-dense-bm25-retrieval.md)
**Used by:** [ADR-XXX: Hub-Anchored MOC Generation as a Complementary Clustering Strategy](../GARDEN/ADR-020-hub-anchored-moc-pipeline.md)
**Related to:**
- [ADR-XXX: Taxonomy-First MOC Clustering with UMAP+HDBSCAN](../GARDEN/ADR-019-taxonomy-first-moc-clustering.md)
- [ADR-XXX: Single LLM Call Per Cluster with Intelligent Routing](../GARDEN/ADR-021-single-llm-call-per-cluster-routing.md)
- [ADR-XXX: Retrieval Result Transparency (Hits vs Candidates)](./ADR-010-retrieval-result-transparency-hits-vs-candidates.md)

## Context and Problem Statement

The retrieval layer needed a way to surface conceptually linked notes that dense-vector search structurally cannot find. A note marked `contradicts` another sits far apart in embedding space precisely because it argues the opposite position, so similarity search alone will never rank it as relevant even though the connection is exactly the kind of signal a researcher wants surfaced. Typed edges in `note_connections` (built by the connector phase) already encode this relationship explicitly; the retrieval layer needed a mechanism to walk that graph and fold it into search results.

A graph expansion layer was added that performs a 1..N hop breadth-first search (BFS) over `note_connections` after hybrid RRF retrieval ranks the initial seeds. Edges are treated as undirected, each relation type is weighted differently, and each additional hop is attenuated by exponential decay. This is a lightweight GraphRAG pattern: rather than relying on a second embedding pass or a learned re-ranker, it reuses the graph structure the vault already maintains and treats relation type itself as a relevance signal dense search cannot see.

This layer was introduced in the same commit as hybrid RRF fusion, as part of a single retrieval-enrichment initiative: RRF fusion is the *what* (combining dense and lexical evidence), while graph expansion is the *how* of enriching those results further. The traversal algorithm and its thresholds (max 1 hop, 0.5 decay, 10-neighbor cap) have been unchanged since introduction.

## Decision Drivers

* Dense embeddings systematically miss conceptually-opposite relationships (e.g. `contradicts`), since such notes are semantically distant by construction.
* Every downstream consumer (ask, article, connector, sync) needs the same enrichment layer, since they all call `Retriever.search_notes()` with graph expansion on by default.
* Expansion must stay additive to the RRF seed set, never displacing directly-retrieved results, to avoid degrading precision for the sake of recall.
* Traversal cost must stay bounded per query, since each hop requires one batched round-trip to `StateDB`.
* Relation types carry different levels of retrieval-relevant signal — a rare `contradicts` edge is more valuable than a generic `related` edge — so uniform edge weighting would blur that distinction.
* Vault presentation favors directional labels (`contradicts` vs. `is-contradicted-by`) for human readability, but retrieval logic needed a simpler model to avoid asymmetry bugs between the two traversal directions.

## Considered Options

* Weighted BFS over undirected edges with per-relation weighting and exponential hop decay (chosen)
* Directed traversal preserving upstream/downstream edge semantics
* No graph expansion — rely solely on RRF-fused hybrid retrieval scores

## Decision Outcome

Chosen option: weighted BFS over undirected edges with per-relation weighting and exponential hop decay, because it surfaces the specific class of relevant notes (conceptual opposites and typed relationships) that dense embeddings cannot find, while staying additive to and bounded around the existing RRF seed set. [NEEDS INPUT: Was BFS chosen over DFS or a random-walk expansion for a specific technical reason, or was it a default assumption? The likely rationale — that BFS surfaces the closest neighbors first, which matters when a `max_neighbors` cutoff is applied — is not documented.]

Treating edges as undirected was a deliberate simplification: a connection is considered relevant to both notes it touches, and the reverse direction reuses the same relation weight, with the inverse label in the vault kept presentation-only. This avoids maintaining separate weight and decay handling for each traversal direction, at the cost of doubling the frontier considered per edge. Hop depth defaults to 1, based on the documented assumption that a single hop already captures most of the lightweight GraphRAG value while deeper traversal adds noise and query cost; [NEEDS INPUT: what evidence, if any, supports max_hops=1 over deeper expansion — was this benchmarked, or is it a pragmatic default?]

## Pros and Cons of the Options

### Weighted undirected BFS with hop decay (chosen)

* Good, because it surfaces conceptually linked notes — especially contradictions — that dense embeddings structurally cannot rank as relevant
* Good, because the undirected model avoids maintaining separate logic for each traversal direction and the asymmetry bugs that implies
* Good, because exponential decay plus a `max_neighbors` cap keeps expansion bounded and strictly additive to the RRF seeds, never displacing them
* Bad, because relation weights, decay, and hop depth are empirically set rather than learned or benchmarked, so any retuning requires manual revalidation across all four consumers

### Directed traversal

* Good, because it would preserve upstream/downstream semantics for relation types where direction carries meaning
* Bad, because it doubles the implementation surface (separate weight/decay handling per direction) without evidence this was evaluated
* Bad, because it would diverge from the vault's own inverse-label presentation model, creating a mismatch between how relationships are stored and how they are displayed

### No graph expansion (RRF-only)

* Good, because it removes an entire configuration surface (weights, decay, hop depth, neighbor cap) and its tuning burden
* Bad, because it forfeits the specific signal typed relationships provide for embedding-blind cases like contradictions
* Bad, because it leaves the `note_connections` graph built by the connector phase unused by retrieval

## Consequences

All four retrieval consumers (ask, article, connector, sync) share one graph-expansion configuration, so a decay or weight change tuned for one use case propagates to the others — the same shared-threshold pattern already noted for the hybrid retrieval floor. [NEEDS INPUT: Should ask/article, which tolerate broader exploratory context, use different graph-expansion thresholds than connector and sync, which need tighter grounding?]

Relation weights live in `config.yaml` as static values rather than being learned from usage, and there is no A/B testing pathway to validate changes. [NEEDS INPUT: What process, if any, exists to retune relation weights as the note graph grows, and has weight drift been observed since introduction?] Any change to the traversal strategy — depth, decay shape, or per-relation weights — requires manually revalidating `ask`, `article`, and `connect` output, since none of these consumers currently apply their own expansion thresholds.

## References

* `zettel/graph.py` — `expand_notes()`, the undirected weighted BFS traversal (114 lines)
* `zettel/retrieval.py:299-331` — `_expand_with_graph()`, orchestrates expansion after RRF fusion
* `zettel/config.py:111-173` — `GraphExpansionConfig` and `DEFAULT_RELATION_WEIGHTS` defaults
