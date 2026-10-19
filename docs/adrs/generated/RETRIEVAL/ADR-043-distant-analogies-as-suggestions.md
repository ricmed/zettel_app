# ADR-043: Distant Analogies as Suggestions, Not Graph Edges

**Status:** Accepted  
**Date:** 2026-09-06  
**Related to:** [ADR-003](../INFRA/ADR-003-hybrid-dense-bm25-retrieval.md), [ADR-009](./ADR-009-graph-based-note-discovery-weighted-bfs.md), [ADR-010](./ADR-010-retrieval-result-transparency-hits-vs-candidates.md)

## Context and Problem Statement

`connect` builds RAG from `linking.topk` (5) nearest notes. In a vault dominated by one domain, a Descartes note only sees Philosophy neighbours — the bridge never enters the window. Lowering the shared `relevance_floor` would poison `ask`. Persisting speculative analogies as `note_connections` would pollute graph expansion for everyone: connect writes edges without a human gate.

## Decision Outcome

**Chosen:** a third RAG group, local floor, suggestion-only persistence.

1. `Retriever.search_distant_analogies` runs a small secondary search with `linking.distant_analogy_min_similarity` (default 0.40). `RelevanceFloorConfig` is untouched.
2. Hits in the candidate's taxonomy bucket (same `argmax` as garden) are dropped. Origin is `distant_analogy`.
3. `connector.context.build_rag_context` renders `### Analogias distantes (outro dominio)`. The prompt judges these by **mechanism that transfers**, preferring `exemplifies` / `contradicts`.
4. LLM connections whose target is in that set go to the `auto-connections` managed block, not `note_connections`. `_extract_body_edges` already ignores that block — a suggestion becomes an edge only when the author moves the wikilink into the prose.

`zettel suggest-links` reuses the same retrieval + block write for a hand-written ZTL without calling Prompt 2 (no rewrite, no `ConnectRejected`).

## Consequences

A wrong analogy stays a suggestion. Graph expansion and hub ranking only see endorsed edges. The cost is one extra embedding of category labels per `connect` run plus a small second search.

## References

* GitHub issues #161, #165
* `zettel/retrieval.py` (`search_distant_analogies`), `zettel/connector/context.py` (`search_distant_analogies`), `zettel/connector/note.py` (`_write_note`, which fills `auto-connections`)
* `zettel/manual_lit.py` (`suggest_connections_for_permanent`)

## Amendment (2026-09-07) — carve-out for `corroborates`

The rule here — *connect writes edges without a human gate, therefore speculative links stay suggestions* — gates **LLM judgement**. [ADR-045](../REVIEW/ADR-045-cross-source-overlap-is-corroboration.md) writes `corroborates` straight to `note_connections`, and that is consistent rather than an exception: the edge is derived by code from `source_id` plus a similarity threshold, never proposed by a model. `corroborates` is deliberately absent from `permanent_note.md`'s relation menu (a test pins the absence), and a model that emits it anyway is downgraded to `supports` before the code's own edges are injected.

The distinction to preserve: a distant analogy is a guess about mechanism and stays a suggestion. Two different `source_id` values are a fact about authorship. "Same idea" is a deterministic similarity judgement — #226 measured its error — so the edge may be written directly only while it is recalculated when the cut changes.

## Amendment (2026-10-08) — preferred type for a distant analogy (#231)

`permanent_note.md` used to steer a distant analogy to `exemplifies` ("the same
scheme in another field"). Under the ordered definitions of #231, `exemplifies` is
a concrete case that adds no mechanism, and "the same mechanism in another domain"
is `related`, so the analogy group now prefers `related` or `contradicts`
(incompatible theses). Distant analogies remain suggestions, never edges.

## Addendum (2026-10-19): authorship is the fact, same-idea is the judgement (#280)

The 2026-09-07 carve-out used to say corroboration "cannot be wrong". That
sentence now says what #226 measured: authorship is the fact, and "same idea"
is a reproducible similarity judgement. Direct write stays licensed when
`scripts/rebuild_corroborates.py` reapplies the cut.

## Addendum (2026-10-19): LLM edges wait for a human (#287)

The premise of this ADR — connect writes edges without a human gate — now stops
at the LLM's own relations. `linking.llm_edges` defaults to `propose`: those
edges are stored as `status='proposed'`, rendered as `auto-connections`, and
left out of graph traversal until `/review?queue=links` or `zettel links`
accepts them. `corroborates` and the dedupe `extends` stay derived and enter
the graph immediately. A retyped edge is stored with `origin='review'`.
`accept` restores the previous direct write. There is no auto-accept threshold;
that would need its own pre-registered measurement.
