# ADR-XXX: Hub-Anchored MOC Generation as a Complementary Clustering Strategy
**Status:** Accepted
**Date:** 2026-08-27
**Depends on:**
- [ADR-XXX: Single LLM Call Per Cluster with Intelligent Routing](./ADR-021-single-llm-call-per-cluster-routing.md)
- [ADR-XXX: Graph-Based Note Discovery with Weighted BFS Expansion](../RETRIEVAL/ADR-009-graph-based-note-discovery-weighted-bfs.md)

**Related to:** [ADR-XXX: Taxonomy-First MOC Clustering with UMAP+HDBSCAN](./ADR-019-taxonomy-first-moc-clustering.md)

## Context and Problem Statement

The gardener module's primary MOC pipeline organizes permanent notes into Maps of Content by matching them against a predefined category taxonomy, then clustering within each category. This works well when the vault has a well-defined domain structure, but it structurally cannot surface organization that emerges from how notes actually connect to each other rather than from which category they were assigned to. A note with unusually high connectivity — one that many other notes cite, contradict, or build on — represents a natural organizational anchor that taxonomy-based clustering has no mechanism to detect, since graph degree is orthogonal to both category membership and embedding similarity.

A second, independent MOC pipeline was added that ranks permanent notes by weighted graph degree, selects the highest-ranked notes as "hubs," expands each hub's neighborhood via breadth-first search over the existing note-connections graph, deduplicates neighborhoods that substantially overlap, and routes each resulting cluster through the same single-LLM-call generation logic as the taxonomy pipeline. Results are persisted with a distinct origin tag so the two strategies can be regenerated or purged independently and coexist rather than compete for the same notes.

This is a complementary strategy, not a replacement: taxonomy-first clustering remains the default `garden` behavior, and hub-anchored generation is opt-in via a `--hubs` flag. The design was introduced deliberately as a dual-strategy pattern rather than as an experiment, and has remained stable since introduction.

## Decision Drivers

* Taxonomy-first clustering depends on a well-defined category structure and cannot discover organization that follows connectivity rather than topic.
* Weighted graph degree is a structural signal independent of content embeddings, so it captures "importance" in a way vector similarity and taxonomy matching cannot.
* The BFS traversal used to build hub neighborhoods reuses the same weighted, decay-based graph expansion already built for retrieval, avoiding a second traversal algorithm and a second set of edge-weight semantics.
* Both strategies must be independently regenerable so that regenerating one never destroys the other's output, since they serve different navigation intents (topical vs. connectivity-based).
* Routing hub clusters through the same single-LLM-call-per-cluster logic as the taxonomy pipeline keeps cost and prompt-maintenance patterns consistent across both strategies rather than introducing a second generation model.

## Considered Options

* Hub-anchored BFS clustering with configurable percentile or absolute degree ranking (chosen)
* Graph community detection (e.g., Louvain/Leiden) in place of degree ranking
* No complementary strategy — taxonomy-first clustering only

## Decision Outcome

Chosen option: hub-anchored BFS clustering with configurable degree ranking, because it surfaces the specific class of organization — connectivity-anchored neighborhoods — that taxonomy-first clustering cannot detect, while reusing the vault's existing graph-expansion mechanism and generation routing rather than introducing new infrastructure. [NEEDS INPUT: Was degree-based ranking validated against real vault usage, or chosen primarily for its simplicity and determinism relative to community detection?]

Supporting both a percentile mode (top N% by degree) and an absolute mode (fixed degree threshold) lets the selection scale with a growing vault or stay fixed for an archive of known size, trading the simplicity of a single default for configuration flexibility across different vault growth patterns.

## Pros and Cons of the Options

### Hub-anchored BFS clustering (chosen)

* Good, because it surfaces MOCs anchored on highly-connected notes that taxonomy clustering has no mechanism to find
* Good, because it reuses the existing weighted BFS traversal and edge-weight configuration already validated for retrieval-time graph expansion
* Good, because origin tagging lets hub MOCs be purged and regenerated without touching taxonomy MOCs
* Bad, because it requires a well-formed note graph with explicit backlinks; sparse or weakly-connected graphs produce few or low-quality hubs

### Graph community detection

* Good, because it would find natural graph clusters directly rather than approximating them via degree and neighborhood expansion
* Bad, because it is slower and produces less transparent, less directly tunable cluster boundaries than explicit BFS hop/neighbor limits
* Bad, because no evidence indicates this approach was implemented or benchmarked against degree ranking

### No complementary strategy (taxonomy-only)

* Good, because it avoids a second pipeline's configuration surface and maintenance burden entirely
* Bad, because it leaves connectivity-anchored organization permanently undiscoverable regardless of how well-connected the note graph becomes
* Bad, because it forces all MOC organization through a taxonomy that may not reflect how a given vault's notes actually relate

## Consequences

The two pipelines increase the module's maintenance surface: two LLM prompt templates, two generation entry points, and a documented duplication where the hub pipeline reaches into roughly a dozen private symbols of the taxonomy pipeline's module to reuse shared cluster-routing and MOC-body-building logic. [NEEDS INPUT: Is consolidating this shared logic into a common module planned, or is the current duplication an accepted long-term trade-off?]

Hub MOCs store a reference to their anchor note in frontmatter. If that note is later deleted, the MOC is not automatically cleaned up or flagged, and becomes orphaned with no warning surfaced to the user. [NEEDS INPUT: Should hub-note deletion trigger automatic MOC cleanup or a warning, and is this in scope for `delete-source`/`purge-source`?]

Edge-weight and decay configuration is shared with the retrieval layer's graph expansion, so retuning either affects both hub MOC generation and search-time graph expansion simultaneously; no separate tuning surface exists for the two use cases. [NEEDS INPUT: What evidence supports the default decay value used for hub-neighborhood expansion, and was it benchmarked separately from the retrieval use case?]

## References

* `zettel/gardener_hub.py` — hub ranking, BFS neighborhood expansion, and deduplication (625 lines)
* `zettel/gardener.py:60-62` — CLI dispatch to the hub pipeline via the `--hubs` flag
* `zettel/config.py` — `HubMocsConfig` (selection mode, percentile/threshold, hop and neighbor limits, decay)
* `zettel/graph.py` — shared weighted BFS traversal reused from the retrieval layer
