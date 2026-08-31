# Potential ADR: Hub-Anchored MOC Pipeline as Complementary Strategy

**Module**: GARDEN  
**Category**: Clustering Strategy / Knowledge Graph Organization  
**Priority**: Must Document (Score: 135)  
**Date Identified**: 2026-08-30  

---

## What Was Identified

The GARDEN module includes a **complementary hub-anchored MOC generation pipeline** (`Phase 4b`) that runs independently from the taxonomy-first clustering pipeline. Rather than organizing MOCs around user-defined categories, the hub pipeline:

1. **Ranks permanent notes** by weighted graph degree (configurable weights for edge types: `contradicts`, `refutes`, `supports`, `related`, etc.)
2. **Selects top-N hubs** using either `percentile` mode (top 10%, 5%, etc.) or `absolute` mode (notes with degree ≥ threshold)
3. **Expands neighborhoods** via BFS (Breadth-First Search) with weighted edges and hop-decay, respecting `max_hops` and `max_neighbors` limits
4. **Deduplicates overlapping neighborhoods** — drops smaller hubs whose neighborhoods are ≥ `dedup_subset_threshold` contained in larger ones
5. **Routes each hub cluster** through the same single-LLM-call logic (incremental if `hub_note_id` already has a MOC; else `moc_hub_generation` with LLM-derived topic)
6. **Persists with distinct origin** (`origin='hub_pipeline'`) — allows `garden --hubs --recreate` to purge only hub MOCs, leaving taxonomy MOCs untouched

**Temporal context**: Introduced in commit 930cb75 (2026-08-27) with keyword "complementary" and "hub-anchored," signaling an intentional dual-strategy design where both taxonomy-first and hub-anchored MOCs coexist. Recent activity is stable with no rollbacks; this represents a refined organizational principle, not a temporary experiment.

## Why This Might Deserve an ADR

- **Impact**: This decision **doubles the MOC generation strategy** — users can build knowledge organization around both taxonomic categories *and* highly-connected "hub" notes. A single note's neighbors become a natural MOC boundary, complementary to topic-based organization.

- **Trade-offs**:
  - **Pro**: Hub-based MOCs mirror how people actually navigate knowledge graphs — starting from a central note and exploring its neighborhood is intuitive.
  - **Con**: Hub MOCs may be redundant with taxonomy MOCs if hubs and category clusters overlap significantly (deduplication helps but doesn't guarantee orthogonality).
  - **Pro**: Hub ranking (graph degree) is a structural signal independent of content embeddings; captures "importance" differently than vector similarity.
  - **Con**: Requires a well-formed note graph with explicit backlinks. Sparse or weakly-connected graphs produce few/poor hubs.
  - **Pro**: Origin tagging allows selective purge/regeneration (`--hubs --recreate` vs. `--recreate`) for experimental tuning.
  - **Con**: Dual MOC pipelines increase complexity and maintenance burden (two LLM prompt templates, two generation functions).

- **Complexity**: The hub pipeline interleaves graph traversal (BFS with weighted edges), hub ranking (percentile or absolute), neighborhood deduplication, and LLM routing. Understanding the ranking strategy and deduplication criteria is essential for tuning.

- **Team Knowledge**: Anyone working on MOC organization or graph-based features must understand:
  - Why hub selection uses percentile (relative ranking) vs. absolute degree thresholds
  - How BFS expansion respects edge weights and decay (a highly-related neighbor counts more than a distant `related` neighbor)
  - What deduplication does and when it filters out reasonable hubs
  - How hub MOCs interact with taxonomy MOCs (coexistence vs. conflict)
  - Why `moc_hub_generation.md` is a distinct prompt from `moc_generation.md`

- **Future Implications**:
  - Changes to the note graph structure (new edges, deleted notes) directly affect hub ranking; requires re-running `garden --hubs` to update.
  - Edge-weight configuration in `config.yaml` has subtle effects on BFS expansion; tuning requires experimentation.
  - Hub MOCs are persistent (frontmatter stores `hub_note_id` and `origin='hub_pipeline'`); if user deletes a hub note, its associated MOC becomes orphaned (no automatic cleanup).

**Temporal Context**: This pattern has been stable for 3 days (2026-08-27 to 2026-08-30) with no rollbacks or fundamental rework. The dual-strategy design appears deliberate and refined.

## Evidence Found in Codebase

### Key Files

- [`zettel/gardener_hub.py`](../../../../zettel/gardener_hub.py) - Full module (625 lines)
  - `rank_note_hubs()`: Ranks permanent notes by weighted degree; selects top-N using percentile or absolute threshold
  - `build_hub_neighborhood()`: BFS expansion around a hub with configurable decay and max hops
  - `dedup_hub_neighborhoods()`: Removes smaller hubs whose neighborhoods are mostly contained in larger ones
  - `run_hub_garden()`: Orchestrates ranking → expansion → dedup → routing (similar structure to `run_garden()`)

- [`zettel/gardener.py`](../../../../zettel/gardener.py) - Lines 60-62 (entry point dispatching)
  - Checks for `--hubs` CLI flag to branch to hub pipeline

- [`zettel/config.py`](../../../../zettel/config.py) - `HubMocsConfig` class
  - Defines tuning knobs: `selection_mode`, `hub_percentile`, `top_n_hubs`, `min_weighted_degree`, `max_hops`, `max_neighbors`, `min_neighbor_weight`, `dedup_subset_threshold`, `decay`

### Code Evidence

**Hub ranking with percentile selection** (gardener_hub.py:44-70):
```python
def rank_note_hubs(db: StateDB, cfg: HubMocsConfig, ...) -> list[tuple[str, float]]:
    weights = relation_weights or DEFAULT_RELATION_WEIGHTS
    degrees = db.get_weighted_note_degrees(weights)
    
    if cfg.selection_mode == "absolute":
        candidates = [(nid, deg) for nid, deg in ranked if deg >= cfg.min_weighted_degree]
    else:
        idx = int((1.0 - cfg.hub_percentile) * (len(ranked) - 1))
        threshold = ranked[idx][1]
        candidates = [(nid, deg) for nid, deg in ranked if deg >= threshold]
    
    return result[: cfg.top_n_hubs]
```

**BFS neighborhood expansion with decay** (gardener_hub.py:89-119):
```python
neighbors = expand_notes(
    db,
    [hub_id],
    max_hops=cfg.max_hops,
    decay=cfg.decay,
    relation_weights=weights,
    max_neighbors=max_neighbor_slots * 2,
    seed_weights={hub_id: 1.0},
)
```

**Deduplication of overlapping hubs** (gardener_hub.py:122-147):
```python
def dedup_hub_neighborhoods(hubs_with_notes: list[tuple[str, float, list[str]]], threshold: float):
    for hub_id, _degree, note_ids in sorted_hubs:
        current = set(note_ids)
        for prev in accepted_sets:
            overlap = len(current & prev) / len(current)
            if overlap >= threshold:
                skip = True
                break
        if not skip:
            accepted.append((hub_id, note_ids))
```

**Origin tagging for selective purge** (gardener_hub.py:~366):
```python
db.upsert_moc(..., origin="hub_pipeline")
```

### Impact Analysis

- **Introduced**: 2026-08-27 (commit 930cb75)
- **Modified**: 3+ commits with themes:
  - "Add hub-anchored MOC pipeline"
  - "Change of type from moc to hub_moc"
  - "Fix hub deduplication"
- **Recent activity**: Stable; no rollbacks or removals
- **Affects**:
  - Every high-degree permanent note (potential hub)
  - Knowledge graph navigation patterns (users can follow hub neighborhoods)
  - MOC organization diversity (dual-strategy reduces single-failure risk)
- **Dependencies**:
  - `note_connections` graph (edge list and weights)
  - Graph traversal library (Python BFS in `graph.py`)
  - Hub-specific LLM prompt template (`prompts/moc_hub_generation.md`)
  - Origin-aware MOC storage in SQLite and ChromaDB

### Alternatives (if observable)

1. **Single MOC pipeline (taxonomy-only)**:
   - Previous design before Phase 4b
   - Observed: `run_garden()` can run with `--hubs` flag omitted
   - Trade-off: Simpler, but loses graph-based organization; users must rely on taxonomy

2. **Hub selection via community detection (e.g., Louvain, Leiden)**:
   - Graph-based clustering instead of degree ranking
   - Not implemented; degree ranking is simpler and faster
   - Trade-off: Community detection finds natural graph clusters; degree ranking finds "important" nodes only

3. **Fixed-size hubs (e.g., always top 10 notes)**:
   - Current: `top_n_hubs` config allows tuning
   - Trade-off: Percentage-based (`hub_percentile`) scales with graph size; absolute count doesn't

## Questions to Address in ADR (if created)

1. **Why two separate MOC pipelines instead of one unified strategy?**
   - Taxonomy-first assumes user has a well-defined domain structure. Hub-first assumes user has a well-formed note graph. These are different organizational principles; supporting both serves different user workflows.

2. **How do taxonomy MOCs and hub MOCs interact?**
   - They coexist independently. A note can appear in both a taxonomy MOC and a hub MOC if it's in the right category and also neighbors a high-degree hub. There's no automatic deduplication across pipeline boundaries (only within hubs).

3. **Why BFS expansion instead of community detection?**
   - BFS is deterministic, fast, and transparent. Community detection is slower and creates hidden cluster assignments; BFS expansion gives explicit control via `max_hops` and `max_neighbors`.

4. **When should `selection_mode` be `percentile` vs. `absolute`?**
   - Percentile scales with graph size (useful for growing knowledge bases). Absolute is stable if your target hub count doesn't change (useful for fixed-size archives).

5. **What happens if a hub note is deleted?**
   - Its associated MOC (`origin='hub_pipeline'`) becomes orphaned. Consider a cleanup pass in future versions.

## Related Potential ADRs

- **Taxonomy-First MOC Clustering** — The primary (taxonomy-driven) complementary strategy
- **Single LLM Call Per Cluster with Intelligent Routing** — The shared routing logic for both pipelines
- **Graph-Based Note Discovery with Weighted BFS Expansion** — Underlying graph traversal used for hub neighborhood expansion

## Additional Notes

- **Duplication with gardener.py**: The mapping.md notes that `gardener_hub.py` "reaches into ~11 private symbols of `gardener.py` (documented duplication)." Consolidating the shared routing logic (`_process_cluster`, MOC body building, etc.) into a shared module could reduce coupling.

- **Origin tagging as a design pattern**: Tagging MOCs with `origin='pipeline'` vs. `origin='hub_pipeline'` vs. `origin='manual'` is a lightweight way to track provenance and enable selective operations. This pattern is useful and worth documenting as a broader data-governance principle.

- **Hub persistence in frontmatter**: MOCs store `hub_note_id` in frontmatter, linking them to their hub. If a hub note is deleted, the MOC still references it. Consider adding a cleanup utility or warning during `delete-source` / `purge-source` operations.

- **BFS decay behavior**: The `decay` parameter (default 0.5) reduces neighbor weight with each hop. A value of 1.0 means no decay; 0 means only direct neighbors count. This is a sensitive tuning knob; consider documenting recommended ranges.
