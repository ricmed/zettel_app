# ADR-060: MOCs and Hubs Are Entry Points, Not Complete Indexes

**Status:** Accepted
**Date:** 2026-10-19
**Issue:** #297 (epic #291)
**Depends on:** [ADR-019: Taxonomy-First MOC Clustering](./ADR-019-taxonomy-first-moc-clustering.md), [ADR-020: Hub-Anchored MOC Generation](./ADR-020-hub-anchored-moc-pipeline.md), [ADR-021: Single LLM Call Per Cluster](./ADR-021-single-llm-call-per-cluster-routing.md)
**Related to:** [ADR-009: Graph-Based Note Discovery](../RETRIEVAL/ADR-009-graph-based-note-discovery-weighted-bfs.md), [ADR-036: Topic Index Is Routing](../RETRIEVAL/ADR-036-topic-index-routing-not-representation.md), [ADR-035: Flat Agent Skill Export](../CLI/ADR-035-flat-agent-skill-export.md)

## Context and Problem Statement

The only written intent about MOC membership was that HDBSCAN noise stays out of MOCs and stays navigable through the graph. The code went the other way: the prompts required that **every** listed note appear in a subsection, and `_build_moc_body` / `_apply_incremental_placements` reconciled any note the LLM skipped into an "Outras notas do cluster" section. On the development vault (`docs/artigo/05-garden.md`, §4.6), 226 of 465 MOC entries (48.6%) sat in that section; "Sistemas Autônomos" listed 124 notes, 104 of them without a word of structure.

The author's intent is the opposite. A MOC (or hub MOC) is a **door into a subject**: it shows the few notes a reader should start from — the most connected, the most representative, the first of the subject — and the reader reaches the rest through the links of those notes. Many notes stay out of every MOC by design, and a large subject should be split rather than grow into a list.

## Decision Drivers

* A MOC must stay readable as a map: a handful of entries with a reason to start from each.
* Notes outside every MOC must stay reachable, so the measure of success is reachability over the graph, not list coverage.
* The choice of entries must be cheap, deterministic and measurable (no LLM call to pick them).
* Hubs already cap their neighbourhood; taxonomy and hub MOCs should follow one rule.

## Decision Outcome

1. **Selection, not coverage.** `zettel/garden_entries.select_entry_notes` ranks a cluster's notes by one signal (`gardener.entry_selection`) and keeps the top `gardener.max_entries_per_moc` (10). Signals, all computed without an LLM: `internal_degree` (weighted degree inside the cluster over traversable edges), `medoid` (cosine to the cluster centroid), `first_in_source` (earliest `created_at`), `relevance` (the concept's `relevance_score`) and `corroboration` (count of `corroborates` edges); ties fall back to internal degree, then `note_id`. Routing still uses the whole cluster (signature, overlap, category); the MOC receives only the entries. A hub MOC lists the hub plus `max_entries_per_moc - 1` neighbours chosen the same way (`gardener_hub.hub_entry_notes`).
2. **Provisional default: `internal_degree`, cap 10.** Measured on the development vault (`evals/results/297-portas-de-entrada.json`, using each existing MOC as a subject), ten entries per MOC by internal degree reach 93.2% of the 781 notes within two hops; the other signals reach 90.3%–93.3%. Reachability barely separates them, so the default is provisional until the blind human judgement pre-registered in `evals/preregistration/297-portas-de-entrada.md`.
3. **No forced reconciliation.** The "Outras notas do cluster" section is gone. An entry the LLM does not place is counted as `omitted` in the run report (#298) and not listed; a subsection holds at most `max_entries_per_subsection` (5) entries. A creation in which the LLM places no entry writes no MOC.
4. **Orientation per entry.** `MOCSubsection.entries` holds `MOCEntry(note_id, orientation)`; the body renders `- [[ZTL - …]] — orientation` and `_parse_moc_structure` reads it back. In incremental updates the placement's `reason` is the orientation.
5. **Capacity.** An incremental update offers a MOC only as many candidates as it has room for; a full MOC takes none and costs no LLM call (`cap_reached` in the report). Those notes remain reachable from the MOC's entries. Splitting a subject that outgrows the cap into sub-MOCs is #296.
6. **Summary follows the entries.** The frontmatter keeps `summary_entries`; when the entries grow by `gardener.summary_refresh_growth` (50%) or more, `prompts/moc_summary_refresh.md` rewrites the summary in one short call.
7. **Measure of success.** Reachability `R_k` within 1 and 2 hops of the entries, over `edge_is_traversable` edges, and subjects without a door (`zettel/garden_report.py`). A note outside every MOC is not a failure by itself.
8. **Dependency on the graph.** An entry point is only as good as the path that leaves it. Since #279/#287, LLM edges enter the graph as `proposed`; without reviewing that queue, the same entries reach far less (23.9% instead of 92.4% within two hops on the measured vault). How hubs and cohesion treat proposed edges is #299.

## Consequences

* MOCs are short and stable; their `auto-moc-backrefs` and their `topic_index_terms` rows (ADR-036) now cover only the entries, so the `ask` topic-index boost routes only to entry notes. Every other note is still found by the hybrid retriever and the graph expansion.
* `zettel skill --moc-id` / `--topic` export the entries, not the whole subject (ADR-035).
* After this change, `zettel garden --recreate` and `zettel garden --hubs --recreate` rebuild the MOCs in the new format.

## References

* `zettel/garden_entries.py` — `select_entry_notes`, the five signals
* `zettel/gardener.py` — `render_moc_body`, `_parse_moc_structure`, `_merge_incremental`, `_update_existing_moc`, `_create_new_moc`, `_refresh_summary`
* `zettel/gardener_hub.py` — `hub_entry_notes`, `_create_new_hub_moc`, `_update_hub_moc`
* `zettel/schemas.py` — `MOCEntry`, `MOCSubsection`, `MOCSummaryOutput`
* `prompts/moc_generation.md`, `prompts/moc_incremental.md`, `prompts/moc_hub_generation.md`, `prompts/moc_hub_incremental.md`, `prompts/moc_summary_refresh.md`
* `scripts/probe_moc_entry_points.py`, `evals/preregistration/297-portas-de-entrada.md`, `evals/results/297-portas-de-entrada.json`

## Addendum (2026-10-20): splitting large subjects (issue #296)

**Status:** Accepted amendment. Point 5 left the split of a subject that outgrows the cap to #296. A category whose clusters do not fit in `max_entries_per_moc` at `min_entries_per_subtopic` entries each becomes a MOC of MOCs: the category MOC lists only its sub-MOCs ("Subtemas", one orientation line each) and every sub-MOC lists its own entry notes under the same cap. Sub-MOC entries count as entries for reachability; the category MOC contributes none of its own. A MOC that is full no longer silently absorbs a new cluster: the cluster gets a sub-MOC. Notes the LLM answers `"ignorar"` are kept per MOC (`moc_discards`) instead of being re-offered every run.
