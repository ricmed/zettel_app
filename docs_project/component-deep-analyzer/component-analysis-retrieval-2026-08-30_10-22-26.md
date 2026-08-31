# Component Deep Analysis Report — `retrieval` (zettel/retrieval.py)

## 1. Executive Summary

The `retrieval` component is the single composition point for permanent-note lookup across the whole `zettel` pipeline. It lives in one file, `zettel/retrieval.py`, and exposes one public class, `Retriever`, with one public method, `search_notes()`.

Its purpose is to fuse three independent recall mechanisms into one ranked, provenance-annotated result set:

1. **Dense vector search** over ChromaDB's `permanent_notes` collection (`VectorIndex.query_similar_notes`).
2. **Lexical BM25 search** over the SQLite FTS5 `fts_notes` virtual table (`StateDB.search_notes_fts`).
3. **Graph expansion** over the typed `note_connections` edge list (`graph.expand_notes`), which surfaces conceptually linked notes that embeddings miss (notably `contradicts` relations, which sit far apart in vector space precisely because they argue the opposite point).

The fusion uses **Reciprocal Rank Fusion (RRF)** rather than score normalization, because it only needs the rank of an id within each list — this sidesteps the problem of reconciling incompatible scales (L2 distance vs. BM25 rank). On top of RRF, the component applies an **absolute relevance floor**, a deliberate design response to a documented production bug: RRF's fused score is purely positional, so a dense kNN search always returns "the N closest available" notes regardless of whether any of them are actually relevant to the query. The floor distinguishes "closest available" from "actually relevant" using raw vector cosine similarity and BM25 rank strength, and is itself a two-tier safety design (a hard absolute backstop plus a tunable main gate) that was hardened after a specific rank-unbounded-bypass bug was found in production (see Business Rule 3 below).

`Retriever` is a **pure orchestration/read layer**: it holds no persisted state of its own, mutates nothing, and every method is a query. Four production consumers depend on it: `connector.py` (RAG context for permanent-note generation), `sync.py` (connection suggestions for manually-authored notes), `ask.py` (the `zettel ask` QA command), and `article_graph.py`/`article.py` (the `zettel article` long-form writing pipeline). Two pipeline paths are explicitly documented as **not** migrated to this component (extractor dedupe, harvester layer-3 semantic-duplicate detection) because their thresholds are calibrated on raw L2 distance and would be disturbed by RRF's positional rescaling.

Key findings:
- The component is small (332 lines), single-file, and has no sub-modules — the entire "Component Structure" is one class plus two dataclasses.
- Business logic is concentrated almost entirely in `_apply_relevance_floor`, a five-branch decision cascade that is unusually well documented in-line (its docstring alone is ~25 lines) and has dedicated unit tests for every branch.
- Test coverage is strong at the unit level (18 tests in `tests/test_retrieval.py`, all passing directly against `Retriever`/`_apply_relevance_floor`) but weak at the integration level — none of the four consumers exercise the real fusion+floor+graph pipeline in their own test suites; they only test how they consume `RetrievedNote`/`NoteSearchResult` objects, or explicitly monkeypatch `Retriever.search_notes` away.
- The component degrades gracefully when SQLite's FTS5 module is unavailable (`StateDB.fts_enabled = False`), falling back to vector-only search with a one-time warning log.

## 2. Data Flow Analysis

```
1. Caller (connector.py / sync.py / ask.py / article_graph.py) builds a Retriever(cfg, db, idx)
2. Caller invokes retriever.search_notes(query, topk=..., mode=..., expand_graph=..., ...)
3. search_notes() resolves defaults from AppConfig (cfg.linking.topk, cfg.retrieval.mode,
   cfg.retrieval.graph_expansion.enabled) for any parameter left as None
4. Pool size computed: pool = max(topk * 3, 20)
5. _vector_notes(query, pool, exclude_id)
     -> VectorIndex.query_similar_notes() [ChromaDB dense kNN over "permanent_notes"]
     -> on exception: logged and treated as empty (defensive; not unit-tested)
6. _bm25_notes(query, pool, exclude_id)  [only if mode == "hybrid"]
     -> StateDB.fts_enabled check; if False, warn once and return []
     -> StateDB.search_notes_fts() -> SQLite FTS5 MATCH query (fts_notes virtual table)
     -> local exclude_id filter (Chroma applies exclude_id server-side; FTS5 path filters client-side)
7. _rrf_fuse_notes(vector_hits, bm25_hits)
     -> per-list rank -> score contribution 1/(k+rank), summed per note_id (dict merge)
     -> merges into RetrievedNote records carrying vector_rank/bm25_rank/vector_distance/document/metadata/title
     -> _hydrate_notes() fills title/document for ids that came only from BM25 (StateDB.get_note lookup)
     -> sorted by fused score, descending
8. _apply_relevance_floor(fused, relevance_floor_override, min_vector_similarity_override)
     -> mutates each RetrievedNote in place: sets passed_floor (bool) + floor_reason (str)
     -> decision order: floor-disabled -> absolute_min_similarity backstop -> bm25 strong-rank bypass
        -> min_vector_similarity gate -> weak-bm25-only rejection -> fail-safe pass
9. candidates = fused[:max(topk, 10)]        <- ALWAYS populated, pre-floor pool, for transparency
10. seeds = [f for f in fused if f.passed_floor][:topk]
11. If no seeds survive the floor -> return NoteSearchResult(hits=[], candidates=candidates)
      (deterministic "nothing relevant" signal; callers like ask.py skip the LLM call entirely)
12. If expand_graph is False -> return NoteSearchResult(hits=seeds, candidates=candidates)
13. Else: _expand_with_graph(seeds, exclude_id)
      -> graph.expand_notes() BFS over StateDB.get_connections_for_notes(), seeded by each
         surviving note's own RRF score (seed_weights), weighted by DEFAULT_RELATION_WEIGHTS
         (or cfg.retrieval.graph_expansion.relation_weights) and hop decay
      -> neighbours already present as seeds get their score REINFORCED (added to), never displacing a seed
      -> pure-graph neighbours (hop >= 1, no title/body yet) are hydrated via _hydrate_notes()
      -> final list sorted by score, descending
14. Caller receives NoteSearchResult(hits, candidates) and uses .hits as evidence/context;
    .candidates only for "what was closest" transparency UI (ask --show-context, article catalog)
```

Downstream consumption per caller:
- **connector.py** (`connect_notes` / candidate-to-ZTL flow): `retriever.search_notes(query_text, topk=cfg.linking.topk, exclude_id=note_id).hits` -> `_build_rag_context(db, similar)` -> injected into the Prompt 2 LLM call as RAG context for permanent-note generation.
- **sync.py** (`_suggest_connections`): `.hits` -> rendered as `[[wikilink]]` list into the note's `auto-connections` managed block (suggestion only, never persisted as a graph edge).
- **ask.py** (`run_ask`): full `NoteSearchResult` kept — `.hits` (truncated to `ask.max_context_notes`) build the cited LLM context; `.candidates` is preserved end-to-end into `AskResult.candidates` for the `--show-context` transparency table, independent of whether the LLM was even called.
- **article_graph.py** (`node_vector_search_merge`, a LangGraph node): calls `search_notes` once per enrichment query, merges `.hits` across queries via `art.merge_retrieved_notes` (note-id-keyed accumulation, not a single one-shot call).

## 3. Business Rules & Logic

### Overview of the business rules:

| Rule Type | Rule Description | Location |
|-----------|------------------|----------|
| Fusion Algorithm | Reciprocal Rank Fusion combines vector and BM25 rankings by rank position, not raw score | retrieval.py:250-280 |
| Search Pool Sizing | Candidate pool fetched per source is `max(topk * 3, 20)` before fusion/floor narrows it | retrieval.py:111 |
| Mode Gating | BM25 search is skipped entirely when `mode != "hybrid"` (i.e. `mode == "vector"`) | retrieval.py:107, 113-115 |
| FTS Degradation | Hybrid mode silently degrades to vector-only when `StateDB.fts_enabled` is False, with a one-time warning | retrieval.py:139-142, 241-246 |
| Relevance Floor — Disabled Bypass | If the floor is disabled (globally or per-call), every hit passes unconditionally | retrieval.py:181-186 |
| Relevance Floor — Absolute Backstop | Similarity below `absolute_min_similarity` (0.15) fails regardless of any BM25 evidence | retrieval.py:201-207 |
| Relevance Floor — BM25 Strong-Rank Bypass | A BM25 hit ranked `<= bm25_bypass_max_rank` (5) bypasses the similarity gate entirely | retrieval.py:209-216 |
| Relevance Floor — Main Similarity Gate | Otherwise, similarity must be `>= min_vector_similarity` (0.70) to pass | retrieval.py:218-225 |
| Relevance Floor — Weak-Lexical-Only Rejection | A hit with no vector distance at all and a BM25 rank worse than the bypass threshold fails (insufficient evidence) | retrieval.py:227-234 |
| Relevance Floor — Fail-Safe Default | A hit with neither similarity nor BM25 rank data defaults to passing, to avoid silently dropping data (documented as "shouldn't normally happen") | retrieval.py:235-239 |
| Candidate Pool Retention | The raw pre-floor ranked pool (`candidates`) is always returned (truncated to `max(topk, 10)`), even when `hits` is empty, for transparency/debugging | retrieval.py:118-119 |
| Seed Truncation | Only the top `topk` floor-surviving notes become graph-expansion seeds; excess passing candidates below `topk` are never expanded | retrieval.py:121 |
| Empty-Seed Short-Circuit | If nothing survives the floor, graph expansion is skipped entirely and `hits` is returned empty | retrieval.py:122-123 |
| Graph Expansion Toggle | Graph expansion only runs if `expand_graph` resolves to True (call-site override or `cfg.retrieval.graph_expansion.enabled`) | retrieval.py:108-109, 124-125 |
| Graph Neighbour Score Bound | A neighbour's weight is `seed_score * relation_weight * decay^(hop-1)`, so it can never exceed the seed it came from | retrieval.py:299-331, graph.py:90 |
| Graph Neighbour Reinforcement, Not Replacement | If graph expansion reaches a note that is already a seed, its score is incremented (reinforced), never overwritten or downgraded | retrieval.py:317-320 |
| Undirected Edge Traversal | `note_connections` edges are treated as undirected for graph expansion — a connection is relevant from either endpoint, and the reverse direction reuses the same relation weight | graph.py:51-53, 86-88 |
| Best-Path-Wins | When multiple paths reach the same neighbour, the highest-weight path's hop/via metadata wins | graph.py:94-98 |
| Relation-Type Weighting | Neighbour weight is scaled by a per-relation-type weight table, with `contradicts` weighted highest (1.0) because embeddings cannot distinguish "supports" from "contradicts" | config.py:154-161, graph.py:84 |
| Hydration-Only-If-Missing | A note's title/document is fetched from `StateDB.get_note` only if not already populated from the vector-search metadata (avoids redundant DB round-trips) | retrieval.py:284-295 |
| Exclude-Self Rule | The originating note (`exclude_id`, typically the note currently being generated/synced) is excluded from vector results (server-side, in Chroma), BM25 results (client-side filter), and graph neighbours (client-side filter) | retrieval.py:132-146, 314-316 |
| Query-Term Sanitization (shared with FTS layer) | Every FTS5 query token is double-quote-wrapped and PT-BR stopwords are stripped before the MATCH expression is built, preventing FTS5 operator injection and stopword-driven false-positive lexical matches | state.py:26-59 (consumed transitively via `search_notes_fts`) |

### Detailed breakdown of the business rules:
---

### Business Rule: Reciprocal Rank Fusion (RRF) of Vector and BM25 Rankings

**Overview**:
`_rrf_fuse_notes` combines two independently ranked lists — dense vector search and BM25 lexical search — into a single ranked list, using each list's *rank position* rather than its raw score.

**Detailed description**:
Vector search returns an L2 distance (a continuous scale, roughly 0–2 in this codebase's embedding space) while BM25 returns SQLite FTS5's `rank` column (a different, unrelated continuous scale). These two scores cannot be meaningfully combined by weighted sum or normalization without an arbitrary calibration choice that would need re-tuning per embedding model, per corpus size, and per query distribution. RRF sidesteps this entirely: for each list, a note's contribution to the fused score is `1 / (k + rank)`, where `rank` is the note's 1-based position in that list and `k` is a smoothing constant (`retrieval.rrf_k`, default and operational value 60 — described in code as "canonical"). A note appearing in both lists sums both contributions; a note appearing in only one list gets only that contribution. The dictionary-merge implementation works because ids are shared across ChromaDB and SQLite (`note_id` is the same key space in both stores), so there is no id-translation step.

The practical effect is that a note ranked #1 in both vector and BM25 will always outrank a note ranked #1 in only one list, and a note deep in one ranking (e.g. rank 50) contributes almost nothing (`1/(60+50) ≈ 0.009`) — so a single strong signal in one channel is not overwhelmed by a weak signal in the other, but two moderate signals across both channels can still outrank one very strong single-channel signal, depending on `k`. This is the intended trade-off: RRF favors consensus across retrieval strategies over confidence in any single one.

`k=60` is the "canonical" value cited from the original RRF literature and is not per-corpus calibrated (unlike the relevance floor's thresholds, which the code explicitly notes are corpus/embedding-specific and should be retuned). This is a design choice, not an oversight — the comment in `RetrievalConfig` explicitly marks `rrf_k` as the "constante canonica" while marking the floor's `min_vector_similarity` as empirically calibrated and needing retuning per corpus.

**Rule workflow**:
```
for rank, hit in enumerate(vector_hits, start=1):
    scores[hit.id] += 1 / (rrf_k + rank)
for rank, hit in enumerate(bm25_hits, start=1):
    scores[hit.note_id] += 1 / (rrf_k + rank)
sort all merged note_ids by summed score, descending
```

---

### Business Rule: Absolute Relevance Floor (5-Branch Decision Cascade)

**Overview**:
`_apply_relevance_floor` is the component's most business-critical logic: it converts RRF's purely positional ranking (which always returns *something*, relevant or not) into a relevance judgment that can produce zero results when nothing is actually relevant.

**Detailed description**:
The rule exists because of a specific, named failure mode: RRF's fused score reflects only where a note falls in the *rank order* of the candidate pool, never whether that note is actually related to the query at all. A dense kNN search against ChromaDB always returns the N geometrically closest vectors in the corpus, even if the closest available vector is still semantically unrelated to an off-topic or out-of-domain query — and that "closest available but irrelevant" result can look just as "confident" (rank #1) as a genuinely relevant hit for an on-topic query. Left unchecked, this means every query — including nonsensical or off-topic ones — would always produce a "top result" for the `ask` and RAG-context pipelines to build an answer from, silently hallucinating relevance where none exists.

The floor is evaluated once per candidate, in a fixed order, and is deliberately conservative in one specific direction: a hard absolute backstop (`absolute_min_similarity`, default 0.15) cannot be overridden by *any* amount of lexical evidence, including a rank-1 BM25 match. This exists specifically to stop an embedding-orthogonal note (one that shares an incidental term with the query but is otherwise conceptually unrelated) from being rescued purely because BM25 found the shared term. Above that hard floor but below the main gate (`min_vector_similarity`, default 0.70), a *strong* BM25 match (rank `<= bm25_bypass_max_rank`, default 5) is treated as independent evidence of relevance and bypasses the vector-similarity gate — this is the mechanism that rescues jargon and acronyms the embedding model systematically underrates (a known weakness of dense embeddings for out-of-vocabulary or highly technical terms). Crucially, a *weak* BM25 match (found only deep in the pool, beyond rank 5) does not get this bypass, and instead falls through to the plain similarity check like any candidate with no lexical support at all — this distinction (rank-bounded vs. unconditional bypass) is called out explicitly in the code as the fix for a real production bug, where any BM25 presence at all used to bypass the floor unconditionally, letting a note that merely shared one common domain word with the query pass despite very low vector similarity.

The remaining two branches handle edge cases: a note with vector similarity data simply passes or fails against `min_vector_similarity`; a note with *no* vector similarity data at all (meaning it was found exclusively via BM25, at a rank too weak to bypass) fails outright, since there is no positive evidence of relevance from either channel. A final fail-safe branch (documented as "shouldn't normally happen") defaults to passing when a hit has neither similarity nor BM25 rank data, prioritizing not silently dropping data over strict floor enforcement in an unreachable-in-practice code path.

Every verdict — pass or fail — is recorded with a human-readable `floor_reason` string (in PT-BR), which is surfaced all the way to the `ask --show-context` CLI table and the `article` catalog, giving the end user visibility into *why* a specific note was or wasn't used as evidence, not just whether it was.

**Rule workflow**:
```
if floor disabled (config or call-site override): PASS, reason="piso desabilitado"
similarity = 1.0 - vector_distance / 2.0   (None if no vector_distance)

if similarity is not None and similarity < absolute_min_similarity:
    FAIL  (hard backstop; no BM25 bypass possible)
elif bm25_hit_bypasses_floor and bm25_rank is not None and bm25_rank <= bm25_bypass_max_rank:
    PASS  ("match lexical forte")
elif similarity is not None:
    PASS if similarity >= min_vector_similarity else FAIL
elif bm25_rank is not None:
    FAIL  ("match lexical fraco", no vector data to corroborate)
else:
    PASS  (fail-safe; no evidence of any kind present — should not occur for a fused hit)
```

---

### Business Rule: Graph Expansion is Additive, Never Displacing

**Overview**:
When `expand_graph` is enabled, floor-surviving seeds are expanded 1..N hops over the typed `note_connections` graph, but the design guarantees graph neighbours can only ever *add* to the result set's relevance signal, never displace or outrank the seed that led to them.

**Detailed description**:
`_expand_with_graph` calls `graph.expand_notes`, a breadth-first search over `note_connections`, seeded with each surviving note's own fused RRF score (`seed_weights`). A neighbour's computed weight is the seed's score multiplied by a per-relation-type weight (`DEFAULT_RELATION_WEIGHTS`, overridable via `cfg.retrieval.graph_expansion.relation_weights`) and an exponential hop-decay factor (`decay ** (hop - 1)`, default decay 0.5). Because both the relation weight and the decay factor are `<= 1.0` by construction (weights range 0.5–1.0, decay < 1.0 for hop > 1), a neighbour's weight is mathematically bounded above by the seed's own score — a neighbour can never outrank the seed it was reached from, and by extension, a graph-only result can never look more "confident" than the strongest genuinely-retrieved seed.

The relation-weight table intentionally ranks `contradicts` as the *highest*-weighted relation (1.0, above even `extends`/`depends_on` at 0.9), which is a deliberate inversion of what a naive intuition might expect (surely "supports" is more useful than "contradicts"?) — the reasoning documented in `config.py` is that dense embeddings cannot distinguish supporting from contradicting evidence (two notes that argue opposite conclusions about the same topic sit *close together* in embedding space, because they share vocabulary and subject matter), so a `contradicts` edge is treated as a strong, otherwise-unrecoverable relevance signal precisely because vector similarity would never have surfaced it on its own.

If graph expansion reaches a note-id that is *already* one of the seeds (e.g., two seeds are mutually connected), the design reinforces rather than replaces: the neighbour's computed weight is added to (not substituted for) the seed's existing score, treating the graph connection as corroborating evidence on top of the direct retrieval hit. Seeds are always excluded from being counted as their own neighbours (`visited` set seeded with all seed ids up front), and the excluded note-id (`exclude_id`, typically the note currently being written) is filtered out of the neighbour set as well, mirroring its exclusion from the initial vector/BM25 search.

**Rule workflow**:
```
seed_weights = {seed.note_id: seed.score for seed in seeds}
neighbours = BFS(note_connections, seeds, max_hops, decay, relation_weights, seed_weights)
for each neighbour found:
    weight = seed_score(anchor) * relation_weight(relation_type) * decay ** (hop - 1)
    if multiple paths reach the same neighbour: keep the highest-weight path (best-path-wins)
for nid, neighbour in neighbours.items():
    if nid == exclude_id: skip
    if nid already a seed: seed.score += neighbour.weight   (reinforcement)
    else: add as a new RetrievedNote with hop >= 1, score = neighbour.weight
hydrate title/body for any new pure-graph neighbours (StateDB.get_note)
return all results sorted by score, descending
```

---

### Business Rule: Hybrid-Mode / FTS5-Availability Degradation

**Overview**:
The component degrades predictably and silently (aside from a one-time log warning) when either the operator has configured `mode: vector` or the underlying SQLite build lacks the FTS5 extension module.

**Detailed description**:
Two independent conditions can suppress the BM25 half of retrieval. First, an explicit configuration choice: `cfg.retrieval.mode` (or a per-call `mode` override) set to `"vector"` skips `_bm25_notes` entirely — this is documented as preserving "historical behaviour" (pure Chroma search), useful for isolating or comparing against the hybrid path. Second, an environmental constraint: some SQLite builds ship without the FTS5 module compiled in; `StateDB._init_fts` detects this at construction time (catching the specific `sqlite3.OperationalError` for "fts5"/"no such module") and sets `self.fts_enabled = False` rather than raising, so the whole pipeline continues to function on vector search alone. `Retriever._bm25_notes` checks this flag before every BM25 call and short-circuits with an empty list, logging a warning exactly once per `Retriever` instance (`self._warned_no_fts`) to avoid log-spamming on every single query in a long-running process (e.g. the web UI worker).

This is a deliberate defensive design rather than a hard dependency: the hybrid mode is additive value on top of a vector-search baseline that always works, and the component never fails a request purely because BM25 is unavailable — it silently narrows to whatever data source(s) remain functional. The relevance floor and graph expansion continue to operate identically in vector-only mode; only the `bm25_rank`-based bypass branch of the floor becomes permanently inapplicable (since no hit will ever carry a `bm25_rank`), meaning every hit's fate is decided purely on vector similarity when running degraded.

**Rule workflow**:
```
mode = call-site override or cfg.retrieval.mode (default "hybrid")
if mode != "hybrid": bm25_hits = []   (explicit operator choice)
elif not db.fts_enabled:
    log warning once ("FTS5 indisponivel — busca hibrida degradada para vetorial pura")
    bm25_hits = []                    (environmental constraint)
else:
    bm25_hits = db.search_notes_fts(query, limit=pool)
    bm25_hits = filter out exclude_id
```

---

### Business Rule: Hydration Only Fills Missing Fields

**Overview**:
`_hydrate_notes` backfills `title`/`document`/`metadata` for any `RetrievedNote` missing them, but never overwrites data already populated from the vector-search response.

**Detailed description**:
A note reaching the fused result set purely through BM25 (no corresponding vector hit) has no `title`/`document`/`metadata` populated by `_rrf_fuse_notes`, since those fields are only filled from the Chroma response payload. `_hydrate_notes` closes this gap with a single `StateDB.get_note` lookup per note lacking either field, pulling `title` and `body` from the SQLite `notes` table (the durable source of truth) and defaulting `metadata["source_id"]`/`metadata["path"]` via `setdefault` (so any value already present from Chroma's metadata is preserved, not clobbered). The `if rn.title and rn.document: continue` short-circuit means a note that arrived with both fields already set from a vector hit incurs zero extra database round-trips — this keeps the typical hybrid case (most notes found via both channels) cheap, at the cost of one extra query per BM25-only or graph-only note.

This same hydration path is reused for pure-graph neighbours after graph expansion (`_expand_with_graph` calls `_hydrate_notes` a second time, filtered to `hop >= 1` entries), since a graph neighbour by definition never went through either search channel and therefore always starts with empty `title`/`document`.

**Rule workflow**:
```
for each RetrievedNote in the fused/expanded list:
    if title and document already set: skip (no DB call)
    row = StateDB.get_note(note_id)
    if row is None: skip (note vanished from SQLite; leave fields empty)
    title = title or row.title
    document = document or row.body
    metadata.setdefault("source_id", row.source_id)
    metadata.setdefault("path", row.path)
```

---

## 4. Component Structure

```
zettel/retrieval.py                 # entire component — single file, no sub-package
├── RetrievedNote (dataclass)       # one retrieval result + full provenance (rank/rank/distance/hop/via/floor)
├── NoteSearchResult (dataclass)    # container: .hits (usable evidence) + .candidates (raw pre-floor pool)
└── Retriever (class)
    ├── __init__(cfg, db, idx)                          # holds AppConfig, StateDB, VectorIndex refs; no owned state
    ├── search_notes(...)                                # PUBLIC API — orchestrates the full pipeline
    ├── _vector_notes(query, pool, exclude_id)            # ChromaDB dense search, exception-safe
    ├── _bm25_notes(query, pool, exclude_id)              # SQLite FTS5 BM25 search, fts_enabled-gated
    ├── _apply_relevance_floor(fused, override, override)  # the 5-branch relevance decision cascade
    ├── _warn_no_fts()                                    # one-time degraded-mode log guard
    ├── _rrf_fuse_notes(vector_hits, bm25_hits)           # RRF scoring + dict-merge into RetrievedNote
    ├── _hydrate_notes(results)                           # backfill title/document/metadata from StateDB
    └── _expand_with_graph(seeds, exclude_id)             # graph.expand_notes() call + score reinforcement

Direct collaborator (not part of this component, but tightly coupled):
zettel/graph.py                     # expand_notes() BFS + GraphNeighbor dataclass — imported as `from . import graph`
```

There is no separate test-organization split within the component itself (it is one file); its dedicated test file is `tests/test_retrieval.py` (272 lines, 18 test functions), and `graph.expand_notes` — its closest collaborator — has its own dedicated `tests/test_graph.py` (96 lines, 11 test functions).

## 5. Dependency Analysis

```
Internal Dependencies (compile-time imports):
retrieval.py -> graph.py                 (from . import graph; direct function call: graph.expand_notes)
retrieval.py -> config.py [TYPE_CHECKING only]  (AppConfig type hint; DEFAULT_RELATION_WEIGHTS consumed
                                                   indirectly via cfg.retrieval.graph_expansion.relation_weights)
retrieval.py -> index.py  [TYPE_CHECKING only]  (VectorIndex type hint; runtime duck-typing on .query_similar_notes)
retrieval.py -> state.py  [TYPE_CHECKING only]  (StateDB type hint; runtime duck-typing on .fts_enabled,
                                                   .search_notes_fts, .get_note, .get_connections_for_notes)

Note: all three collaborator modules are imported ONLY under `if TYPE_CHECKING:` — at runtime, Retriever
never imports config.py/index.py/state.py directly; it receives already-constructed instances via its
constructor and calls duck-typed methods on them. This is a deliberate decoupling: retrieval.py has zero
runtime import-time dependency on the concrete StateDB/VectorIndex/AppConfig classes, only on graph.py.

Internal Dependents (who imports/uses this component):
connector.py    -> Retriever, RetrievedNote     (RAG context for Prompt 2 / ZTL note generation)
sync.py         -> Retriever                    (auto-connections suggestion block for manual notes)
ask.py          -> Retriever, RetrievedNote     (zettel ask QA command)
article_graph.py -> Retriever                   (LangGraph node: node_vector_search_merge)
article.py      -> RetrievedNote                (dataclasses only: dict_to_retrieved_note / merge_retrieved_notes
                                                   / retrieved_note_to_dict — does not instantiate Retriever itself,
                                                   article_graph.py does)
cli.py          -> (references retrieval config / mode; CLI flags for ask/article surface Retriever settings)

External Dependencies:
- ChromaDB          - Vector similarity search backend (via VectorIndex.query_similar_notes; not called directly
                        by retrieval.py — always through the injected VectorIndex instance)
- SQLite FTS5        - BM25 lexical search backend (via StateDB.search_notes_fts; extension module, may be
                        absent on some SQLite builds -> triggers the fts_enabled degradation path)
- Standard library   - dataclasses, logging, typing (no third-party import inside retrieval.py itself)
```

## 6. Afferent and Efferent Coupling

Coupling unit = class / dataclass (this is a Python OO codebase; module-level functions in `graph.py` are treated as a single collaborator unit `graph.expand_notes`).

| Component | Afferent Coupling (used by) | Efferent Coupling (depends on) | Critical |
|-----------|------------------------------|----------------------------------|----------|
| `Retriever` | 4 (connector.py, sync.py, ask.py, article_graph.py) | 4 (`graph.expand_notes`, `VectorIndex.query_similar_notes`, `StateDB.{fts_enabled, search_notes_fts, get_note, get_connections_for_notes}`, `AppConfig.retrieval.*`) | High |
| `RetrievedNote` | 5 (connector.py, ask.py, article.py, article_graph.py, and internally within retrieval.py) | 0 (plain dataclass, no behaviour) | High |
| `NoteSearchResult` | 3 (ask.py directly consumes both `.hits`/`.candidates`; connector.py/sync.py consume `.hits` only; article_graph.py consumes `.hits`) | 1 (`RetrievedNote`, its own field type) | Medium |
| `graph.expand_notes` / `GraphNeighbor` | 1 (`Retriever._expand_with_graph`; also independently unit-tested) | 1 (`StateDB.get_connections_for_notes`, `config.DEFAULT_RELATION_WEIGHTS`) | Medium |

`Retriever` and `RetrievedNote` sit at the center of the ask/article/connector/sync fan-in — a change to `RetrievedNote`'s field set (e.g. renaming `passed_floor` or `floor_reason`) would ripple into at least four other modules' serialization code (`ask.py:_to_ask_source`, `article.py:retrieved_note_to_dict`/`dict_to_retrieved_note`), making it the component's highest-blast-radius surface.

## 7. Endpoints

Not applicable — `retrieval.py` exposes no REST/GraphQL/gRPC/CLI endpoints of its own. It is a library-level component consumed in-process by the CLI commands (`zettel ask`, `zettel article`, `zettel connect`, `zettel sync-manual`) and by the web UI's job dispatch (`web_app.py` routes `ask`/`connect`/`sync` jobs through the same underlying functions that instantiate `Retriever`). No network-facing surface exists in this file.

## 8. Integration Points

| Integration | Type | Purpose | Protocol | Data Format | Error Handling |
|-------------|------|---------|----------|-------------|-----------------|
| ChromaDB (`permanent_notes` collection) | Embedded vector database | Dense kNN similarity search over note embeddings | In-process Python API (via `VectorIndex`) | Python dict (`id`/`document`/`metadata`/`distance`) | `try/except Exception` around the whole call in `_vector_notes`; logs a warning and returns `[]` on any failure (explicitly marked `# pragma: no cover - defensive around Chroma`, i.e. untested) |
| SQLite FTS5 (`fts_notes` virtual table) | Embedded full-text search index | BM25 lexical search over note title+body | In-process Python API (via `StateDB`) | Python dict (`note_id`/`rank`) | Availability gated by `StateDB.fts_enabled` (set once at DB init if the FTS5 module is missing); query-level `sqlite3.OperationalError` also caught inside `StateDB.search_notes_fts` itself (outside this component) |
| `note_connections` table (SQLite) | Internal graph store | Typed edge traversal for GraphRAG-style expansion | In-process Python API (via `StateDB.get_connections_for_notes`) | Python dict rows (`source_note_id`/`target_note_id`/`relation_type`/`description`) | No explicit error handling inside `graph.expand_notes`/`_expand_with_graph`; a DB error here would propagate uncaught (unlike the vector path, which is defensively wrapped) |
| `notes` table (SQLite) | Internal metadata store | Hydration of title/body/source_id/path for search results lacking them | In-process Python API (via `StateDB.get_note`) | Python dict row or `None` | `if not row: continue` — silently skips hydration for a vanished/missing note_id; no exception raised |
| `AppConfig.retrieval.*` | Configuration | Supplies every tunable threshold (rrf_k, floor thresholds, graph expansion settings, topk) | In-process Pydantic model | Typed Python object | No validation performed by `retrieval.py` itself — trusts `AppConfig`/Pydantic's own schema validation at load time |

## 9. Design Patterns & Architecture

| Pattern | Implementation | Location | Purpose |
|---------|-----------------|----------|---------|
| Facade / Single Composition Point | `Retriever.search_notes` | retrieval.py:78-128 | One call hides three independent subsystems (Chroma, FTS5, graph BFS) behind a single API, as explicitly stated in the module docstring |
| Strategy (config-selected) | `mode: Literal["vector", "hybrid"]` gating `_bm25_notes` | retrieval.py:107, 113-115 | Lets callers/config switch between "historical" pure-vector behaviour and the newer hybrid fusion without code changes |
| Result Object / Two-Tier Result | `NoteSearchResult(hits, candidates)` | retrieval.py:52-64 | Separates "safe to use as evidence" from "raw pool for transparency", so callers cannot accidentally use unfiltered candidates as if they were vetted, while still supporting a debugging/explainability UI |
| Provenance-Carrying Value Object | `RetrievedNote` dataclass | retrieval.py:34-49 | Every result carries its full derivation (which rank in which list, similarity, hop distance, path, floor verdict+reason) rather than just an id+score, enabling downstream UIs (`ask --show-context`) to explain results |
| Reciprocal Rank Fusion | `_rrf_fuse_notes` | retrieval.py:250-280 | Standard IR technique for combining heterogeneous rankers without score-scale reconciliation |
| Graceful Degradation | `fts_enabled` check + `mode` fallback | retrieval.py:139-146, 241-246 | Keeps the pipeline functional (vector-only) when BM25 is unavailable, rather than failing hard |
| Fail-Open Defensive Wrapping | `try/except Exception` in `_vector_notes` | retrieval.py:132-137 | Isolates a third-party dependency (Chroma) failure from crashing retrieval entirely — trades correctness (silently treats a Chroma outage as "no vector hits") for availability |
| Breadth-First Search with Decay | `graph.expand_notes` | graph.py:40-113 | Classic weighted-graph traversal reused as a "light GraphRAG" — additive signal on top of RRF fusion, not a replacement retrieval mechanism |
| Dependency Injection via Constructor | `Retriever(cfg, db, idx)` | retrieval.py:70-73 | All collaborators passed in explicitly; no global state, no singleton, trivially testable with fakes (as `tests/test_retrieval.py`'s `FakeIndex` demonstrates) |

## 10. Technical Debt & Risks

| Risk Level | Component Area | Issue | Impact |
|------------|-----------------|-------|--------|
| Medium | `_expand_with_graph` / `graph.expand_notes` | No error handling around `StateDB.get_connections_for_notes` — unlike the vector-search path, a database error during graph expansion is not caught and will propagate to the caller uncaught | A transient SQLite error during graph expansion crashes the entire `search_notes` call (including the already-successfully-fused vector+BM25 result), rather than degrading gracefully like the vector-search failure path does |
| Medium | `_vector_notes` exception path | Marked `# pragma: no cover - defensive around Chroma` — this path has zero test coverage by the project's own admission | A behaviour change or regression in Chroma error handling (e.g. a new exception type, or a change in whether it retries) would go undetected until it manifests in production |
| Low | `rrf_k` constant | Documented as a fixed "canonical" value (60) with no per-corpus calibration guidance, in contrast to the relevance-floor thresholds which are explicitly called out as needing retuning per corpus/embedding model | If corpus size or embedding-quality characteristics change significantly, RRF's fusion behaviour (how much a rank-50 BM25 hit vs. a rank-5 vector hit matters) is not revisited alongside the floor thresholds, creating an inconsistency in how carefully different parts of the same pipeline are tuned |
| Low | Integration test coverage | None of the four production consumers (`connector.py`, `sync.py`, `ask.py`, `article_graph.py`) exercise a real end-to-end `Retriever.search_notes` call against a populated vector+FTS+graph fixture in their own test suites — `test_ask.py` explicitly monkeypatches `Retriever.search_notes` away, and `test_connector.py`/`test_sync.py` only test consumption of already-constructed `RetrievedNote` objects | A regression in how a specific consumer *calls* `search_notes` (wrong `topk`, wrong `exclude_id`, forgetting `mode=`) would not be caught by that consumer's own tests — only `test_retrieval.py`'s direct unit tests protect `Retriever` itself, and those don't verify caller-side wiring |
| Low | Hydration cost model | `_hydrate_notes` issues one `StateDB.get_note` call per note lacking title/document — for a BM25-heavy or graph-expansion-heavy result set, this is N sequential single-row SQLite queries rather than one batched `IN (...)` query (contrast with `get_connections_for_notes`, which is explicitly batched "one query per BFS frontier") | Not currently a measured bottleneck (SQLite is embedded/local, and `topk`/`max_neighbors` are small, single-digit-to-low-double-digit values), but the inconsistency in batching discipline between this method and its graph-traversal sibling is a latent scalability gap should `topk`/pool sizes grow |
| Low | Fail-safe floor branch | The final `else` branch in `_apply_relevance_floor` (no similarity, no bm25_rank -> PASS) is documented as "shouldn't normally happen for a fused result" but is still reachable code with a real behavioural consequence (unconditional pass) | A future change to `_rrf_fuse_notes` that produces a `RetrievedNote` with neither field populated (e.g. a new fusion source added without wiring both fields) would silently start passing everything from that source through the floor, and the only signal would be the generic `floor_reason` string, not a hard failure |

## 11. Test Coverage Analysis

| Component | Unit Tests | Integration Tests | Coverage | Test Quality |
|-----------|------------|---------------------|----------|----------------|
| `Retriever` (RRF fusion, hybrid/vector mode, exclude_id, hydration, graph expansion) | 7 (`test_rrf_combines_vector_and_bm25`, `test_vector_only_mode_ignores_fts`, `test_degrades_when_fts_disabled`, `test_hydration_fills_bm25_only_note`, `test_exclude_id`, `test_graph_expansion_adds_neighbors`, plus fixture setup) in `tests/test_retrieval.py:56-126` | 0 dedicated (no test wires a real Chroma+SQLite+graph corpus through a consumer's public entry point) | Good for the core class in isolation | Assertions are specific (checks exact id membership/exclusion, hop numbers, `via` relation type); uses a lightweight `FakeIndex` stub rather than a real Chroma instance, so the ChromaDB integration itself (query shape, distance semantics) is not exercised by these tests |
| `_apply_relevance_floor` (5-branch cascade) | 11 dedicated tests in `tests/test_retrieval.py:131-271` covering: low/high similarity, floor-disabled override, call-site override, bm25-only bypass, all-below-floor-but-candidates-shown, configurable bypass rank, strong/weak bm25 rank, absolute-minimum backstop (both blocking and non-blocking cases), weak-bm25-with-no-vector-data, and the disabled-floor reason string | 0 | Excellent — every documented branch of the decision cascade in the docstring has at least one corresponding test, including the two "refinement" edge cases (`bm25_bypass_max_rank` configurability, `absolute_min_similarity` hard backstop vs. legitimate rescue) | High quality: tests call `_apply_relevance_floor` directly on synthetic `RetrievedNote` instances (bypassing DB/index entirely) for fast, precise, branch-level verification; assertions check both the boolean verdict and a substring of the human-readable reason string |
| `graph.expand_notes` (BFS, decay, weights, cycles) | 11 tests in `tests/test_graph.py` (`test_one_hop_neighbors`, `test_two_hops_with_decay`, `test_max_hops_one_excludes_distant`, `test_undirected_reverse_reachable`, `test_cycle_does_not_loop`, `test_seeds_excluded_from_result`, `test_max_neighbors_cap`, `test_relation_weight_override`, `test_seed_weights_scale_neighbors`, `test_via_records_path`, `test_empty_seeds`) | 1 (`test_graph_expansion_adds_neighbors` in test_retrieval.py exercises it through `Retriever`) | Very good — covers cycles, undirected traversal, hop-limit cutoffs, decay, weight overrides, and provenance (`via`) | Direct, deterministic tests against a real `StateDB` fixture (not mocked), giving genuine confidence in the graph traversal's correctness |
| `_vector_notes` exception path | 0 | 0 | None (explicitly `# pragma: no cover`) | Untested by the project's own admission — see Technical Debt table |
| Consumer integration (`connector.py`, `sync.py`, `ask.py`, `article_graph.py` calling `Retriever.search_notes`) | 0 direct | Indirect only: `test_ask.py` monkeypatches `Retriever.search_notes` to return canned `NoteSearchResult` objects (2 tests: `test_run_ask_below_floor_shows_candidates_but_no_llm_call` and a no-hits variant) and asserts on `ask.py`'s handling of empty/rejected results; `test_connector.py`/`test_sync.py` test `_build_rag_context`/`_suggest_connections`-adjacent helpers using hand-built `RetrievedNote` instances, never a real `Retriever` call | Weak at the seam | The monkeypatch approach in `test_ask.py` verifies `ask.py`'s *reaction* to a given `NoteSearchResult` (correctly skips the LLM when `hits` is empty) but does not verify that `ask.py` calls `search_notes` with the right arguments, nor that a real fused+floored+graph-expanded result from `Retriever` would actually look like the canned fixture |

Test files located: `tests/test_retrieval.py` (component-specific, 272 lines / 18 test functions), `tests/test_graph.py` (closest collaborator, 96 lines / 11 test functions), `tests/test_ask.py` (partial consumer-side coverage via monkeypatch), `tests/test_connector.py` and `tests/test_sync.py` (helper-function coverage only, no `Retriever` exercise), `tests/test_article.py` / `tests/test_article_graph.py` (no direct `Retriever`/`retrieval.py` references found).

---

**Report saved to:** `D:\projetos\zettel_app\docs_project\component-deep-analyzer\component-analysis-retrieval-2026-08-30_10-22-26.md`
