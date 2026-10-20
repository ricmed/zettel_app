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

## Addendum (2026-10-19): hub MOCs are never a taxonomy target (issues #292, #293)

**Status:** Accepted amendment. The taxonomy pipeline only routes into `origin='pipeline'` MOCs, by folded topic equality (ADR-021 addendum), so a hub MOC whose free topic names a category is no longer rewritten and relabeled by it. `_update_hub_moc` checks ownership the same way (`gardener.owns_moc`: row and frontmatter must say `hub_pipeline`), and `--hubs --recreate` goes through the shared `purge_mocs_by_origin`, which keeps a file whose frontmatter names another origin. `run_garden_hubs` follows the garden fail-fast (ADR-059 addendum): the LLM client is built before `--recreate` purges anything, and any escaping exception records the run `failed`.

## Addendum (2026-10-19): the hub MOC lists the hub and its best neighbours (issue #297)

**Status:** Accepted amendment. The BFS neighbourhood still decides routing and deduplication, but the MOC lists the hub plus `gardener.max_entries_per_moc - 1` neighbours ranked by the entry signal (ADR-060), each with an orientation line. The prompt no longer asks for every neighbour, the reconciliation section is gone, and the incremental update goes through the shared `gardener._update_existing_moc` with `moc_hub_incremental.md` (which no longer takes `{hub_note_title}`: the hub is visible in the "Porta de entrada" section).

## Addendum (2026-10-20): proposed edges, configured weights and graph warnings (issue #299)

**Status:** Accepted amendment.

**Problem.** Since #279/#287 the connect phase writes LLM edges as `proposed`, and only accepted, derived and manual edges are traversable. The hub degree and BFS applied that rule, so right after connect the hubs ranked almost only on `corroborates` (189 derived edges out of 2,305): 3 hubs and 26 notes instead of 15 hubs and 274.

**Decision.** Proposed LLM edges count in the garden at a configurable reduced weight, `gardener.proposed_edge_factor` (0.5): `state.connections.EdgePolicy` weighs a traversable edge by its relation, a proposed one by `factor * relation weight`, a rejected one by 0. The policy is built by `gardener_assign.garden_edge_policy(cfg)` and applies to the hub degree (`get_weighted_note_degrees(..., proposed_factor=)`), the hub BFS (`expand_notes(..., proposed_factor=)`), the cluster cohesion and the internal degree that picks entry notes. Retrieval, connect context and reachability keep walking traversable edges only: the reader never walks a proposed edge. `0` restores the walkable-only graph. The relation weights now come from `retrieval.graph_expansion.relation_weights` everywhere in the garden (and in `zettel skill` ranking and the web dashboard), not from the `DEFAULT_RELATION_WEIGHTS` constant.

**Measurement.** `scripts/probe_hub_policy.py` (`evals/results/299-arestas-propostas.json`) ran the real hub and entry code on a copy of the development graph (781 notes, 2,305 edges, 2,116 of them LLM). With every LLM edge accepted, the factor changes nothing: 15 hubs cover 274 notes and the doors (hub and MOC entries) reach 99.2% of the notes within two hops. With every LLM edge still proposed: factor 0 gives 3 hubs covering 26 notes and the doors reach 24.7%; factor 0.25 is the same, because a neighbour weighs `factor * relation weight` and falls under `hub_mocs.min_neighbor_weight` (0.3); factor 0.5 gives the same 15 hubs covering 248 notes and 35.9%; factor 1 gives 274 notes and 35.6%. Reachability is measured on the graph the reader walks, so only reviewing the queue (`zettel links`) moves it toward 99%. Factor 0.5 is the smallest of the measured values that keeps the hub set of the reviewed graph while still discounting unreviewed judgement; it is a provisional value, not a calibrated one.

**Warnings.** Every garden report carries `warnings` (`garden_report.graph_warnings`): no edge at all; walkable share under `gardener.warn_min_traversable_share` (0.5); reachability within two hops of the doors under `gardener.warn_min_reach` (0.5); and how many proposed edges weighed in the garden's choices. The CLI prints them after `garden` and `garden --hubs`, the web job detail lists them, and `zettel doctor` runs the same check on the current edges and the last garden's reach ("Grafo das portas"). A `--hubs` run on an unreviewed graph with factor 0 now ends with no hub and a warning pointing to `zettel links`, instead of silently producing nothing.

**Hub entries.** The neighbours listed in a hub MOC are chosen by the entry signal of ADR-060 within `max_entries_per_moc` (since #297); the BFS still decides the neighbourhood used for routing and deduplication.

**Not decided here.** The hub topic stays free (`strict=False`): a hub is an emergent theme of the graph, not a taxonomy category, so validating it against the categories would defeat its purpose. `max_hops`, `decay`, `max_neighbors`, `min_neighbor_weight` and `dedup_subset_threshold` were not re-tuned, and Leiden communities were not compared with the degree-plus-BFS neighbourhoods; both need their own measurement.

## Addendum (2026-10-20): orphan hub MOCs (issue #300)

**Status:** Accepted amendment. Answering the open question about a hub MOC whose hub note was deleted: `StateDB.list_orphan_hub_mocs` finds them, the hub report (`orphan_hubs`, with a warning) and `zettel doctor` ("MOCs hub orfaos") flag them, and `delete-source` lists the ones its purge left. They are not removed automatically; `garden --hubs --recreate` removes them and `zettel adopt-moc` keeps them as manual MOCs (ADR-061). Hub MOCs no longer carry `cluster_signature` in the frontmatter.
