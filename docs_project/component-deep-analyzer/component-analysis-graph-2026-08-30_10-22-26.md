# Component Deep Analysis Report — `graph` (zettel/graph.py)

## 1. Executive Summary

`zettel/graph.py` is a single-purpose, ~114-line module that implements one public function, `expand_notes`, and one dataclass, `GraphNeighbor`. It performs a weighted, decayed breadth-first search (BFS) over the typed edge list persisted in the `note_connections` SQLite table by the `connect` phase (`connector.py`).

The pipeline's connection graph (`note_connections`) is otherwise write-only: `connect` writes typed edges (`supports`, `contradicts`, `extends`, `depends_on`, `exemplifies`, `related`) between permanent notes, but nothing reads them back as a retrieval signal until this module. `graph.py` turns that graph into a **retrieval booster**: given a set of seed notes (already ranked by vector/BM25 relevance), it walks 1..N hops of typed edges and returns reachable neighbours weighted by relation type and hop distance — surfacing notes that a dense embedding search would miss. The canonical example, documented in the module's own docstring, is a `contradicts` relationship: two notes that argue opposite theses sit far apart in embedding space precisely because they are semantically dissimilar, yet the relation is a first-class piece of evidence a retrieval consumer (e.g. `ask`) should see.

The module is deliberately implemented as plain Python BFS rather than a recursive SQL CTE (this trade-off is explained in the module docstring): the graph is small enough that Python traversal is cheap, and the traversal needs per-relation-type weighting plus provenance (the winning path, for citation/explanation purposes) — both awkward to express in SQL. It performs exactly one batched SQL query per BFS frontier (`StateDB.get_connections_for_notes`), keeping round-trips to O(hops) rather than O(nodes).

Architecturally, `graph.py` sits at the bottom of the dependency graph for retrieval-adjacent code: it depends only on `StateDB` (for edge data) and `config.DEFAULT_RELATION_WEIGHTS` (for a fallback weight table), and is itself depended on by three consumers: the hybrid `Retriever` (`retrieval.py`, used by `ask`/`connect`/`sync`), the long-form `article` LangGraph pipeline (`article_graph.py`), and the hub-anchored MOC pipeline (`gardener_hub.py`, two call sites). It performs no writes, no LLM calls, and no I/O beyond the batched SQLite reads — it is a pure query/compute function over already-persisted state, easy to reason about and to test in isolation, which is reflected in its dedicated, thorough unit test suite (`tests/test_graph.py`, 11 test cases).

Key findings:
- **Very low internal complexity, moderate design subtlety.** The function is one loop nest, but the "best path wins" / visited-set / frontier-propagation interaction (documented in detail in Business Rules below) has non-obvious edge cases that are well covered by tests but not obvious from a first read.
- **Fan-out coupling, not fan-in complexity.** Three independent call sites wire `expand_notes` with different `seed_weights`, `relation_weights`, and `max_hops`/`max_neighbors` policies (retrieval's per-query hop budget vs. article's deeper "extra hops" budget vs. the hub pipeline's static per-hub radius) — the function itself stays generic, but callers must each get the wiring right.
- **One untested consumer branch.** `article_graph.py`'s "extra graph hops beyond the retrieval default" branch (`node_vector_search_merge`, lines ~174-207, the only place `expand_notes` is called with `art_cfg.max_hops` rather than the retrieval config's `max_hops`) has no coverage in `tests/test_article_graph.py`.
- **No domain validation logic.** All "business rules" in this module are graph-traversal and score-computation semantics (weighting, decay, best-path selection), not input validation — the module trusts `note_connections` rows and caller-supplied weights/hops without range-checking them.

## 2. Data Flow Analysis

### 2a. Internal data flow — `expand_notes` itself

```
1. Caller supplies: seed_ids (ranked note ids), max_hops, decay, relation_weights,
   max_neighbors, seed_weights (e.g. RRF/vector scores of the seeds)
2. Guard: empty seed_ids or max_hops < 1 -> return {} immediately
3. Initialize: visited = set(seeds); frontier = {seed: (seed_weight, [])}
4. For each hop 1..max_hops:
   a. One batched SQL query: StateDB.get_connections_for_notes(current frontier ids)
      -> all edges where any frontier note is source OR target
   b. For each edge, treat it as undirected: consider both (src,tgt) and (tgt,src)
      as (anchor, other) if anchor is in the current frontier
   c. Compute candidate weight = anchor_accumulated_weight * relation_weight
      * decay^(hop-1)
   d. Update `best[other]` if this is the first time `other` is reached, or if
      this candidate beats the previously recorded best weight for `other`
   e. If `other` has not yet been fully visited, stage it into next_frontier
      (keeping only the strongest incoming candidate for further expansion)
   f. Mark next_frontier's ids as visited; frontier <- next_frontier
5. After all hops (or early exit when frontier is empty): sort `best` by weight
   descending, truncate to max_neighbors
6. Return {note_id: GraphNeighbor} for the surviving top-N neighbours
```

### 2b. Consumer flow — hybrid retrieval (`ask`, `connect`, `sync`)

```
1. Retriever.search_notes(query) fuses vector (Chroma) + BM25 (FTS5) rankings via RRF
2. _apply_relevance_floor() marks each fused candidate passed_floor / floor_reason
3. Seeds = candidates that passed the floor (>= topk)
4. If graph_expansion.enabled: Retriever._expand_with_graph(seeds)
   -> zettel.graph.expand_notes(db, seed_ids=seeds, seed_weights=seed.score, ...)
5. Neighbours returned by expand_notes are merged into the result set:
   - if a neighbour is already a seed, its score is *reinforced* (added to)
   - otherwise a new RetrievedNote is created with hop >= 1 and via = path
6. Pure-graph neighbours (hop >= 1, no title/body yet) are hydrated from StateDB
7. NoteSearchResult.hits (seeds + graph neighbours) is handed to the caller
   (ask.py builds cited context; connector.py / sync.py use it for suggestions)
```

### 2c. Consumer flow — `article` long-form pipeline (deeper hops)

```
1. node_vector_search_merge (article_graph.py) runs incremental hybrid searches
   per enriched query, merging hits into `existing` (capped at max_context_notes)
2. If article's own max_hops (art_cfg.max_hops) exceeds the retrieval config's
   graph_expansion.max_hops, a SECOND, deeper expansion is run directly:
   seeds = existing hits with hop == 0 (i.e. real search seeds, not prior
   graph neighbours), or the top-topk hits as fallback
3. zettel.graph.expand_notes(db, seed_ids=seeds, max_hops=art_cfg.max_hops, ...)
   is called with the seeds' own scores as seed_weights
4. New neighbours not already present are hydrated from StateDB and merged in,
   tagged with floor_reason = "vizinho de grafo (article max_hops)"
5. Result feeds the article's context/catalog/outline stages
```

### 2d. Consumer flow — hub-anchored MOCs (`garden --hubs`)

```
1. rank_note_hubs() ranks permanent notes by weighted graph degree
   (StateDB.get_weighted_note_degrees, itself independent of graph.py)
2. For each selected hub note:
   a. build_hub_neighborhood() calls expand_notes([hub_id], seed_weights={hub_id: 1.0})
      to gather 2x the neighbor budget, filters by min_neighbor_weight, ranks,
      and truncates to max_neighbor_slots -> [hub_id] + neighbor_ids
   b. get_neighbor_graph_context() calls expand_notes AGAIN (same hub, same
      config) purely to recover per-neighbour {hop, weight, relation} metadata
      for the LLM prompt (MOC hub generation)
3. The neighborhood note ids are handed to _process_hub_cluster for MOC
   generation/incremental update
```

## 3. Business Rules & Logic

### Overview of the business rules

| Rule Type | Rule Description | Location |
|-----------|------------------|----------|
| Traversal Semantics | Edges are treated as undirected for traversal purposes | graph.py:51-53, 86 |
| Scoring | Neighbour weight = seed_weight * relation_weight * decay^(hop-1) | graph.py:90 |
| Scoring | Unknown/unmapped relation types fall back to the `related` weight (or 0.5 if `related` itself is absent) | graph.py:84 |
| Scoring | Missing/`None` `relation_weights` argument falls back to `DEFAULT_RELATION_WEIGHTS` | graph.py:61 |
| Scoring | Missing/`None` `seed_weights` defaults every seed to weight 1.0 | graph.py:66, 71 |
| Selection | When multiple paths reach the same neighbour, the highest-weight path wins (score, hop, and via are all overwritten together) | graph.py:94-98 |
| Selection | Seeds are always excluded from the result set, even if reachable via a cycle back to themselves | graph.py:67, 99, 104 |
| Selection | Results are capped at `max_neighbors`, strongest weight first | graph.py:112-113 |
| Termination | BFS never revisits an already-visited node; each hop only expands through nodes first discovered at the previous hop | graph.py:99, 104-107, 109-110 |
| Termination | Empty `seed_ids` (after filtering falsy ids) or `max_hops < 1` short-circuits to an empty result with no DB query | graph.py:62-64 |
| Termination | An empty edge frontier (no more reachable unvisited nodes) stops the BFS early, before `max_hops` is exhausted | graph.py:75-76 |
| Provenance | Each neighbour carries its full winning path (`via`) as a list of `{from, relation_type, description}` steps for downstream citation/explanation | graph.py:35-37, 91-92 |
| I/O efficiency | Exactly one batched SQL query per BFS hop/frontier, regardless of frontier size | graph.py:77, module docstring |

---

### Business Rule: Undirected Edge Traversal

**Overview**:
Although `note_connections` stores each edge with a directed `source_note_id` / `target_note_id` pair (reflecting how the `connect` phase's LLM described the relationship, e.g. "note A extends note B"), `expand_notes` treats every edge as bidirectional for the purposes of graph reachability and scoring.

**Detailed description**:
The rationale, stated directly in the module docstring, is that "a connection is relevant to both notes it touches, and the reverse direction reuses the same relation weight — the inverse label in the vault is presentation-only." In other words, the *direction* of an edge is a narrative/display detail (the vault renders "B is extended by A" as the inverse of "A extends B"), but the *relevance* of the relationship to retrieval is symmetric: if a user is looking at note B, the fact that note A extends it is exactly as useful a piece of context as it would be if they were looking at note A. Enforcing directionality here would silently drop half of the graph's retrieval value — any note that only ever appears as a `target_note_id` (e.g. a foundational note that many other notes extend/support) would never surface as a neighbour of anything, only the reverse.

Practically, the implementation achieves this by, for every edge fetched from `get_connections_for_notes`, considering both `(src, tgt)` and `(tgt, src)` as candidate `(anchor, other)` pairs, and processing whichever one has its `anchor` present in the current BFS frontier. Because `get_connections_for_notes` itself queries `WHERE source_note_id IN (...) OR target_note_id IN (...)`, an edge where the frontier note is the target is still returned, and the `(tgt, src)` branch of the anchor/other loop picks it up correctly. `tests/test_graph.py::test_undirected_reverse_reachable` directly verifies this: an edge stored as `c -> a` is still reachable when expanding *from* `a`.

**Rule workflow**:
1. Edge `(src, tgt, rel)` is fetched because either endpoint is in the current frontier.
2. Both orderings `(src, tgt)` and `(tgt, src)` are evaluated as `(anchor, other)`.
3. Whichever ordering has `anchor` in the frontier proceeds; the other is skipped via `if anchor not in frontier: continue`.
4. If both `src` and `tgt` happen to be in the frontier simultaneously, both orderings fire, and each treats the other side as its "other" — meaning a bidirectional edge between two co-frontier nodes is processed from both perspectives in the same hop.

---

### Business Rule: Relation-Type Weighting with Fallback

**Overview**:
Every edge's contribution to a neighbour's score is scaled by a per-relation-type weight (`DEFAULT_RELATION_WEIGHTS` in `config.py`, overridable via `retrieval.graph_expansion.relation_weights` in `config.yaml`), and any relation type not present in that table falls back to the weight configured for `related`.

**Detailed description**:
The weight table encodes a deliberate business judgment about which relation types the *embedding-based* half of retrieval systematically underrates, and therefore deserve a boost when found via the graph: `contradicts: 1.0` (highest — two notes that disagree sit far apart in vector space despite being highly relevant to each other), `extends: 0.9`, `depends_on: 0.9`, `supports: 0.8`, `exemplifies: 0.7`, `related: 0.5` (lowest — the generic catch-all relation carries the least specific signal). This ordering is directly asserted by `tests/test_graph.py::test_one_hop_neighbors` (`contradicts` outweighs `supports` at the same hop).

The fallback (`weights.get(rel, weights.get("related", 0.5))`) exists for forward/backward compatibility: if the `connect` phase (or a future version of it) ever writes a relation type that isn't in the currently-configured weight table — whether because the table was hand-edited in `config.yaml` to omit an entry, or because a new relation type is introduced in `schemas.RelationType` before the weight table is updated — the traversal does not crash or treat the edge as zero-weight (which would silently break undirected reachability through it); it instead treats it as a generic, moderately-weighted relation. The `0.5` inner fallback further guards against a pathological config override that removes `related` itself.

This is a **defensive design choice with a subtle sharp edge**: an operator who overrides `relation_weights` in `config.yaml` and simply forgets to list one of the six canonical relation types (e.g. omits `exemplifies`) will not get an error — that relation type will silently be scored as `related` (0.5) instead of its intended weight, which could meaningfully change which notes clear a downstream `min_neighbor_weight` filter (e.g. in the hub pipeline) without any visible failure.

**Rule workflow**:
1. `weights = relation_weights or DEFAULT_RELATION_WEIGHTS` — a falsy (`None` or empty `{}`) override reverts entirely to the factory table (see the related caveat in Technical Debt & Risks).
2. For each edge, `rel_weight = weights.get(rel, weights.get("related", 0.5))`.
3. `rel_weight` multiplies directly into the candidate path weight for that hop.

---

### Business Rule: Multiplicative Hop Decay

**Overview**:
A neighbour's weight decays multiplicatively with distance from its seed: `weight = seed_weight * relation_weight * decay^(hop-1)`, where `decay` defaults to `0.5` and is configurable per call site.

**Detailed description**:
This encodes the intuitive business rule that graph-based relevance should degrade with distance — a note two hops away from a highly relevant seed is a weaker piece of supporting evidence than a note one hop away, all else equal. The decay is applied as `decay ** (hop - 1)`, so a hop-1 neighbour receives no decay penalty (`decay^0 = 1`), and each additional hop multiplies the accumulated weight by `decay` again. `tests/test_graph.py::test_two_hops_with_decay` verifies this directly: with a `related -> related` two-hop chain, the hop-2 neighbour's weight is strictly less than the hop-1 neighbour's.

Because the decay is applied to the *accumulated* weight passed forward through the frontier (`anchor_weight` in the loop, which itself is the previous hop's `cand_weight`), decay compounds hop over hop rather than being computed fresh from the original seed weight each time — a two-hop path through two `related` edges (weight 0.5 each) with `decay=0.5` yields `1.0 * 0.5 * 1 = 0.5` at hop 1, then `0.5 * 0.5 * 0.5 = 0.125` at hop 2 (relation weight applied again, plus one decay factor) — i.e. both the relation weight *and* the hop decay compound multiplicatively hop over hop, not just the decay term alone. This means longer paths are penalized doubly: once for each additional weak-relation hop, and again for the decay factor, which makes deep graph expansion aggressively conservative by design (matching the module's stated intent of being "a light GraphRAG" addition, not a replacement for the primary hybrid search).

Each call site chooses its own `max_hops`/`decay` trade-off: the default retrieval path uses `max_hops=1` ("1 hop already brings the value of light GraphRAG", per `config.yaml`'s comment), the `article` pipeline allows a deeper `max_hops=2` for its supplementary expansion pass, and the hub pipeline also defaults to `max_hops=2` for building wider MOC neighborhoods.

**Rule workflow**:
1. At hop *h*, `cand_weight = anchor_weight * rel_weight * decay^(h-1)`.
2. `anchor_weight` is the accumulated weight carried forward from the previous hop's winning candidate for that anchor (or the raw `seed_weight` at hop 1).
3. Successive hops compound both the per-edge relation weight and the decay factor, so weight shrinks super-linearly with path length.

---

### Business Rule: Best-Path-Wins (Non-Additive) Score Selection

**Overview**:
When a neighbour is reachable via more than one path (multiple seeds converging on it, or multiple edges within the same hop), its recorded weight/hop/via is the single **strongest** path found — paths are never summed or averaged.

**Detailed description**:
This is a deliberate maximization rule rather than an accumulation rule: `if cand_weight > best[other].weight: update`. The design rationale (implicit from the "additive context, never displaces" comment in `retrieval.py`, which is the direct downstream consumer) is that graph weight is meant to represent "how confidently is this neighbour connected to something already known to be relevant," not "how many ways is it connected" — summing could let a neighbour with several weak, low-quality connections outscore a neighbour with one strong, highly relevant connection, which would misrepresent the *strength* of the graph evidence.

This selection happens at two points that must stay consistent: (1) the `best` dict, which is the final answer for a note's neighbour record (weight/hop/via all swapped atomically together — never a weight from one path mixed with a `via` from another), and (2) the `next_frontier` dict, which independently tracks only the strongest way to continue expanding *through* that node in the next hop. Both use the identical `cand_weight` comparison, so the path that wins as "best answer for this neighbour" is always the same path that gets propagated forward for further expansion — there is no scenario where a suboptimal path is used for continued traversal while a better one is recorded as the answer.

Because `best[other]` can be updated by a later, stronger path even in a *later* hop (the `if other in best` check does not require `other` to still be in `visited`'s complement — it only requires the edge's `anchor` to be in the *current* frontier), the invariant "the recorded weight for any neighbour is the global maximum over every path examined during the whole BFS" holds even though the traversal itself only actively re-expands each node once. In practice this rarely fires beneficially, since later hops carry additional decay and are usually weaker, but it is a safety property, not a performance optimization.

**Rule workflow**:
1. First time `other` is reached (not yet in `best`, not yet `visited`): insert into `best`.
2. Any subsequent time `other` is reached (from a different anchor, or a later hop touching an already-frontier-adjacent anchor): compare `cand_weight` to `best[other].weight`; overwrite `weight`, `hop`, and `via` together only if strictly greater.
3. `next_frontier[other]` independently keeps only the single strongest incoming candidate for that node, used purely to seed the next hop's expansion — a node is only added to `next_frontier` (and thus only gets a chance to expand further) the first time it is reached in a given hop, subsequently only its stored weight is updated if a stronger candidate arrives within the same hop.

---

### Business Rule: Seed Exclusion and Cycle Safety

**Overview**:
Seed notes are never returned as neighbours of themselves or of each other, and cycles in the graph cannot cause a seed (or any node) to be revisited/reprocessed.

**Detailed description**:
`visited` is initialized to the full seed set before any traversal begins (`visited: set[str] = set(seeds)`), and every insertion into `best` requires `other not in visited`. This guarantees two things simultaneously: first, that no seed can ever appear in the returned neighbour map, even if the graph contains a cycle that loops back to a seed (verified by `tests/test_graph.py::test_cycle_does_not_loop`, a two-node mutual-`related` cycle where `a` never reappears as its own neighbour); second, that if multiple seeds are passed together, none of them is reported as a "neighbour" of another seed even if a direct edge connects them (verified by `test_seeds_excluded_from_result`).

This matters for correctness at the call sites: `Retriever._expand_with_graph` treats the returned neighbour map as strictly *additive* context beyond the seeds — if a seed could reappear in that map, the consumer's logic for "already a seed, reinforce its score" vs. "brand-new neighbour, hydrate title/body" would be exercised on a note that doesn't need either treatment, and worse, a would-be seed masquerading as a low-hop neighbour could have its real (higher) RRF-fused score silently overwritten by a much weaker graph-derived weight if the consumer's merge logic were ever written naively. The current consumer code in `retrieval.py` (`by_id[nid].score += neigh.weight` only for ids already in `by_id`) works correctly today, but it structurally relies on `expand_notes` upholding this exclusion guarantee.

**Rule workflow**:
1. `visited` starts as exactly `set(seed_ids)` (after filtering falsy ids).
2. A node can only enter `best` (and thus the final result) if, at the moment it is first reached, it is not in `visited`.
3. `visited` is only ever grown (`visited.update(next_frontier.keys())`), never shrunk, so once a node is excluded it stays excluded for the remainder of the BFS.

---

### Business Rule: Result Truncation to `max_neighbors`

**Overview**:
The function never returns more than `max_neighbors` neighbours, chosen as the globally strongest-weighted ones across the entire multi-hop traversal, not the strongest per hop.

**Detailed description**:
After the BFS completes (or exits early), all discovered neighbours in `best` — regardless of which hop they were reached at — are sorted by weight descending and sliced to `max_neighbors` (`sorted(best.values(), key=lambda n: n.weight, reverse=True)[:max_neighbors]`). This means a hop-1 neighbour with a mediocre relation weight can be pushed out of the final result by several hop-1 neighbours with stronger relations, but it also means a very strong hop-2 path (rare, given decay, but possible with high-weight relations like back-to-back `contradicts` edges) can outrank a weak hop-1 neighbour. `tests/test_graph.py::test_max_neighbors_cap` verifies the cap is enforced with a simple 10-edge star graph capped to 3.

This caps the amount of "extra context" any single call injects downstream — for the default retrieval path this bounds how much the graph can dilute the vector/BM25-driven context (`graph_expansion.max_neighbors: 10` in `config.yaml`), and for the hub pipeline it directly bounds the size of a generated MOC's neighbourhood (`hub_mocs.max_neighbors: 25`).

**Rule workflow**:
1. Every candidate ever inserted or updated in `best` remains in `best` until the function returns (no separate top-K pruning happens mid-traversal).
2. Final step: `sorted(...)[:max_neighbors]`.
3. Only the surviving top-N are converted into the returned `{note_id: GraphNeighbor}` dict — note that a node that lost out to the cap is fully discarded, not demoted or deprioritized in some secondary structure.

---

### Business Rule: Early Termination Guards

**Overview**:
The function short-circuits to an empty result without touching the database in two degenerate-input cases, and stops iterating hops early once the graph is exhausted.

**Detailed description**:
Three related guards keep the function cheap and correct on edge-case inputs. First, `seeds = [s for s in seed_ids if s]` filters out falsy entries (empty string / `None`) before anything else runs, and if the filtered list is empty, or `max_hops < 1`, the function returns `{}` immediately (`tests/test_graph.py::test_empty_seeds` covers both `expand_notes(db, [], max_hops=1)` and `expand_notes(db, ["x"], max_hops=0)`). This protects every consumer from having to special-case "no seeds this round" or "graph expansion configured off via max_hops=0" — they can call `expand_notes` unconditionally and treat the result uniformly.

Second, inside the hop loop, `if not frontier: break` stops the BFS the moment a hop produces no new frontier — i.e. the reachable component of the graph from the seeds has been fully explored — rather than continuing to loop (and querying the database) for the remaining configured hops with nothing left to expand. This is a performance guard, not a correctness one (an empty query would simply return no edges and produce the same final result more slowly), but it means `max_hops` is safely treated as an upper bound rather than a mandatory traversal depth.

**Rule workflow**:
1. Filter falsy seed ids -> if empty, or `max_hops < 1` -> return `{}` (zero DB calls).
2. Enter the hop loop; at the top of each iteration, if `frontier` is empty, `break` immediately.
3. Otherwise proceed with the single batched query for that hop.

---

## 4. Component Structure

```
zettel/
└── graph.py                          # The component under analysis
    ├── GraphNeighbor (dataclass)      # note_id, weight, hop, via[] — the return-value shape
    └── expand_notes()                 # sole public function — BFS entry point
```

The module is intentionally minimal: no classes beyond the plain result dataclass, no module-level mutable state, no I/O helpers of its own (all persistence access is delegated to the injected `StateDB`). Its only import from the rest of the codebase is `config.DEFAULT_RELATION_WEIGHTS` (a plain dict constant) and, for type-checking only, `state.StateDB` (guarded by `TYPE_CHECKING` to avoid a runtime import cycle, since `state.py` does not import `graph.py`).

## 5. Dependency Analysis

```
Internal Dependencies (compile-time imports):
zettel/graph.py -> zettel/config.py            (DEFAULT_RELATION_WEIGHTS constant)
zettel/graph.py -> zettel/state.py             (StateDB, type-checking only)

Internal Dependencies (runtime, via injected StateDB):
zettel/graph.py -> StateDB.get_connections_for_notes()   (zettel/state.py:1188)
                    -> SQLite table note_connections       (zettel/state.py:202-209)

Consumers (afferent — who imports/calls graph.py):
zettel/retrieval.py         -> graph.expand_notes()        (Retriever._expand_with_graph)
zettel/article_graph.py     -> graph.expand_notes()        (node_vector_search_merge, "extra hops" branch)
zettel/gardener_hub.py      -> graph.expand_notes()        (build_hub_neighborhood, get_neighbor_graph_context)
tests/test_graph.py         -> graph.expand_notes()        (unit tests)

External Dependencies:
- None directly. graph.py imports only the Python standard library
  (dataclasses, typing) plus the project's own config module.
- Transitively, via StateDB: SQLite (Python stdlib sqlite3), which stores
  note_connections in state.db (WAL mode).
```

Note the asymmetry: `graph.py` itself has essentially zero external dependencies and a tiny internal dependency footprint (config + StateDB), yet it has meaningful **afferent** coupling — three separate, independently-evolving call sites each responsible for correctly wiring `seed_ids`, `seed_weights`, `max_hops`, `decay`, `relation_weights`, and `max_neighbors` for their own use case. `graph.py` provides no validation of these parameters (see Technical Debt), so correctness of each integration rests entirely on the caller.

## 6. Afferent and Efferent Coupling

This component exposes one function and one dataclass; "coupling units" here are the function/dataclass plus its direct call-site integrations (the closest equivalent to "classes" for this procedural module).

| Component | Afferent Coupling | Efferent Coupling | Critical |
|-----------|-------------------|--------------------|----------|
| `expand_notes()` | 4 (Retriever, article_graph node, 2x gardener_hub call sites) + test suite | 2 (StateDB.get_connections_for_notes, config.DEFAULT_RELATION_WEIGHTS) | High |
| `GraphNeighbor` (dataclass) | 4 (every caller consumes `.weight`/`.hop`/`.via`; retrieval.py and article_graph.py re-project it into their own `RetrievedNote`/dict shapes) | 0 (pure data holder) | Medium |
| `StateDB.get_connections_for_notes()` (efferent target) | 1 (only `expand_notes` calls it) | 1 (SQLite `note_connections` table) | Low |
| `config.DEFAULT_RELATION_WEIGHTS` (efferent target) | 3 (graph.py default, gardener_hub.py default, article_graph.py via GraphExpansionConfig default) | 0 (plain constant) | Low |

`expand_notes` is rated **High** criticality despite its small size: it is the sole implementation of graph-based retrieval boosting across three independent pipelines (ask/connect/sync via `Retriever`, `article`, and `garden --hubs`), so a behavioral regression here silently degrades retrieval quality and MOC neighbourhood composition simultaneously across all of them, without any of those call sites having their own fallback logic.

## 7. Integration Points

`graph.py` has no network/API/database-driver integrations of its own; its only integration point is the internal `StateDB` query interface.

| Integration | Type | Purpose | Protocol | Data Format | Error Handling |
|-------------|------|---------|----------|-------------|----------------|
| `StateDB.get_connections_for_notes` | Internal (SQLite) | Batched fetch of all edges touching the current BFS frontier | In-process method call -> SQL (`sqlite3`) | List of `dict` rows (`source_note_id`, `target_note_id`, `relation_type`, `description`) | None in `graph.py` itself — no try/except around the call; a `StateDB`/SQLite failure propagates as an uncaught exception to the caller (`Retriever`, `article_graph`, `gardener_hub`), none of which wrap the `expand_notes` call in error handling either |

## 8. Design Patterns & Architecture

| Pattern | Implementation | Location | Purpose |
|---------|----------------|----------|---------|
| Breadth-First Search (level-order graph traversal) | `for hop in range(1, max_hops+1): ... frontier = next_frontier` | graph.py:74-110 | Explore the note graph outward from seeds, hop by hop, bounding traversal depth |
| Batched query per traversal level (avoids N+1 queries) | `db.get_connections_for_notes(list(frontier.keys()))` called once per hop, not once per node | graph.py:77 | Keeps DB round-trips to O(hops) instead of O(nodes visited) |
| Best-path / relaxation (Dijkstra-like "keep the best known cost" without a priority queue) | `if cand_weight > best[other].weight: update` | graph.py:94-98 | Ensures the reported weight/path to any neighbour is the strongest one found, not the first |
| Value Object / dataclass-as-DTO | `@dataclass class GraphNeighbor` | graph.py:28-37 | Immutable-by-convention (fields are mutated in place by the algorithm, but the shape is a plain data carrier) result record with built-in provenance (`via`) |
| Strategy-via-parameters (pluggable weighting) | `relation_weights` / `decay` / `max_hops` all passed in per call rather than hardcoded | graph.py:40-47 | Lets each of the three consumers (retrieval, article, hub MOCs) tune graph-expansion aggressiveness independently from shared config sections |
| Dependency Injection (of the data layer) | `db: "StateDB"` passed as the first argument rather than imported/instantiated internally | graph.py:41 | Testability (tests use an in-memory/temp-file `StateDB`) and decoupling from any particular persistence backend |

## 9. Technical Debt & Risks

| Risk Level | Component Area | Issue | Impact |
|------------|-----------------|-------|--------|
| Low | `expand_notes` weight fallback (`weights = relation_weights or DEFAULT_RELATION_WEIGHTS`, graph.py:61) | Passing an explicit empty dict `{}` as `relation_weights` silently reverts to `DEFAULT_RELATION_WEIGHTS` rather than being treated as "no weights configured." No current call site does this, but the function offers no way to distinguish "use defaults" from "I intentionally passed nothing" | A future caller intending to pass a dynamically-built (possibly empty) override dict would get silently-wrong behavior with no warning or error |
| Low | Relation-weight fallback (graph.py:84) | An edge whose `relation_type` isn't present in the active weight table is silently scored as `related` (0.5) instead of raising or logging. Combined with the operator-editable `config.yaml` override, an incomplete override table degrades scoring for the omitted relation type without any visible signal | Silent scoring degradation for whichever relation type is missing from a hand-edited config; could misrank neighbours in `min_neighbor_weight`-gated pipelines (hub MOCs) without any error trail |
| Low | No parameter validation | `expand_notes` does not validate that `decay` is in `[0, 1]`, that `max_neighbors`/`max_hops` are non-negative (beyond the `max_hops < 1` short-circuit), or that `relation_weights` values are non-negative. A misconfigured `decay > 1` in `config.yaml` would cause weight to *grow* with hop distance rather than shrink, inverting the intended relevance-degradation semantics | Config typo in `config.yaml`'s `retrieval.graph_expansion.decay` or `hub_mocs.decay` would silently invert graph-distance semantics rather than fail fast |
| Low | No defensive error handling around the DB call | Unlike sibling code in `retrieval.py` (`_vector_notes`, which wraps its Chroma call in try/except and degrades gracefully), `expand_notes` has no try/except around `db.get_connections_for_notes`; a `StateDB` error propagates directly | A transient SQLite error during graph expansion would abort the entire `ask`/`article`/`garden --hubs` operation rather than degrading to "no graph neighbours this run," unlike the vector-search path which explicitly tolerates its own backend failing |
| Low | Uneven test coverage across consumers | `article_graph.py`'s "extra graph hops beyond the retrieval default" branch (`node_vector_search_merge`, using `art_cfg.max_hops`) has zero coverage in `tests/test_article_graph.py` (verified: no reference to `expand_notes`, `note_graph`, `max_hops`, or `graph_expansion` in that test file) | A regression in the article pipeline's deeper-hop expansion path (e.g. wrong seed filtering by `hop == 0`, or wrong `art_cfg.max_hops > gcfg.max_hops` gating) would not be caught by the existing test suite |
| Informational | Duplicated `expand_notes` calls in `gardener_hub.py` | `build_hub_neighborhood` and `get_neighbor_graph_context` both call `expand_notes` independently with identical arguments for the same hub, once to select the neighbourhood and once more to recover per-neighbour metadata for the prompt | Not a correctness bug (the module is stateless/pure and cheap), but is a redundant computation — the second call re-derives data the first call already computed, doubling the BFS/DB work per hub processed in `garden --hubs` |

## 10. Test Coverage Analysis

| Component | Unit Tests | Integration Tests | Coverage | Test Quality |
|-----------|------------|--------------------|----------|---------------|
| `graph.expand_notes` (direct) | 11 (`tests/test_graph.py`) | 0 (none needed — pure function over an injected `StateDB`) | High — every documented business rule has at least one dedicated assertion | Good: precise, single-behavior tests (one-hop weighting, two-hop decay, hop cap, undirected reverse edges, cycle safety, seed exclusion, max_neighbors cap, relation-weight override, seed-weight scaling, via/path recording, empty-input guards). No test exercises multi-seed convergence onto the same neighbour (the "best path wins across seeds" scenario) or a config with `decay` outside `[0,1]` |
| `Retriever._expand_with_graph` (retrieval.py consumer) | 0 dedicated | 1 (`tests/test_retrieval.py::test_graph_expansion_adds_neighbors`) | Moderate — the happy path (one `contradicts` edge, one hop) is verified end-to-end through `search_notes`, including `hop` and `via` propagation, but score-reinforcement-when-neighbour-is-also-a-seed and `exclude_id` interaction with graph neighbours are not directly tested | Good for what it covers; narrow in scenario count |
| `article_graph.py` "extra hops" branch (`node_vector_search_merge`) | 0 | 0 | None — `tests/test_article_graph.py` mocks `build_article_graph` entirely (`FakeBuilder`) and never exercises this module's node functions directly | Gap — flagged in Technical Debt above |
| `gardener_hub.build_hub_neighborhood` / `get_neighbor_graph_context` | 0 dedicated to `expand_notes` interaction, but indirect | 1 (`tests/test_gardener_hub.py::test_build_hub_neighborhood`) exercises `build_hub_neighborhood` end-to-end over a small hand-built graph fixture (`_setup_graph_db`) | Moderate — verifies the neighbourhood includes the hub and has a plausible size, but does not assert specific weights/hops/relations, and `get_neighbor_graph_context` has no test at all | `test_build_hub_neighborhood` is a coarse smoke test (`len(neighborhood) >= 4`) rather than a precise behavioral assertion |
| `StateDB.get_connections_for_notes` / `upsert_note_connection` (the data-layer dependency) | 2 (`tests/test_state.py::test_note_connections_roundtrip_and_batch`, `test_note_connection_upsert_updates_description`) | 0 | High for the dependency itself | Good: verifies both-direction edge retrieval, batch fetch across multiple ids, empty-input handling, and upsert-updates-description semantics |

Overall: the core algorithm in `graph.py` itself is thoroughly and precisely unit-tested. The gap is entirely on the **consumer-integration** side — one of three call sites (`article_graph.py`'s deeper-hop branch) has no test coverage at all, and the hub-pipeline consumer's tests are coarse smoke tests rather than precise weight/hop assertions.
