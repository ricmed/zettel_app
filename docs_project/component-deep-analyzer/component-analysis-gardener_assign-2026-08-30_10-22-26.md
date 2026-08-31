# Component Deep Analysis Report — `gardener_assign`

Analyzed file: `zettel/gardener_assign.py` (284 lines)
Analysis date: 2026-08-30

---

## 1. Executive Summary

`gardener_assign.py` is a pure, side-effect-free helper module that implements the **taxonomy-assignment and per-category clustering layer** of Phase 4 of the Zettelkasten pipeline (the "Gardener", documented in `CLAUDE.md` as `gardener.py`). It has no CLI command, no HTTP endpoint, and is never instantiated as a class — it is a library of stateless functions consumed exclusively by `zettel/gardener.py` (the orchestrator) and, for one narrow utility, by `zettel/moc_backrefs.py`.

Its responsibility is threefold:

1. **Taxonomy-first bucketing** — embed category labels (from `config/moc_topics.yaml`) and assign each permanent note to the category whose label vector is closest by cosine similarity (`embed_category_labels`, `assign_notes_to_categories`).
2. **Per-category clustering** — run UMAP+HDBSCAN (or a KMeans fallback) *inside* each category bucket, and also expose a global (non-taxonomy) clustering path for when no taxonomy is configured (`cluster_notes_within_buckets`, `cluster_notes_global`, `_cluster_embeddings`, `_cluster_kmeans`).
3. **Graph-based signals for MOC routing** — compute how internally-connected a candidate cluster already is (`graph_cohesion`) and detect whether a cluster substantially overlaps an existing pipeline MOC so the caller can route to an incremental update instead of generating a new MOC (`find_moc_by_note_overlap`, `extract_note_ids_from_moc_body`).

Key findings:

- The module is **deliberately non-LLM, non-I/O** (aside from ChromaDB embedding calls and one `StateDB` read for edges/MOCs) — all decision math is local numpy/sklearn/umap/hdbscan logic, keeping `gardener.py`'s single LLM call per cluster invariant intact.
- Unit test coverage (`tests/test_gardener_assign.py`) exercises only 4 of the module's 13 public/private functions directly (`assign_notes_to_categories`, `extract_note_ids_from_moc_body`, `find_moc_by_note_overlap`, `graph_cohesion`). The clustering functions (`embed_category_labels`, `cluster_notes_within_buckets`, `cluster_notes_global`, `dominant_category_for_cluster`, `_cluster_embeddings`, `_cluster_kmeans`, `load_category_names`, `build_embeddings_by_id`) have **no dedicated unit test** anywhere in the repository — see § 11.
- `requirements.txt` comments out `umap-learn` and `hdbscan` (lines 33–34, "Descomente para clusterização avançada"), meaning the module's primary, documented clustering algorithm (UMAP+HDBSCAN) silently degrades to KMeans in a fresh install unless those packages are manually added — see § 10.
- The module never raises on missing embeddings or malformed data; it degrades by silently dropping notes from buckets/clusters (see Business Rules § 3.3 and § 3.6), which is a deliberate resilience choice but also a source of "vanishing note" risk if not logged loudly enough downstream.

---

## 2. Data Flow Analysis

`gardener_assign` is invoked from within `zettel/gardener.py:run_garden` (the only orchestration entry point) in this order:

```
1. run_garden() loads permanent-note embeddings from ChromaDB (idx.get_all_permanent_embeddings())
   → zettel/gardener.py:95
2. load_category_names(gcfg.topics_path)
   → reads config/moc_topics.yaml via zettel/taxonomy.py, returns flat category-name list
   → gardener_assign.py:275-283
3. IF categories exist AND cfg.gardener.cluster_within_category:
   a. embed_category_labels(idx, categories, domain, template)
      → formats "{domain}: {categoria}" labels, calls idx.embed_texts() (ChromaDB embedding fn)
      → gardener_assign.py:27-40
   b. assign_notes_to_categories(note_ids, embeddings_by_id, category_vectors)
      → cosine-similarity argmax per note → {category: [note_ids]} buckets
      → gardener_assign.py:43-64
   c. cluster_notes_within_buckets(buckets, embeddings_by_id, cfg)
      → for each bucket ≥ min_notes_for_moc: _cluster_embeddings() (UMAP+HDBSCAN or KMeans)
      → gardener_assign.py:67-96 → 198-272
4. ELSE (no taxonomy, or step 3 raised an exception):
   a. cluster_notes_global(note_ids, embeddings_array, cfg) — same clustering core, no bucketing
      → gardener_assign.py:99-105
   b. dominant_category_for_cluster(cluster_ids, buckets) — majority-vote label per global cluster
      → gardener_assign.py:108-123
5. For each (category, cluster_ids) pair, gardener.py._process_cluster() calls:
   a. find_moc_by_note_overlap(db, note_ids, cfg.gardener.overlap_threshold)
      → parses every pipeline MOC body via extract_note_ids_from_moc_body(), computes overlap
      → gardener_assign.py:157-182 (regex NOTE_ID_RE, line 20)
      → IF match found → incremental MOC update path (no further gardener_assign involvement)
   b. IF no overlap match AND cfg.gardener.graph_cohesion_enabled:
      graph_cohesion(db, note_ids, DEFAULT_RELATION_WEIGHTS)
      → db.get_connections_for_notes() → weighted internal-edge ratio
      → gardener_assign.py:126-154
      → IF cohesion < graph_cohesion_min_ratio → cluster rejected, no MOC created
6. Otherwise gardener.py proceeds to its own LLM call (_create_new_moc) — outside this component.
```

`extract_note_ids_from_moc_body` is also called independently by `zettel/moc_backrefs.py` (outside the Gardener run) whenever a MOC's body changes, to diff old vs. new linked notes for backref maintenance (`moc_backrefs.py:94-95, 121`).

---

## 3. Business Rules & Logic

### Overview of the business rules

| Rule Type | Rule Description | Location |
|-----------|-------------------|----------|
| Validation | Category assignment falls back to a single `"_unassigned"` bucket when no category vectors exist | gardener_assign.py:50-51 |
| Business Logic | Each note is assigned to exactly the category with maximum cosine similarity (nearest-centroid, hard assignment) | gardener_assign.py:56-62 |
| Validation | A note with no embedding entry is silently skipped (not assigned to any bucket) | gardener_assign.py:57-59 |
| Business Logic | A bucket is only clustered if it has at least `min_notes_for_moc` notes | gardener_assign.py:79-80, 84-85 |
| Business Logic | A bucket smaller than `min_cluster_size` is treated as one whole cluster (skips the clustering algorithm) | gardener_assign.py:87-89 |
| Business Logic | Sub-clusters produced by `_cluster_embeddings` are only kept if they still meet `min_notes_for_moc` | gardener_assign.py:92-94 |
| Business Logic | Global clustering path assigns each cluster to whichever category owns the most member notes ("majority vote"); ties resolve to dict/insertion order | gardener_assign.py:108-123 |
| Business Logic | UMAP+HDBSCAN is the preferred clustering algorithm; KMeans is a deterministic fallback | gardener_assign.py:202-207 |
| Business Logic | HDBSCAN noise points (label `-1`) are dropped from all resulting clusters | gardener_assign.py:242-245 |
| Business Logic | UMAP hyperparameters (`n_neighbors`, `n_components`, `init`) are dynamically capped/chosen based on sample count | gardener_assign.py:209-221 |
| Business Logic | KMeans `k` is heuristically derived from note count and `min_cluster_size` | gardener_assign.py:262-264 |
| Business Logic | KMeans clusters smaller than `min_cluster_size` are discarded (not returned) | gardener_assign.py:272 |
| Business Logic | Graph cohesion score is the weighted-internal-edge sum divided by cluster size (avg weighted degree proxy) | gardener_assign.py:126-154 |
| Business Logic | Duplicate edges between the same note pair count only once in cohesion scoring | gardener_assign.py:146-149 |
| Validation | Cohesion of a cluster with fewer than 2 notes is defined as `0.0` | gardener_assign.py:132-133 |
| Business Logic | An unrecognized relation type defaults to weight `0.5` in both cohesion and elsewhere (`DEFAULT_RELATION_WEIGHTS`) | gardener_assign.py:150-151 |
| Business Logic | Only `origin == "pipeline"` MOCs are candidates for note-overlap routing (hub MOCs are excluded) | gardener_assign.py:171-172 |
| Business Logic | Overlap fraction is computed relative to the **new cluster's** size, not the existing MOC's size | gardener_assign.py:177 |
| Business Logic | A MOC only qualifies as an overlap match if `overlap >= threshold` AND it strictly beats the current best score (first-found wins ties) | gardener_assign.py:178-180 |
| Validation | Category-name loading degrades to an empty list (never raises) if the taxonomy YAML is missing/invalid | gardener_assign.py:278-283 |
| Validation | `embed_category_labels` short-circuits to `{}` for an empty category list (avoids an embedding call) | gardener_assign.py:34-35 |

### Detailed breakdown of the business rules

---

### Business Rule: Taxonomy-First Category Assignment (Nearest-Centroid Bucketing)

**Overview**:
Every permanent note is deterministically routed into exactly one taxonomy category bucket by comparing its embedding vector against a set of category "label" embeddings and taking the single best cosine-similarity match.

**Detailed description**:
`embed_category_labels` first turns each configured category name (from `config/moc_topics.yaml`, loaded via `zettel/taxonomy.py`) into a natural-language label using `cfg.gardener.category_label_template` (default `"{domain}: {categoria}"`, e.g. `"Ciencia de Dados: Machine Learning Classico"`), then embeds all labels in a single batched call through `VectorIndex.embed_texts`. This keeps the number of embedding-provider round trips at exactly one per `run_garden` call regardless of how many categories exist, which matters for cost/latency given the project's LiteLLM-priced embedding calls.

`assign_notes_to_categories` then performs a hard (non-fuzzy, non-probabilistic) nearest-centroid classification: for every note id, it computes cosine similarity against the stacked matrix of category vectors (`_cosine_similarity_batch`) and assigns the note to `argmax`. Because `numpy.argmax` returns the *first* index attaining the maximum value, ties between two categories with identical similarity always resolve in favor of whichever category appears earlier in `category_vectors` (which itself follows the order categories were loaded from the taxonomy YAML — pillar order, then category order within each pillar, per `taxonomy.allowed_topic_names`). This is a deterministic but essentially arbitrary tie-break; it is not documented as intentional anywhere in the code or `CLAUDE.md`.

This rule underpins the entire "taxonomy-first" design philosophy documented in `CLAUDE.md` Phase 4: instead of clustering the whole vault and then guessing a topic per cluster (which is what the legacy/global path in § below still does), notes are pre-sorted into human-curated buckets *before* any clustering runs, which biases MOC topics toward the curated taxonomy and reduces LLM topic hallucination downstream in `gardener.py._create_new_moc`'s topic-validation step.

**Rule workflow**:
```
categories (list[str]) ──► embed_category_labels() ──► {category: vector}
note_ids + embeddings_by_id ──► assign_notes_to_categories()
   for each note_id:
     if no embedding found → note dropped (not placed in any bucket)
     else: sims = cosine(note_vec, all_category_vectors)
           best_category = argmax(sims)   # first-wins on tie
           buckets[best_category].append(note_id)
   if category_vectors is empty ──► return {"_unassigned": all note_ids} (bypass path)
```

---

### Business Rule: Silent Note Exclusion on Missing Embeddings

**Overview**:
Notes that lack an entry in `embeddings_by_id` are quietly excluded from bucket assignment rather than raising an error or being routed to a fallback bucket.

**Detailed description**:
Line 57-59 of `assign_notes_to_categories` (`if vec is None: continue`) means any `note_id` passed in without a corresponding embedding vector simply never appears in any bucket's list — not even `"_unassigned"`. The same silent-drop pattern reappears in `cluster_notes_within_buckets` (line 83: `if nid in embeddings_by_id`) when stacking a bucket's embeddings into a matrix.

This is a defensive design choice: `idx.get_all_permanent_embeddings()` and the caller's `note_ids` list are expected to be in lockstep (both come from the same ChromaDB query in `gardener.py`), so in practice this branch should be unreachable during normal `run_garden` execution. However, because it fails *silently* (no log line, no counter), if the two lists ever desynchronize (e.g. a race between an embedding upsert and a `garden` run, or a partially-failed `sync_permanent_note`), notes would vanish from MOC clustering with no diagnostic trail. This is flagged as a risk in § 10.

**Rule workflow**:
```
for nid in note_ids:
    vec = embeddings_by_id.get(nid)
    if vec is None:
        continue   # note silently excluded, no logging, no counter increment
    ... proceed with similarity/stacking ...
```

---

### Business Rule: Threshold-Gated Clustering Entry (`min_notes_for_moc` / `min_cluster_size`)

**Overview**:
Two independent size thresholds from `GardenerConfig` gate whether a bucket is clustered at all, and whether the clustering algorithm actually runs versus being bypassed.

**Detailed description**:
`cluster_notes_within_buckets` skips the `"_unassigned"` bucket entirely (line 77) and any bucket smaller than `cfg.gardener.min_notes_for_moc` (default `10` per `config/config.yaml:107`, though the Pydantic model default in `config.py:111` is `3` — the two defaults diverge; the operational YAML value wins per `CLAUDE.md`'s config-loading rule). This is the first gate: a category with, say, 4 notes when `min_notes_for_moc=10` never reaches the clustering algorithm at all.

The second gate is `cfg.gardener.min_cluster_size` (operational default `5`): if a bucket has *fewer* embeddings than `min_cluster_size` but still meets `min_notes_for_moc`, the code takes a shortcut at line 87-89 — it appends the **entire bucket** as a single cluster without invoking `_cluster_embeddings` at all. This means small-but-qualifying categories become one MOC covering every note in that category, with no internal sub-topic splitting. Only buckets with `len(emb) >= min_cluster_size` go through actual UMAP/HDBSCAN or KMeans clustering, and even then, resulting sub-clusters are filtered a third time (line 93) to discard any sub-cluster below `min_notes_for_moc`.

The practical effect is a three-stage filter — bucket floor, clustering-algorithm floor, sub-cluster floor — all keyed off the same two config values, which means changing `min_notes_for_moc` or `min_cluster_size` has compounding effects across all three checks simultaneously (there is no way to tune "minimum bucket size to attempt clustering" independently from "minimum final cluster size to become a MOC" — they are the same knob in two of the three checks).

**Rule workflow**:
```
for category, note_ids in buckets:
    skip if category == "_unassigned" or empty
    skip if len(note_ids) < min_notes_for_moc          # gate 1
    emb = stack embeddings for note_ids present in embeddings_by_id
    skip if len(emb) < min_notes_for_moc               # gate 1b (post-stacking)
    if len(emb) < min_cluster_size:
        results.append((category, note_ids))            # gate 2: bypass clustering entirely
        continue
    subclusters = _cluster_embeddings(emb, note_ids, cfg)
    for cluster_ids in subclusters:
        if len(cluster_ids) >= min_notes_for_moc:        # gate 3
            results.append((category, cluster_ids))
```

---

### Business Rule: UMAP+HDBSCAN Clustering with KMeans Fallback

**Overview**:
The core clustering algorithm reduces dimensionality with UMAP and density-clusters with HDBSCAN, with three independent fallback triggers that degrade to a deterministic KMeans implementation.

**Detailed description**:
`_cluster_embeddings` attempts to `import umap` and `import hdbscan` at call time (not at module import time — this is a lazy/optional-dependency pattern). If either import fails, it logs a warning ("umap-learn ou hdbscan nao instalados. Usando KMeans.") and calls `_cluster_kmeans` instead. This is the **first** of three fallback triggers.

If the imports succeed, UMAP hyperparameters are computed defensively against small sample counts: `n_neighbors` is `cfg.gardener.umap_n_neighbors` (default `null`/`None` → auto `min(15, n_samples - 1)`), and if that computed value is `< 2`, the function returns `[ids]` — i.e., treats the entire input as one unsplit cluster without attempting UMAP at all (**second** fallback trigger, distinct from the other two — no KMeans, no warning, just a pass-through). Similarly `n_components` is `min(5, n_samples - 2)`; if that is `< 2`, it falls back to KMeans directly (**third** trigger path, distinct from the import-failure path but reaching the same `_cluster_kmeans` function).

UMAP's `init` method is chosen based on sample density relative to the target dimensionality (`"spectral"` if `n_samples > n_components + 2`, else `"random"`) — this avoids a known UMAP failure mode where spectral initialization is numerically unstable on very small datasets. If `UMAP.fit_transform` itself raises any exception, this is caught broadly (`except Exception as e`) and again falls back to KMeans (**fourth** distinct fallback branch, this one from a runtime failure rather than a pre-check).

HDBSCAN is then run with `min_cluster_size` and `metric="euclidean"` (on the UMAP-reduced space, not the original embedding space) plus an optional `min_samples` override. Any note assigned label `-1` (HDBSCAN's noise designation) is **excluded from every returned cluster** — per `CLAUDE.md`, "HDBSCAN noise stays out of MOCs; notes remain navigable via graph edges" — meaning noise notes get no MOC membership from this pipeline but can still be discovered via the sync/connect graph-edge mechanisms elsewhere in the system.

**Rule workflow**:
```
try: import umap, hdbscan
except ImportError: return _cluster_kmeans(...)          # fallback #1

n_neighbors = min(cfg.umap_n_neighbors or 15, n_samples - 1)
if n_neighbors < 2: return [ids]                          # fallback #2 (no clustering)

n_components = min(5, n_samples - 2)
if n_components < 2: return _cluster_kmeans(...)          # fallback #3

init = "spectral" if n_samples > n_components + 2 else "random"
try:
    reduced = UMAP(n_neighbors, n_components, metric="cosine", init).fit_transform(embeddings)
except Exception: return _cluster_kmeans(...)             # fallback #4

labels = HDBSCAN(min_cluster_size, metric="euclidean", min_samples?).fit_predict(reduced)
clusters = group ids by label, EXCLUDING label == -1 (noise)
if no clusters survive: return [ids] if len(ids) >= min_cluster_size else []
return list(clusters.values())
```

---

### Business Rule: KMeans Fallback Cluster-Count Heuristic

**Overview**:
When UMAP/HDBSCAN is unavailable or fails, KMeans is run with a heuristically derived `k` and its output is filtered by the same minimum-cluster-size rule.

**Detailed description**:
`_cluster_kmeans` first re-checks for `scikit-learn` availability (a *second*, independent optional-dependency check — even though `scikit-learn` is a mandatory dependency per `requirements.txt:32`, the code still guards against `ImportError` defensively and logs an error, returning either the whole set as one cluster or an empty list depending on whether it meets `min_cluster_size`).

The number of clusters `k` is computed as `max(2, n // min_cluster_size)`, capped at `n` (`k = min(k, n)`). This is a simple heuristic aiming for clusters of roughly `min_cluster_size` notes each, with a floor of 2 clusters whenever KMeans runs at all (i.e., KMeans is never asked to produce a single cluster — that shortcut only happens in the size-gate check in `cluster_notes_within_buckets`, not inside this function). `random_state=42` makes the KMeans result **deterministic and reproducible** across runs given identical input embeddings — an intentional choice given the pipeline's broader emphasis on deterministic caching (`hashing.py`, `llm_cache`). `n_init=10` runs KMeans 10 times with different centroid seeds and keeps the best (sklearn's own robustness mechanism against local minima).

Unlike HDBSCAN, KMeans has no "noise" concept — every note is assigned to some cluster — but clusters below `min_cluster_size` are discarded outright (line 272), meaning some notes can still be dropped from MOC membership under the KMeans path, just via a different mechanism (small-cluster elimination rather than explicit noise labeling).

**Rule workflow**:
```
try: from sklearn.cluster import KMeans
except ImportError: return [ids] if len(ids) >= min_cluster_size else []

k = max(2, n // min_cluster_size); k = min(k, n)
labels = KMeans(n_clusters=k, random_state=42, n_init=10).fit_predict(embeddings)
clusters = group ids by label
return [c for c in clusters.values() if len(c) >= min_cluster_size]   # small clusters dropped
```

---

### Business Rule: Global-Cluster Dominant-Category Attribution

**Overview**:
When the taxonomy-first path is skipped or fails, clusters are produced without any category label; `dominant_category_for_cluster` retroactively assigns each such cluster a category by majority vote against the (still-computed) taxonomy buckets.

**Detailed description**:
This function exists specifically for the fallback branch in `gardener.py:run_garden` (lines 124-141): when `cluster_pairs` is empty after attempting the taxonomy-first path (either because no categories were configured, or `embed_category_labels`/`assign_notes_to_categories` raised an exception), the code clusters *all* notes globally via `cluster_notes_global` and then, if a taxonomy still exists, independently recomputes the category buckets and asks `dominant_category_for_cluster` to pick, for each resulting global cluster, whichever category contributed the most member notes.

The counting logic explicitly excludes the `"_unassigned"` bucket from consideration (line 116-117) and returns `"_unassigned"` itself as the category if no bucket has any overlap with the cluster at all (line 121-122). Ties in vote count are resolved by Python's `max(counts, key=counts.get)`, which — like `numpy.argmax` — returns the first key achieving the maximum in **dict iteration order** (insertion order, i.e. whichever category was encountered first while iterating `buckets.items()`), not any category priority or alphabetical rule. This mirrors the same "first-wins" tie-break philosophy as `assign_notes_to_categories`, applied at the cluster level instead of the note level.

This dual-computation is somewhat redundant: `gardener.py` calls `embed_category_labels` + `assign_notes_to_categories` a **second time** in the fallback branch (lines 129-134) purely to get `buckets` for this function, discarding the previous attempt's `cat_vectors`/`buckets` if the taxonomy-first path had failed with an exception (they're back in scope for a plain empty-`cluster_pairs` case, but a fresh call is still made either way per the code as written) — an efficiency concern noted in § 10.

**Rule workflow**:
```
for each global cluster:
    id_set = set(cluster_ids)
    counts = {}
    for category, members in buckets.items():
        if category == "_unassigned": continue
        overlap = |id_set ∩ set(members)|
        if overlap > 0: counts[category] = overlap
    return "_unassigned" if counts is empty
    return max(counts, key=counts.get)   # first-wins tie-break, dict insertion order
```

---

### Business Rule: Graph Cohesion Scoring for Cluster Acceptance

**Overview**:
Before a brand-new MOC is generated for a cluster (no existing overlap or topic match found), the cluster's internal connectivity in the note graph is scored; a configurable minimum ratio can reject clusters that are only "embedding-similar" but not actually linked by any typed relation.

**Detailed description**:
`graph_cohesion` computes, for a given set of note ids, the sum of weighted internal edges (edges where **both** endpoints are inside the cluster) divided by the cluster's size — described in the code comment as an "avg weighted degree proxy" rather than a normalized ratio (it is *not* divided by the maximum possible edge count, so it is not a true density/cohesion ratio in the graph-theory sense; it scales with both edge count and edge weight simultaneously). Edge weights come from `DEFAULT_RELATION_WEIGHTS` in `config.py` (`contradicts=1.0` highest down to `related=0.5` lowest), reflecting the project-wide convention (also used in `graph.py`'s BFS expansion for `ask`/`article`) that stronger semantic relations should count for more than a generic "related" link.

Deduplication is handled via `frozenset((src, tgt))` — meaning if `note_connections` ever contains both an `A→B` and a `B→A` row (or a duplicate insert of the same directed edge), only the **first-encountered** edge's weight counts toward cohesion, not the sum of all matching rows. Any relation type not present in the weights dict defaults to `0.5` (the same default as `"related"`), and cohesion is explicitly `0.0` for clusters of size `< 2` (can't have "internal" edges with 0 or 1 member).

This score feeds a single decision point in `gardener.py._process_cluster`: if `cfg.gardener.graph_cohesion_enabled` and `graph_cohesion_min_ratio > 0`, a cluster whose cohesion falls below the ratio is **rejected outright** — no MOC is created, and it is tallied in `stats.rejected_cohesion`. With the operational default `graph_cohesion_min_ratio: 0.0` (`config.yaml:115`), this rejection is currently inert in production (cohesion is only *logged*, never used to reject), which the code comment self-documents ("0 = so log; >0 exige coesao minima p/ MOC novo").

**Rule workflow**:
```
if len(note_ids) < 2: return 0.0
edges = db.get_connections_for_notes(note_ids)   # one batched SQL query
internal_weight = 0.0; seen_pairs = set()
for edge in edges:
    if src not in cluster or tgt not in cluster: continue    # external edge, ignored
    pair = frozenset((src, tgt))
    if pair in seen_pairs: continue                           # dedupe repeated/reciprocal edges
    seen_pairs.add(pair)
    internal_weight += weights.get(relation_type, 0.5)
return internal_weight / len(cluster)
```

---

### Business Rule: Overlap-Based Incremental-MOC Routing

**Overview**:
Before generating a brand-new MOC, every candidate cluster is checked against all existing pipeline MOCs for substantial note-id overlap; a sufficiently overlapping MOC is chosen for incremental update instead, guaranteeing at most one LLM call per cluster as documented in `CLAUDE.md`.

**Detailed description**:
`find_moc_by_note_overlap` iterates every row from `db.list_mocs()`, filtering to `origin == "pipeline"` only (line 171) — this is a deliberate boundary that keeps the taxonomy pipeline's MOC-merging logic from ever touching hub-anchored MOCs (`origin == "hub_pipeline"`, managed by `gardener_hub.py`) or manually created MOCs (`origin == "manual"`), preserving the two pipelines' independent lifecycles as documented in `CLAUDE.md` Phase 4b.

For each surviving pipeline MOC, its body is parsed via `extract_note_ids_from_moc_body`, which applies the module-level regex `NOTE_ID_RE = r"\[\[ZTL\s*-\s*(\S+)\s*-\s*[^\]]*\]\]"` to pull every `ZTL` wikilink's ID token out of the markdown body — this regex is intentionally whitespace-tolerant around the dashes but requires the `ZTL - <ID> - <slug>` wikilink shape exactly; any other note-reference format would not be counted.

The overlap fraction is `|cluster ∩ moc_note_ids| / |cluster|` — notably normalized by the **new cluster's size**, not the existing MOC's size or the union. This means a small new cluster that is entirely a subset of a much larger existing MOC scores `1.0` (perfect match), while a large new cluster that only marginally overlaps a small existing MOC scores low even if it fully contains that MOC's notes. The best-match search keeps the highest-scoring MOC across all pipeline MOCs, but only overwrites `best_moc` when the new score is *both* `>= threshold` and *strictly greater than* the running best — meaning on an exact tie between two MOCs, the first one returned by `db.list_mocs()` (which orders `created_at DESC`, i.e. newest first) keeps priority.

**Rule workflow**:
```
if not note_ids: return None
cluster_set = set(note_ids); best_moc = None; best_score = 0.0
for moc in db.list_mocs():
    if moc.origin != "pipeline": continue
    moc_ids = extract_note_ids_from_moc_body(moc.body)
    if not moc_ids: continue
    overlap = |cluster_set ∩ moc_ids| / |cluster_set|
    if overlap >= threshold and overlap > best_score:
        best_score = overlap; best_moc = moc
return best_moc   # None if nothing qualifies
```

---

### Business Rule: Resilient Taxonomy-Name Loading

**Overview**:
Category-name loading for the assignment pipeline never propagates an exception; any failure to read/validate `config/moc_topics.yaml` degrades to an empty category list, which in turn routes the entire run through the global-clustering fallback path.

**Detailed description**:
`load_category_names` is a thin wrapper: if `topics_path` is `None`, it returns `[]` immediately (line 276-277); otherwise it calls `zettel.taxonomy.load_moc_taxonomy` + `allowed_topic_names` inside a broad `try/except Exception`, logging a warning and returning `[]` on any failure. This is notably a **different failure policy** than the one enforced elsewhere in the pipeline: `zettel/taxonomy.py:resolve_allowed_topics` and `gardener.py:run_garden`'s pre-flight check both raise `TaxonomyLoadError` and abort the entire `garden` run when `cfg.gardener.strict_topics` is `True` and the taxonomy file is missing/invalid (`CLAUDE.md`'s "Fail fast" comment, `gardener.py:73-83`).

In practice this means: by the time `load_category_names` is called (later in `run_garden`, line 106), the strict pre-flight check has already passed, so a *second* failure at this specific call (e.g. a transient file-system issue between the two reads, or the YAML becoming invalid mid-run) is treated permissively regardless of `strict_topics` — it silently degrades to the global-clustering path with categories `[]`, rather than aborting the run. This is an inconsistency between the two taxonomy-loading call sites (see § 10).

**Rule workflow**:
```
if topics_path is None: return []
try:
    tax = load_moc_taxonomy(topics_path)
    return allowed_topic_names(tax)        # flat, deduplicated, pillar-then-category order
except Exception as e:
    logger.warning(...)
    return []   # never raises, regardless of cfg.gardener.strict_topics
```

---

## 4. Component Structure

`gardener_assign.py` is a single flat module (no sub-package, no classes) organized into four informal sections by function:

```
zettel/gardener_assign.py
├── Module-level constants
│   ├── NOTE_ID_RE (line 20)                       # regex for [[ZTL - ID - slug]] wikilinks
│   └── logger (line 18)
├── MOC-body parsing
│   └── extract_note_ids_from_moc_body() (23-24)    # shared with moc_backrefs.py
├── Taxonomy assignment
│   ├── embed_category_labels() (27-40)             # category name -> embedding vector
│   └── assign_notes_to_categories() (43-64)        # note -> nearest category bucket
├── Clustering orchestration
│   ├── cluster_notes_within_buckets() (67-96)       # per-category clustering entry point
│   ├── cluster_notes_global() (99-105)              # legacy/no-taxonomy clustering entry point
│   └── dominant_category_for_cluster() (108-123)    # majority-vote label for global clusters
├── Graph-based routing signals
│   ├── graph_cohesion() (126-154)                   # weighted internal-edge scoring
│   └── find_moc_by_note_overlap() (157-182)         # incremental-vs-new MOC routing
├── Shared numeric helpers
│   ├── build_embeddings_by_id() (185-186)           # list[id]+list[vec] -> dict[id, ndarray]
│   └── _cosine_similarity_batch() (189-195)         # vectorized cosine similarity
├── Clustering algorithm internals (private)
│   ├── _cluster_embeddings() (198-250)              # UMAP+HDBSCAN with fallback chain
│   └── _cluster_kmeans() (253-272)                  # deterministic KMeans fallback
└── Taxonomy loading
    └── load_category_names() (275-283)              # resilient wrapper over taxonomy.py
```

There is no separate test-fixture, config, or model file dedicated to this component; its Pydantic configuration surface (`GardenerConfig`) lives in `zettel/config.py:109-133`, and its only data-model dependency (`MocTaxonomy`, `Categoria`, `Pilar`) lives in `zettel/taxonomy.py`.

---

## 5. Dependency Analysis

```
Internal Dependencies (imports):
gardener_assign.py → zettel.config (DEFAULT_RELATION_WEIGHTS, GardenerConfig)
gardener_assign.py → zettel.index (VectorIndex — type hint only, used via embed_texts())
gardener_assign.py → zettel.taxonomy (allowed_topic_names, load_moc_taxonomy)
gardener_assign.py → zettel.state (StateDB — TYPE_CHECKING-only import; runtime duck-typed)

Internal Dependents (who imports gardener_assign):
zettel/gardener.py            → assign_notes_to_categories, build_embeddings_by_id,
                                 cluster_notes_global, cluster_notes_within_buckets,
                                 dominant_category_for_cluster, embed_category_labels,
                                 find_moc_by_note_overlap, graph_cohesion, load_category_names
zettel/moc_backrefs.py        → extract_note_ids_from_moc_body (only)

External Dependencies:
- numpy (>=1.24.0, requirements.txt:31)          — cosine similarity, embedding matrix ops
- scikit-learn (>=1.3.0, requirements.txt:32)     — KMeans fallback clustering, TfidfVectorizer (gardener.py, not this module)
- umap-learn (commented out, requirements.txt:33) — dimensionality reduction; imported lazily, optional
- hdbscan (commented out, requirements.txt:34)    — density clustering; imported lazily, optional
- ChromaDB (via VectorIndex.embed_texts, indirect) — category-label embedding provider call
- SQLite (via StateDB.get_connections_for_notes / list_mocs, indirect) — graph edges & MOC bodies
```

Note: `umap` and `hdbscan` are NOT declared as active dependencies in `requirements.txt` (they are commented out with a note to uncomment "para clusterização avançada"), yet `gardener_assign.py`'s primary documented clustering algorithm depends on them. See § 10 for the resulting risk.

---

## 6. Afferent and Efferent Coupling

Coupling is measured at function granularity (this module has no classes) — afferent (Ca) = number of distinct external call sites depending on the function; efferent (Ce) = number of distinct external modules/functions each function calls out to.

| Function | Afferent Coupling | Efferent Coupling | Critical |
|----------|--------------------|--------------------|----------|
| `extract_note_ids_from_moc_body` | 2 (gardener.py, moc_backrefs.py) | 0 (pure regex) | High — shared parsing contract across two modules; a format change to `NOTE_ID_RE` silently breaks MOC-overlap routing AND backref sync simultaneously |
| `assign_notes_to_categories` | 1 (gardener.py, called 2x per run in the fallback branch) | 1 (`_cosine_similarity_batch`, in-module) | High — sole point of note→category routing logic |
| `embed_category_labels` | 1 (gardener.py, called 2x per run in the fallback branch) | 1 (`VectorIndex.embed_texts`) | Medium — external I/O (embedding provider) wrapped in business logic |
| `cluster_notes_within_buckets` | 1 (gardener.py) | 1 (`_cluster_embeddings`, in-module) | High — gates whether any per-category MOC is ever produced |
| `cluster_notes_global` | 1 (gardener.py) | 1 (`_cluster_embeddings`, in-module, aliased) | Medium — fallback-only path |
| `dominant_category_for_cluster` | 1 (gardener.py) | 0 | Low — pure dict aggregation |
| `graph_cohesion` | 1 (gardener.py) | 1 (`StateDB.get_connections_for_notes`) | Medium — only advisory today (`graph_cohesion_min_ratio` default 0.0) |
| `find_moc_by_note_overlap` | 1 (gardener.py) | 2 (`StateDB.list_mocs`, `extract_note_ids_from_moc_body`) | High — the primary "avoid duplicate MOC" gate |
| `build_embeddings_by_id` | 1 (gardener.py) | 0 | Low — trivial adapter |
| `load_category_names` | 1 (gardener.py) | 2 (`taxonomy.load_moc_taxonomy`, `taxonomy.allowed_topic_names`) | Medium — silently permissive (see Business Rules § "Resilient Taxonomy-Name Loading") |
| `_cluster_embeddings` | 2 (in-module: `cluster_notes_within_buckets`, `cluster_notes_global`) | 3 (`umap`, `hdbscan`, `_cluster_kmeans` in-module) | High — sole clustering-algorithm implementation for both entry paths |
| `_cluster_kmeans` | 1 (in-module: `_cluster_embeddings`, 3 call sites within it) | 1 (`sklearn.cluster.KMeans`) | High — the de facto default algorithm given umap/hdbscan's optional-install status |
| `_cosine_similarity_batch` | 1 (in-module: `assign_notes_to_categories`) | 0 | Low — pure numpy math |

---

## 7. Integration Points

| Integration | Type | Purpose | Protocol | Data Format | Error Handling |
|-------------|------|---------|----------|--------------|-----------------|
| ChromaDB (via `VectorIndex.embed_texts`) | Internal Service (embedded library) | Embed category labels for taxonomy-first bucketing | In-process function call → configured embedding provider (OpenAI/SentenceTransformers/Ollama per `CLAUDE.md`) | `list[str]` in, `list[list[float]]` out | No try/except in `embed_category_labels` itself — an exception propagates to `gardener.py`'s enclosing `try/except Exception` (line 120), which logs a warning and falls back to global clustering |
| SQLite (via `StateDB.get_connections_for_notes`) | Internal Service | Fetch graph edges for cohesion scoring | In-process SQL query (parameterized `IN (...)`) | `list[dict]` rows | No error handling in `graph_cohesion` — a DB error propagates uncaught |
| SQLite (via `StateDB.list_mocs`) | Internal Service | Enumerate existing pipeline MOCs for overlap detection | In-process SQL query | `list[dict]` rows (includes `body`, `origin`, `moc_id`) | No error handling in `find_moc_by_note_overlap`; a malformed/missing `body` field is defensively handled (`moc.get("body") or ""`) but a DB-level exception is not caught |
| `config/moc_topics.yaml` (via `zettel.taxonomy`) | Configuration File | Source of category-name whitelist for bucketing | File read + YAML parse + Pydantic validation | YAML → `MocTaxonomy` Pydantic model | `load_category_names` catches `Exception` broadly and degrades to `[]` (see Business Rules) |
| `umap-learn` / `hdbscan` (optional PyPI packages) | External Library | Dimensionality reduction + density clustering | In-process Python import + function calls | numpy arrays in/out | `ImportError` and any `Exception` from `UMAP.fit_transform` both caught and redirected to KMeans fallback |
| `scikit-learn` (`sklearn.cluster.KMeans`) | External Library | Fallback/default clustering algorithm | In-process Python import + function calls | numpy arrays in/out | `ImportError` caught (defensive, since sklearn is a hard requirement); logs error and returns a degraded result rather than raising |

---

## 8. Design Patterns & Architecture

| Pattern | Implementation | Location | Purpose |
|---------|-----------------|-----------|----------|
| Strategy Pattern (implicit, via exception-driven dispatch) | `_cluster_embeddings` selecting between UMAP+HDBSCAN and `_cluster_kmeans` | gardener_assign.py:198-250 | Swap clustering algorithm based on runtime dependency availability/success, without a formal interface/ABC |
| Fail-Soft / Graceful Degradation | `load_category_names`, `_cluster_embeddings`, `_cluster_kmeans`, `assign_notes_to_categories` (empty-vectors branch) | throughout | Every failure mode returns a usable (if degraded) default rather than propagating an exception — consistent with `CLAUDE.md`'s "Falls back gracefully if dependencies are missing" design goal for the whole Gardener |
| Pure Function / Functional Core | The entire module — no classes, no mutable module state, no I/O side effects beyond read-only calls to injected `VectorIndex`/`StateDB` | whole file | Keeps the assignment/clustering math independently testable and free of the ordering/state concerns that `gardener.py`'s orchestration (LLM calls, file writes, MOC upserts) has to manage |
| Adapter | `build_embeddings_by_id` | gardener_assign.py:185-186 | Bridges ChromaDB's parallel-list embedding return shape (`ids: list[str]`, `embeddings: list[list[float]]`) into the `dict[str, np.ndarray]` shape the rest of the module expects |
| Sentinel Value | `"_unassigned"` string key used as a bucket/category placeholder | gardener_assign.py:51, 77, 116, 122, 171 | Represents "no taxonomy match" without needing `Optional[str]`/`None` threaded through dict keys, which would complicate the `dict[str, list[str]]` typing used throughout |
| Lazy/Optional Import | `import umap` / `import hdbscan` performed inside the function body, not at module top | gardener_assign.py:203-204 | Allows the module (and everything importing it, including `gardener.py`) to load successfully even when these optional packages are absent |

---

## 9. Afferent/Efferent Summary Note

(See § 6 for the full per-function table.) At the module level, `gardener_assign.py` has exactly **2 afferent dependents** (`gardener.py`, `moc_backrefs.py`) and **4 efferent dependencies** (`config.py`, `index.py`, `taxonomy.py`, `state.py`), plus 3 external libraries (`numpy` hard, `scikit-learn` hard, `umap`+`hdbscan` soft/optional). This is a low module-level fan-in/fan-out ratio, consistent with its role as a narrowly-scoped algorithmic helper rather than a hub component.

---

## 10. Technical Debt & Risks

| Risk Level | Component Area | Issue | Impact |
|------------|-----------------|-------|--------|
| High | Dependency management | `umap-learn` and `hdbscan` are commented out in `requirements.txt` (lines 33-34), while `_cluster_embeddings` treats them as the primary/preferred algorithm and only logs a `warning` (not visible unless logging is configured to show warnings) on fallback | A fresh `pip install -r requirements.txt` environment silently runs KMeans instead of the documented UMAP+HDBSCAN pipeline for every `garden` run, with materially different clustering behavior (KMeans has no noise concept, uses a heuristic `k` instead of density-based cluster discovery) and no hard failure to alert the operator |
| High | Test coverage | 9 of 13 functions (`embed_category_labels`, `cluster_notes_within_buckets`, `cluster_notes_global`, `dominant_category_for_cluster`, `build_embeddings_by_id`, `load_category_names`, `_cosine_similarity_batch`, `_cluster_embeddings`, `_cluster_kmeans`) have no dedicated unit test anywhere in the repository | The entire clustering algorithm (UMAP/HDBSCAN/KMeans selection, fallback chain, hyperparameter capping for small sample counts, noise filtering) is only exercised indirectly through the mocked-LLM integration tests in `test_gardener.py`, none of which appear to feed real embedding arrays through the actual clustering code path — regressions in cluster quality or the fallback chain would not be caught by CI |
| Medium | Silent data loss | Notes missing from `embeddings_by_id` are dropped without logging in both `assign_notes_to_categories` (line 57-59) and `cluster_notes_within_buckets` (line 83) | If the note-id list and embedding list ever desynchronize (partial embedding failure, race condition), affected notes silently disappear from the entire MOC-assignment run with zero diagnostic trace, making the bug very hard to detect in production |
| Medium | Inconsistent strictness | `load_category_names` (this module) always degrades permissively regardless of `cfg.gardener.strict_topics`, while `zettel.taxonomy.resolve_allowed_topics` (used elsewhere in the same `run_garden` call, and in the pre-flight check) raises `TaxonomyLoadError` under `strict_topics=True` | A taxonomy file that becomes invalid between the pre-flight check (gardener.py:74-83) and the later `load_category_names` call (gardener.py:106) is handled inconsistently — the run doesn't abort as `strict_topics=True` would otherwise guarantee, it silently falls through to global clustering |
| Medium | Redundant computation | `gardener.py`'s fallback branch (lines 124-136) re-calls `embed_category_labels` + `assign_notes_to_categories` a second time solely to obtain `buckets` for `dominant_category_for_cluster`, discarding the earlier computation in the `try` block above it | Doubles the category-label embedding cost (an LLM/embedding-provider call) on every run that reaches the fallback branch — wasteful and a candidate for `cat_vectors`/`buckets` reuse |
| Low | Non-standard cohesion metric | `graph_cohesion`'s inline comment calls the result an "avg weighted degree proxy," but it is not a normalized ratio (not bounded to `[0,1]` despite the docstring claiming "0..1 scale" on line 131) — a highly-interconnected cluster with many `contradicts`/`extends` edges can exceed `1.0` | The docstring's "(0..1 scale)" claim is misleading; any consumer treating `graph_cohesion_min_ratio` as a literal probability/fraction threshold (e.g. `0.5` meaning "half the possible edges exist") would be miscalibrated — the actual scale depends on average node degree and relation-weight mix |
| Low | Ambiguous tie-breaking | Both `assign_notes_to_categories` (via `np.argmax`) and `dominant_category_for_cluster` (via `max(..., key=...)`) resolve ties by insertion/iteration order rather than any documented priority rule | Behavior on tied similarity/vote-count scores is technically deterministic but not intentionally designed or documented, making it fragile to reordering the taxonomy YAML (which would silently change which category wins ties) |

---

## 11. Test Coverage Analysis

Dedicated test file: `tests/test_gardener_assign.py` (69 lines, 4 test functions). Indirect/integration coverage also comes from `tests/test_gardener.py` (which exercises `gardener.py._process_cluster`/`_update_existing_moc` with a `category` string argument, but does not exercise this module's clustering or embedding functions with real inputs) and `tests/test_gardener_hub.py` (does not import `gardener_assign` at all — confirmed via search — the hub pipeline in `gardener_hub.py` does not use this module).

| Component (function) | Unit Tests | Integration Tests | Coverage | Test Quality |
|------------------------|------------|---------------------|----------|----------------|
| `assign_notes_to_categories` | 1 (`test_assign_notes_to_categories`) | 0 direct (only via `category` string params in `test_gardener.py` routing tests, which never call this function) | Partial — happy path only (2 orthogonal 2D vectors, exact bucket match); no test for empty `category_vectors`, missing embeddings, or ties | Good basic assertion on bucket contents; missing edge cases (empty input, tie-break behavior, `_unassigned` bypass) |
| `extract_note_ids_from_moc_body` | 1 (`test_extract_note_ids_from_moc_body`) | Indirectly exercised via `test_find_moc_by_note_overlap` and (outside this component) `tests/test_moc_backrefs.py` if present | Good for the happy path (2 well-formed wikilinks) | No negative test (malformed wikilink, empty body, mixed link types) |
| `find_moc_by_note_overlap` | 1 (`test_find_moc_by_note_overlap`) | 0 | Good — covers both a match above threshold and a non-match below threshold, using a real `StateDB` instance | Missing: tie-break-at-threshold case, `origin != "pipeline"` exclusion case, empty `note_ids` early-return case |
| `graph_cohesion` | 1 (`test_graph_cohesion_internal_edges`) | 0 | Partial — verifies a positive score exists for a connected triple, and a non-negative score for an isolated pair with an external edge added | The `isolated` assertion (`>= 0`) is weak (would pass even if the function always returned 0); no test asserts the exact expected numeric value, the `<2 notes -> 0.0` rule, or edge deduplication (reciprocal edges) |
| `embed_category_labels` | 0 | 0 | None | Not exercised with real or mocked `VectorIndex` at all |
| `cluster_notes_within_buckets` | 0 | 0 | None | Not exercised; the three-gate threshold logic (bucket floor / cluster-algorithm floor / sub-cluster floor) is entirely untested |
| `cluster_notes_global` | 0 | 0 | None | Not exercised |
| `dominant_category_for_cluster` | 0 | 0 | None | Not exercised, including the tie-break and no-overlap (`"_unassigned"`) branches |
| `build_embeddings_by_id` | 0 | 0 | None | Trivial function, low risk, but zero explicit coverage |
| `load_category_names` | 0 | 0 | None | The permissive-degradation behavior (returns `[]` on any exception) is untested |
| `_cosine_similarity_batch` | 0 (indirectly via `assign_notes_to_categories`'s one test) | 0 | Partial | The zero-vector guard (`v_norm == 0 → zeros`) and zero-norm matrix-row guard are untested |
| `_cluster_embeddings` | 0 | 0 | None | The entire UMAP/HDBSCAN/KMeans fallback chain (5 distinct branches: import failure, `n_neighbors<2` early-return, `n_components<2` fallback, UMAP runtime exception, all-noise HDBSCAN result) has no direct test |
| `_cluster_kmeans` | 0 | 0 | None | The `k` heuristic, `ImportError` guard, and small-cluster filtering are untested |

Overall: the module's **routing/graph logic** (bucket assignment, MOC-overlap detection, cohesion scoring) has baseline happy-path unit coverage; the module's **numerical clustering core** (UMAP/HDBSCAN/KMeans, all of §3's clustering-related business rules) has **no automated test coverage** anywhere in the repository.

---

*Report generated by component-deep-analyzer. Analysis is read-only; no project files were modified.*
