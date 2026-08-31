# Potential ADR: Single LLM Call Per Cluster with Intelligent Routing

**Module**: GARDEN  
**Category**: Cost Optimization / Generation Strategy  
**Priority**: Must Document (Score: 130)  
**Date Identified**: 2026-08-30  

---

## What Was Identified

The GARDEN module enforces a **single LLM call per cluster** rule across both MOC generation pipelines (taxonomy-first and hub-anchored). Rather than calling the LLM multiple times to refine a MOC, the system routes each cluster through an intelligent decision tree that avoids LLM calls whenever possible:

1. **Signature match** — If the exact same note set has already been processed (same notes in same order), reuse the existing MOC (zero LLM calls)
2. **Overlap detection** — If ≥ `overlap_threshold` (default 40%) of notes already exist in a previously-generated MOC, treat it as an **incremental update** (one LLM call to `moc_incremental.md`, not regeneration)
3. **Category match** — If a MOC for this cluster's dominant category already exists, also treat as incremental (again, one LLM call, not generation)
4. **Cohesion gating** — If graph cohesion scoring is enabled and the cluster fails the gate, reject silently (zero LLM calls, MOC not created)
5. **Generation as last resort** — Only if none of the above conditions match, call the LLM to generate a new MOC (one LLM call to `moc_generation.md`)

**Result**: Each cluster produces at most one LLM call. Incremental updates reuse existing MOC structure and refine it with new notes, rather than regenerating from scratch.

**Temporal context**: Introduced in commit 216a725 (2026-08-26) as part of the taxonomy-first clustering pipeline. The routing logic has been stable for 4 days with no fundamental changes, only parameter tuning (overlap thresholds, cohesion weights). This pattern was intentional from inception, not an optimization retrofit.

## Why This Might Deserve an ADR

- **Impact**: This decision **directly controls LLM cost and latency** for MOC generation. In a knowledge base with hundreds of notes, the difference between 1 call/cluster vs. N calls/cluster (iterative refinement) is 10x-100x in cost and latency.

- **Trade-offs**:
  - **Pro**: Cost-predictable (at most one LLM call per unique cluster). Fast enough for interactive use (no waiting for 10 LLM refinement rounds).
  - **Con**: Loses iterative refinement — the LLM cannot improve an MOC structure based on feedback or weak initial generation.
  - **Pro**: Deterministic and cacheable (same cluster signature always produces same MOC structure, enabling deterministic LLM response caching).
  - **Con**: If the first-pass generation is poor, the MOC persists until next `--recreate` run.
  - **Pro**: Incremental updates preserve user edits to existing MOC structure (e.g., manually reordered subsections).
  - **Con**: Incremental updates may produce incoherent topic names if new notes don't fit the original category.

- **Complexity**: The routing decision tree (`_process_cluster()`) has 5 decision points in order. Understanding when each path is taken is essential for debugging MOC behavior and tuning overlap/cohesion thresholds.

- **Team Knowledge**: Anyone working on MOC quality, cost optimization, or batch operations must understand:
  - Why incremental updates are preferred over regeneration
  - How `overlap_threshold` decides incremental vs. generation paths
  - What "cluster signature" is and why identical clusters skip LLM entirely
  - Why cohesion gating can silently drop clusters
  - How category matching influences routing (and why it matters for organized knowledge)
  - The performance characteristics of each path (signature check is O(1), overlap detection is O(n*m) where n=existing MOCs, m=cluster size)

- **Future Implications**:
  - Cost monitoring is straightforward (count MOC creations, not LLM calls, to estimate cost).
  - If LLM generation improves (e.g., via system prompt tuning), existing MOCs won't automatically improve — users must manually delete and regenerate.
  - Adding new decision points to the routing tree (e.g., "skip if cohesion < 0.3") requires careful performance analysis.

**Temporal Context**: This pattern has been stable for 4 days with only configuration tuning (threshold values, weights). No rollbacks or fundamental rework; the single-call constraint is foundational to the design.

## Evidence Found in Codebase

### Key Files

- [`zettel/gardener.py`](../../../../zettel/gardener.py) - Lines 232-290
  - `_process_cluster()`: Entry point for the routing decision tree
  - Implements the 5-step routing logic in sequence
  
- [`zettel/gardener.py`](../../../../zettel/gardener.py) - Lines 293-397
  - `_create_new_moc()`: Single LLM call to `moc_generation.md`; called only when no prior path matches

- [`zettel/gardener_hub.py`](../../../../zettel/gardener_hub.py) - Similar routing in `_process_hub_cluster()`
  - Hub pipeline mirrors the same single-call constraint with hub-specific prompts

### Code Evidence

**Routing decision tree** (gardener.py:232-290):
```python
def _process_cluster(cfg, db, idx, llm, category, note_ids, stats) -> str | None:
    """Route a cluster to incremental update or new MOC creation (at most one LLM call)."""
    
    # Step 1: Signature match
    cluster_signature = sha256_hex("|".join(sorted_ids))
    existing_sig = db.get_moc_by_signature(cluster_signature)
    if existing_sig:
        stats.skipped_signature += 1
        return existing_sig["moc_id"]  # Zero LLM calls
    
    # Step 2: Overlap detection
    overlap_moc = find_moc_by_note_overlap(db, note_ids, cfg.gardener.overlap_threshold)
    if overlap_moc:
        stats.incremental += 1
        return _update_existing_moc(...)  # One LLM call (incremental)
    
    # Step 3: Category match
    if category and category != "_unassigned":
        topic_moc = db.find_moc_by_topic(category)
        if topic_moc:
            stats.incremental += 1
            return _update_existing_moc(...)  # One LLM call (incremental)
    
    # Step 4: Cohesion gating
    if cfg.gardener.graph_cohesion_enabled:
        cohesion = graph_cohesion(db, note_ids, ...)
        if cohesion < cfg.gardener.graph_cohesion_min_ratio:
            stats.rejected_cohesion += 1
            return None  # Zero LLM calls; cluster rejected
    
    # Step 5: Generation (last resort)
    moc_id = _create_new_moc(...)  # One LLM call (generation)
    if moc_id:
        stats.created += 1
    return moc_id
```

**Incremental MOC update** (gardener.py:400-500):
```python
def _update_existing_moc(cfg, db, idx, llm, existing_moc, new_note_ids, ...):
    # Reuse existing MOC structure, call LLM only to classify new notes
    prompt_parts = load_prompt_parts(cfg.prompts_path / "moc_incremental.md")
    response = call_llm(...)  # Single LLM call
    # Merge new notes into existing subsections
```

**Signature calculation** (gardener.py:242-243):
```python
sorted_ids = sorted(note_ids)
cluster_signature = sha256_hex("|".join(sorted_ids))
```

**Overlap-based routing** (gardener_assign.py:157-182):
```python
def find_moc_by_note_overlap(db, note_ids, threshold):
    cluster_set = set(note_ids)
    for moc in db.list_mocs():
        moc_ids = extract_note_ids_from_moc_body(moc.get("body") or "")
        overlap = len(cluster_set & moc_ids) / len(cluster_set)
        if overlap >= threshold and overlap > best_score:
            best_score = overlap
            best_moc = moc
    return best_moc
```

### Impact Analysis

- **Introduced**: 2026-08-26 (commit 216a725)
- **Modified**: 7+ commits with themes:
  - "Add intelligent routing"
  - "Implement overlap detection"
  - "Add cohesion gating"
  - "Fix incremental routing"
- **Recent activity**: Stable; no rollbacks or cost-model changes
- **Affects**:
  - Total LLM cost per `zettel garden` run (linear in number of new clusters, not exponential in refinement rounds)
  - MOC generation latency (no waiting for iterative improvements)
  - User expectations (MOCs are "good enough" on first pass, not perfected over rounds)
  - MOC semantics (incremental updates may gradually drift from original topic)
- **Dependencies**:
  - SQLite `mocs` table (stores cluster signatures, topics, note overlap)
  - Prompts: `moc_generation.md`, `moc_incremental.md`, `moc_hub_generation.md`, `moc_hub_incremental.md`
  - `note_connections` graph (for cohesion scoring)

### Alternatives (if observable)

1. **Multi-round LLM refinement** (not implemented):
   - Call `moc_generation.md` → analyze output → call `moc_refine.md` → iterate until quality threshold
   - Cost: 3-5x higher LLM spend per MOC
   - Latency: 3-5x slower
   - Quality: Potentially better, but no feedback mechanism in current pipeline

2. **Greedy/heuristic MOC generation** (not implemented):
   - Use TF-IDF or LDA to extract topics without LLM
   - Cost: Near-zero (no LLM call)
   - Latency: Fast
   - Quality: Lower semantic quality, but deterministic

3. **User-driven MOC creation** (parallel feature):
   - Manual MOC creation via `zettel new-note moc`; indexed but not generated by `garden`
   - Trade-off: User controls quality and topic; no automation bias
   - Observed: Manual MOCs have `origin='manual'` vs. pipeline MOCs with `origin='pipeline'`

## Questions to Address in ADR (if created)

1. **Why single LLM call per cluster instead of multi-round refinement?**
   - Cost control and determinism. Refinement loops are expensive (3-5x higher LLM cost) and hard to cache. A single generation pass is fast and cost-predictable.

2. **Why incremental updates instead of regenerating existing MOCs?**
   - To preserve user edits and structure. If a user has manually reorganized MOC subsections, regeneration would overwrite that work. Incremental updates classify new notes into existing structure, preserving intent.

3. **How does overlap detection work, and when should the threshold be tuned?**
   - Overlap is calculated as `(notes in cluster ∩ notes in existing MOC) / (notes in cluster)`. Threshold of 0.4 means "if 40%+ of new notes are already in a MOC, treat as incremental." Tune higher (0.6+) if you want to be conservative about merging; lower (0.2-0.3) if you want aggressive consolidation.

4. **What happens when a cluster fails cohesion gating?**
   - It's silently dropped (no MOC created). Consider logging as a "debug" event or adding a `--skip-cohesion-gate` CLI flag if users want to override.

5. **Can the routing be customized (e.g., skip overlap detection, always regenerate)?**
   - Currently no CLI flags to override routing logic. Would require adding feature gates to `GardenerConfig`.

## Related Potential ADRs

- **Taxonomy-First MOC Clustering** — Source of clusters routed by this logic
- **Hub-Anchored MOC Pipeline** — Hub clusters also use this routing logic
- **Graph Cohesion Scoring Gate** — Optional gating mechanism within the routing tree

## Additional Notes

- **Signature-based deduplication**: Using `sha256(sorted(note_ids))` as a cluster signature is elegant — if the exact same cluster is detected, no processing needed. However, this is case-sensitive to note ID changes (if IDs are regenerated, signatures don't match). Consider documenting this dependency.

- **Overlap-based routing vs. category-based routing**: The current order is signature → overlap → category. If overlap and category detection contradict (e.g., 50% overlap suggests one MOC, but category suggests another), overlap wins. This preference is not documented; consider surfacing it as a configuration decision.

- **Incremental vs. generation prompt asymmetry**: `moc_incremental.md` and `moc_generation.md` are separate prompts with different instructions. A user who changes `moc_generation.md` may not realize incremental updates use a different prompt. Consider adding a note in the prompt file headers.

- **Cost tracking**: Each routing path should log cost differently:
  - Signature skip: $0
  - Overlap/category incremental: cost of `moc_incremental.md` LLM call
  - Generation: cost of `moc_generation.md` LLM call
  - Cohesion rejection: $0 (no LLM call, but worth tracking as "rejected cluster")
  
  Ensure `usage.py` and `CostTracker` capture this granularity for billing/observability.
