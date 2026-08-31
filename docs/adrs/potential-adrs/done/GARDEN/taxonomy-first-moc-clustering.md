# Potential ADR: Taxonomy-First MOC Clustering with UMAP+HDBSCAN

**Module**: GARDEN  
**Category**: Clustering Strategy / Knowledge Organization Architecture  
**Priority**: Must Document (Score: 150)  
**Date Identified**: 2026-08-30  

---

## What Was Identified

The GARDEN module (Phases 4/4b) implements a **hybrid MOC clustering pipeline** that prioritizes **taxonomy-first organization** over purely algorithmic clustering. Rather than globally clustering permanent notes and hoping they map to categories post-hoc, the system:

1. **Embeds category labels** from `config/moc_topics.yaml` into a learned vector space, using a configurable template (e.g., `"{domain}: {categoria}"`)
2. **Assigns each note** to the category with highest cosine similarity
3. **Clusters within each category bucket** using UMAP (dimensionality reduction) + HDBSCAN (density clustering), or falls back gracefully to KMeans if optional libraries are absent
4. **Routes each cluster** through a **single LLM call** per cluster (never twice) — either incremental update (if note overlap ≥ `overlap_threshold` or category already has a MOC) or new MOC generation (with `suggested_category` hint)
5. **Validates generated topics** against the allowed category list (substring match; rejects if `strict_topics: true`)
6. **Scores graph cohesion** (optional gate) to reject clusters with weak internal connectivity

**Temporal context**: Introduced in commit 216a725 (2026-08-26) with keywords "hybrid MOC pipeline," "taxonomy-first," "clustering-within-category," signaling a deliberate shift from pure global clustering to taxonomy-aligned MOC generation. Modified 8+ times over 4 days with themes "taxonomy assignment," "clustering tuning," "overlap detection," indicating active refinement and stability-seeking behavior.

## Why This Might Deserve an ADR

- **Impact**: This decision **shapes the entire knowledge graph organization** — users cannot get MOCs that don't align with their taxonomy. The taxonomy becomes the primary organizational superstructure, not a post-hoc categorization of algorithmic clusters.
  
- **Trade-offs**:
  - **Pro**: Semantically coherent MOCs that fit user-defined domains. Clusters are anchored to known categories, reducing "surprise" MOCs that don't fit the schema.
  - **Con**: If taxonomy is incomplete or misaligned with note embeddings, clusters may be fragmented across unrelated buckets. The system favors taxonomy consistency over cluster tightness.
  - **Pro**: Category hints during MOC generation bias the LLM toward consistent naming and structure.
  - **Con**: If no categories match a cluster well, notes fall into `_unassigned` and must cluster globally, defeating the intent.

- **Complexity**: The pipeline interleaves multiple decision points: category assignment (cosine similarity) → per-bucket clustering (UMAP+HDBSCAN) → note overlap detection → category matching → cohesion gating. Understanding the order and fallback conditions is essential for tuning or debugging.

- **Team Knowledge**: Anyone working on MOC generation, taxonomy evolution, or cluster-quality expectations must understand:
  - Why notes are assigned to categories *before* clustering (not after)
  - What happens when UMAP/HDBSCAN is unavailable (silent fallback to KMeans)
  - How `overlap_threshold` decides incremental vs. generation paths
  - Why graph cohesion rejection can silently drop clusters without LLM call
  
- **Future Implications**:
  - Changing the taxonomy structure (adding/removing categories) requires re-running `garden --recreate` to realign clusters.
  - The `cluster_within_category` config flag allows disabling this strategy entirely; understanding that trade-off is important.
  - Embedding provider changes (e.g., from default OpenAI to local Ollama) directly affect category assignment quality, a non-obvious dependency.

**Temporal Context**: This pattern has been stable for 4 days with incremental tuning (config defaults, parameter names). The decision appears well-settled, with no regression or rollback signals in recent commits.

## Evidence Found in Codebase

### Key Files

- [`zettel/gardener.py`](../../../../zettel/gardener.py) - Lines 60-169
  - `run_garden()`: Entry point, loads taxonomy, validates categories, invokes clustering with fallback
  - `_process_cluster()`: Intelligent routing (signature skip → overlap → category match → cohesion gate → generation)
  
- [`zettel/gardener_assign.py`](../../../../zettel/gardener_assign.py) - Lines 27-105
  - `embed_category_labels()`: Batch embeds category labels into semantic space
  - `assign_notes_to_categories()`: Assigns each note to highest-similarity category via cosine similarity
  - `cluster_notes_within_buckets()`: Runs UMAP+HDBSCAN per bucket
  - `cluster_notes_global()`: Fallback when category buckets are not used
  
- [`zettel/gardener_assign.py`](../../../../zettel/gardener_assign.py) - Lines 198-250
  - `_cluster_embeddings()`: UMAP(15 neighbors, cosine metric, spectral/random init) → HDBSCAN(min_cluster_size from config)
  - Lines 205-207: **Silent fallback** — `ImportError` on umap/hdbscan logs "warning" and calls `_cluster_kmeans()`
  - Lines 230-232: Catch-all exception handler — any UMAP failure also falls back to KMeans without signal

### Code Evidence

**Category assignment loop** (gardener_assign.py:56-62):
```python
for nid in note_ids:
    vec = embeddings_by_id.get(nid)
    if vec is None:
        continue
    sims = _cosine_similarity_batch(vec, cat_matrix)
    best_idx = int(np.argmax(sims))
    buckets[cat_names[best_idx]].append(nid)
```

**UMAP+HDBSCAN configuration** (gardener_assign.py:223-239):
```python
reducer = umap.UMAP(
    n_neighbors=n_neighbors,
    n_components=n_components,
    metric="cosine",
    init=init_method,
)
reduced = reducer.fit_transform(embeddings)
clusterer = hdbscan.HDBSCAN(**hdbscan_kwargs)
labels = clusterer.fit_predict(reduced)
```

**Silent fallback on ImportError** (gardener_assign.py:202-207):
```python
try:
    import umap
    import hdbscan
except ImportError:
    logger.warning("umap-learn ou hdbscan nao instalados. Usando KMeans.")
    return _cluster_kmeans(embeddings, ids, min_cluster_size)
```

**Single LLM call per cluster** (gardener.py:232-290):
```python
def _process_cluster(...) -> str | None:
    # ... check signature, overlap, category match, cohesion gate ...
    # Only if all checks pass:
    moc_id = _create_new_moc(...)  # Single LLM call or zero calls (if checks fail)
```

### Impact Analysis

- **Introduced**: 2026-08-26 (commit 216a725)
- **Modified**: 8+ commits over 4 days (2026-08-26 to 2026-08-29) with themes:
  - "Add hybrid MOC pipeline"
  - "Fix category assignment"
  - "Add graph cohesion scoring"
  - "Update taxonomy validation"
- **Recent activity**: Stable; no rollbacks or fundamental rework since initial commit
- **Affects**: 
  - Every MOC generated by `zettel garden` (taxonomy pipeline)
  - Every note's cluster membership and category assignment
  - Every user's knowledge graph organization strategy
- **Dependencies**:
  - `config/moc_topics.yaml` (taxonomy structure)
  - UMAP + HDBSCAN optional imports (or KMeans fallback)
  - Category label embeddings (uses active embedding provider)
  - `note_connections` graph for cohesion scoring

### Alternatives (if observable)

1. **Pure global clustering** (pre-taxonomy era):
   - Cluster all notes with UMAP+HDBSCAN, then assign clusters to categories post-hoc
   - Observed in code: `cluster_notes_global()` is still present as a fallback path (gardener.py:124-141) when category assignment fails or `cluster_within_category=false`
   - Trade-off: Simpler, but clusters may not align with user's taxonomy; category assignment becomes lossy

2. **KMeans clustering** (optional-dependency fallback):
   - Used when UMAP/HDBSCAN unavailable; simpler, no external deps, but less sophisticated density-based clustering
   - Observed: `_cluster_kmeans()` with k calculated as `max(2, n // min_cluster_size)`
   - Trade-off: Loses HDBSCAN's noise-handling (noise points included in clusters); less robust to outliers

3. **Fixed cluster count (e.g., k=5)** vs. dynamic k:
   - Current: k scales with note count (`n // min_cluster_size`)
   - Trade-off: Avoids hard-coded cluster count but creates unpredictable number of MOCs

## Questions to Address in ADR (if created)

1. **Why embed category labels, not just assign notes to pre-defined clusters?**
   - Because user's taxonomy may include abstract concepts (e.g., "Artificial Intelligence") that aren't explicitly in notes; embedding allows soft assignment via semantic similarity.

2. **What happens if categories are orthogonal to note embeddings?**
   - Notes may be assigned to "wrong" category; the `--recreate` flow allows re-running if taxonomy improves.

3. **Why UMAP+HDBSCAN over alternatives (e.g., Gaussian Mixture Models, OPTICS)?**
   - UMAP+HDBSCAN is faster, more scalable, no fixed cluster count assumption. HDBSCAN preserves noise as unassigned points (label == -1), which keeps outliers out of MOCs.

4. **Why a single LLM call per cluster?**
   - Cost control (LLM calls are expensive) and determinism (no iterative refinement). Incremental updates reuse existing MOC structure rather than regenerating.

5. **When should graph cohesion gating be enabled?**
   - If you have manually-curated notes with explicit backlinks, cohesion is a strong signal. If notes are auto-generated with sparse connections, cohesion gating may over-filter.

## Related Potential ADRs

- **Hub-Anchored MOC Pipeline as Complementary Strategy** — Distinct MOC generation via graph-degree ranking (complementary to taxonomy-first)
- **Single LLM Call Per Cluster with Intelligent Routing** — The routing decision that enables cost control
- **Graph Cohesion Scoring Gate** — Optional quality gate for cluster acceptance

## Additional Notes

- **Silent fallback risk**: The UMAP/HDBSCAN → KMeans fallback logs a warning but produces no visible signal at the MOC generation point. A user may not realize their clusters were generated with KMeans, leading to different cluster shapes than expected. Consider making the clustering method a logged metadata field or front-end display.

- **Taxonomy-embedding coupling**: Category label embeddings are generated on every `garden` run, increasing latency. Consider caching if category list is stable.

- **Undocumented KMeans fallback behavior**: The mapping.md and code comments note the fallback as a "candidate for Phase 2," indicating this coupling was known to be risky.

- **Config schema**: The `gardener.cluster_within_category` flag (default: `true`) allows disabling this entire strategy, falling back to global clustering. This escape hatch is important for users with flat taxonomies.
