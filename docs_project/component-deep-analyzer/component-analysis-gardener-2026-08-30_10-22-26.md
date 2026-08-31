# Component Deep Analysis Report — gardener

## 1. Executive Summary

The **gardener** component is Phase 4 of the Zettelkasten pipeline (`harvest → extract → review → connect → garden`). Its job is to take the pool of `permanent` (ZTL) notes already embedded into ChromaDB and periodically organize them into **Maps of Content (MOC)** — curated, hierarchical index notes that group related permanent notes under a topic, with short human-readable descriptions per subsection.

The component is split across three cooperating modules plus one cross-cutting helper:

- `zettel/gardener.py` — the **taxonomy-first pipeline** (`zettel garden`): embeds category labels from a YAML taxonomy, buckets notes by nearest category, clusters within each bucket (UMAP+HDBSCAN, KMeans fallback), and routes each cluster through a single LLM call that either creates a new MOC or extends an existing one.
- `zettel/gardener_assign.py` — pure, side-effect-free helpers used by the pipeline above: category embedding/assignment, per-bucket and global clustering, graph cohesion scoring, and MOC/cluster overlap lookup.
- `zettel/gardener_hub.py` — a **complementary, graph-anchored pipeline** (`zettel garden --hubs`): ranks permanent notes by weighted graph degree, expands each hub's neighborhood via BFS, deduplicates overlapping neighborhoods, and generates/updates "HUB" MOCs anchored on a single entry-point note.
- `zettel/moc_backrefs.py` — shared post-write step used by both pipelines (and by `sync.py`): maintains an `auto-moc-backrefs` managed block on every permanent note that a MOC links to, and removes stale links when a MOC's membership changes or a MOC is purged.

Both pipelines share the same design contract: **at most one LLM call per cluster**, deterministic non-LLM routing decisions (signature match → note-overlap match → topic match → graph-cohesion gate), idempotent re-runs via a cluster signature hash, and "ghost ID" / "missing ID" reconciliation so a hallucinated or incomplete LLM response never corrupts the MOC file or silently drops a note. Both pipelines rely on `zettel/taxonomy.py` for the optional topic whitelist (`config/moc_topics.yaml`) and on `zettel/vault.py` for safe frontmatter/managed-block file I/O.

Key findings:
- The component gracefully degrades at every external-dependency boundary: missing `umap`/`hdbscan` falls back to KMeans; a missing/invalid taxonomy file is either fatal (`strict_topics: true`) or permissive; an LLM failure on any single cluster only skips that cluster (logged), it never aborts the whole `garden` run.
- Idempotency is achieved through a SHA-256 cluster signature (`sorted(note_ids)` joined and hashed) checked before any LLM call — re-running `garden` with no new data is a no-op.
- The taxonomy pipeline and the hub pipeline are fully independent at the data level (`origin: pipeline` vs `origin: hub_pipeline` in the `mocs` table, `MOC -` vs `HUB -` filename prefixes), but they share code (gardener_hub imports several private helpers directly from gardener.py) and both funnel through `moc_backrefs.sync_moc_backrefs`.
- `--recreate` is destructive but scoped: it only purges the origin matching the invoked mode, and always removes the note-level backref links before deleting the MOC file/row.


## 2. Data Flow Analysis

### 2a. Taxonomy pipeline (`run_garden`, gardener.py)

```
1.  CLI `zettel garden` (or web job "garden") calls run_garden(cfg, db, idx, recreate=...)
2.  [optional] recreate=True -> purge_pipeline_mocs(): delete origin="pipeline" MOCs
                                  (clear_moc_backrefs -> idx.delete_mocs -> unlink .md files)
3.  resolve_allowed_topics(topics_path, allowed_topics, strict) — fail-fast if taxonomy
    required but missing/invalid (TaxonomyLoadError -> run marked "failed")
4.  Guard: idx.count_permanent_notes() < gardener.min_cluster_size -> return [] (no-op)
5.  idx.get_all_permanent_embeddings() -> (ids, embeddings) from Chroma "permanent_notes"
6.  load_category_names(topics_path) [gardener_assign] -> category label list
7.  IF categories AND cluster_within_category:
       embed_category_labels() -> per-category embedding vectors (embeds "{domain}: {categoria}")
       assign_notes_to_categories() -> cosine-similarity nearest-category bucket per note
       cluster_notes_within_buckets() -> UMAP+HDBSCAN (or KMeans) INSIDE each bucket
    ELSE (or on assignment failure): cluster_notes_global() over the whole embedding matrix,
       then dominant_category_for_cluster() backfills a category label per cluster
8.  For each (category, cluster_ids) pair -> _process_cluster():
       a. sorted+hash note_ids -> cluster_signature
       b. db.get_moc_by_signature() -> exact repeat -> return existing moc_id (no LLM)
       c. find_moc_by_note_overlap() -> cluster mostly already inside an existing pipeline
          MOC -> route to _update_existing_moc() (1 LLM call, moc_incremental.md prompt)
       d. db.find_moc_by_topic(category) -> a pipeline MOC already owns this topic name
          -> route to _update_existing_moc() (same as above)
       e. [if graph_cohesion_enabled] graph_cohesion() ratio < graph_cohesion_min_ratio
          -> reject cluster, no MOC created
       f. otherwise -> _create_new_moc() (1 LLM call, moc_generation.md prompt)
9.  _create_new_moc() / _update_existing_moc() each:
       - build the prompt payload (notes list, TF-IDF cluster terms, taxonomy detail)
       - call_llm() -> parse MOCGenerationOutput / MOCIncrementalOutput (JSON, robust to
         markdown code fences via extract_json)
       - validate + reconcile note references (_resolve_note_ref: alias, exact ID, edit-
         distance-1 fuzzy match, else dropped with a warning)
       - build/patch the Markdown body (# Topic / ## Subsections / - [[ZTL wikilinks]])
       - safe_write_note() to 40_MOCs/MOC - <ULID> - <slug>.md
       - sync_moc_backrefs() -> update auto-moc-backrefs block on every linked permanent note
       - db.upsert_moc() (persists body + frontmatter JSON so `zettel rebuild` can restore
         the file without re-calling the LLM)
       - idx.upsert_moc() -> embed "topic\n\nsummary" into Chroma "mocs" collection
10. Aggregate moc_ids, log summary stats (incremental/created/skipped/rejected), finish run.
```

### 2b. Hub-anchored pipeline (`run_garden_hubs`, gardener_hub.py)

```
1.  CLI `zettel garden --hubs` calls run_garden_hubs(cfg, db, idx, recreate=...)
2.  [optional] recreate=True -> purge_hub_pipeline_mocs(): delete origin="hub_pipeline" MOCs
3.  rank_note_hubs(): db.get_weighted_note_degrees() over note_connections, filtered to
    permanent notes, thresholded by percentile or absolute degree, existing hub anchors
    always kept in the candidate pool, capped at hub_mocs.top_n_hubs
4.  For each candidate hub -> build_hub_neighborhood(): expand_notes() BFS (graph.py) up to
    max_hops, filtered by min_neighbor_weight, capped at max_neighbors - 1
    -> skip hubs whose neighborhood < min_neighbors
5.  dedup_hub_neighborhoods(): drop a smaller hub whose neighborhood is >= dedup_subset_
    threshold contained within a larger, already-accepted hub's neighborhood
6.  For each (hub_id, note_ids) -> _process_hub_cluster():
       a. db.find_moc_by_hub_note_id(hub_id) -> existing hub MOC -> _update_hub_moc()
          (1 LLM call, moc_hub_incremental.md)
       b. else: cluster_signature = sha256("hub:<id>|"+sorted ids); signature match ->
          return existing moc_id (no LLM)
       c. else: _create_new_hub_moc() (1 LLM call, moc_hub_generation.md)
7.  _create_new_hub_moc()/_update_hub_moc() build a body with a fixed "## Porta de entrada"
    (entry point) section linking the hub note itself, plus LLM-authored subsections for
    the neighbors; same wikilink resolution/reconciliation, safe_write_note, sync_moc_
    backrefs, db.upsert_moc, idx.upsert_moc as the taxonomy pipeline.
8.  Aggregate moc_ids, log stats, finish run.
```

Both flows terminate in the same MOC file format and the same `mocs` SQLite table, differentiated by the `origin` column and filename prefix (`MOC -` vs `HUB -`).


## 3. Business Rules & Logic

### Overview of the business rules

| Rule Type | Rule Description | Location |
|-----------|-------------------|----------|
| Guard | Skip clustering entirely if the vault has fewer than `min_cluster_size` permanent notes | gardener.py:88-93 |
| Guard | Skip clustering if fewer embeddings than `min_cluster_size` are available | gardener.py:96-99 |
| Validation | Taxonomy file must exist and parse if `strict_topics: true`; otherwise `garden` fails the run | gardener.py:74-83, taxonomy.py:73-104 |
| Business Logic | A cluster smaller than `min_notes_for_moc` never becomes a MOC | gardener.py:138, gardener_assign.py:79,84,93 |
| Business Logic | Notes are assigned to the taxonomy category with the highest cosine similarity to `"{domain}: {categoria}"` | gardener_assign.py:43-64 |
| Business Logic | Clustering runs *within* each category bucket, not globally, when `cluster_within_category: true` | gardener.py:112-123, gardener_assign.py:67-96 |
| Business Logic | UMAP+HDBSCAN is the primary clustering algorithm; KMeans is a pure fallback when the libraries are absent or UMAP fails | gardener_assign.py:198-272 |
| Idempotency | A cluster whose signature (hash of sorted note IDs) already exists is returned unchanged — no LLM call, no file write | gardener.py:242-249, gardener_hub.py:304-308 |
| Business Logic | A cluster with ≥ `overlap_threshold` fraction of its notes already inside an existing pipeline MOC is routed to incremental update, not new-MOC creation | gardener.py:251-259, gardener_assign.py:157-182 |
| Business Logic | A cluster whose assigned category name already matches an existing MOC's topic (substring match) is routed to incremental update | gardener.py:261-271, state.py `find_moc_by_topic` |
| Quality Gate | A brand-new cluster can be rejected before any LLM call if its internal graph cohesion ratio is below `graph_cohesion_min_ratio` | gardener.py:273-283, gardener_assign.py:126-154 |
| Validation | A new MOC's `topic` must match (substring, case-insensitive, either direction) an allowed taxonomy category name; rejected outright under `strict_topics` | gardener.py:400-408, 422-458 |
| Reconciliation | Any note reference from the LLM that isn't in the cluster (a "ghost ID") is discarded with a warning; a near-miss (edit distance 1) is auto-corrected | gardener.py:770-821 |
| Reconciliation | Any cluster note the LLM forgot to place in a subsection is appended to a fallback "Outras notas do cluster" subsection so no note is silently dropped | gardener.py:850-886, 637-724 |
| Business Logic | Incremental updates never touch notes already present in the MOC (`truly_new` diff); if there are zero new notes, only the cluster signature is refreshed (no LLM call) | gardener.py:550-560 |
| Business Logic | The LLM may only place a new note under an *existing* subsection title or a newly proposed one; `subsection: "ignorar"` explicitly excludes a note from the MOC | gardener.py:656-666, 619-621 |
| Business Logic | Hubs are selected by weighted graph degree (either top percentile or an absolute floor), restricted to permanent notes, with existing hub anchors always retained regardless of current rank | gardener_hub.py:44-86 |
| Business Logic | A hub whose expanded neighborhood is smaller than `min_neighbors` is discarded | gardener_hub.py:212-214 |
| Business Logic | Overlapping hub neighborhoods are deduplicated — a smaller hub subsumed (≥ `dedup_subset_threshold`) by a larger one's neighborhood is dropped, larger hub wins by degree | gardener_hub.py:122-147 |
| Data Integrity | `--recreate` only deletes MOCs of the matching origin (`pipeline` vs `hub_pipeline`); manually authored MOCs (`origin: manual`) are always preserved | gardener.py:172-191, gardener_hub.py:258-278 |
| Data Integrity | Deleting a MOC always strips its backref links from every permanent note it referenced, before the MOC file/row is removed | gardener.py:180-181, gardener_hub.py:267-268, moc_backrefs.py:109-124 |
| Data Integrity | Backref sync is diff-based: notes removed from a MOC's body lose the link, notes newly added gain it; unrelated managed-block content and manual edits are preserved | moc_backrefs.py:78-106 |

### Detailed breakdown of the business rules

---

### Business Rule: Minimum-size guards before clustering

**Overview**:
The gardener refuses to run any clustering algorithm — and refuses to consider a cluster "MOC-worthy" — below two independent size thresholds: `gardener.min_cluster_size` (global) and `gardener.min_notes_for_moc` (per-cluster).

**Detailed description**:
Before any embedding or clustering work happens, `run_garden` checks `idx.count_permanent_notes()` against `cfg.gardener.min_cluster_size` (default 5) and returns an empty list immediately if the vault doesn't have enough permanent notes yet. This is a coarse circuit-breaker: clustering algorithms like HDBSCAN are statistically meaningless on a handful of points, and the pipeline would otherwise waste an LLM call generating a low-quality MOC from noise. A second, independent check happens after fetching embeddings from Chroma — even if the note count check passed, an embedding-provider hiccup or partial index could leave `embeddings` shorter than expected, so the same threshold is re-applied to the actual vector array length.

A second, more granular guard applies per-cluster rather than globally: `min_notes_for_moc` (default 10 in `config.yaml`, default 3 in the Pydantic schema fallback — operationally the YAML value wins). This is checked in three places: once when clustering within a taxonomy bucket (`cluster_notes_within_buckets`, both on the whole bucket size and on each HDBSCAN subcluster), and once when clustering globally (`run_garden`'s loop over `global_clusters`). A cluster that survives HDBSCAN's own noise-filtering (label `-1` is discarded) but is still too small to justify a dedicated MOC is silently skipped — no LLM call, no rejection log line, it simply never enters `cluster_pairs`.

The practical effect is that MOC generation is deliberately conservative: a topic needs a critical mass of permanent notes before the system will curate it, and the vault reaches a threshold at which fragmented small clusters don't spawn a flood of one-off MOCs. Because both thresholds are configuration-driven (`config.yaml` → `gardener.min_cluster_size` / `min_notes_for_moc`), operators can tune them per corpus size without touching code, and CLI users can override `min_cluster_size` per-invocation with `zettel garden --min-cluster-size`.

**Rule workflow**:
```
count_permanent_notes() < min_cluster_size?  -> YES -> return [] (log + finish_pipeline_run)
                                                 NO  -> fetch embeddings
len(embeddings) < min_cluster_size?           -> YES -> return []
                                                 NO  -> proceed to clustering
per bucket/subcluster: len(cluster) < min_notes_for_moc? -> YES -> drop cluster (no MOC)
                                                             NO  -> cluster enters cluster_pairs
```

---

### Business Rule: Taxonomy-driven category assignment and per-category clustering

**Overview**:
When a topic taxonomy is configured (`gardener.topics_path`), notes are first bucketed into taxonomy categories by embedding similarity, and clustering runs independently inside each bucket rather than over the whole vault at once.

**Detailed description**:
`load_category_names` reads `config/moc_topics.yaml` via `zettel/taxonomy.py` and flattens its `pilar > categoria > topicos` hierarchy down to a list of category names (the `categoria.nome` values, deduplicated, order-preserving). Each category name is turned into an embeddable label using `gardener.category_label_template` (default `"{domain}: {categoria}"`, e.g. `"Ciencia de Dados: Machine Learning Classico"`) and embedded through the same embedding function used for note content (`idx.embed_texts`), so category vectors live in the same vector space as note embeddings. `assign_notes_to_categories` then computes cosine similarity between every note's embedding and every category vector, and assigns each note to its single best-matching category (`np.argmax` over the similarity row) — there is no soft/multi-category assignment and no minimum-similarity floor, so every note is always assigned to *some* category, even a marginal one.

Clustering then proceeds bucket-by-bucket (`cluster_notes_within_buckets`): a category with fewer notes than `min_notes_for_moc` is skipped outright; a category at or above that threshold but below `min_cluster_size` is treated as a single cluster without running HDBSCAN (too few points for the algorithm to be meaningful, but still enough to be a MOC candidate); a category at or above `min_cluster_size` is passed through `_cluster_embeddings` (UMAP dimensionality reduction + HDBSCAN density clustering, see the next rule) to split it into semantically coherent sub-clusters, each independently checked against `min_notes_for_moc` again. This hybrid approach — taxonomy first, then unsupervised refinement inside each taxonomy bucket — is what prevents HDBSCAN from freely mixing notes across unrelated top-level domains, while still letting it discover natural sub-topics inside a large category (e.g. splitting a huge "Machine Learning" bucket into "Redes Neurais" and "Métodos Ensemble" sub-clusters).

If category embedding or assignment raises any exception (e.g. embedding provider failure), `run_garden` catches it, logs a warning, and falls back to global clustering with `categories = []` — the taxonomy step is best-effort and never fatal to the whole run (the *taxonomy file* itself being missing/invalid, in contrast, is fatal under `strict_topics`, per the next rule). When `cluster_within_category` is `false` in config, this whole step is skipped by design and global clustering (`cluster_notes_global`) is used directly, with `dominant_category_for_cluster` backfilling a category label for the incremental/topic-based routing decisions.

**Rule workflow**:
```
categories = load_category_names(topics_path)  (empty if no taxonomy or unreadable)
IF categories AND cluster_within_category:
    try:
        cat_vectors = embed_category_labels(categories)       # 1 embedding call per category
        buckets     = assign_notes_to_categories(notes, cat_vectors)  # argmax cosine sim
        cluster_pairs = cluster_notes_within_buckets(buckets)  # per-bucket UMAP+HDBSCAN
    except Exception:
        categories = []   # fall through to global clustering, cluster_pairs stays []
IF NOT cluster_pairs:
    global_clusters = cluster_notes_global(all_notes)          # UMAP+HDBSCAN over everything
    category = dominant_category_for_cluster(cluster, buckets) # best-effort label backfill
    cluster_pairs = [(category, cluster) for cluster large enough]
```

---

### Business Rule: UMAP+HDBSCAN clustering with automatic KMeans fallback

**Overview**:
The core unsupervised clustering step reduces note embeddings to a low-dimensional space with UMAP and clusters them by density with HDBSCAN, discarding noise points; if either library is unavailable or UMAP fails at runtime, the system transparently falls back to KMeans so the pipeline never hard-fails on an optional dependency.

**Detailed description**:
`_cluster_embeddings` (gardener_assign.py) first attempts to `import umap` and `import hdbscan`; an `ImportError` on either logs a warning and defers immediately to `_cluster_kmeans`. When both are available, the neighbor count for UMAP is `min(cfg.umap_n_neighbors or 15, n_samples - 1)` — capped so it never exceeds the number of points minus one, which would otherwise crash UMAP on small clusters. If the resulting `n_neighbors < 2` the function short-circuits and returns the whole set as a single cluster (too few points to meaningfully reduce dimensionality). Similarly, `n_components` for the UMAP embedding is `min(5, n_samples - 2)`; if that drops below 2, the function falls back to KMeans instead of attempting a degenerate UMAP projection. UMAP's `init` method switches from `"spectral"` (better quality, needs enough points to build a connectivity graph) to `"random"` when `n_samples <= n_components + 2`, to avoid a known UMAP failure mode on very small inputs.

After the UMAP reduction (cosine metric on the original embeddings), HDBSCAN clusters the reduced space with `min_cluster_size` from config and an optional `min_samples` override (`cfg.hdbscan_min_samples`). HDBSCAN's label `-1` (noise — points that don't belong to any dense cluster) is explicitly discarded; those notes remain in the vault, fully navigable through the note graph and other MOCs, but are simply not routed into a MOC of their own for this run. If HDBSCAN produces no clusters at all, the function either returns the whole input as one cluster (when it's already ≥ `min_cluster_size`) or an empty list. Any exception during UMAP fitting (not just missing imports) is caught and also falls back to KMeans, making the "real" clustering path resilient to both missing dependencies and runtime numerical failures (e.g. a degenerate embedding matrix).

The KMeans fallback (`_cluster_kmeans`) picks `k = max(2, n // min_cluster_size)` capped at `n`, and after fitting drops any resulting cluster smaller than `min_cluster_size` — this is a much cruder clustering strategy (no noise concept, no density awareness) but guarantees the pipeline still produces *some* clusters even without the optional ML dependencies installed, and is itself gated by another `try/except ImportError` around `scikit-learn` (if that's missing too, the function just returns the input as a single all-or-nothing cluster).

**Rule workflow**:
```
try import umap, hdbscan:
    except ImportError -> _cluster_kmeans(...)
n_neighbors = min(cfg.umap_n_neighbors or 15, n_samples-1); < 2 -> return [all_ids] (single cluster)
n_components = min(5, n_samples-2); < 2 -> _cluster_kmeans(...)
init = "spectral" if n_samples > n_components+2 else "random"
try: reduced = UMAP(...).fit_transform(embeddings)
except Exception -> _cluster_kmeans(...)
labels = HDBSCAN(min_cluster_size, min_samples?).fit_predict(reduced)
drop label == -1 (noise); group remaining by label -> list of clusters
if no clusters -> [all_ids] if len >= min_cluster_size else []
```

---

### Business Rule: Cluster-to-MOC routing (signature -> overlap -> topic -> cohesion -> new)

**Overview**:
`_process_cluster` is the single decision point that decides, for every cluster discovered in a run, whether to (a) do nothing because it's an exact repeat, (b) extend an existing MOC, (c) reject the cluster outright, or (d) create a brand-new MOC — and it guarantees at most one LLM call regardless of which branch fires.

**Detailed description**:
The routing order is deliberately layered from cheapest/most-certain to most-expensive/least-certain. First, `sorted(note_ids)` is hashed (SHA-256) into a `cluster_signature`; `db.get_moc_by_signature` performs an exact lookup and, if found, returns the existing `moc_id` with zero further work — this is what makes repeated `garden` runs on an unchanged vault a true no-op, and it is checked before either of the two "routing" heuristics below, so an identical cluster never re-triggers an incremental LLM call just because it also happens to overlap an old MOC.

Second, `find_moc_by_note_overlap` (gardener_assign.py) scans every `origin="pipeline"` MOC's body for `[[ZTL - ...]]` note IDs, computes `|cluster ∩ moc_notes| / |cluster|`, and picks the highest-scoring MOC whose overlap meets `gardener.overlap_threshold` (default 0.4). If found, the cluster is treated as "this is basically the same topic, evolved" and routed to `_update_existing_moc` — a single incremental LLM call that only classifies the *new* notes (the ones not already in the MOC) into existing or new subsections. This catches semantic drift: even if the *category label* assigned to the cluster this run differs from a prior run (e.g. taxonomy assignment flipped due to embedding noise near a decision boundary), a MOC that already contains most of the same notes is recognized and extended rather than duplicated.

Third, if no overlap match was found but the cluster's assigned `category` is a real category (not `_unassigned`), `db.find_moc_by_topic(category)` does a case-insensitive substring match against existing MOC topics. This is the taxonomy-label-based recognition path — useful when a MOC already exists for "Machine Learning Classico" and a *newly discovered* cluster is independently assigned that same category, even though its members barely overlap the existing MOC's members (e.g. the corpus grew a second cluster within the same taxonomy category). This, too, routes to `_update_existing_moc`.

Only if none of the above three deterministic checks match does the function consider *creating* a new MOC — and even then, if `graph_cohesion_enabled` is true, it first computes a weighted-edge cohesion ratio over `note_connections` for the cluster and rejects it (no MOC, no LLM call) if the ratio falls below `graph_cohesion_min_ratio` (default `0.0`, meaning by default this is purely observational/logged and never actually blocks). This is the system's defense against embedding-only false-positive clusters: two notes can look "close" in vector space (e.g. share vocabulary) while having no actual conceptual relationship recorded in the connect-phase-built note graph; a near-zero internal edge weight is a signal the cluster may be spurious. Only after clearing this gate does `_create_new_moc` fire the "full" LLM call that invents a brand-new topic, summary, and subsection structure.

**Rule workflow**:
```
signature = sha256(sorted(note_ids))
existing_by_signature = db.get_moc_by_signature(signature)
IF existing_by_signature: return its moc_id                          # 0 LLM calls

overlap_moc = find_moc_by_note_overlap(cluster, overlap_threshold)
IF overlap_moc: return _update_existing_moc(overlap_moc, ...)        # 1 LLM call (incremental)

IF category != "_unassigned":
    topic_moc = db.find_moc_by_topic(category)
    IF topic_moc: return _update_existing_moc(topic_moc, ...)        # 1 LLM call (incremental)

IF graph_cohesion_enabled:
    cohesion = graph_cohesion(cluster)
    IF graph_cohesion_min_ratio > 0 AND cohesion < graph_cohesion_min_ratio:
        return None                                                  # 0 LLM calls, rejected

return _create_new_moc(category, cluster, ...)                       # 1 LLM call (new MOC)
```

---

### Business Rule: MOC topic taxonomy validation

**Overview**:
A freshly LLM-generated MOC `topic` must correspond to one of the whitelisted taxonomy category names (a bidirectional, case-insensitive substring check); under `strict_topics: true` a non-matching topic causes the whole MOC to be discarded, while under `strict_topics: false` it is logged and allowed through.

**Detailed description**:
`_validate_moc_topic` re-resolves the allowed topic list via `resolve_allowed_topics` (the same function used to build the prompt's whitelist section) rather than trusting a cached value, so validation always reflects the current taxonomy file state even if it changed mid-run. If the resolved list is empty (`no topics_path` and `no override`), validation is a no-op that always approves — the taxonomy feature is entirely opt-in. Otherwise, each allowed category name is compared against the LLM's chosen `topic` with `allowed_lower in topic_lower or topic_lower in allowed_lower`; this deliberately loose bidirectional substring match tolerates the LLM producing a slightly more specific or more general phrasing of an allowed category (e.g. "Deep Learning" matching an allowed "Deep Learning e Modelos Neurais", in either direction) without requiring exact string equality, which would be brittle against an LLM's natural language variance.

When no allowed category matches and `strict_topics` is true (the project default), the MOC is rejected: `_create_new_moc` returns `None` without writing any file, embedding, or DB row, and the LLM's `topic_justification` field (a free-text explanation the LLM is asked to provide for its topic choice) is logged so an operator can see *why* the model picked an off-taxonomy topic and decide whether to broaden the taxonomy or adjust the prompt. When `strict_topics` is false, the same off-taxonomy topic is accepted, with an info-level log line carrying the same justification — this permissive mode is useful when the taxonomy is treated as a soft suggestion rather than an authoritative whitelist (e.g. exploratory vaults not yet fully mapped to a fixed knowledge taxonomy).

A related mechanism, `_topic_matches_allowed`, runs *before* this validation, inside `_create_new_moc`: if the cluster's `suggested_category` (from taxonomy-first bucket assignment) matches an allowed topic, the LLM's own topic choice is silently overwritten with the suggested category name — this is an optimization/consistency measure that prefers the deterministic taxonomy assignment over the LLM's free-text choice whenever they agree, reducing topic-string drift across MOCs that should logically share the same category name.

**Rule workflow**:
```
IF suggested_category matches an allowed topic (substring, either direction):
    moc_output.topic = suggested_category      # prefer deterministic assignment
allowed, _ = resolve_allowed_topics(...)
IF allowed is empty: return True                # taxonomy not configured -> allow-all
FOR each allowed_topic:
    IF allowed_topic.lower() in topic.lower() OR topic.lower() in allowed_topic.lower():
        return True                             # match found -> approved
# no match:
IF strict_topics: log rejection + justification; return False   # MOC discarded
ELSE:              log permissive approval + justification; return True
```

---

### Business Rule: Incremental MOC update — new-note diffing, LLM placement, and reconciliation

**Overview**:
When a cluster is routed to an existing MOC, only the notes *not already present* in that MOC are sent to the LLM for classification into existing or newly proposed subsections; the LLM's response is then defensively reconciled against the actual cluster membership before the file is rewritten.

**Detailed description**:
`_update_existing_moc` starts by parsing the MOC's current on-disk structure (`_parse_moc_structure`): frontmatter is stripped, the `# Topic` H1 becomes the summary block, each `## Subsection` heading starts a new subsection whose body lines are scanned for `[[ZTL - <id> - ...]]` wikilinks (collected as `note_ids`) or, if not a wikilink and not a bullet, appended to that subsection's free-text `description`. The union of every subsection's note IDs becomes `existing_ids`. The function then computes `truly_new = [nid for nid in note_ids if nid not in existing_ids]` — if this is empty, no LLM call happens at all; only the cluster signature is refreshed in the DB (`db.upsert_moc` with the new signature) so future runs treat this exact set as already handled, while the MOC file itself is untouched.

If there are new notes, the LLM is given the existing topic, summary, and a reconstructed view of existing subsections (title + description + one bullet per note, using the *current* wikilink for each, resolved fresh from `StateDB` rather than reused verbatim from the file — this keeps links correct even if a note's title/slug changed since the MOC was last written) plus the new notes list (aliased `N1, N2, ...` to keep the LLM's response compact and avoid ID-transcription errors). The `moc_incremental.md` prompt asks the LLM to return `MOCIncrementalOutput`: a `placements` list (`note_id` -> `subsection` title, or the literal string `"ignorar"` to explicitly exclude a note) and a `new_subsections` list (title/description/note_ids) for notes that don't fit any existing subsection.

`_apply_incremental_placements` then reconstructs the entire MOC body from scratch (not a line-level patch) to guarantee structural consistency: for each placement, `_resolve_note_ref` maps the LLM's reference back to a real note ID (exact alias match, exact ID match, or an edit-distance-1 fuzzy correction for likely LLM typos — logged as a warning either way) and is dropped if it can't be resolved or refers to a note outside the batch (`allowed_ids`, the cluster's `truly_new` set). A `subsection: "ignorar"` marks the note as intentionally placed but excluded from the body (it's still recorded in `placed`, so it isn't picked up by the reconciliation fallback below). A placement into a subsection title that doesn't exist and wasn't declared in `new_subsections` is dropped — no orphan headings are silently invented. Any note in `truly_new` that ends up in neither a placement nor a new subsection (`missing = allowed_ids - placed`) is force-appended to the `_MOC_FALLBACK_SUBSECTION` ("Outras notas do cluster") so the cluster invariant — every note the pipeline decided belongs in this MOC actually appears in the file — always holds, even against an incomplete LLM response.

After the body is rewritten, `sync_moc_backrefs` is called with both the `previous_body` (captured before the rewrite) and the new body, so backref links are added for genuinely new note associations and removed for any that (in edge cases) disappeared. `db.upsert_moc` persists a full snapshot (`_snapshot_moc_file`) of the rewritten body/frontmatter, and `idx.upsert_moc` refreshes the Chroma embedding for the MOC's topic+summary text (using the *existing* structure's summary, not a new one — incremental updates never ask the LLM to rewrite the summary).

**Rule workflow**:
```
structure = _parse_moc_structure(moc_path)          # existing subsections + note_ids
truly_new = [n for n in cluster_note_ids if n not in structure.all_note_ids]
IF truly_new is empty:
    db.upsert_moc(new_signature only)                # 0 LLM calls
    return moc_id
response = call_llm(moc_incremental.md, existing structure + truly_new notes)
output = MOCIncrementalOutput(placements, new_subsections)
FOR each placement: resolve note ref (alias/exact/fuzzy) -> place in named subsection,
                    drop into "ignorar" bucket, or discard if unresolved/unknown subsection
FOR each new_subsections entry: resolve refs, append as a brand-new "## Title" section
missing = truly_new - placed  -> force-appended to "Outras notas do cluster"
rewrite full MOC body -> safe_write_note -> sync_moc_backrefs -> db.upsert_moc -> idx.upsert_moc
```

---

### Business Rule: Ghost-ID rejection and orphan-note reconciliation (shared invariant)

**Overview**:
Every MOC body construction path — new-MOC creation, incremental update, and hub-MOC variants — enforces the same two-sided invariant: no note ID that isn't actually part of the cluster may appear in the file (rejecting LLM hallucinations), and no note ID that *is* part of the cluster may be silently missing from the file (guaranteeing completeness).

**Detailed description**:
`_resolve_note_ref` is the single choke point for turning an LLM-produced reference (which may be a short alias like `N3`, a full note ID, or a typo of either) into a validated note ID. It checks the alias map first (fast path for the common case — the LLM was asked to use aliases specifically to reduce transcription errors), then checks whether the raw token is itself a known allowed ID (handles an LLM that ignores the alias convention and echoes the real ID), and only then falls back to `_fuzzy_match_note_id`, which accepts a correction only when *exactly one* allowed ID is within Levenshtein-style edit distance 1 of the LLM's token (implemented as a hand-rolled `_within_edit_distance_one`, not a library) — an ambiguous fuzzy match (two or more candidates within distance 1) is treated as unresolvable rather than guessed, favoring silent omission over a wrong guess. Any reference that still can't be resolved is logged as a warning and dropped; this is the mechanism that prevents a hallucinated note ID from ever reaching a wikilink in the vault.

The completeness side of the invariant is enforced separately in `_build_moc_body` (new MOC) and `_apply_incremental_placements` (incremental): after processing every subsection the LLM proposed, whatever cluster note IDs were never placed (`allowed_ids - placed`) are appended, sorted, into the fallback subsection `"Outras notas do cluster"`. This means the pipeline's own routing decision (which notes belong in this MOC, decided by the clustering/graph/overlap logic, not the LLM) is authoritative over the LLM's placement choices — the LLM only decides *where inside the MOC* a note goes, never *whether* it belongs, and a partial or malformed LLM response degrades to "correct membership, worse organization" rather than data loss. Both rules are backed by targeted unit tests (`test_build_moc_body_filters_ghost_and_reconciles_missing`, `test_apply_incremental_reconciles_unplaced`, `test_apply_incremental_ghost_id_ignored`, `test_resolve_note_ref_fuzzy_typo`).

**Rule workflow**:
```
for every note ref emitted by the LLM:
    IF ref in alias_to_id:                 -> resolved (alias hit)
    ELIF ref in allowed_ids:               -> resolved (verbatim ID hit)
    ELIF exactly one allowed_id within edit distance 1: -> resolved (fuzzy correction, warned)
    ELSE:                                  -> discarded (warned, "fora do cluster")
after all placements processed:
    missing = allowed_ids - placed
    IF missing: append to "## Outras notas do cluster" (sorted), mark as placed
```

---

### Business Rule: Graph-degree hub ranking and neighborhood expansion (hub pipeline)

**Overview**:
The hub pipeline selects "entry point" notes by weighted graph connectivity rather than embedding similarity, then builds each hub's MOC content from its BFS-expanded neighborhood in the note graph, with existing hub anchors always retained across runs regardless of current degree.

**Detailed description**:
`rank_note_hubs` computes a weighted degree for every note via `db.get_weighted_note_degrees(DEFAULT_RELATION_WEIGHTS)` (edge count weighted by relation type — `contradicts` weighs highest at 1.0, `related` lowest at 0.5, reflecting that a `contradicts` edge is strong evidence of a meaningful connection an embedding alone wouldn't surface), restricted to notes that are actually `permanent` (so a highly-connected source or literature note, if such edges existed, wouldn't wrongly become a hub anchor). Two selection strategies are supported: `"absolute"` keeps every note at or above `hub_mocs.min_weighted_degree`, while `"percentile"` (the default) computes a rank-based threshold — the degree value at the `(1 - hub_percentile)` position in the sorted degree list — so the selection automatically scales with vault size rather than needing manual threshold retuning as the graph grows. Critically, `existing_anchors` (hub IDs already used by a live hub MOC, from `db.list_hub_anchor_note_ids()`) are always re-included even if their current degree falls below the threshold — this prevents a hub MOC from being orphaned (unable to receive incremental updates) just because its relative rank slipped as the graph grew elsewhere. The final candidate list is capped at `top_n_hubs`.

For each candidate hub, `build_hub_neighborhood` runs `expand_notes` (graph.py's BFS) seeded at the hub with `seed_weights={hub_id: 1.0}`, requesting `2x` the eventual neighbor budget so that the `min_neighbor_weight` filter (drop neighbors whose BFS-decayed weight is too weak — i.e., too many hops away or reached only through weak relation types) still leaves enough candidates to fill `max_neighbors - 1` slots after filtering. A hub whose neighborhood — even before dedup — doesn't reach `min_neighbors` is discarded (`stats.skipped_small`), on the reasoning that a hub MOC needs a genuine neighborhood to justify its existence, not just a technically-nonzero degree.

Because hub neighborhoods are built independently and can substantially overlap (two structurally central notes in the same subgraph will each pull in much of the same neighborhood), `dedup_hub_neighborhoods` processes hubs from highest to lowest degree and drops any hub whose neighborhood-set overlap with an already-accepted hub's set is `>= dedup_subset_threshold` — this keeps only the most central "true" entry points and prevents near-duplicate hub MOCs from being generated for what is effectively the same cluster of notes viewed from two different anchor points.

**Rule workflow**:
```
degrees = get_weighted_note_degrees(DEFAULT_RELATION_WEIGHTS), filtered to permanent notes
IF selection_mode == absolute: candidates = degrees >= min_weighted_degree
ELSE (percentile):             threshold = degree at rank (1 - hub_percentile)
                                candidates = degrees >= threshold
always re-include existing hub anchors even if below threshold
ranked = sort(candidates + retained anchors, by degree desc)[:top_n_hubs]

for each hub in ranked:
    neighborhood = BFS(hub, max_hops, decay, min_neighbor_weight)[:max_neighbors-1]
    IF len(neighborhood)+1 < min_neighbors: skip hub

dedup: process hubs by degree desc; drop hub if overlap(hub_set, any accepted_set) >= dedup_subset_threshold
```

---

### Business Rule: MOC back-reference synchronization

**Overview**:
Every permanent note linked from any MOC (pipeline, hub, or manually authored) carries a managed `auto-moc-backrefs` block listing every MOC that references it, kept in sync every time a MOC is written, updated, or purged, without disturbing the rest of the note's manually-authored content.

**Detailed description**:
`sync_moc_backrefs` is called after *every* MOC write across both pipelines (new MOC creation, incremental update) and from `sync.py`'s manual-MOC sync path — it is the single mechanism that keeps the backref relationship bidirectional, since a MOC file only stores forward links to its member notes. It diffs the set of note IDs extracted from the MOC's `previous_body` (or an empty set, for a brand-new MOC) against `new_body`: notes that disappeared from the MOC (`old_ids - new_ids`) have the MOC's link line removed from their `auto-moc-backrefs` block; notes newly referenced (`new_ids - old_ids`) get the link line appended. The link line itself (`moc_link_line`) prefers deriving the wikilink from the MOC file's actual path stem (so it always matches the real filename on disk, including its `MOC -`/`HUB -` prefix and slug) rather than reconstructing it from the topic string, which could drift from the real filename if slugification rules ever changed.

Both the addition (`_add_moc_link_to_note`) and removal (`_remove_moc_link_from_note`) helpers operate exclusively through `zettel.vault`'s managed-block primitives (`read_managed_block`/`safe_update_managed_blocks`), meaning any content the user hand-wrote in a permanent note outside the `auto-moc-backrefs` fenced block — including hand-added MOC links elsewhere in the body — is untouched. Addition is idempotent (skips if the exact link line is already present); removal is line-based (filters out any line containing the target `moc_id`, including possible manual duplicates using that same ID). `clear_moc_backrefs` (used exclusively during MOC purge/`--recreate`) is a coarser one-directional version: it removes the MOC's link from *every* note the MOC's stored body ever referenced, without needing a `new_body` to diff against, since the MOC is being deleted entirely.

**Rule workflow**:
```
new_ids = extract_note_ids(new_body)
old_ids = extract_note_ids(previous_body) if previous_body else {}
link_line = moc_link_line(moc_id, topic, path=moc_path)     # derived from file stem when possible

FOR note_id in (old_ids - new_ids): remove link_line (by moc_id substring) from that note's block
FOR note_id in (new_ids - old_ids): append link_line to that note's block (idempotent)

# on purge:
clear_moc_backrefs(moc): FOR note_id in extract_note_ids(moc.body): remove this moc_id's link
```

---

### Business Rule: Scoped `--recreate` purge

**Overview**:
`zettel garden --recreate` (and its `--hubs` counterpart) deletes only the MOCs produced by the pipeline currently being invoked, never manually authored MOCs and never MOCs from the *other* pipeline, and always cleans up dependent backref state before the MOC itself is removed.

**Detailed description**:
`purge_pipeline_mocs` calls `db.delete_pipeline_mocs()`, which is implicitly scoped to `origin="pipeline"` rows (verified by `test_purge_pipeline_mocs_keeps_manual`, which shows an `origin="manual"` MOC surviving a purge). Symmetrically, `purge_hub_pipeline_mocs` calls `db.delete_hub_pipeline_mocs()`, scoped to `origin="hub_pipeline"` (verified by `test_purge_hub_pipeline_mocs_keeps_taxonomy`, showing both `origin="pipeline"` and `origin="manual"` MOCs surviving a hub purge). This means running `garden --recreate` followed by `garden --hubs --recreate` is safe and won't cross-contaminate: each command only ever touches its own origin's rows.

For every MOC row returned by the delete call, three cleanup steps happen in a fixed order: first `clear_moc_backrefs(db, moc)` strips the MOC's link from every permanent note's `auto-moc-backrefs` block (using the *already-fetched* `moc` dict's stored `body`, avoiding a second file read when possible); second, `idx.delete_mocs([...])` removes the corresponding embeddings from Chroma's `mocs` collection in a single batched call; third, each MOC's vault file is looked up via `_moc_vault_path` (preferring the DB-stored `path`, falling back to reconstructing the expected filename from `moc_id`/`topic` if `path` is somehow absent) and deleted from disk if it exists. This ordering — backrefs first, then vector index, then filesystem — ensures that even if the process is interrupted partway through a purge, there's no window where a permanent note points to a `[[MOC - ...]]` file that no longer exists without the backref itself also having been (or about to be) cleaned in the same batch; a partial purge worst-case leaves an orphaned MOC file on disk (harmless, re-creatable) rather than a dangling reference from a note the user might not think to check.

**Rule workflow**:
```
removed = db.delete_pipeline_mocs()          # OR delete_hub_pipeline_mocs(), origin-scoped
IF not removed: return 0
FOR moc in removed: clear_moc_backrefs(db, moc)         # strip backref links first
idx.delete_mocs([moc_id for moc in removed])            # batch vector-index cleanup
FOR moc in removed: unlink(_moc_vault_path(cfg, moc)) if it exists
return len(removed)
```


## 4. Component Structure

```
zettel/
├── gardener.py                  # Taxonomy pipeline: run_garden(), MOC generation/incremental
│                                 # LLM orchestration, topic validation, note-ref reconciliation,
│                                 # purge_pipeline_mocs(). ~893 lines.
├── gardener_assign.py           # Pure helpers used by gardener.py: category embedding/
│                                 # assignment, per-bucket + global clustering (UMAP/HDBSCAN/
│                                 # KMeans), graph_cohesion(), find_moc_by_note_overlap(),
│                                 # extract_note_ids_from_moc_body(). ~283 lines, no LLM/IO calls.
├── gardener_hub.py               # Hub-anchored pipeline: run_garden_hubs(), rank_note_hubs(),
│                                 # build_hub_neighborhood(), dedup_hub_neighborhoods(),
│                                 # _process_hub_cluster(), purge_hub_pipeline_mocs(). Imports
│                                 # several private helpers directly from gardener.py (shared
│                                 # note-ref resolution, body formatting, snapshotting).
│                                 # ~625 lines.
├── moc_backrefs.py              # sync_moc_backrefs(), clear_moc_backrefs(), moc_wikilink()/
│                                 # moc_link_line(). Cross-cutting: called by both gardener
│                                 # pipelines AND by sync.py's manual-MOC sync path. ~124 lines.
├── taxonomy.py                  # MocTaxonomy/Categoria/Pilar Pydantic models, load_moc_
│                                 # taxonomy(), allowed_topic_names(), format_taxonomy_for_
│                                 # prompt(), resolve_allowed_topics(), TaxonomyLoadError.
│                                 # Depended on by gardener.py's validation/prompt-building.
├── graph.py                     # expand_notes() BFS — depended on by gardener_hub.py for
│                                 # neighborhood expansion (not owned by this component, but
│                                 # a required collaborator).
├── config.py                    # GardenerConfig, HubMocsConfig, DEFAULT_RELATION_WEIGHTS —
│                                 # all tunable knobs for both pipelines.
├── schemas.py                   # MOCGenerationOutput, MOCSubsection, MOCIncrementalOutput,
│                                 # MOCNotePlacement, MOCHubGenerationOutput — LLM structured
│                                 # output contracts.
├── vault.py                     # safe_write_note(), parse_frontmatter(), note_filename(),
│                                 # permanent_wikilink(), managed-block I/O — shared vault
│                                 # persistence primitives used throughout.
├── cli.py                       # `zettel garden [--hubs] [--recreate] [--min-cluster-size]`
│                                 # command (lines 691-745); also invoked from `run-all`.
└── web_app.py                   # WebApplication job dispatch: "garden" operation routes to
                                  # run_garden() or run_garden_hubs() based on payload["hubs"].

prompts/
├── moc_generation.md             # New taxonomy MOC — system/user template consumed by
│                                 # _create_new_moc()
├── moc_incremental.md            # Incremental taxonomy MOC update — consumed by
│                                 # _update_existing_moc()
├── moc_hub_generation.md         # New hub MOC — consumed by _create_new_hub_moc()
└── moc_hub_incremental.md        # Incremental hub MOC update — consumed by _update_hub_moc()

config/
└── moc_topics.yaml               # Operational taxonomy: pilar > categoria > topicos
                                  # (whitelist source for gardener.topics_path)

tests/
├── test_gardener.py              # Taxonomy validation, MOC structure parsing, incremental
│                                 # update/placement, routing (_process_cluster), note-ref
│                                 # resolution/reconciliation, purge_pipeline_mocs. 827 lines.
├── test_gardener_assign.py       # assign_notes_to_categories, extract_note_ids_from_moc_
│                                 # body, find_moc_by_note_overlap, graph_cohesion. 68 lines.
├── test_gardener_hub.py          # get_weighted_note_degrees, rank_note_hubs, build_hub_
│                                 # neighborhood, dedup, find_moc_by_hub_note_id, purge_hub_
│                                 # pipeline_mocs, _process_hub_cluster routing. 210 lines.
└── test_moc_backrefs.py          # sync_moc_backrefs add/remove, clear_moc_backrefs on
                                  # purge, sync-manual integration, moc_link_line path-stem
                                  # behavior. 174 lines.
```


## 5. Dependency Analysis

```
Internal Dependencies:

zettel.cli (garden command)
    -> zettel.gardener.run_garden / zettel.gardener_hub.run_garden_hubs
zettel.web_app (WebApplication._dispatch, operation="garden"|"run_all")
    -> zettel.gardener.run_garden / zettel.gardener_hub.run_garden_hubs

zettel.gardener.run_garden
    -> zettel.gardener_assign (build_embeddings_by_id, embed_category_labels,
       assign_notes_to_categories, cluster_notes_within_buckets, cluster_notes_global,
       dominant_category_for_cluster, load_category_names, find_moc_by_note_overlap,
       graph_cohesion)
    -> zettel.taxonomy (resolve_allowed_topics, TaxonomyLoadError)
    -> zettel.llm (get_llm, call_llm, load_prompt_parts, fill_template, extract_json)
    -> zettel.schemas (MOCGenerationOutput, MOCIncrementalOutput)
    -> zettel.vault (note_filename, permanent_wikilink, safe_write_note, parse_frontmatter)
    -> zettel.moc_backrefs.sync_moc_backrefs / clear_moc_backrefs (lazy import)
    -> zettel.hashing.sha256_hex
    -> zettel.state.StateDB (get_moc_by_signature, find_moc_by_topic, list_mocs, get_note,
       upsert_moc, delete_pipeline_mocs, get_connections_for_notes, get_web_dashboard-adjacent
       run bookkeeping via start_run/finish via usage.py)
    -> zettel.index.VectorIndex (count_permanent_notes, get_all_permanent_embeddings,
       embed_texts, upsert_moc, delete_mocs)
    -> zettel.usage (begin_run, finish_pipeline_run) / zettel.progress.report

zettel.gardener_hub.run_garden_hubs
    -> zettel.graph.expand_notes (BFS neighborhood expansion)
    -> zettel.gardener (imports _allowed_note_ids, _build_note_alias_map, _moc_embeddable,
       _apply_incremental_placements, _build_notes_list, _parse_incremental_output,
       _parse_moc_structure, _snapshot_moc_file, _format_note_links, _resolve_note_ref,
       _moc_vault_path — direct cross-module reuse of "private" (underscore) functions)
    -> zettel.taxonomy.resolve_allowed_topics
    -> zettel.schemas.MOCHubGenerationOutput
    -> zettel.moc_backrefs.sync_moc_backrefs / clear_moc_backrefs
    -> zettel.llm, zettel.vault, zettel.hashing, zettel.state, zettel.index, zettel.usage,
       zettel.progress (same shared infrastructure as gardener.py)

zettel.gardener_assign
    -> zettel.taxonomy (allowed_topic_names, load_moc_taxonomy)
    -> zettel.index.VectorIndex (embed_texts, for embed_category_labels)
    -> zettel.config (DEFAULT_RELATION_WEIGHTS, GardenerConfig)
    -> zettel.state.StateDB (type-checking import only; graph_cohesion/find_moc_by_note_
       overlap take a db instance as a parameter, no direct import coupling)

zettel.moc_backrefs
    -> zettel.gardener_assign.extract_note_ids_from_moc_body
    -> zettel.vault (note_filename, parse_frontmatter, read_managed_block,
       safe_update_managed_blocks)
    -> zettel.state.StateDB (type-checking import only)

zettel.sync (run_sync_manual, not part of this component but a consumer)
    -> zettel.moc_backrefs.sync_moc_backrefs (manual MOC sync also maintains backrefs)


External Dependencies:

- numpy                — embedding array manipulation, cosine similarity (gardener_assign.py)
- umap-learn (optional) — dimensionality reduction before HDBSCAN; ImportError-guarded
- hdbscan (optional)    — density-based clustering; ImportError-guarded, falls back to KMeans
- scikit-learn          — TfidfVectorizer for cluster term extraction (gardener.py); KMeans
                          fallback clustering (gardener_assign.py); also ImportError-guarded
- python-ulid (ulid)    — ULID generation for new moc_id values
- pyyaml (via taxonomy.py) — parsing config/moc_topics.yaml
- pydantic              — MocTaxonomy/Categoria/Pilar models (taxonomy.py); GardenerConfig/
                          HubMocsConfig (config.py); MOCGenerationOutput family (schemas.py)
- ChromaDB (via VectorIndex) — "permanent_notes" collection read (embeddings source),
                          "mocs" collection write (MOC embeddings)
- SQLite (via StateDB)  — "mocs" table (CRUD, signature/topic/hub lookups), "note_connections"
                          table (graph_cohesion, get_weighted_note_degrees), "notes" table
                          (get_note for titles/paths/bodies)
- LLM provider (via zettel.llm/call_llm) — one call per cluster for MOC generation or
                          incremental placement; provider-agnostic (OpenAI/Anthropic/Gemini/
                          Ollama/OpenAI-compatible per project-wide llm.provider config)
```


## 6. Afferent and Efferent Coupling

Analysis unit: Python module (this project's clustering paradigm is functional/procedural rather than class-based — the component exposes no public classes, only module-level functions and a couple of internal `@dataclass` stat holders).

| Component (module) | Afferent Coupling (Ca) | Efferent Coupling (Ce) | Critical |
|---------------------|------------------------|--------------------------|----------|
| `gardener.py` | 4 (cli.py, web_app.py, gardener_hub.py, moc_backrefs.py-adjacent tests) | 9 (gardener_assign, taxonomy, llm, schemas, vault, moc_backrefs, hashing, state, index) | High |
| `gardener_hub.py` | 2 (cli.py, web_app.py) | 10 (graph, gardener [heavy private-symbol reuse], taxonomy, schemas, moc_backrefs, llm, vault, hashing, state, index) | High |
| `gardener_assign.py` | 2 (gardener.py, moc_backrefs.py) | 3 (taxonomy, index, config) | Medium |
| `moc_backrefs.py` | 3 (gardener.py, gardener_hub.py, sync.py) | 2 (gardener_assign, vault) | High |
| `taxonomy.py` | 3 (gardener.py, gardener_hub.py, cli.py `doctor`) | 1 (pyyaml/pydantic only, external) | Medium |
| `_GardenStats` (dataclass, gardener.py) | 1 (run_garden/_process_cluster internal use) | 0 | Low |
| `_HubGardenStats` (dataclass, gardener_hub.py) | 1 (run_garden_hubs/_process_hub_cluster internal use) | 0 | Low |

Notes on criticality:
- `gardener.py` and `gardener_hub.py` are both High: they sit at the center of the pipeline's fan-out (many collaborators) and are also directly reachable from both entry points (CLI and web), so a defect here affects both interfaces.
- `moc_backrefs.py` is High despite low efferent count precisely *because* of its afferent count — it is a single choke point shared by three otherwise-independent write paths (taxonomy pipeline, hub pipeline, manual sync); a bug here corrupts backref state regardless of which pipeline produced the MOC.
- The `gardener.py` <-> `gardener_hub.py` relationship is unusually tight for separate modules: `gardener_hub.py` imports roughly a dozen underscore-prefixed ("private") symbols directly from `gardener.py` (`_allowed_note_ids`, `_build_note_alias_map`, `_moc_embeddable`, `_apply_incremental_placements`, `_build_notes_list`, `_parse_incremental_output`, `_parse_moc_structure`, `_snapshot_moc_file`, `_format_note_links`, `_resolve_note_ref`, `_moc_vault_path`) rather than through a shared public module — this is flagged again under Technical Debt.


## 7. Endpoints

Not applicable. The gardener component exposes no network endpoints (REST/GraphQL/gRPC). It is invoked as:
- a CLI command (`zettel garden [--hubs] [--recreate] [--min-cluster-size N] [--yes]`), and
- an internal job operation (`WebApplication._dispatch(operation="garden")` / `"run_all"`) enqueued by the web UI's pipeline page, which itself is fronted by FastAPI routes owned by `web.py` — those routes belong to the Web UI component, not to gardener itself.


## 8. Integration Points

| Integration | Type | Purpose | Protocol | Data Format | Error Handling |
|-------------|------|---------|----------|-------------|----------------|
| ChromaDB `permanent_notes` collection | Internal datastore | Source of note embeddings for clustering (`get_all_permanent_embeddings`) | In-process Chroma client API | float vector lists + string IDs | Empty/short result treated as "insufficient embeddings" guard, not an error |
| ChromaDB `mocs` collection | Internal datastore | Persist/refresh MOC topic+summary embeddings for retrieval | In-process Chroma client API | `{topic, note_count[, hub_note_id]}` metadata + embedded summary text | Batch `delete_mocs` on purge; upsert is fire-and-forget (no explicit retry) |
| SQLite `mocs` table (StateDB) | Internal datastore | CRUD for MOC metadata, signature/topic/hub lookups, origin-scoped listing/deletion | Direct SQLite via StateDB wrapper | Row dict with `moc_id, topic, path, cluster_signature, body, frontmatter_json, origin` | Lookups return `None`/`[]` on miss; no explicit transaction rollback logic visible in this component (delegated to StateDB) |
| SQLite `note_connections` table | Internal datastore | Graph cohesion scoring (gardener) and weighted-degree hub ranking (gardener_hub) | Direct SQLite via StateDB wrapper | Edge rows with `source_note_id, target_note_id, relation_type` | Missing/empty edges yield cohesion `0.0` or empty degree map, not an exception |
| Vault filesystem (`40_MOCs/`) | Internal filesystem | Persist MOC `.md` files (frontmatter + body) | Direct file I/O via `zettel.vault` | Markdown with YAML frontmatter, `[[wikilinks]]` | `safe_write_note` creates parent dirs; missing file on read (`_parse_moc_structure`) logged and returns `None`, aborting that update gracefully |
| Vault filesystem (`30_Permanent/*.md`) | Internal filesystem | Read/patch `auto-moc-backrefs` managed blocks on permanent notes | Direct file I/O via `zettel.vault` managed blocks | Fenced managed-block markers inside Markdown | Missing note file (`_note_path_from_db`) silently skipped (path check `is_file()`) |
| `config/moc_topics.yaml` | Configuration file | Taxonomy source for category names + prompt detail | YAML load via `zettel.taxonomy` | `taxonomia_conhecimento: [{pilar, categorias: [{nome, topicos}]}]` | `TaxonomyLoadError` raised on missing/invalid file; fatal under `strict_topics`, warned-and-empty otherwise |
| LLM provider (via `zettel.llm.call_llm`) | External service | Generate new MOC content or classify incremental placements | Provider SDK (OpenAI/Anthropic/Gemini/Ollama/OpenAI-compatible), one call per cluster | `SystemMessage`+`HumanMessage`; JSON response parsed via `extract_json` into Pydantic schemas | Any exception during the call or JSON parse is caught, logged (`logger.error`), and the cluster is skipped (returns `None`) — never aborts the whole `garden` run |
| Embedding provider (via `VectorIndex.embed_texts`) | External service | Embed category labels for taxonomy-first assignment | Provider SDK, batched call | list of float vectors | Exception propagates up to `run_garden`'s `try/except`, which falls back to global clustering |


## 9. Design Patterns & Architecture

| Pattern | Implementation | Location | Purpose |
|---------|----------------|----------|---------|
| Strategy (with graceful degradation) | `_cluster_embeddings` tries UMAP+HDBSCAN, falls back to `_cluster_kmeans` on ImportError or runtime failure | gardener_assign.py:198-272 | Keep the pipeline functional without optional heavy ML dependencies installed |
| Chain of Responsibility | `_process_cluster`'s ordered checks (signature -> overlap -> topic -> cohesion -> create) | gardener.py:232-290 | Deterministic, cheapest-first routing that guarantees at most one LLM call |
| Idempotent write / content-addressed cache key | `cluster_signature = sha256(sorted(note_ids))` gates both new-MOC creation and hub-MOC creation | gardener.py:242-249, gardener_hub.py:294-308 | Safe re-runs of `garden` on unchanged data (no duplicate LLM spend, no duplicate MOCs) |
| Managed block / safe partial file update | `safe_update_managed_blocks` confines automated edits to fenced regions (`auto-moc-backrefs`) | moc_backrefs.py + vault.py | Preserve user hand-edits in permanent notes while keeping backlinks in sync |
| Snapshot / persisted rebuildability | `_snapshot_moc_file` stores rendered body+frontmatter JSON into SQLite on every write | gardener.py:734-743 | Enables `zettel rebuild` to regenerate the vault file from DB state without re-invoking the LLM |
| Defensive output validation / reconciliation | `_resolve_note_ref` + ghost/missing note handling in `_build_moc_body` / `_apply_incremental_placements` | gardener.py:637-724, 770-886 | Neutralizes LLM hallucination/omission before it reaches persisted vault content |
| Template Method (implicit) | Both `_create_new_moc`/`_create_new_hub_moc` and `_update_existing_moc`/`_update_hub_moc` follow the same build-prompt -> call_llm -> parse -> reconcile -> write -> backref -> persist skeleton | gardener.py, gardener_hub.py | Consistency between the two MOC "flavors" despite separate implementations |
| Facade over ML stack | `cluster_notes_within_buckets`/`cluster_notes_global` hide UMAP/HDBSCAN/KMeans selection details from `run_garden` | gardener_assign.py | Keeps orchestration code (`run_garden`) free of ML library specifics |
| Weighted graph BFS (reused, not owned) | `expand_notes` (graph.py) reused by `build_hub_neighborhood` with hub-specific parameters | gardener_hub.py:89-119 | Avoids re-implementing graph traversal logic already built for retrieval's GraphRAG expansion |


## 10. Technical Debt & Risks

| Risk Level | Component Area | Issue | Impact |
|------------|-----------------|-------|--------|
| Medium | `gardener_hub.py` <-> `gardener.py` coupling | `gardener_hub.py` imports ~11 underscore-prefixed ("private") functions directly from `gardener.py` instead of a shared public module | Any refactor of gardener.py's internal helpers (rename, signature change, removal) silently breaks gardener_hub.py unless the maintainer explicitly checks for these cross-imports; the underscore convention no longer signals "safe to change freely" |
| Medium | `_cluster_embeddings` (gardener_assign.py) | Silent, broad `except Exception` around UMAP fitting falls back to KMeans without surfacing *which* error occurred to the caller (only a log line) | A systematic misconfiguration (e.g. bad embedding dimensionality) could go unnoticed for a long time, quietly degrading cluster quality to KMeans instead of failing loudly |
| Medium | `assign_notes_to_categories` (gardener_assign.py) | Every note is force-assigned to its single best-matching category with no minimum-similarity floor — a genuinely off-taxonomy note is always bucketed somewhere | Notes with no real taxonomic home can pollute an unrelated category's cluster, potentially triggering a spurious cohesion rejection or a misleading MOC subsection |
| Low-Medium | `_validate_moc_topic` / `_topic_matches_allowed` | Bidirectional substring matching for topic validation (`a in b or b in a`) can produce false positives between semantically distinct but lexically overlapping category names | A cluster could validate against the wrong taxonomy category purely on string overlap, with no test currently exercising an adversarial near-miss pair |
| Low | `run_garden` / `run_garden_hubs` | A per-cluster LLM/JSON-parse failure is caught and logged, but the run's final summary log only distinguishes `created`/`incremental`/`skipped_signature`/`rejected_cohesion` — a cluster lost to an LLM exception isn't separately counted, making failure-rate monitoring harder from logs alone | Operators cannot easily tell "no MOCs today" apart from "N clusters silently failed today" without grepping error-level logs |
| Low | `_fuzzy_match_note_id` (gardener.py) | Hand-rolled edit-distance-1 check (`_within_edit_distance_one`) rather than a vetted library function | Low risk given the narrow use case and existing unit test coverage, but any future generalization (e.g. distance-2) would need careful re-verification |
| Low | Configuration schema drift | `GardenerConfig.min_notes_for_moc` Pydantic default (`3`) differs from the operational `config.yaml` value (`10`); the module docstring calls this out explicitly ("Nao confundir None com 'usar o default'") | A test or a fresh install relying on the Pydantic default alone would behave differently from the documented production configuration; already partially mitigated by comments, but remains a latent source of confusion |
| Low | `graph_cohesion_min_ratio` default (`0.0`) | The cohesion gate is effectively observational-only out of the box (never rejects), despite `graph_cohesion_enabled: true` | Operators may believe the quality gate is active in production when it is currently a no-op unless explicitly tuned above 0 |


## 11. Test Coverage Analysis

| Component | Unit Tests | Integration Tests | Coverage (qualitative) | Test Quality |
|-----------|------------|--------------------|--------------------------|---------------|
| `gardener.py` (taxonomy validation, MOC structure parsing, incremental update/placement, routing, note-ref resolution/reconciliation, purge) | `tests/test_gardener.py` — 827 lines, ~35 test functions | Cross-module integration exercised implicitly (real `StateDB`, real `safe_write_note`, mocked LLM/`VectorIndex`) | Strong for topic validation, structure parsing, incremental placement, ghost/missing reconciliation, and routing (`_process_cluster` overlap + topic-match paths); `_create_new_moc`'s full "new MOC" LLM path is exercised indirectly through `_process_cluster` routing tests but has no test with `topic_justification`/rejection combined with an actual `_create_new_moc` invocation beyond `_validate_moc_topic` unit tests | Good, specific assertions (checks both presence and absence of note IDs in rendered content); mocks isolate LLM calls (`MagicMock` on `llm.invoke`) while using real file I/O and real `StateDB`, giving reasonably high-fidelity coverage of the write path |
| `gardener_assign.py` (category assignment, note-overlap lookup, graph cohesion) | `tests/test_gardener_assign.py` — 68 lines, 4 test functions | None beyond direct `StateDB` usage | Covers the core pure functions (`assign_notes_to_categories`, `extract_note_ids_from_moc_body`, `find_moc_by_note_overlap`, `graph_cohesion`) at a basic/smoke level | Adequate but thin: `graph_cohesion`'s isolated-pair assertion (`isolated = graph_cohesion(...)` then unused) suggests a partially-written test; no coverage at all for `cluster_notes_within_buckets`, `cluster_notes_global`, `_cluster_embeddings`, `_cluster_kmeans`, or the UMAP/HDBSCAN-vs-KMeans fallback branch — the most algorithmically complex code in the component is untested |
| `gardener_hub.py` (hub ranking, neighborhood BFS, dedup, purge, routing) | `tests/test_gardener_hub.py` — 210 lines, ~10 test functions | Uses real `StateDB` graph fixtures (`_setup_graph_db`) | Good coverage of `rank_note_hubs` (both selection modes), `build_hub_neighborhood`, `dedup_hub_neighborhoods` (both dedup and distinct-neighborhood cases), `find_moc_by_hub_note_id`, `purge_hub_pipeline_mocs`, and the incremental routing branch of `_process_hub_cluster` | Good; mirrors the taxonomy pipeline's test style (mocked LLM, real DB/files) | No test exercises `_create_new_hub_moc` (brand-new hub MOC creation) directly — only the incremental-update branch of `_process_hub_cluster` is tested, leaving the "new hub MOC" LLM-call path, `_format_hub_note_section`, `_format_neighbors_list`, and `_format_graph_context` untested |
| `moc_backrefs.py` (sync, clear, link formatting) | `tests/test_moc_backrefs.py` — 174 lines, 5 test functions | Includes a real cross-component integration test (`test_sync_manual_updates_moc_backrefs`, via `zettel.sync.run_sync_manual`) | Solid: covers add-link, remove-stale-link (diff-based sync), purge-triggered `clear_moc_backrefs`, manual-MOC sync integration, and path-stem-based link formatting | Good; the manual-sync integration test is a genuine cross-module regression guard rather than a pure unit test |
| `taxonomy.py` (used by gardener for validation/prompt-building) | Covered within `tests/test_gardener.py` (`test_load_moc_taxonomy`, `test_allowed_topic_names_are_categories`, `test_format_taxonomy_for_prompt`, `test_resolve_allowed_topics_*`, plus a project-file smoke test against the real `config/moc_topics.yaml`) | The smoke test (`test_load_project_moc_topics_yaml`) doubles as an integration check against the actual operational taxonomy file | Strong — covers happy path, override behavior, strict vs. permissive missing-file handling | Good; the real-config smoke test is a nice guard against the shipped taxonomy YAML silently breaking |

Overall risk note: the component's LLM-facing orchestration (`_create_new_moc`, `_create_new_hub_moc`) and the ML clustering internals (`_cluster_embeddings`'s UMAP/HDBSCAN path, `_cluster_kmeans`) are the two areas with the thinnest direct test coverage relative to their complexity — both are exercised only indirectly (through routing tests that stop short of the "new MOC" branch, or not exercised as a unit at all for clustering). This is a coverage gap worth flagging rather than a defect: the surrounding logic (validation, reconciliation, routing, backrefs) that *consumes* their output is thoroughly tested, which limits blast radius, but a regression introduced purely inside `_cluster_embeddings`'s UMAP/HDBSCAN branch or inside `_create_new_moc`'s prompt-building/write sequence would not be caught by the current suite.
