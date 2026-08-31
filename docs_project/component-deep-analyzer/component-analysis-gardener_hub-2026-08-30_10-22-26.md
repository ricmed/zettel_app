# Component Deep Analysis Report — `gardener_hub`

**Component**: `zettel/gardener_hub.py`
**Analysis date**: 2026-08-30
**Analyzer**: Component Deep Analysis (read-only)

---

## 1. Executive Summary

`gardener_hub.py` implements **Phase 4b** of the Zettelkasten pipeline: the *hub-anchored* MOC (Map of Content) generator invoked via `zettel garden --hubs`. It is a complementary, opt-in alternative to the taxonomy-driven clustering pipeline in `gardener.py` (Phase 4).

Instead of clustering notes by embedding similarity within taxonomy categories (UMAP+HDBSCAN), this component mines the **existing typed graph** (`note_connections`, built by the `connect` phase and by manual `[[wikilink]]` sync) to find naturally important notes — "hubs" — and builds a radial MOC around each one: the hub note as the "front door" (`Porta de entrada`) plus its most strongly connected neighbors, discovered via weighted BFS graph expansion (reusing `zettel.graph.expand_notes`, the same primitive that powers `ask`/`article` retrieval-time graph expansion).

Key characteristics:
- **Pure/testable core**: `rank_note_hubs`, `build_hub_neighborhood`, `dedup_hub_neighborhoods`, and `get_neighbor_graph_context` are side-effect-free functions over a `StateDB`, independently unit-tested.
- **At most one LLM call per hub cluster** — either full generation (`moc_hub_generation.md`) for a new hub or incremental classification (`moc_hub_incremental.md`) when the hub already anchors a MOC — mirroring the design constraint of the taxonomy pipeline (`_process_cluster`).
- **Heavy code reuse**: rather than duplicating MOC-writing/parsing/backref logic, it imports private helpers directly from `zettel.gardener` (`_allowed_note_ids`, `_build_note_alias_map`, `_moc_embeddable`, `_apply_incremental_placements`, `_build_notes_list`, `_parse_incremental_output`, `_parse_moc_structure`, `_snapshot_moc_file`, `_format_note_links`, `_resolve_note_ref`, `_moc_vault_path`). This is a strong internal coupling documented further in §6.
- **Isolated lifecycle**: hub MOCs are tagged `origin="hub_pipeline"` in both SQLite (`mocs.origin`) and vault frontmatter, so `garden --hubs --recreate` purges only hub MOCs (`purge_hub_pipeline_mocs`) and never touches `origin="pipeline"` (taxonomy) or `origin="manual"` MOCs — verified explicitly by `test_purge_hub_pipeline_mocs_keeps_taxonomy`.
- **No independent persistence layer**: it writes vault files via `zettel.vault.safe_write_note`, indexes via the shared `VectorIndex.upsert_moc`/`delete_mocs`, and persists rows via the shared `StateDB.upsert_moc`. It owns no schema of its own beyond the `hub_note_id` / `origin="hub_pipeline"` convention embedded in the generic `mocs` table's `frontmatter_json`.

The component's role in the system is navigational rather than generative: it does not create new knowledge (permanent notes), it only creates **discovery structure** (MOC files) on top of notes and edges other phases already produced. It requires `note_connections` to be non-empty (populated by `connect` and/or `sync-manual --rebuild-graph`) — on an empty graph it degrades gracefully to a no-op (`rank_note_hubs` returns `[]`).

---

## 2. Data Flow Analysis

```
1.  CLI: `zettel garden --hubs [--recreate] [--yes]` (zettel/cli.py:691-736)
2.  cli.garden() loads AppConfig, opens StateDB + VectorIndex, dispatches to
    gardener_hub.run_garden_hubs(cfg, db, idx, recreate=...)
3.  [optional] recreate=True -> purge_hub_pipeline_mocs():
      StateDB.delete_hub_pipeline_mocs() -> moc_backrefs.clear_moc_backrefs() per MOC
      -> VectorIndex.delete_mocs() -> unlink() vault .md files
4.  run_garden_hubs():
      a. db.start_run("garden_hubs") + usage.begin_run(run_id)   [cost tracking]
      b. rank_note_hubs(db, cfg.hub_mocs)
           -> StateDB.get_weighted_note_degrees(DEFAULT_RELATION_WEIGHTS)
           -> StateDB.list_permanent_note_ids()  (filter to 30_Permanent/ only)
           -> threshold by percentile or absolute mode
           -> StateDB.list_hub_anchor_note_ids() (force-include existing hub anchors)
           -> sort desc, truncate to top_n_hubs
      c. For each ranked (hub_id, degree):
           build_hub_neighborhood(db, hub_id, cfg)
             -> graph.expand_notes() BFS over note_connections (weighted, decayed)
             -> filter by min_neighbor_weight, truncate to max_neighbors-1
           -> if len(neighborhood) < min_neighbors: skip (stats.skipped_small)
      d. dedup_hub_neighborhoods(hubs_with_notes, dedup_subset_threshold)
           -> sort by degree desc; drop a smaller hub whose neighborhood overlaps
              an already-accepted (larger) hub's neighborhood >= threshold
      e. get_llm(cfg)  [instantiate LangChain client once for the whole run]
      f. For each (hub_id, note_ids) surviving dedup:
           _process_hub_cluster(cfg, db, idx, llm, hub_id, note_ids, degree, stats)
             -> compute cluster_signature = sha256(hub_id + sorted note_ids)
             -> StateDB.find_moc_by_hub_note_id(hub_id):
                  FOUND  -> _update_hub_moc()      [incremental path]
                  MISSING-> StateDB.get_moc_by_signature(cluster_signature):
                              FOUND  -> reuse existing moc_id, no LLM call
                              MISSING-> _create_new_hub_moc()  [full generation path]
5a. _create_new_hub_moc():
      - build prompt context (hub excerpt, neighbor list w/ hop+relation+weight,
        taxonomy detail via zettel.taxonomy.resolve_allowed_topics)
      - fill_template(moc_hub_generation.md) -> call_llm() -> extract_json() ->
        MOCHubGenerationOutput (Pydantic)
      - assign new ULID moc_id, build frontmatter (origin=hub_pipeline, hub_note_id,
        hub_weighted_degree, cluster_signature)
      - _build_hub_moc_body(): "Porta de entrada" section (hub) + LLM subsections
        (resolved via _resolve_note_ref, alias or fuzzy-match) + fallback
        "Outras notas do cluster" section for any allowed note the LLM omitted
      - safe_write_note() to 40_MOCs/HUB - <ulid> - <slug>.md
      - moc_backrefs.sync_moc_backrefs() -> writes auto-moc-backrefs block on
        every linked permanent note
      - StateDB.upsert_moc() + VectorIndex.upsert_moc() (embeds topic+summary)
5b. _update_hub_moc() [incremental]:
      - _parse_moc_structure() re-reads the existing MOC .md, extracts subsections
        + all_note_ids via a [[ZTL - <id> - ...]] regex
      - truly_new = note_ids not already present in the file
      - if truly_new is empty: just refresh cluster_signature + snapshot (no LLM call)
      - else: fill_template(moc_hub_incremental.md) -> call_llm() ->
        MOCIncrementalOutput -> _apply_incremental_placements() rewrites the file,
        placing each new note into an existing/new subsection or "ignorar"
      - StateDB.upsert_moc() + VectorIndex.upsert_moc() with updated note_count
6.  db.finish_run(run_id) via usage.finish_pipeline_run() -> cost totals persisted
7.  CLI prints created/updated MOC IDs
```

---

## 3. Business Rules & Logic

### Overview of the business rules

| Rule Type | Rule Description | Location |
|-----------|-------------------|----------|
| Selection | Hub notes are ranked by undirected weighted graph degree, restricted to `30_Permanent/` notes when any exist | `gardener_hub.py:44-86`, `state.py:1275-1300` |
| Selection | Two selection modes: `percentile` (top `1 - hub_percentile` of ranked notes) or `absolute` (degree >= `min_weighted_degree`) | `gardener_hub.py:61-70` |
| Selection | Existing hub anchors are always re-considered even if they fall below the current threshold | `gardener_hub.py:72-83` |
| Selection | At most `top_n_hubs` candidates survive per run | `gardener_hub.py:86` |
| Expansion | Neighborhood = hub + up to `max_neighbors - 1` BFS neighbors within `max_hops`, decayed by `decay` per hop, filtered by `min_neighbor_weight` | `gardener_hub.py:89-119` |
| Filtering | A hub whose neighborhood (incl. itself) has fewer than `min_neighbors` notes is dropped entirely | `gardener_hub.py:212-214` |
| Deduplication | A smaller-degree hub is dropped if its neighborhood overlaps an already-accepted larger hub's neighborhood by >= `dedup_subset_threshold` | `gardener_hub.py:122-147` |
| Idempotency | A cluster's identity is a signature `sha256("hub:<hub_id>|" + sorted(note_ids))`; unchanged clusters reuse the existing MOC and skip the LLM | `gardener_hub.py:294-308` |
| Routing | Existing hub MOC for `hub_id` -> always incremental path (1 LLM call, only if new notes exist) | `gardener_hub.py:297-302` |
| Routing | No existing hub MOC, no signature match -> full generation path (1 LLM call) | `gardener_hub.py:304-315` |
| Content constraint | Hub note MUST appear in its own "Porta de entrada" section, distinct from ordinary subsections | `gardener_hub.py:587-599`, `prompts/moc_hub_generation.md:18` |
| Reconciliation | Any allowed note not placed into a subsection by the LLM is appended to a fallback `"Outras notas do cluster"` section, guaranteeing no note is silently dropped | `gardener_hub.py:615-623` |
| Isolation | Hub MOCs are tagged `origin="hub_pipeline"`; recreate/purge operates only on this origin, never on `origin="pipeline"` (taxonomy) or `"manual"` | `gardener_hub.py:258-278`, `state.py:1267-1273` |
| Backref sync | Every MOC write/update triggers `sync_moc_backrefs`, keeping `auto-moc-backrefs` blocks on linked permanent notes consistent; purge triggers `clear_moc_backrefs` first | `gardener_hub.py:396-398, 267-268` |
| Cost accounting | Each `garden --hubs` invocation opens/closes a `runs` row and aggregates LLM+embedding cost via the global `CostTracker` | `gardener_hub.py:187-190, 254` |
| Failure isolation | An LLM call failure (exception, malformed JSON) for one hub cluster is caught and logged; that hub's MOC is skipped (returns `None`), the run continues to the next hub | `gardener_hub.py:359-370, 479-490` |

### Detailed breakdown of the business rules

---

### Business Rule: Weighted-Degree Hub Ranking

**Overview**:
A "hub" candidate is any permanent note whose undirected weighted degree in `note_connections` places it among the most connected notes in the vault, computed via `rank_note_hubs`.

**Detailed description**:
The ranking starts from `StateDB.get_weighted_note_degrees(DEFAULT_RELATION_WEIGHTS)`, which sums, for every note appearing as either `source_note_id` or `target_note_id` in `note_connections`, the weight of its relation type (`contradicts=1.0`, `extends=0.9`, `depends_on=0.9`, `supports=0.8`, `exemplifies=0.7`, `related=0.5`, default `0.5` for unknown types). This means a note tied into many `contradicts`/`extends` edges ranks far higher than one with the same edge *count* but weaker `related` edges — the ranking is not a naive degree count, it reflects the semantic strength of connections. If `list_permanent_note_ids()` returns any IDs (i.e. at least one note lives under `30_Permanent/`), the degree map is filtered down to only those IDs before ranking — this prevents a heavily-referenced source/literature note (SRC/LIT, not a permanent Zettelkasten note) from being selected as a "hub," since hubs are meant to be navigational anchors within the permanent-note layer specifically. If no permanent notes exist at all in `notes`, the filter is skipped entirely (defensive fallback, likely relevant for tests or freshly bootstrapped vaults where the path convention hasn't been established).

Two selection strategies determine the cut line: `percentile` mode (the default, `hub_percentile=0.90` in `config/config.yaml`) computes an index into the sorted-descending degree list at position `(1 - percentile) * (n-1)`, and every note at or above that index's degree value passes — this scales naturally as the vault grows, always surfacing roughly the top decile regardless of absolute vault size. `absolute` mode instead applies a fixed floor (`min_weighted_degree`, default `8.0`), useful when the caller wants a stable, size-independent cutoff (e.g. small or synthetic vaults, as exercised by `test_rank_note_hubs_absolute`). A hidden third input is *incumbency*: `list_hub_anchor_note_ids()` scans existing `hub_pipeline` MOCs' frontmatter and force-includes their `hub_note_id` in the candidate pool even if it no longer clears the current threshold (as long as it still exists in the degree map at all) — this prevents a hub MOC from being silently orphaned by natural degree fluctuation between garden runs, favoring continuity/incremental updates over strict re-evaluation each time. The final candidate list — regular ranked candidates plus forced-in incumbents, deduplicated by ID — is re-sorted by degree and truncated to `top_n_hubs` (config default 15), bounding the LLM cost of any single run.

The consequence of empirically miscalibrating `hub_percentile`/`min_weighted_degree` per corpus is the same class of risk flagged for the unrelated `RelevanceFloorConfig` cousin in `config.py`: too high a bar yields zero hubs (silent no-op, `run_garden_hubs` returns `[]` immediately, logged at `info` level only), too low a bar produces many overlapping, low-value MOCs that then rely on the deduplication rule (below) to be pruned back down.

**Rule workflow**:
```
degrees = weighted undirected degree per note (note_connections, DEFAULT_RELATION_WEIGHTS)
if any note under 30_Permanent/: degrees = degrees restricted to those note_ids
ranked = degrees sorted descending
if selection_mode == absolute: candidates = ranked where degree >= min_weighted_degree
else (percentile): threshold = degree at rank floor((1-hub_percentile)*(n-1))
                    candidates = ranked where degree >= threshold
result = dedup(candidates + existing hub anchors still present in degrees)
return top_n_hubs of result, sorted by degree desc
```

---

### Business Rule: Radial Neighborhood Expansion (BFS)

**Overview**:
For each hub candidate, `build_hub_neighborhood` determines which other notes belong "in its orbit" by running a weighted breadth-first search over the typed graph, reusing the shared `expand_notes` primitive from `zettel/graph.py` (the same code that powers `ask`/`article` retrieval-time graph expansion).

**Detailed description**:
`expand_notes` is seeded with the single hub note at weight `1.0` and walks up to `cfg.max_hops` (default `2`) hops through `note_connections`, treating every edge as undirected. At each hop, a neighbor's accumulated weight is `seed_weight * relation_weight * decay^(hop-1)`, so second-hop neighbors are inherently discounted relative to direct connections (`decay=0.5` halves the contribution per additional hop), and the *strongest* path to any given neighbor wins if multiple paths reach it. `gardener_hub.py` deliberately requests `max_neighbor_slots * 2` from `expand_notes` (double the eventual budget) so that after the caller's own `min_neighbor_weight` filter, there's still enough headroom to fill the final list — evidence that the two truncation layers are supposed to compose (expand_notes' generic cap and the domain-specific weight floor).

After expansion, `build_hub_neighborhood` re-filters the returned neighbors to only those meeting `min_neighbor_weight` (default `0.3`), then sorts by weight descending and keeps the top `max_neighbors - 1` (reserving one slot conceptually for the hub itself, which is always prepended as element `[0]` of the returned list). This two-stage filtering (BFS decay + explicit floor) means a neighbor reached only via a long chain of weak `related` edges at `max_hops` depth is unlikely to survive even if `expand_notes` technically returned it — e.g., with `decay=0.5` and a `related` weight of `0.5`, a 2-hop path already sits at `0.5*0.5*0.5=0.125`, below the `0.3` floor, so in practice `max_hops=2` combined with `min_neighbor_weight=0.3` mostly captures strong direct connections plus a handful of strongly-weighted (`contradicts`/`extends`/`depends_on`) second-hop notes.

The resulting neighborhood list, if smaller than `cfg.min_neighbors` (default `8`, counting the hub itself), is discarded entirely by the caller (`run_garden_hubs`) — a note with high graph degree can still fail to produce a hub MOC if its immediate neighbors are too weakly connected to each other or too few to justify a dedicated MOC. This is the mechanism by which "high degree" and "worth a MOC" are decoupled: degree drives candidacy, neighborhood size after decay/filtering drives whether a MOC is actually built.

**Rule workflow**:
```
neighbors = expand_notes(seed=[hub_id], max_hops, decay, relation_weights,
                          max_neighbors=2*(max_neighbors-1), seed_weights={hub_id: 1.0})
kept = [n for n in neighbors if n.weight >= min_neighbor_weight]
kept = top (max_neighbors - 1) of kept, sorted by weight desc
neighborhood = [hub_id] + kept_ids
# caller: if len(neighborhood) < min_neighbors -> hub dropped (stats.skipped_small += 1)
```

---

### Business Rule: Overlapping-Neighborhood Deduplication

**Overview**:
Because multiple high-degree notes in a tightly-connected subgraph often produce near-identical neighborhoods, `dedup_hub_neighborhoods` removes redundant, smaller hub candidates whose neighborhood is mostly a subset of a larger, already-accepted hub's neighborhood.

**Detailed description**:
The function processes candidates in descending degree order, greedily accepting a hub's neighborhood set and comparing every subsequent (lower-degree) candidate's neighborhood against **all previously accepted** sets (not just the immediately preceding one). Overlap is computed asymmetrically as `|current ∩ prev| / |current|` — i.e. "what fraction of *this candidate's* neighborhood is already covered by an accepted hub" — rather than a symmetric Jaccard index. This asymmetry is intentional: a small, tightly-scoped hub whose 5 notes are all already inside a large 20-note accepted hub's neighborhood should be dropped (overlap = 5/5 = 1.0), even though the reverse fraction would be small (5/20 = 0.25) and would not itself trigger the threshold. An empty `current` set is treated as an automatic skip (`if not current: skip = True`), which in practice cannot occur since neighborhoods always include at least the hub itself, but guards a hypothetical zero-neighbor call path.

The `dedup_subset_threshold` (default `0.8`) is a business judgment call about how much redundancy is tolerable — at `0.8`, a hub whose neighborhood is 80%+ contained within a bigger, already-accepted hub's neighborhood is considered "the same navigational cluster" and is dropped, leaving the larger hub's MOC as the sole entry point for that region of the graph. This directly bounds how many MOCs the pipeline produces per `garden --hubs` run and prevents near-duplicate MOCs proliferating around a single densely connected cluster of notes (e.g., a cluster of 10 mutually `related` notes might otherwise produce up to 10 separate near-identical hub MOCs, one per note, without this rule).

Once dedup completes, its output count feeds directly into `_HubGardenStats.skipped_dedup`, which is surfaced in the run's final log line — this is the only place skipped-due-to-dedup counts are reported; there is no separate CLI-visible breakdown per skipped hub (only aggregate counts).

**Rule workflow**:
```
sorted_hubs = hubs_with_notes sorted by degree desc
accepted = [], accepted_sets = []
for (hub_id, degree, note_ids) in sorted_hubs:
    current = set(note_ids)
    if current is empty: skip
    for prev_set in accepted_sets:
        if |current ∩ prev_set| / |current| >= dedup_subset_threshold: skip; break
    if not skipped: accepted.append((hub_id, note_ids)); accepted_sets.append(current)
return accepted
```

---

### Business Rule: Signature-Based Idempotency and Incremental-Only Update Path

**Overview**:
Every hub cluster is identified by a deterministic content signature; the pipeline never regenerates a MOC's full body via a second full-generation LLM call once one exists for a given hub — all subsequent runs for that hub go through the cheaper, narrower incremental prompt.

**Detailed description**:
`_process_hub_cluster` first computes `cluster_signature = sha256_hex("hub:<hub_id>|" + "|".join(sorted(note_ids)))`. This signature captures the *exact* set of notes currently in the neighborhood (hub + BFS neighbors after filtering), independent of their order — sorting before hashing ensures neighborhood churn that doesn't change membership (e.g. re-ranking due to tie-breaking) doesn't spuriously invalidate the signature. The routing logic then checks two things in order: (1) does a MOC already exist with `hub_note_id == hub_id`, regardless of signature (via `find_moc_by_hub_note_id`, which scans `frontmatter_json` of all `hub_pipeline`-origin MOCs)? If yes, the pipeline unconditionally takes the **incremental path** (`_update_hub_moc`) — it never re-runs full generation for a hub that already has a MOC, even if the neighborhood has drastically changed; the incremental prompt only ever proposes placements for *new* notes not yet in the file, it cannot restructure existing subsections. (2) If no existing MOC is anchored on this hub, does the exact `cluster_signature` already exist under some other MOC record? If yes, the existing `moc_id` is returned directly with **no LLM call at all** — this branch guards against exact-duplicate reprocessing (e.g., a MOC was already created for this signature by an earlier interrupted run, or was orphaned from `hub_note_id` tracking through prior data manipulation) and increments `stats.skipped_signature`.

Within `_update_hub_moc`, a second-level idempotency check exists: `truly_new = [nid for nid in note_ids if nid not in existing_ids]` (existing IDs are re-parsed from the MOC file's own body via a regex over `[[ZTL - <id> - ...]]` wikilinks, not from any DB-side membership list). If `truly_new` is empty — meaning every note in the freshly computed neighborhood is already represented as a wikilink in the file — the function **skips the LLM call entirely**, just re-persisting the (unchanged) body/frontmatter under the new `cluster_signature` so that future runs' signature lookups stay in sync. This is the cheapest of the three paths and is the expected steady state once a hub's neighborhood has stabilized.

The combined effect of these three layers (hub-anchor lookup -> signature lookup -> truly-new-notes check) is that the pipeline makes **at most one LLM call per hub per run**, and frequently makes **zero** calls for hubs whose neighborhoods haven't meaningfully changed since the last run — a deliberate cost-control design mirrored from the sibling taxonomy pipeline (`gardener.py`'s `_process_cluster`, per the module's inline `overlap_threshold` docstring reference in CLAUDE.md).

**Rule workflow**:
```
cluster_signature = sha256("hub:<hub_id>|" + "|".join(sorted(note_ids)))
existing_moc = find_moc_by_hub_note_id(hub_id)
if existing_moc:
    return _update_hub_moc(...)          # incremental route, stats.incremental += 1
existing_sig_moc = get_moc_by_signature(cluster_signature)
if existing_sig_moc:
    return existing_sig_moc.moc_id       # no-op route, stats.skipped_signature += 1
return _create_new_hub_moc(...)          # full generation route, stats.created += 1

# inside _update_hub_moc:
existing_ids = parse [[ZTL - id - ...]] wikilinks from the MOC file body
truly_new = note_ids - existing_ids
if not truly_new: persist signature only (0 LLM calls)
else: call moc_hub_incremental.md prompt (1 LLM call), place truly_new notes
```

---

### Business Rule: Hub-Section Content Contract ("Porta de entrada")

**Overview**:
Every generated hub MOC body is structurally required to present the hub note in a distinguished "Porta de entrada" (entry point) section before any thematic subsection, and every note admitted into the cluster must end up linked exactly once somewhere in the file.

**Detailed description**:
`_build_hub_moc_body` hard-codes the `## Porta de entrada` heading in Python (not left to the LLM), followed by the LLM-authored `hub_role` explanation (why this note is the best starting point) and a `permanent_wikilink` to the hub note itself. This guarantees structural consistency across every hub MOC in the vault regardless of what the LLM returns — the LLM only supplies `hub_role` prose and the `subsections` list; it cannot omit or relocate the entry-point section, because the Python code, not the model, controls that section's existence and position. The prompt (`moc_hub_generation.md`) reinforces this at the instruction level ("A nota-hub DEVE aparecer em uma subsecao destacada") as a belt-and-braces measure, but the code doesn't actually trust the LLM to have honored it — the hub link is written unconditionally by `_build_hub_moc_body`.

For the remaining subsections, the LLM references notes only by short aliases (`N1`, `N2`, ...) built by `_build_note_alias_map`, never by raw note IDs — this keeps prompts compact and avoids the model needing to reproduce long ULIDs verbatim (a source of transcription errors). `_resolve_note_ref` (shared with the taxonomy pipeline) translates each LLM-supplied alias back to a real note ID, falling back to direct ID match, then to a single-edit-distance fuzzy match (`_fuzzy_match_note_id`) to tolerate minor LLM typos, and finally giving up (returning `None`, silently dropping that specific reference) if no unambiguous match exists.

Critically, the function tracks every note ID it has `placed` across all subsections in a `set`, and after processing every LLM-proposed subsection, computes `missing = allowed_ids - placed` — any note that was eligible for the cluster (`_allowed_note_ids`: exists in StateDB) but wasn't placed into any subsection by the LLM (whether due to LLM omission, a malformed reference, or a fuzzy-match miss) is appended to a synthetic `## Outras notas do cluster` fallback section. This is a **reconciliation guarantee**: no note that was part of the analyzed neighborhood is ever silently absent from the final MOC file, even if the LLM's structuring was incomplete — it may end up in a less meaningful location, but it will be linked. This mirrors the equivalent fallback subsection in the taxonomy pipeline and is a deliberate defense against LLM output unreliability, logged at `info` level with a count whenever it triggers.

**Rule workflow**:
```
body = "# {topic}\n\n{summary}\n\n"
body += "## Porta de entrada\n\n{hub_role}\n\n- {hub_wikilink}\n\n"   # always, code-controlled
placed = {hub_note_id}
for subsection in llm_output.subsections:
    body += "## {subsection.title}\n\n{subsection.description}\n\n"
    for ref in subsection.note_ids:
        nid = resolve_note_ref(ref, allowed_ids, alias_to_id)   # alias -> id -> fuzzy -> None
        if nid and nid not in placed: body += link(nid); placed.add(nid)
missing = allowed_ids - placed
if missing: body += "## Outras notas do cluster\n\n" + links(sorted(missing))
```

---

### Business Rule: Origin-Scoped Purge Isolation

**Overview**:
`garden --hubs --recreate` must delete only MOCs this pipeline created, leaving taxonomy-pipeline MOCs (`origin="pipeline"`) and hand-authored MOCs (`origin="manual"`) completely untouched.

**Detailed description**:
`purge_hub_pipeline_mocs` delegates the actual row selection to `StateDB.delete_hub_pipeline_mocs()`, which issues `DELETE FROM mocs WHERE origin='hub_pipeline'` — the `origin` column is the single source of truth for pipeline ownership, set at creation time (`_create_new_hub_moc` always writes `origin=_HUB_ORIGIN` = `"hub_pipeline"` into both the SQLite row and the vault frontmatter's `meta["origin"]`). Before deleting each MOC's SQLite row, the function calls `moc_backrefs.clear_moc_backrefs(db, moc)` for every returned row, which removes that MOC's link from every permanent note's `auto-moc-backrefs` managed block — this ordering (backrefs cleared before the MOC row/file are gone) matters because `clear_moc_backrefs` needs the MOC's `body`/`path` to enumerate which notes to touch, and it prefers `moc["body"]` (already in the DB row) before falling back to re-reading the vault file, so it works even if the vault file was already deleted out of band.

After backrefs are cleared, `VectorIndex.delete_mocs()` removes the corresponding embeddings from ChromaDB's `mocs` collection in a single batched call, and finally the vault `.md` files are unlinked from disk (guarded by `path.is_file()`, tolerating a file that's already missing). The function returns an integer count of removed MOCs, which the CLI does not display directly but which `run_garden_hubs` logs.

This origin-based partitioning is what allows `garden` (taxonomy) and `garden --hubs` to coexist as genuinely independent, recreatable pipelines over the same `mocs` table and the same `40_MOCs/` vault directory without either's `--recreate` clobbering the other's output — verified directly by `test_purge_hub_pipeline_mocs_keeps_taxonomy`, which asserts a `hub_pipeline` MOC is deleted while sibling `pipeline` and `manual` MOCs in the same directory survive untouched.

**Rule workflow**:
```
removed_rows = DELETE FROM mocs WHERE origin='hub_pipeline' RETURNING previous rows
for moc in removed_rows: clear_moc_backrefs(db, moc)      # strip auto-moc-backrefs blocks
idx.delete_mocs([moc_id for moc in removed_rows])          # ChromaDB mocs collection
for moc in removed_rows: unlink vault file if it exists
return len(removed_rows)
```

---

## 4. Component Structure

`gardener_hub.py` is a single flat module (626 lines) with no package structure of its own; it is organized into four internally-commented sections:

```
zettel/
├── gardener_hub.py                  # THIS COMPONENT — Phase 4b hub-anchored MOCs
│   ├── _HubGardenStats (dataclass)  # run counters: incremental/created/skipped_*
│   ├── ── Graph ranking (pure) ──
│   │   ├── rank_note_hubs()              # weighted-degree candidate selection
│   │   ├── build_hub_neighborhood()      # BFS neighborhood via graph.expand_notes
│   │   ├── dedup_hub_neighborhoods()     # subset-overlap dedup
│   │   └── get_neighbor_graph_context()  # per-neighbor hop/weight/relation for prompts
│   ├── ── Public API ──
│   │   ├── run_garden_hubs()             # orchestrator, entry point from cli.py
│   │   └── purge_hub_pipeline_mocs()     # origin-scoped deletion (recreate + tests)
│   ├── ── Hub cluster processing ──
│   │   ├── _process_hub_cluster()        # routes to incremental / signature-reuse / create
│   │   ├── _create_new_hub_moc()         # full LLM generation path
│   │   └── _update_hub_moc()             # incremental LLM classification path
│   └── ── Prompt / body helpers ──
│       ├── _parse_hub_moc_output()       # JSON -> MOCHubGenerationOutput
│       ├── _format_hub_note_section()    # hub excerpt for the prompt
│       ├── _format_neighbors_list()      # neighbor list w/ alias+hop+relation+weight
│       ├── _format_graph_context()       # raw hop/weight/relation dump for the prompt
│       └── _build_hub_moc_body()         # final Markdown body assembly + reconciliation
│
├── gardener.py                      # sibling Phase 4 (taxonomy pipeline); SOURCE of
│                                     #   ~11 private helpers imported by gardener_hub
│                                     #   (_allowed_note_ids, _build_note_alias_map,
│                                     #    _moc_embeddable, _apply_incremental_placements,
│                                     #    _build_notes_list, _parse_incremental_output,
│                                     #    _parse_moc_structure, _snapshot_moc_file,
│                                     #    _format_note_links, _resolve_note_ref,
│                                     #    _moc_vault_path)
├── graph.py                         # expand_notes() — shared weighted BFS primitive
├── config.py                        # HubMocsConfig, DEFAULT_RELATION_WEIGHTS
├── schemas.py                       # MOCHubGenerationOutput, MOCSubsection (shared),
│                                     #   MOCIncrementalOutput (shared)
├── moc_backrefs.py                  # sync_moc_backrefs(), clear_moc_backrefs()
├── vault.py                         # note_filename(), permanent_wikilink(), safe_write_note()
├── taxonomy.py                      # resolve_allowed_topics() — taxonomy detail for prompt
├── llm.py                           # get_llm(), call_llm(), fill_template(), load_prompt_parts(),
│                                     #   extract_json()
├── progress.py                      # report() — optional ProgressObserver notification
├── usage.py                         # begin_run(), finish_pipeline_run() — cost tracking
├── state.py                         # StateDB: get_weighted_note_degrees, list_permanent_note_ids,
│                                     #   list_hub_anchor_note_ids, find_moc_by_hub_note_id,
│                                     #   get_moc_by_signature, delete_hub_pipeline_mocs,
│                                     #   upsert_moc, get_connections_for_notes, ...
├── index.py                         # VectorIndex: upsert_moc(), delete_mocs()
└── cli.py                           # `zettel garden --hubs [--recreate] [--yes]` (lines 691-745)

prompts/
├── moc_hub_generation.md            # full-generation system+user prompt (PT-BR)
└── moc_hub_incremental.md           # incremental-classification system+user prompt (PT-BR)

tests/
└── test_gardener_hub.py             # unit tests for this component (see §11)
```

---

## 5. Dependency Analysis

```
Internal Dependencies:

cli.garden(--hubs)
  -> gardener_hub.run_garden_hubs()
       -> usage.begin_run() / finish_pipeline_run()
       -> gardener_hub.rank_note_hubs()
            -> state.StateDB.get_weighted_note_degrees()
            -> state.StateDB.list_permanent_note_ids()
            -> state.StateDB.list_hub_anchor_note_ids()
       -> gardener_hub.build_hub_neighborhood()  (per candidate)
            -> graph.expand_notes()
                 -> state.StateDB.get_connections_for_notes()
       -> gardener_hub.dedup_hub_neighborhoods()          [pure, no deps]
       -> llm.get_llm()
       -> gardener_hub._process_hub_cluster()  (per surviving cluster)
            -> hashing.sha256_hex()
            -> state.StateDB.find_moc_by_hub_note_id()
            -> state.StateDB.get_moc_by_signature()
            -> gardener_hub._create_new_hub_moc()
                 -> gardener._build_note_alias_map(), gardener._allowed_note_ids()
                 -> gardener_hub.get_neighbor_graph_context() -> graph.expand_notes()
                 -> llm.load_prompt_parts() / fill_template() / call_llm() / extract_json()
                 -> taxonomy.resolve_allowed_topics()
                 -> schemas.MOCHubGenerationOutput (validation)
                 -> gardener_hub._build_hub_moc_body()
                      -> gardener._format_note_links(), gardener._resolve_note_ref()
                      -> vault.permanent_wikilink()
                 -> vault.note_filename(), vault.safe_write_note()
                 -> moc_backrefs.sync_moc_backrefs()
                 -> state.StateDB.upsert_moc()
                 -> index.VectorIndex.upsert_moc() -> gardener._moc_embeddable()
            -> gardener_hub._update_hub_moc()
                 -> gardener._parse_moc_structure(), gardener._snapshot_moc_file()
                 -> gardener._build_notes_list(), gardener._apply_incremental_placements()
                 -> llm.* (same as above)
                 -> schemas.MOCIncrementalOutput (via gardener._parse_incremental_output())
                 -> state.StateDB.upsert_moc(), index.VectorIndex.upsert_moc()
  -> gardener_hub.purge_hub_pipeline_mocs()   [when --recreate]
       -> gardener._moc_vault_path()
       -> moc_backrefs.clear_moc_backrefs()
       -> state.StateDB.delete_hub_pipeline_mocs()
       -> index.VectorIndex.delete_mocs()

External Dependencies:
- ulid-py (`ulid.ULID`)         - MOC ID generation (ULID, sortable unique ID)
- LangChain-based LLM client (via zettel.llm.get_llm / call_llm) - configurable provider
    (OpenAI / Gemini / Ollama / OpenAI-compatible per llm.provider in config.yaml)
- SQLite (via StateDB)          - note_connections, notes, mocs table persistence
- ChromaDB (via VectorIndex)    - `mocs` collection embeddings for hub MOC topic+summary
- PyYAML (indirectly, via taxonomy.resolve_allowed_topics -> load_moc_taxonomy)
- Pydantic v2 (schemas.MOCHubGenerationOutput, MOCIncrementalOutput, MOCSubsection)
```

Note: `gardener_hub.py` imports 11 underscore-prefixed ("private") names from `zettel.gardener` at call time (inside function bodies, not at module top-level) — see §9/§10 for the architectural implications of this.

---

## 6. Afferent and Efferent Coupling

Analyzed at function granularity (this is a functional module, not class-based), counting call edges to/from this component's public and module-private functions as observed in the codebase (cli.py, gardener_hub.py, gardener.py, tests).

| Function | Afferent Coupling (callers) | Efferent Coupling (callees) | Critical |
|----------|------------------------------|-------------------------------|----------|
| `run_garden_hubs` | 1 (cli.garden) | 8 (usage x2, rank_note_hubs, build_hub_neighborhood, dedup_hub_neighborhoods, progress.report, get_llm, _process_hub_cluster) | High |
| `rank_note_hubs` | 2 (run_garden_hubs, test_gardener_hub.py) | 3 (StateDB.get_weighted_note_degrees/list_permanent_note_ids/list_hub_anchor_note_ids) | Medium |
| `build_hub_neighborhood` | 2 (run_garden_hubs, tests) | 1 (graph.expand_notes) | Medium |
| `dedup_hub_neighborhoods` | 2 (run_garden_hubs, tests) | 0 (pure function) | Low |
| `get_neighbor_graph_context` | 1 (`_create_new_hub_moc`) | 1 (graph.expand_notes) | Low |
| `_process_hub_cluster` | 2 (run_garden_hubs, tests) | 5 (hashing.sha256_hex, StateDB.find_moc_by_hub_note_id/get_moc_by_signature, `_update_hub_moc`, `_create_new_hub_moc`) | High |
| `_create_new_hub_moc` | 1 (`_process_hub_cluster`) | ~14 (3 gardener.* imports, get_neighbor_graph_context, load_prompt_parts/fill_template/call_llm/extract_json, resolve_allowed_topics, `_build_hub_moc_body`, note_filename, safe_write_note, sync_moc_backrefs, StateDB.upsert_moc, VectorIndex.upsert_moc) | High |
| `_update_hub_moc` | 1 (`_process_hub_cluster`) | ~13 (7 gardener.* imports, load_prompt_parts/fill_template/call_llm, `_apply_incremental_placements`, StateDB.upsert_moc, VectorIndex.upsert_moc) | High |
| `_build_hub_moc_body` | 1 (`_create_new_hub_moc`) | 4 (2 gardener.* imports, vault.permanent_wikilink, StateDB.get_note) | Medium |
| `purge_hub_pipeline_mocs` | 3 (run_garden_hubs, tests, cli-adjacent recreate flow) | 4 (gardener._moc_vault_path, moc_backrefs.clear_moc_backrefs, StateDB.delete_hub_pipeline_mocs, VectorIndex.delete_mocs) | Medium |
| `_parse_hub_moc_output` | 1 (`_create_new_hub_moc`) | 1 (llm.extract_json) | Low |
| `_format_hub_note_section` / `_format_neighbors_list` / `_format_graph_context` | 1 each (`_create_new_hub_moc`) | 0-1 (StateDB.get_note) | Low |

**Coupling observation**: `_create_new_hub_moc` and `_update_hub_moc` are the highest-efferent-coupling functions in the module, each fanning out to roughly a dozen collaborators spanning three other modules (`gardener`, `vault`, `llm`) plus SQLite and ChromaDB. This is a direct consequence of the module's design choice to reuse `gardener.py`'s private MOC-body/parsing helpers rather than reimplementing them — it minimizes duplication at the cost of tight, cross-module coupling to non-public symbols (see §9, §10).

---

## 7. Endpoints

Not applicable — `gardener_hub.py` exposes no REST/GraphQL/gRPC/HTTP surface. It is invoked exclusively through the Typer CLI subcommand `zettel garden --hubs` (`zettel/cli.py:691-745`) and is explicitly listed in `CLAUDE.md` as **not exposed** in the FastAPI web UI (`web_app.py`'s enqueued operations list only includes plain `garden` and `garden`+hubs is mentioned generically, but no dedicated hub-only web route exists in `web.py`/`web_app.py` beyond what the shared `garden` job dispatch provides).

---

## 8. Integration Points

| Integration | Type | Purpose | Protocol | Data Format | Error Handling |
|-------------|------|---------|----------|-------------|-----------------|
| LLM provider (OpenAI/Gemini/Ollama/compatible) | External Service | Hub MOC topic/summary/subsection generation (full) and new-note placement (incremental) | HTTPS (via LangChain client) | JSON (structured prompt output, `extract_json` + Pydantic validation) | `try/except Exception` around `call_llm`+parse; logs error, returns `None` (cluster skipped, run continues) |
| SQLite (`state.db`, via `StateDB`) | Internal/Embedded DB | Persist `mocs` rows (origin, signature, frontmatter), read `note_connections`/`notes` for ranking and graph expansion | Local file, SQL (WAL mode) | Relational rows | No explicit try/except around DB calls in this module — failures propagate as uncaught exceptions to the CLI/caller |
| ChromaDB (`data/chroma`, via `VectorIndex`) | Internal/Embedded Vector DB | Embed hub MOC `topic+summary` into the `mocs` collection for downstream retrieval (ask/article/sync) | Local persistent Chroma client | Vector + metadata (dict, sanitized via `_sanitize_metadata`) | No explicit try/except; upsert failures propagate |
| Vault filesystem (`vault/40_MOCs/`) | Internal/Filesystem | Write/read hub MOC `.md` files (frontmatter + body) | Local file I/O (UTF-8) | Markdown + YAML frontmatter | `safe_write_note` creates parent dirs; `_update_hub_moc` checks `moc_path.exists()` and returns `None`/logs a warning if missing rather than crashing |
| `zettel.graph.expand_notes` | Internal library call | Weighted BFS neighbor discovery, shared with retrieval-time graph expansion (ask/article) | In-process function call | Python dataclasses (`GraphNeighbor`) | No error handling needed — pure computation over already-fetched rows |
| `zettel.gardener` private helpers | Internal library call (cross-module, non-public API) | MOC body construction, structure parsing, incremental placement — reused rather than duplicated | In-process function call, imported inside function bodies (lazy import to avoid circular import with `gardener.py`) | N/A | N/A |
| `zettel.taxonomy.resolve_allowed_topics` | Internal library call | Supplies taxonomy context to the generation prompt (informational only, not enforced as a hub topic constraint) | In-process function call | Markdown detail string | Wrapped in `try/except Exception: taxonomy_detail = "_(Taxonomia indisponivel.)_"` — degrades gracefully |
| `zettel.usage` (`begin_run`/`finish_pipeline_run`) | Internal library call | Cost/usage tracking for the `garden_hubs` run | In-process (contextvars) | Run cost summary dict persisted to `runs` table | No explicit error handling; relies on `finish_pipeline_run` always being reached (not wrapped in try/finally — a raised exception mid-run would leave the run row unfinished) |

---

## 9. Design Patterns & Architecture

| Pattern | Implementation | Location | Purpose |
|---------|----------------|----------|---------|
| Strategy | `HubMocsConfig.selection_mode` (`percentile` vs `absolute`) switches the thresholding algorithm in `rank_note_hubs` | `gardener_hub.py:61-70` | Let operators choose a scale-relative vs. fixed hub bar per corpus size |
| Signature / Content-addressing (idempotency) | `cluster_signature = sha256("hub:<id>|" + sorted note_ids)` gates whether an LLM call happens at all | `gardener_hub.py:294-315` | Avoid redundant LLM cost when a cluster's membership hasn't changed |
| Incremental-update / append-only classification | `_update_hub_moc` re-uses a narrower prompt (`moc_hub_incremental.md`) that only classifies *new* notes into *existing* subsections, never regenerating the whole MOC | `gardener_hub.py:413-509` | Cost control (1 LLM call max) + structural stability of existing MOCs across runs |
| Template Method (partial, cross-module) | The generation/update flow (`_create_new_hub_moc`/`_update_hub_moc`) follows the same skeleton as `gardener.py`'s `_process_cluster`/`_create_new_moc`/`_update_moc`, but is a separate hand-written parallel implementation rather than a shared base class or higher-order function | `gardener_hub.py` vs `gardener.py` (`_process_cluster` family) | Mirrors an existing, proven pipeline shape for a new selection strategy without risking regressions in the taxonomy pipeline |
| Reconciliation / fallback bucket | `_build_hub_moc_body`'s `"Outras notas do cluster"` fallback for any note not placed by the LLM | `gardener_hub.py:615-623` | Guarantees no analyzed note is silently dropped from the output, defending against unreliable LLM structuring |
| Alias indirection for LLM I/O | `_build_note_alias_map` (N1, N2, ...) + `_resolve_note_ref` (alias -> ID, with fuzzy-match fallback) | shared with `gardener.py:760-798` | Shrinks prompt size and reduces LLM transcription errors versus using raw ULIDs |
| Lazy/deferred imports to break a dependency cycle | `from zettel.gardener import ...` performed **inside** function bodies (`_create_new_hub_moc`, `_update_hub_moc`, `purge_hub_pipeline_mocs`) rather than at module top-level | `gardener_hub.py:328-332, 423-432, 260` | Avoids a circular import between `gardener_hub` and `gardener` (or defers cost until actually needed) — see §10 for the coupling this implies |
| Origin-tagged soft-partitioning of a shared table | `mocs.origin` column (`hub_pipeline` / `pipeline` / `manual`) used as the sole discriminator for scoped deletion | `gardener_hub.py:258-278`, `state.py:1259-1273` | Lets two independent MOC-generation pipelines (taxonomy + hub) and manual authoring share one `mocs` table/`40_MOCs/` directory without interfering with each other's `--recreate` |
| Observer (optional) | `observer` parameter forwarded to `zettel.progress.report()` for optional progress notification (used by the web job worker) | `gardener_hub.py:184, 200-201, 236-239` | Decouples this pipeline from any specific UI (CLI spinner vs. web job progress) |
| Dataclass as a mutable stats accumulator | `_HubGardenStats` | `gardener_hub.py:32-38` | Simple counters threaded through the run for a single structured log line at the end |

---

## 10. Technical Debt & Risks

| Risk Level | Component Area | Issue | Impact |
|------------|-----------------|-------|--------|
| High | `_create_new_hub_moc` / `_update_hub_moc` | Both functions import ~7-11 **underscore-prefixed, module-private** symbols from `zettel.gardener` (`_allowed_note_ids`, `_build_note_alias_map`, `_moc_embeddable`, `_apply_incremental_placements`, `_build_notes_list`, `_parse_incremental_output`, `_parse_moc_structure`, `_snapshot_moc_file`, `_format_note_links`, `_resolve_note_ref`) | Any refactor of `gardener.py`'s internals (rename, signature change, or eventual extraction into a proper shared module) will silently break `gardener_hub.py` with no type-checker or import-time signal, since Python does not enforce module-privacy boundaries. This is the single largest coupling risk in the component. |
| Medium | `purge_hub_pipeline_mocs` / `run_garden_hubs` | `usage.begin_run()`/`finish_pipeline_run()` are not wrapped in `try/finally`; an unhandled exception anywhere in the ranking/expansion/LLM loop (e.g., a StateDB error, a ChromaDB connection failure) leaves the `runs` row in `status='running'` forever and the `CostTracker` contextvar unreset | A crashed `garden --hubs` run pollutes `zettel status`/run history with a stuck "running" row and can leak cost-tracking state into whatever runs next in the same process (relevant to the web worker, which is long-lived) |
| Medium | `_update_hub_moc` | `existing_ids` (the set of notes already in a hub MOC) is derived by **regex-parsing the MOC's own Markdown file** (`[[ZTL - (\S+) - ...]]`) via `gardener._parse_moc_structure`, not from any database-tracked membership list | If a user (or another tool) hand-edits a hub MOC file and reformats/removes a wikilink outside the expected pattern, that note silently becomes "new" again on the next run and gets reclassified — the file is the sole source of truth for cluster membership, with no reconciliation against `note_connections`-derived state |
| Medium | `dedup_hub_neighborhoods` | Dedup is `O(hubs^2)` in the worst case (each candidate compared against every previously accepted set) — fine at `top_n_hubs<=15` but not designed to scale if that config value is raised significantly | Low current impact given `top_n_hubs` default of 15, but a latent scalability ceiling if the config is changed without revisiting this function |
| Low | `get_neighbor_graph_context` vs. `build_hub_neighborhood` | Both functions independently call `graph.expand_notes()` with **the same parameters** (`hub_id`, `max_hops`, `decay`, weights, `seed_weights={hub_id: 1.0}`) but different `max_neighbors` values, recomputing the BFS from scratch for the same hub | Redundant computation per hub per run (2x `expand_notes` calls instead of 1) — a minor performance inefficiency, not a correctness bug, since `expand_notes` is pure and re-derives the same neighbor set (mod the neighbor cap) |
| Low | `rank_note_hubs` | `list_permanent_note_ids()` filters by the substring `"30_Permanent"` appearing anywhere in a note's stored `path` (after normalizing backslashes) | Fragile string-matching convention rather than a structured `note_type`/`kind` column check; a vault reorganization or a manually created note with `"30_Permanent"` incidentally in a different path segment could silently mis-classify |
| Low | `_process_hub_cluster` / `run_garden_hubs` | LLM call failures are caught per-cluster (`except Exception as e: logger.error(...); return None`) with a bare `Exception` catch, masking the specific failure mode (network error vs. malformed JSON vs. Pydantic validation error) in the log message beyond the exception's own `str()` | Harder to distinguish transient (retry-worthy) failures from structural prompt/schema mismatches from logs alone; no retry logic exists for a failed hub cluster within the same run |
| Low | Module-wide | No explicit handling for `note_ids` in `build_hub_neighborhood`/`dedup_hub_neighborhoods` containing duplicate IDs across different hub clusters beyond the subset-overlap dedup itself; a note can legitimately appear in multiple accepted hub MOCs simultaneously (by design, per the module's docstring framing as "complementary" navigation, but worth noting as a source of link duplication across MOCs in the vault) | Expected/intentional per design, but not documented anywhere as an explicit invariant — a maintainer might mistake it for a bug |

---

## 11. Test Coverage Analysis

| Component Area | Unit Tests | Integration Tests | Coverage (functions exercised) | Test Quality |
|------------------|------------|---------------------|-------------------------------|----------------|
| `rank_note_hubs` (percentile + absolute modes) | 2 (`test_rank_note_hubs_percentile`, `test_rank_note_hubs_absolute`) | 0 | Both selection modes covered against a real `StateDB` fixture | Good: asserts the expected top hub wins in both modes; does not test the `top_n_hubs` truncation, the `existing_anchors` force-include branch, or the empty-degrees early-return (`return []`) |
| `get_weighted_note_degrees` (StateDB, exercised via this component's test file) | 1 (`test_get_weighted_note_degrees`) | 0 | Confirms relative ordering (HUB > A, HUB > E) | Good: uses a realistic small graph with mixed relation types; does not assert exact numeric degree values |
| `build_hub_neighborhood` | 1 (`test_build_hub_neighborhood`) | 0 | Confirms hub-first ordering and a minimum neighborhood size | Adequate: does not test the `min_neighbor_weight` filtering boundary, `max_neighbors` truncation, or `max_hops` depth limiting in isolation |
| `dedup_hub_neighborhoods` | 2 (`test_dedup_hub_neighborhoods`, `test_dedup_keeps_distinct_neighborhoods`) | 0 | Both the "subset dropped" and "distinct kept" cases | Good: directly tests the pure function with hand-crafted inputs at the threshold boundary |
| `find_moc_by_hub_note_id` (StateDB) | 1 (`test_find_moc_by_hub_note_id`) | 0 | Found and not-found cases | Good |
| `purge_hub_pipeline_mocs` | 1 (`test_purge_hub_pipeline_mocs_keeps_taxonomy`) | 1 (writes real vault files, real StateDB, mocked `VectorIndex`) | Confirms origin-scoped deletion across `hub_pipeline`/`pipeline`/`manual` MOCs, vault file removal, and `idx.delete_mocs` call arguments | Very good: closest thing to an integration test in this suite, verifies the cross-cutting isolation guarantee (§3, Origin-Scoped Purge Isolation) end-to-end except for `clear_moc_backrefs` side effects (not asserted) |
| `_process_hub_cluster` (incremental routing) | 1 (`test_process_hub_cluster_routes_to_incremental`) | 1 (mocked LLM `.invoke`, real StateDB/vault file, mocked `VectorIndex`) | Confirms an existing hub MOC routes to `_update_hub_moc` and makes exactly 1 LLM call | Good: asserts `llm.invoke.call_count == 1`, directly verifying the "at most one LLM call" cost-control invariant for the incremental path |
| `_create_new_hub_moc` (full generation path) | **0** | **0** | **Not covered** | **Gap**: no test exercises full hub MOC generation (LLM call, `_build_hub_moc_body`, the "Porta de entrada" contract, or the "Outras notas do cluster" fallback reconciliation) |
| `get_neighbor_graph_context` | **0** | **0** | **Not covered** | **Gap**: no direct test of hop/weight/relation metadata formatting for the prompt |
| `run_garden_hubs` (top-level orchestrator) | **0** | **0** | **Not covered** | **Gap**: no test drives the full `run_garden_hubs` entry point (ranking -> neighborhood -> dedup -> cluster loop -> stats logging); the constituent pieces are tested individually but never as a wired pipeline |
| `_build_hub_moc_body` (fallback reconciliation, "Porta de entrada" section) | **0** | **0** | **Not covered** | **Gap**: the reconciliation rule described in §3 ("Hub-Section Content Contract") has no assertion anywhere in the suite |
| `_update_hub_moc` "no new notes" short-circuit (0-LLM-call path) | **0** | **0** | **Not covered** | **Gap**: the cost-saving branch where `truly_new` is empty (re-persist signature, skip LLM) is not exercised |
| CLI wiring (`zettel garden --hubs`, `--recreate`) | **0** | **0** | **Not covered** | **Gap**: no test in `tests/` invokes the Typer CLI command with `--hubs`; a search across the whole `tests/` directory for `hubs`/`gardener_hub`/`garden_hubs` returns only `tests/test_gardener_hub.py` itself |

**Test file location**: `D:\projetos\zettel_app\tests\test_gardener_hub.py` (211 lines, 9 test functions, all using `pytest` + `tmp_path` fixtures and `unittest.mock.MagicMock` for `llm`/`idx` doubles — no real LLM or ChromaDB calls are made in this suite).

**Overall assessment**: the pure/algorithmic core (ranking, BFS neighborhood sizing, dedup) and the origin-isolation/incremental-routing guarantees are well covered with focused, fast, fixture-based tests. The LLM-integration-heavy paths — full generation (`_create_new_hub_moc`), the structural body-building contract, and the top-level `run_garden_hubs` orchestration — have **no test coverage at all**, relying entirely on the sibling `gardener.py` taxonomy pipeline's more extensive test suite (not analyzed here) and manual/CLI verification for confidence in those code paths.

---

**Analysis scope note**: `.venv`, `.git`, `node_modules`, `data`, `vault`, `attached_assets`, and `.pytest_cache` were excluded per the requested `ignore-folders` parameter. This report analyzes `zettel/gardener_hub.py` in isolation, describing its dependencies on sibling modules (`gardener.py`, `graph.py`, `state.py`, etc.) only to the extent needed to document this component's own behavior, coupling, and risk surface — those sibling modules were not themselves subjected to a full deep analysis.
