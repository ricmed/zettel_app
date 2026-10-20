# ADR-XXX: Single LLM Call Per Cluster with Intelligent Routing
**Status:** Accepted
**Date:** 2026-08-26
**Used by:** [ADR-XXX: Hub-Anchored MOC Generation as a Complementary Clustering Strategy](./ADR-020-hub-anchored-moc-pipeline.md)
**Related to:**
- [ADR-XXX: Graph-Based Note Discovery with Weighted BFS Expansion](../RETRIEVAL/ADR-009-graph-based-note-discovery-weighted-bfs.md)
- [ADR-XXX: Taxonomy-First MOC Clustering with UMAP+HDBSCAN](./ADR-019-taxonomy-first-moc-clustering.md)
- [ADR-XXX: Layered Hashing Strategy for Deterministic Caching and Drift Detection](../INFRA/ADR-007-layered-hashing-strategy.md)

## Context and Problem Statement

The GARDEN module builds MOCs (Maps of Content) from clusters of notes through two pipelines — taxonomy-first and hub-anchored. Each cluster needs to become a coherent MOC, but calling an LLM once per cluster to generate it, and again to refine it if the result is weak, scales cost and latency linearly with refinement rounds rather than with clusters. In a vault with hundreds of notes, that difference compounds quickly across a single `zettel garden` run.

The system instead routes every cluster through a fixed five-step decision tree before ever reaching generation: an exact note-set signature match reuses an existing MOC at zero cost; overlap with a previously generated MOC above a configurable threshold triggers a single incremental-update call instead of regeneration; a category match against an existing MOC does the same; a graph cohesion gate can silently reject a cluster before any call is made; only if none of these apply does the system make one generation call. The result is a hard ceiling of one LLM call per cluster, with several paths costing nothing at all.

[NEEDS INPUT: Quantified cost/latency savings versus multi-round refinement have not been benchmarked — only the relative estimate (3-5x higher cost for iterative refinement) is documented.]

This pattern was introduced in commit 216a725 (2026-08-26) as part of the taxonomy-first clustering pipeline and has been stable for several days, with only threshold and weight tuning since — no rollback or structural rework.

## Decision Drivers

- LLM cost per `zettel garden` run must scale with the number of new clusters, not with refinement rounds.
- MOC generation latency must stay low enough for interactive CLI use, ruling out multi-round refinement loops.
- Routing must be deterministic and cacheable so identical clusters (by signature) never repeat an LLM call.
- Incremental updates must preserve user edits to existing MOC structure rather than overwrite them on regeneration.
- The decision path for any given cluster must be traceable, so tuning `overlap_threshold` and cohesion gates is debuggable.

## Considered Options

1. Single LLM call per cluster via five-step routing tree (signature → overlap → category → cohesion → generation)
2. Multi-round LLM refinement (generate, evaluate, refine until a quality threshold is met)
3. Heuristic/statistical topic extraction (TF-IDF or LDA) without any LLM call

## Decision Outcome

Chosen option: single LLM call per cluster via the five-step routing tree, because it bounds LLM spend to a constant per cluster, lets identical clusters skip generation entirely through signature caching, and routes overlapping or same-category clusters into incremental updates that preserve prior structure instead of discarding it. The trade-off is that a weak first-pass MOC has no feedback loop to improve itself short of a manual `--recreate`.

## Pros and Cons of the Options

### Single LLM call per cluster with routing (chosen)

- Good, because cost is predictable: at most one call per cluster, with several paths costing zero.
- Good, because signature and overlap routing are deterministic and cache-friendly.
- Good, because incremental updates preserve manually reorganized MOC subsections.
- Bad, because a poor first-pass generation persists until an explicit recreate; there is no in-pipeline feedback mechanism.

### Multi-round LLM refinement

- Good, because iterative feedback could improve MOC quality beyond a single pass.
- Bad, because cost and latency run 3-5x higher per MOC with no bound on refinement rounds.
- Bad, because there is no quality-scoring mechanism in the current pipeline to know when to stop refining.

### Heuristic/statistical topic extraction

- Good, because it eliminates LLM cost and latency entirely.
- Bad, because topic and subsection quality is lower and less semantically coherent than LLM-generated structure.
- Bad, because it would require a separate code path from the LLM-based prompts already used for both generation and incremental updates.

## Consequences

Cost monitoring only needs to count MOC creations and incremental updates, not raw LLM calls, since the routing tree already guarantees at most one call per cluster. Silent cohesion rejection means clusters that fail the gate leave no MOC and no visible record beyond internal stats counters. [NEEDS INPUT: Is silent, unlogged cohesion rejection an accepted trade-off, or should rejected clusters be surfaced for review?]

Because generation (`moc_generation.md`) and incremental update (`moc_incremental.md`) are separate prompts — mirrored again for the hub pipeline — editing one without the other can produce inconsistent MOC quality between a cluster's first pass and its later incremental updates. If prompt or model quality improves later, existing MOCs do not retroactively improve; they carry the quality of whichever pass last touched them until manually regenerated.

## References

- `zettel/gardener.py:232-290` — `_process_cluster()`, the five-step routing tree
- `zettel/gardener.py:293-397` — `_create_new_moc()`, the single generation call path
- `zettel/gardener_assign.py:157-182` — `find_moc_by_note_overlap()`, overlap-threshold routing
- `zettel/gardener_hub.py` — `_process_hub_cluster()`, mirrored routing for the hub-anchored pipeline

## Addendum (2026-10-19): category routing stays inside its own pipeline (issue #292)

**Status:** Accepted amendment. The third routing branch used `find_moc_by_topic`, which scanned every MOC (manual and hub included) by substring in both directions. A cluster in the "Epistemologia" bucket could land in a manual MOC "Epistemologia" or in a hub MOC whose free topic contained the word; `_update_existing_moc` then rewrote the whole file with `safe_write_note` and stored `origin='pipeline'`, and the next `garden --recreate` deleted it.

- `StateDB.find_moc_by_topic(topic, *, origins)` takes the origin set explicitly (`None` means any origin and must be spelled out) and matches by **folded equality** (`search_terms.fold`), never substring. The taxonomy pipeline passes `("pipeline",)`. The article's MOC boost passes `None`, so a manual MOC still boosts its topic, but "IA" no longer matches "Avaliação".
- `gardener.owns_moc` guards both incremental rewrites (taxonomy and hub): the row **and** the file frontmatter must name the writing pipeline, otherwise the MOC is not touched.
- `purge_mocs_by_origin` (shared by `--recreate` and `--hubs --recreate`) keeps any row whose file frontmatter names another origin, file and row both. `delete_pipeline_mocs` / `delete_hub_pipeline_mocs` were replaced by `list_mocs(origin=...)` + `delete_mocs(ids)`.

Correction to the decision drivers above: the incremental update preserves the structure the pipeline itself writes (summary, subsection titles and descriptions, `[[ZTL - …]]` lines). It does **not** preserve arbitrary user edits in a pipeline MOC; prose outside that structure is lost on the next rewrite. Adopting a generated MOC as manual is tracked in #300.

## Addendum (2026-10-19): every routing decision is recorded (issue #298)

**Status:** Accepted amendment. Answering the open question above ("Is silent, unlogged cohesion rejection an accepted trade-off?"): no. The routing tree is now the read-only `gardener.route_cluster`, shared by the run and by `zettel garden --plan`, and every cluster's outcome (`signature_skip`, `incremental`, `created`, `rejected_cohesion`, `failed`) lands in the run's `GardenReport` (`garden_reports` table), together with the notes the incremental LLM answered `"ignorar"`. The report also carries the entry-point metrics of epic #291 — reachability within 1 and 2 hops of the notes listed in MOCs, and subjects without a door — because under the entry-point design a note outside every MOC is expected and is not, by itself, a failure.
