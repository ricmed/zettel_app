# ADR-XXX: Retrieval Result Transparency (Hits vs Candidates)
**Status:** Accepted
**Date:** 2026-07-18
**Depends on:** [ADR-XXX: Hybrid Dense+BM25 Retrieval with RRF Fusion and Absolute Relevance Floor](../INFRA/ADR-003-hybrid-dense-bm25-retrieval.md)
**Related to:** [ADR-XXX: Graph-Based Note Discovery with Weighted BFS Expansion](./ADR-009-graph-based-note-discovery-weighted-bfs.md)

## Context and Problem Statement

The hybrid retrieval pipeline fuses dense-vector and BM25 results via Reciprocal Rank Fusion and then gates them through an absolute relevance floor, so a share of the ranked pool is rejected on every call. Once results can be rejected, every consumer of `Retriever.search_notes()` — the `ask` command, the `article` command, the connector's RAG step, and `sync`'s auto-suggestions — needs a consistent way to know both what is safe to use as evidence and what was close but did not qualify, since debugging "why did retrieval return nothing/the wrong thing" requires seeing the rejected pool, not just the survivors.

`NoteSearchResult`, the return type of `search_notes()`, was designed from the same commit that introduced the relevance floor to carry two parallel lists: `hits` (results that cleared the floor, plus optional graph neighbours) and `candidates` (the raw RRF-ranked pool before the floor, always populated when the corpus is non-empty). Each candidate carries a `floor_reason` explaining why it did or did not pass, alongside provenance fields (`vector_rank`, `bm25_rank`, `hop`, `via`). This turns an otherwise opaque filtering step into an inspectable one, at the cost of requiring every consumer to explicitly choose which list to trust.

## Decision Drivers

* The result contract is shared by every retrieval consumer (`ask`, `article`, connector, `sync`), so any change to it has system-wide reach.
* When the relevance floor rejects everything, callers still need a way to show "what was closest" instead of a bare empty result, both for debugging and for user-facing transparency.
* `ask --show-context` needs a per-result explanation (`floor_reason`) of why a near-miss failed, not just a rejection.
* `article` needs a fallback source of candidate notes when no seed clears the floor, so long-form generation degrades gracefully instead of failing outright.
* Consumers that only need high-confidence evidence (connector RAG, `sync` suggestions) should be able to ignore the raw pool entirely without extra ceremony.

## Considered Options

* Dual result structure (`hits` + `candidates` in one `NoteSearchResult`) — chosen
* Single filtered list only (`hits`), with no access to rejected candidates
* Two separate retrieval methods (one for filtered hits, one for raw candidates)

## Decision Outcome

Chosen option: dual result structure in a single `NoteSearchResult`, because `hits` and `candidates` come from the same query and the same fused ranking — they differ only in whether the relevance floor was applied — so splitting them into separate method calls would force a second retrieval pass or duplicate ranking logic for no benefit. Returning both from one call keeps the floor's filtering decision transparent by construction: a consumer that ignores `candidates` pays no extra cost, while one that needs the rejected pool (for a debug table or a fallback) already has it without an additional query.

The single-structure choice also matches how consumers actually use the two lists: `ask` and `article` read both (`hits` for the answer/context, `candidates` for `--show-context` or fallback), while connector and `sync` read only `hits`. A hits-only API would have required removing `ask --show-context` and `article`'s no-hits fallback; a two-method API would have doubled the retrieval surface for a distinction that is really just "the same ranked pool, before and after one gate."

## Pros and Cons of the Options

### Dual result structure (chosen)

* Good, because both lists come from one retrieval call, avoiding a duplicate ranking pass
* Good, because it makes the relevance floor's decision inspectable per result via `floor_reason`, rather than hiding rejected candidates entirely
* Good, because consumers that only need filtered evidence (connector, `sync`) can ignore `candidates` at no cost
* Bad, because every consumer must explicitly decide which list to use, and nothing enforces that a consumer defaults to the safer `hits`

### Single filtered list only (hits-only)

* Good, because the API surface is simpler, with one list and no filtered/raw distinction to reason about
* Bad, because it would remove `ask --show-context`'s ability to explain why near-misses failed
* Bad, because `article` would lose its fallback path when no result clears the floor, likely forcing a hard failure instead of degraded output

### Two separate retrieval methods

* Good, because it would make the filtered-vs-raw distinction explicit at the call site rather than in the return type
* Bad, because both lists are derived from the same fused ranking, so a second method would either duplicate the RRF/BM25 fusion work or require passing internal state between calls
* Bad, because most consumers need both lists in the same code path (e.g., `ask` uses `hits` for the answer and `candidates` for the debug table in the same request)

## Consequences

Every retrieval consumer independently decides how to treat an empty `hits` list — there is no shared "hits empty" handler, so `ask` returns a "no evidence" answer while `article` falls back to `candidates` to keep generating. Adding a new consumer requires deciding this behavior deliberately rather than inheriting a default, and a consumer that reads `candidates` without checking `passed_floor` risks treating a rejected near-miss as trustworthy evidence.

The `candidates` list is capped at `max(topk, 10)` results before the floor is applied, so very large candidate pools are never fully exposed even in debug views. [NEEDS INPUT: Is the max(topk, 10) cap on candidates intentional as a fixed ceiling, or should it become a configurable retrieval setting?]

The result structure has no explicit versioning — its contract is the set of dataclass fields on `NoteSearchResult` and `RetrievedNote` — so adding, renaming, or removing a field is a breaking change for every consumer without a compatibility layer to catch it. [NEEDS INPUT: Should `floor_reason` and the other provenance fields be treated as a stable public contract, or are they allowed to change without notice since all current consumers live in the same codebase?]

## References

* `zettel/retrieval.py:52-65` — `NoteSearchResult` and `RetrievedNote` field definitions
* `zettel/retrieval.py:78-128` — `search_notes()`, where `hits` and `candidates` are assembled from the fused, floor-gated pool
* `zettel/ask.py` — consumes `.hits` for answer generation and `.candidates` for the `--show-context` debug table
* `zettel/article.py` — consumes `.hits` for section context and `.candidates` as a fallback when `hits` is empty
