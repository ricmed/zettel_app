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
3. `_build_rag_context` renders `### Analogias distantes (outro dominio)`. The prompt judges these by **mechanism that transfers**, preferring `exemplifies` / `contradicts`.
4. LLM connections whose target is in that set go to the `auto-connections` managed block, not `note_connections`. `_extract_body_edges` already ignores that block — a suggestion becomes an edge only when the author moves the wikilink into the prose.

`zettel suggest-links` reuses the same retrieval + block write for a hand-written ZTL without calling Prompt 2 (no rewrite, no `ConnectRejected`).

## Consequences

A wrong analogy stays a suggestion. Graph expansion and hub ranking only see endorsed edges. The cost is one extra embedding of category labels per `connect` run plus a small second search.

## References

* GitHub issues #161, #165
* `zettel/retrieval.py` (`search_distant_analogies`), `zettel/connector.py` (`_search_distant_for_candidate`, `_write_distant_suggestions`)
* `zettel/manual_lit.py` (`suggest_connections_for_permanent`)
