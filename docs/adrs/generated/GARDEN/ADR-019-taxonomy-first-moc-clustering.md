# ADR-XXX: Taxonomy-First MOC Clustering with UMAP+HDBSCAN
**Status:** Accepted
**Date:** 2026-08-26
**Related to:**
- [ADR-XXX: Graph-Based Note Discovery with Weighted BFS Expansion](../RETRIEVAL/ADR-009-graph-based-note-discovery-weighted-bfs.md)
- [ADR-XXX: Single LLM Call Per Cluster with Intelligent Routing](./ADR-021-single-llm-call-per-cluster-routing.md)
- [ADR-XXX: Hub-Anchored MOC Generation as a Complementary Clustering Strategy](./ADR-020-hub-anchored-moc-pipeline.md)

## Context and Problem Statement

The MOC generation pipeline (Phase 4) needed a way to group permanent notes into Maps of Content that align with the user's own knowledge domains, rather than with whatever structure a similarity metric happens to produce. A purely algorithmic approach — clustering all notes globally and mapping the resulting groups to categories afterward — risks producing MOCs that don't correspond to any domain the user recognizes, since embedding-space proximity and taxonomic meaning are not guaranteed to align.

The system instead embeds the labels from the user's category taxonomy (`config/moc_topics.yaml`) into the same vector space as the notes, assigns each note to its highest-similarity category first, and only then clusters within each category bucket using UMAP for dimensionality reduction and HDBSCAN for density-based grouping (falling back to KMeans when either optional dependency is unavailable). This reverses the usual cluster-then-label order: the taxonomy becomes the primary organizing structure, and clustering happens only to subdivide an already-relevant bucket into coherent sub-topics.

This design was introduced in commit 216a725 (2026-08-26) and modified 8+ times over the following four days — tuning category assignment, adding graph cohesion scoring, and refining taxonomy validation — before stabilizing with no rollback or fundamental rework since.

## Decision Drivers

* Generated MOCs must map onto the user's own taxonomy so navigation stays predictable, rather than surfacing clusters with no recognizable domain.
* Taxonomy labels can be abstract concepts not literally present in note text, so assignment needs semantic (embedding) similarity rather than keyword or rule matching.
* LLM calls are costly, so cluster routing must guarantee at most one call per cluster, reusing existing MOC structure when note overlap is high instead of regenerating.
* Density-based clustering must be able to leave ambiguous or outlier notes unassigned rather than forcing every note into some MOC.
* UMAP and HDBSCAN are optional dependencies, so the pipeline must keep working when they are not installed.
* Cluster acceptance should be able to draw on existing graph structure (`note_connections`) to reject clusters that are only superficially similar in embedding space.

## Considered Options

* Taxonomy-first: embed category labels, assign notes to categories, then cluster within each category bucket (UMAP+HDBSCAN, KMeans fallback) — chosen
* Pure global clustering: cluster all notes first, then map the resulting clusters to categories post-hoc
* Fixed cluster count (e.g., a constant k) instead of density-based, note-count-scaled cluster sizing

## Decision Outcome

Chosen option: taxonomy-first clustering with per-category UMAP+HDBSCAN, because assigning notes to categories before clustering guarantees that MOC output stays anchored to domains the user already defined, rather than to emergent groupings that clustering alone would produce. HDBSCAN's explicit noise label additionally lets ambiguous notes stay out of MOCs entirely instead of being force-fit into the nearest cluster, and the single-LLM-call-per-cluster routing keeps generation cost bounded regardless of which clustering backend produced the buckets.

The pure global clustering path was not removed — it remains in the code as `cluster_notes_global()`, used when `gardener.cluster_within_category` is disabled or category assignment fails — but taxonomy-first is the default because it directly optimizes for the property the pipeline is meant to guarantee: navigable, domain-aligned MOCs. [NEEDS INPUT: Was the KMeans fallback's behavioral difference from HDBSCAN — no noise handling, every note forced into a cluster — accepted as a deliberate trade-off for environments without the optional dependencies, or is it treated as a known gap the code comments describe as a "candidate for Phase 2"?]

## Pros and Cons of the Options

### Taxonomy-first clustering (chosen)

* Good, because MOCs stay aligned to the user's own domain vocabulary instead of arbitrary embedding-space groupings
* Good, because HDBSCAN's noise label keeps outlier notes out of MOCs rather than forcing a poor-fit assignment
* Good, because category hints bias the per-cluster LLM call toward consistent naming and structure
* Bad, because an incomplete or misaligned taxonomy fragments notes across unrelated buckets, favoring taxonomy consistency over cluster tightness

### Pure global clustering (retained as fallback)

* Good, because it does not depend on taxonomy quality — clusters form purely from note similarity
* Good, because it still exists in code (`cluster_notes_global()`) as an escape hatch when category assignment fails
* Bad, because clusters may not correspond to any category a user recognizes, making category mapping lossy after the fact
* Bad, because it was the pipeline's original approach and was deliberately superseded for MOC generation

### Fixed cluster count

* Good, because it produces a predictable, bounded number of MOCs per run
* Bad, because a hard-coded k does not adapt to how many notes actually exist in a category bucket
* Bad, because it was not adopted — the current dynamic sizing (`n // min_cluster_size`) was chosen instead, and no evidence in the codebase shows fixed-k was implemented or benchmarked

## Consequences

Because category assignment runs before clustering, changing the taxonomy — adding, removing, or renaming categories in `config/moc_topics.yaml` — can leave existing MOCs misaligned with the new structure, requiring a full `garden --recreate` to reassign notes and regenerate clusters. The `gardener.cluster_within_category` flag remains available as an escape hatch for taxonomies that are too flat or incomplete for per-category clustering to be useful, falling back to the pre-existing global clustering path.

The silent fallback from UMAP+HDBSCAN to KMeans on `ImportError` (or any exception during reduction/clustering) changes cluster shape and noise handling without any signal at the point a MOC is generated — only a log warning is emitted. [NEEDS INPUT: Should the clustering backend actually used be recorded as MOC metadata or surfaced to the user, given that KMeans forces every note into a cluster while HDBSCAN does not?] Category label embeddings are also recomputed on every `garden` run rather than cached, which adds latency proportional to taxonomy size on each invocation. [NEEDS INPUT: Was caching category embeddings across runs evaluated and rejected, or is this an accepted cost given how infrequently taxonomies change?]

## References

* `zettel/gardener.py:60-169` — `run_garden()` entry point: taxonomy loading, category validation, clustering fallback orchestration
* `zettel/gardener.py:232-290` — `_process_cluster()`: routing through overlap detection, category match, cohesion gate, single LLM call
* `zettel/gardener_assign.py:27-105` — `embed_category_labels()`, `assign_notes_to_categories()`, `cluster_notes_within_buckets()`
* `zettel/gardener_assign.py:198-239` — `_cluster_embeddings()`: UMAP+HDBSCAN configuration and silent KMeans fallback
* `config/moc_topics.yaml` — taxonomy structure driving category assignment

## Amendment (2026-09-06)

Category labels default to `"{pilar}: {categoria}"`. A shared `domain` prefix on every label collapsed cosine argmax in a mixed vault. `embed_category_labels` takes `(pilar, categoria)` pairs; `domain` remains available on the template for a custom override. Category names must be globally unique (`duplicate_category_names`). The transversal pillar "Problemas Transversais" is the intended home for bridge notes. After this change, `zettel garden --recreate` is required.

## Addendum (2026-10-06): shadow category per cluster

Before each cluster is routed, `run_garden` calls `decision.shadow.shadow_moc_category`. The call asks the typed decision layer for a `choice` over the taxonomy categories, plus `none`, given at most 20 of the cluster's notes and its frequent terms. The verdict is recorded next to the embedding argmax and changes nothing ([ADR-055](../LLM/ADR-055-typed-decision-layer-shadow.md)).

The baseline is the argmax, not the LLM's `topic`: `_create_new_moc` overwrites the LLM's topic with the embedding category whenever that category is allowed, so the LLM's topic is not an independent judgement. The `none` option also measures something the argmax cannot express, because the argmax has no threshold and always picks a category.

## Addendum (2026-10-19): reproducible partition, effective minimum and reported backend (issue #294)

**Status:** Accepted amendment.

- **Seed.** UMAP ran without `random_state`. Re-running the garden's own `_cluster_embeddings` five times on the same vectors gave a mean pairwise ARI of 0.36–0.69 and 0–38% identical clusters, so the signature branch of ADR-021 almost never fired (0 of 22 clusters on 2026-10-07). `gardener.random_state` (default `42`, also seeds the KMeans fallback) fixes the partition; `null` restores the faster nondeterministic path. `scripts/probe_garden_stability.py` (no LLM, no embedding) measures it: with the seed, ARI 1.0 and 100% identical clusters on every scope of the measured vault.
- **Effective minimum.** `config.yaml` sets `min_cluster_size: 5` and `min_notes_for_moc: 10`, so HDBSCAN accepted clusters of 5–9 notes that were then dropped. The density step now uses `effective_min_cluster_size = max(min_cluster_size, min_notes_for_moc)`; `min_cluster_size` keeps its role as the minimum vault size to try clustering. Consequence measured on the same vault: partitions are coarser (4 global clusters instead of 16; several MOC-sized buckets stay whole), which suits the entry-point design tracked in #297.
- **Backend reported.** Answering the open question above: `gardener_assign.ClusterReport` records which backends produced clusters (`umap+hdbscan`, `kmeans`, `single` for a group kept whole), the cluster count, notes left as noise and buckets too small for a MOC. The garden logs it and warns when KMeans ran (no noise, every note forced into a cluster); persisting it in the run report is #298.
- A bucket note without a vector no longer shifts the id/vector pairing in `cluster_notes_within_buckets`.

## Addendum (2026-10-19): clusters feed entry points, not lists (issue #297)

**Status:** Accepted amendment. A cluster is still the routing unit, but its MOC lists only the entry notes chosen by `garden_entries.select_entry_notes` (ADR-060). The drivers' claim that ambiguous notes "stay out of MOCs" now holds by design for every note outside the selection, not only for HDBSCAN noise; reachability from the entries (#298) is the measure.
