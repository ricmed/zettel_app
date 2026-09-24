# ADR-053: Connect Phase as Python Package

**Status**: Accepted (2026-09-24)  
**Depends on**: [ADR-027](../HARVEST/ADR-027-harvest-phase-as-python-package.md), [ADR-032](../CLI/ADR-032-cli-as-python-package.md), [ADR-039](../WEB/ADR-039-web-as-python-package.md)  
**Relates to**: [ADR-029](../QA-WRITING/ADR-029-article-graph-as-python-package.md), [ADR-043](../RETRIEVAL/ADR-043-distant-analogies-as-suggestions.md), [ADR-045](../REVIEW/ADR-045-cross-source-overlap-is-corroboration.md), [ADR-051](../QA-WRITING/ADR-051-citation-provenance-on-permanent-note.md)

## Context

`zettel/connector.py` had reached 1135 lines. Most of that sat in one function: `_process_candidate` (~340 lines) did all of the following in sequence:

- decided which note to write to
- resolved the citation and the images
- ran the RAG and distant-analogy searches
- filled Prompt 2 and ran it through the SQLite cache
- applied the PT-BR guard
- computed the per-note cost
- assembled typed connections
- wrote the vault file, the SQLite row and the embedding
- rebuilt backlinks

It also called `clear_progress()` from five different exits. The ADR overview had deferred any CONNECT decision "pending connector refactor".

Reading the monolith closely turned up problems that the size had hidden:

- **Dead branch, real bug.** The `extends` edge for a `refine_existing` / `merge` dedupe verdict read `refines_note_id` from a dict that never crossed the review/connect boundary. Fixed as recorded in the [ADR-045 amendment](../REVIEW/ADR-045-cross-source-overlap-is-corroboration.md#amendment-2026-09-24-refine_existing-now-reaches-connect).
- **Double writes.** The chunk row was loaded twice for the same candidate. A note with distant suggestions was written to SQLite twice, the second time by re-reading the file and copying every column back.
- **Duplication across modules.** The taxonomy fallback (`topics_path`, then `allowed_topics`) was copied in `gardener.py`. It is now `gardener_assign.category_pairs`.

## Decision

Convert `zettel/connector.py` into the package `zettel/connector/`:

```
zettel/connector/
├── __init__.py   public API re-exports (+ __all__)
├── run.py        load_approved_candidates (entry gate), run_connect, post-run refreshes
├── note.py       ConnectSession + process_candidate: one candidate -> one ZTL
├── prompt.py     ConnectRejected, prompt2_messages, cached Prompt 2 call, PT-BR guard
├── context.py    RAG context, taxonomy + distant analogies, images
└── links.py      typed relations, corroboration, edge assembly, backlinks
```

`process_candidate` is now a short orchestrator. `set_progress`/`clear_progress` wrap it in `try/finally`, and each step is a named function. The fixed inputs of a run (config, stores, LLM client, prompt, retriever, few-shots, taxonomy, origin) travel together in a frozen `ConnectSession` instead of twelve parameters.

### Public API

```python
from zettel.connector import (
    ConnectRejected,
    literature_ref_for_chunk,
    load_approved_candidates,
    load_connect_taxonomy,
    rebuild_auto_backlinks,
    run_connect,
    search_distant_analogies,
)
```

The CLI, the web worker and `sync.py` keep importing from the package. `manual_lit.py` used to import three private names (`_literature_ref_for_chunk`, `_load_connect_taxonomy`, `_search_distant_for_candidate`). Those are now public, because another module depends on them.

### Four rules (from ADR-032 / ADR-039)

1. **Siblings import by absolute path** (`from zettel.connector.links import ...`), which is ADR-032's deliberate deviation from ADR-027.
2. **Submodules never import symbols from `__init__`.** `from zettel.connector import context` names a submodule and is allowed. Importing a re-exported symbol would close a cycle.
3. **No submodule is named after an exported symbol.** Such a module would rebind the package attribute. `run.py` does not collide with `run_connect`.
4. **Monkeypatch the consuming module**, the same rule as ADR-039: `zettel.connector.prompt.call_llm`, `zettel.connector.run.get_llm`, `zettel.connector.context.load_connect_taxonomy`. `run.py` and `note.py` call `context.*` through the module attribute, so patching at the definition site takes effect.

`tests/test_connector_package.py` enforces rules 1–3 and the public API with AST checks. `tests/test_prompts.py` now finds Prompt 2's `mapping` literal in `connector/prompt.py:prompt2_messages`.

## Consequences

- Behaviour is unchanged except for three deliberate corrections:
  - `refine_existing` now produces its `extends` edge.
  - SQLite receives one `upsert_note` per ZTL, mirroring the file as it sits on disk, including `auto-connections` and the bumped `updated_at`.
  - `needs_ptbr_fix` counts English function words as whole words. The old substring match fired on PT-BR text such as "grande sandes ... mandioca".
- `persist_and_backlink` lost an unused `new_title` parameter, and `fallback_image_ids` takes the chunk row that was already loaded. Tests move to the new import and patch paths.
- No module exceeds ~380 lines. `note.py` is the ceiling.

## Alternatives

- **Keep one file and only split `_process_candidate`.** Rejected: the result would still be about 900 lines mixing five concerns, the same argument ADR-027 made.
- **Relative imports inside the package.** Rejected for the reason ADR-032 gives.
- **A shared LLM-cache helper in `zettel/llm.py`.** The cache-then-call pattern is repeated in extract, ask, article, summarize and bibliography. Worth doing, but it is a cross-module change and out of scope here.

## Acceptance Criteria

- [x] `zettel/connector.py` removed; `zettel/connector/` has 5 modules + `__init__`
- [x] Public API re-exported; CLI, web and sync callers unchanged
- [x] `tests/test_connector_package.py` locks the layout rules
- [x] The `refine_existing` target reaches `connect` (extractor + connector tests)
- [x] Full test suite green

## References

* `zettel/connector/__init__.py` — `__all__`
* `zettel/connector/run.py` — `load_approved_candidates`, `run_connect`
* `zettel/connector/note.py` — `ConnectSession`, `process_candidate`
* `zettel/connector/prompt.py` — `prompt2_messages`, `generate_permanent_note`, `apply_ptbr_guard`
* `zettel/connector/context.py` — `build_rag_context`, `load_connect_taxonomy`, `search_distant_analogies`
* `zettel/connector/links.py` — `assemble_connections`, `persist_and_backlink`, `rebuild_auto_backlinks`
* `zettel/gardener_assign.py` — `category_pairs`
* `tests/test_connector_package.py`, `tests/test_connector.py`, `tests/test_prompts.py`
