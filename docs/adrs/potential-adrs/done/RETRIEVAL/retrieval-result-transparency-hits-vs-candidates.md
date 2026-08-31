# Potential ADR: Retrieval Result Transparency (Hits vs Candidates)

**Module**: RETRIEVAL  
**Category**: Retrieval Architecture / API Design  
**Priority**: Must Document (Score: 105)  
**Date Identified**: 2026-08-30

---

## Existing ADR Context

ℹ️ **RELATED DECISIONS**

This decision is a direct consequence of:
- **hybrid-dense-bm25-retrieval.md**: Absolute relevance floor gates which results pass; this ADR documents how we expose both filtered and raw results to callers.

**Relationship**: The relevance floor decides *what passes*; this ADR documents the *how to expose* both passes and near-misses.

---

## What Was Identified

The `NoteSearchResult` data structure returned by `Retriever.search_notes()` deliberately carries **two parallel result sets**:

1. **`hits`**: Candidates that cleared the absolute relevance floor (plus optional graph neighbours). What callers **should** use as evidence.
2. **`candidates`**: The raw RRF-ranked pool *before* the floor was applied. Always populated (when corpus non-empty), even when nothing cleared the floor.

This design provides transparency: when `hits` is empty (nothing met the relevance threshold), callers can still show "what was closest" for debugging/UX purposes, each candidate carrying `floor_reason` (human-readable explanation of why it didn't pass).

This is a deliberate architectural choice to surface both "answer" (hits) and "reasoning" (why candidates failed the floor) in a single structure.

**Introduced**: Same as relevance floor (`ed22565`, 2026-07-18 16:19:36). The NoteSearchResult structure itself appears in the same commit, suggesting the result transparency design was foundational to how the relevance floor was exposed to callers.

**Stable**: Result structure unchanged since introduction. Pattern reused across all retrieval consumers (ask, article, connector, sync).

---

## Why This Might Deserve an ADR

- **Impact**: API contract affects every consumer of retrieval (ask, article, connector, sync).
- **Intentional Transparency**: The dual result set design is **not** a side effect; it's deliberate.
  - Code comments in retrieval.py lines 56-60 explain the intent: "`hits` is what callers should actually use as evidence/context — it only contains candidates that cleared the absolute relevance floor... `candidates` is the raw ranked pool *before* the floor was applied."
  - This signals an architectural value: prefer transparency over hiding complexity.
- **Callers' Burden**: Every consumer must decide whether to use `hits` (filtered) or `candidates` (raw).
  - ask.py uses `.hits` for answer generation, `.candidates` for --show-context table (shows why near-misses failed)
  - article.py uses `.hits` for context building, `.candidates` for fallback when hits empty
  - connector.py uses `.hits` only (assumes filtered results are good enough for RAG)
  - sync.py uses `.hits` for auto-suggestion
- **Team Knowledge**: Required for understanding retrieval behavior when results seem wrong (is it the floor? Is it RRF? Is it a seed result?)
- **Cost to Change**: Removing candidates (returning only hits) would break ask.py --show-context transparency and article.py fallback logic.

---

## Evidence Found in Codebase

### Key Files
- [`zettel/retrieval.py`](../../../zettel/retrieval.py) - Lines 52-65 define `NoteSearchResult`; lines 78-128 show search_notes() API
- [`zettel/ask.py`](../../../zettel/ask.py) - Uses `.hits` for answer, `.candidates` for debug table (if exists)
- [`zettel/article.py`](../../../zettel/article.py) - Uses `.hits` for sections, `.candidates` for fallback

### Code Evidence

```python
# From zettel/retrieval.py (NoteSearchResult):
@dataclass
class NoteSearchResult:
    """Result of :meth:`Retriever.search_notes`.

    ``hits`` is what callers should actually use as evidence/context — it only
    contains candidates that cleared the absolute relevance floor (plus their
    graph neighbours). ``candidates`` is the raw ranked pool *before* the floor
    was applied, always populated, so a caller can still show "what was closest"
    for transparency even when nothing was relevant enough to answer from.
    """

    hits: list[RetrievedNote] = field(default_factory=list)
    candidates: list[RetrievedNote] = field(default_factory=list)

# From zettel/retrieval.py (search_notes):
def search_notes(
    self,
    query: str,
    topk: Optional[int] = None,
    exclude_id: Optional[str] = None,
    mode: Optional[str] = None,
    expand_graph: Optional[bool] = None,
    relevance_floor: Optional[bool] = None,
    min_vector_similarity: Optional[float] = None,
) -> NoteSearchResult:
    """Retrieve permanent notes for ``query``.

    Returns a :class:`NoteSearchResult`:

    - ``hits``: up to ``topk`` seeds that cleared the absolute relevance
      floor, plus (when graph expansion is on) their graph neighbours with
      ``hop >= 1``. This is what callers should use as evidence/context. If
      nothing clears the floor, ``hits`` is empty — callers should treat
      that as "nothing relevant found" rather than forcing an answer from
      the closest-available-but-irrelevant candidates.
    - ``candidates``: the raw RRF-ranked pool *before* the floor, always
      populated (when the corpus is non-empty) so a caller can still show
      "what was closest" for transparency/debugging.
    """
    # ...
    fused = self._rrf_fuse_notes(vector_hits, bm25_hits)
    self._apply_relevance_floor(fused, relevance_floor, min_vector_similarity)
    candidates = fused[: max(topk, 10)]  # Top-10 candidates before floor

    seeds = [f for f in fused if f.passed_floor][:topk]  # Filtered seeds
    if not seeds:
        return NoteSearchResult(hits=[], candidates=candidates)
    if not expand_graph:
        return NoteSearchResult(hits=seeds, candidates=candidates)
    return NoteSearchResult(
        hits=self._expand_with_graph(seeds, exclude_id), candidates=candidates
    )

# From zettel/ask.py (run_ask) — shows how results are consumed:
def run_ask(query: str, ...) -> AskResult:
    result = retriever.search_notes(query)
    
    # Use hits for the answer (high confidence)
    if result.hits:
        context_notes = result.hits[:cfg.retrieval.ask.max_context_notes]
        answer = generate_answer_with_context(context_notes)
    else:
        # Nothing passed the floor
        answer = "Nao ha evidencia no vault para responder."
    
    # Separately, expose candidates for transparency (why near-misses failed)
    return AskResult(
        answer=answer,
        sources=result.hits,  # What we used
        candidates=result.candidates,  # What was close but failed floor
        retrieval_params=...
    )

# From CLI rendering (ask command):
if show_context:
    print_candidates_table(
        result.candidates,
        headers=["Note", "Similarity", "BM25 Rank", "Floor Reason"]
    )
    # Each candidate shows: title, vector_distance, bm25_rank, floor_reason
    # E.g.: "similaridade 0.65 abaixo do piso (0.70)"
```

### Impact Analysis

- **API Contract**: `NoteSearchResult(hits, candidates)` is the public interface returned by search_notes()
- **Cardinality**: When `hits` is empty, `candidates` is non-empty (and vice versa)
  - `hits` = `candidates` filtered by `passed_floor == True` + optional graph expansion
  - `candidates` = raw `fused` pool (top-10, pre-floor)
- **Provenance per result**: Each `RetrievedNote` carries:
  - `passed_floor` (bool)
  - `floor_reason` (str) - human-readable explanation
  - `vector_rank`, `bm25_rank`, `vector_distance` - source evidence
  - `hop`, `via` - graph provenance if expanded
- **Consumer patterns**:
  - **ask.py**: `hits` for answer, `candidates` for --show-context debug table
  - **article.py**: `hits` for main retrieval, `candidates` for fallback expansion
  - **connector.py**: `hits` only (assumes quality sufficient for RAG)
  - **sync.py**: `hits` only (auto-suggestion)
- **Cost to change**: Removing candidates would require:
  - ask.py to remove --show-context transparency
  - article.py to remove fallback mechanism
  - Callers to lose "why didn't this pass?" reasoning

### Design Intent (Implicit in Code)

1. **Separate concerns**: Callers decide how strict to be. Hits is "production-ready"; candidates is "debug info".
2. **Transparency over hiding**: Don't hide near-misses; explain why they failed (floor_reason).
3. **Debuggability**: When retrieval behavior seems wrong, floor_reason field lets users understand the gate that rejected a result.
4. **Graceful degradation**: When hits is empty, candidates provide fallback (article.py) or explanation (ask.py) rather than hard error.

---

## Questions to Address in ADR (if created)

1. Why expose both hits and candidates? Why not just hits?
   - Answer likely: Transparency for debugging; fallback for article generation; show near-misses to users

2. Should the API be split into two methods (get_hits, get_candidates) or stay as one structure?
   - Answer likely: Single structure is better; they're conceptually related (same query, different filters)

3. Should floor_reason be mandatory or optional?
   - Answer likely: Mandatory; every hit should explain its floor verdict

4. Should consumers be required to use hits, or is candidates acceptable in production?
   - Answer likely: hits for production, candidates for debugging/fallback only

5. Is the 10-result minimum for candidates (line 119: `max(topk, 10)`) tunable?
   - Answer likely: Hardcoded; rationale is to always show some fallback even when topk is small

---

## Related Potential ADRs

- **hybrid-dense-bm25-retrieval.md**: Relevance floor decides what passes; this ADR documents exposure
- **graph-based-note-discovery-weighted-bfs.md**: Graph neighbours are added to hits; transparency extends to graph provenance
- **ask-command-architecture** (if created): Ask command's use of candidates for --show-context

---

## Additional Notes

- Result transparency is language-neutral: `floor_reason` messages are PT-BR but structure is generic
- No versioning of result structure; API contract is implicit (dataclass fields)
- Tests likely exercise both `hits` (positive cases) and `candidates` (edge cases where floor rejects all)
- The dual-result design is a form of "explainable AI" for retrieval; shows reasoning to users
- Candidates list size is capped at `max(topk, 10)` to avoid exposing too much ranked data; not configurable
- Each consumer decides independently how to handle empty hits; no global "hits empty handler"
