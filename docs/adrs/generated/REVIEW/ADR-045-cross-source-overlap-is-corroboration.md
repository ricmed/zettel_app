# ADR-045: Cross-Source Overlap Is Corroboration, Not Duplication

**Status:** Accepted
**Date:** 2026-09-07
**Supersedes (in part):** [ADR-016](./ADR-016-post-approval-concept-deduplication-timing.md) — its *scope*, not its timing
**Related to:** [ADR-009](../RETRIEVAL/ADR-009-graph-based-note-discovery-weighted-bfs.md), [ADR-034](../EXTRACT/ADR-034-optional-author-judgement-fields.md), [ADR-043](../RETRIEVAL/ADR-043-distant-analogies-as-suggestions.md)

## Context and Problem Statement

Concept dedupe compared each candidate against the whole `permanent_notes` collection: `idx.query_similar_notes` has no `source_id` filter. A candidate whose nearest note came from a *different* source was routed to the same four-way LLM decision as one repeating its own book, and an `ignore` verdict dropped it — the row survived as `status='duplicate'`, but the idea never became a note and nothing recorded which note had absorbed it.

For research this inverts the value. "Segundo Kahneman, e corroborado por Gigerenzer" is the product, not noise to collapse. A single note fed by two sources also loses the thing a thesis needs most: the ability to cite each author for their own claim.

**No ADR defended the global scope.** ADR-016 documents *when* dedupe runs (post-approval, so the LLM is only paid for human-approved chunks) and is silent on scope; its Context even describes candidate collection as "scoped to the source being reviewed". The globality fell out of `permanent_notes` being one collection — an implementation consequence, never a recorded decision.

Within one source the old behaviour is still right: a chunk that repeats itself is noise, and an author revisiting a concept as the book advances is what `refine_existing` already covers.

## Decision Outcome

**Chosen:** scope dedupe to one source; make cross-source overlap a typed edge derived by code.

1. **`extractor._same_source_notes`** filters `query_similar_notes` hits to the candidate's own `source_id` before the threshold check. A cross-source hit can never make a candidate redundant, so it **never reaches the LLM** — the decision's only admissible answer was already known from `source_id`. This is a net cost *reduction*, not a new call.
2. Within one source the four-way decision (`create_new` / `ignore` / `refine_existing` / `merge`) is unchanged. `prompts/dedupe_decision.md` states the scope so the model is not asked a question the pipeline no longer poses.
3. **`RelationType.CORROBORATES`** is new. `connector.links.corroborating_note_ids` reads the hits `Retriever.search_notes` **already returned** for the RAG context: search seeds (`hop == 0`) whose cosine similarity clears `linking.corroborates_min_similarity` (0.85) and whose note has a different `source_id`, capped at `linking.corroborates_max_edges` (3). Zero extra embeddings, zero extra LLM calls.
4. The row is persisted with **`origin='derived'`**, not `'llm'`. The edge was never proposed by a model, and that is precisely what licenses writing it as a real edge instead of an `auto-connections` suggestion — an audit querying `origin='llm'` must not sweep it up. Graph weighting is unaffected: only `manual` overrides the relation weight (ADR-009 amendment).
5. One row is written, not the symmetric pair. `graph.expand_notes` already iterates edges undirected and `rebuild_auto_backlinks` renders the inverse label (`corroborado por`) on the other note — exactly how `extends` works.

### Why the edge is not an LLM judgement

`corroborates` is deliberately **absent from `prompts/permanent_note.md`**, and `tests/test_prompts.py` pins that absence in both directions. Corroboration is a fact about *authorship* (two different `source_id`), not a judgement about content. Offered in the relation menu, the model would emit it rhetorically ("this note also agrees") and the signal would be indistinguishable from `supports`. `connector.links.demote_llm_corroborates` downgrades a model-emitted `corroborates` to `supports` at the LLM trust boundary — applied to `note_output.connections` only, *before* injection, so the edges this module asserts survive.

This is the ADR-034 pattern (judgement fields copied verbatim from the candidate, never routed through the LLM), and a deliberate carve-out from ADR-043: that ADR keeps distant analogies as suggestions because *connect writes edges without a human gate* and an LLM analogy is a guess. A `corroborates` edge is not a guess — it is derived deterministically from `source_id` plus a similarity threshold.

### Why the traversal weight is low

`DEFAULT_RELATION_WEIGHTS["corroborates"] = 0.45` — below `related` (0.5), on purpose.

Weight governs **traversal, not importance**. N sources on one idea form a clique of near-identical notes. That clique is exactly what a thesis writer wants to read, and the worst thing to spend `max_neighbors` slots on: with `config.yaml` at `max_hops: 5, max_neighbors: 16`, every such hop returns a paraphrase instead of new information. Worse, it is self-reinforcing — the next candidate's RAG context would show five restatements of one idea. ADR-009 already warns that all four retrieval consumers share one graph configuration, so a weight tuned for one degrades the others.

The relation earns its prominence in the rendered `## Conexões` and `auto-backlinks`, where a human reads it. `tests/test_config.py` pins `corroborates <= related`.

### Processing order

The edge is derived at `connect`, the latest possible point, from a search that already ran. If A was connected before B existed, B links B→A on its own run; traversal is undirected and the backlink renders on A. No retroactive sweep is needed.

## Consequences

An idea stated by two authors yields two notes and one typed edge, so each author can be cited for their own claim and the graph records the convergence. Cross-source candidates stop paying for a dedupe LLM call. `ignore` remains reachable and meaningful, now with a nameable cause: a repeat inside one work.

**Accepted without mitigation in code**, to be revisited only if observed: `gardener` cluster cohesion (computed over `note_connections`) is inflated by corroboration cliques, and `build_rag_context`'s graph-neighbour group can contain paraphrases. The fix, if needed, is to collapse a clique to one representative plus a count at those two context builders — not to change the weight, which is already the primary mitigation.

`connect` now reads `db.get_note` once per qualifying hit. Bounded by `linking.topk` (5) and only for hits above 0.85.

## References

* `zettel/extractor.py` (`_same_source_notes`, `deduplicate_candidates`)
* `zettel/connector/links.py` (`corroborating_note_ids`, `demote_llm_corroborates`, `assemble_connections`, `INVERSE_RELATION`)
* `zettel/schemas.py` (`RelationType.CORROBORATES`), `zettel/config.py` (`DEFAULT_RELATION_WEIGHTS`, `LinkingConfig`)
* `prompts/dedupe_decision.md` (scope note), `prompts/permanent_note.md` (deliberate omission)
* `tests/test_extractor.py`, `tests/test_connector.py`, `tests/test_prompts.py`, `tests/test_config.py`

## Amendment (2026-09-24): `refine_existing` now reaches `connect`

The Context above says an author revisiting a concept "is what `refine_existing` already covers". That was false in practice. `deduplicate_candidates` put `refines_note_id` on the candidate dict it held in memory, and `connect` injected an `extends` edge when it found that key. The two phases talk only through SQLite, though, and the target was never persisted: `load_approved_candidates` rebuilt the dicts without it. Every `refine_existing` / `merge` verdict therefore behaved as `create_new`, and the connector branch was dead code.

The fix keeps the boundary where it is and sends the verdict through it:

1. On approval, `extractor.deduplicate_candidates` writes `{refines_note_id, reason}` to `concepts.dedupe_json`. Every other approval still passes `dedupe=None`, so an `override` the reviewer set is preserved.
2. `connector.run.load_approved_candidates` reads that payload back onto the candidate.
3. `connector.links.assemble_connections` injects `extends` towards the target unless the model already linked that note. Precedence is: model edges (with `corroborates` demoted), then `extends`, then `corroborates`.

The edge is written with `origin='llm'`, unlike `corroborates`. It is a model's judgement (the dedupe prompt's), not a fact derived from `source_id`, and an audit of LLM-asserted edges should include it. A target id the model made up is harmless: `resolve_connections` normalizes it and drops any note that does not exist on disk.

Covered by `tests/test_extractor.py::test_dedupe_same_source_can_still_refine` and `tests/test_connector.py::test_run_connect_links_refinement_and_writes_suggestions_once`.
