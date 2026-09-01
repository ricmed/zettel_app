# ADR-029: Article Graph as Python Package

**Status**: Accepted (2026-09-01)  
**Depends on**: ADR-028  
**Relates to**: ADR-027, ADR-003, ADR-009, ADR-024, ADR-025

## Context

`zettel/article_graph.py` has grown to 716 lines that mix four unrelated concerns in one file:

- **State contract** — `ArticleGraphState`, a 39-key `TypedDict` with no grouping, plus `ArticleRuntime`
- **Retrieval domain logic** — `node_vector_search_merge` (~119 lines): the multi-query search loop,
  the MOC boost, extra graph hops, and a 20-key `retrieval_params` snapshot. This is the only part of
  the file that carries real business logic.
- **Node adapters** — twelve `node_*` functions of ~20 lines each, nearly all thin wrappers over
  helpers in `zettel/article.py`
- **Wiring and runner** — `build_article_graph()` (52 lines) and `run_article_graph()` (112 lines:
  option resolution, the initial-state literal, and the HITL interrupt/resume loop)

The cost is readability. The graph topology declared in `build_article_graph()` — the single most
useful thing to read when working on this pipeline (ADR-028) — sits below ~500 lines of node bodies.
An LLM agent asked to change one node must load the entire file to find it.

This is the same problem ADR-027 solved for `zettel/harvester.py` (1776 lines to a package of 8
modules, none over 640 lines). That ADR explicitly **rejected** flat sibling files (`harvest_*.py`):
*"Siblings in flat namespace don't scale; import hell for circular deps."* The same reasoning applies
here, so the same solution applies.

## Decision

Convert `zettel/article_graph.py` into the package `zettel/article_graph/` with 4 focused modules:

```
zettel/article_graph/
├── __init__.py    # Public API re-exports (~40 lines)
├── runtime.py     # Graph state, runtime, option resolution, result mapping (~160 lines)
├── search.py      # Retrieval logic extracted from node_vector_search_merge (~150 lines)
├── nodes.py       # 13 node_* functions + 3 route_after_* routers (~250 lines)
└── graph.py       # build_article_graph + run_article_graph (~120 lines)
```

`runtime.py` is deliberately not named `state.py`: package modules import both this module and
`zettel/state.py` (`StateDB`), and two things called "state" in one import block is a readability
regression, not an improvement.

### Public API (from `__init__.py`)

Unchanged for every existing caller:

```python
from zettel.article_graph import (
    run_article_graph,
    build_article_graph,
    ArticleGraphState,
    ArticleRuntime,
)
```

`zettel/cli.py` and `zettel/article.py` need no edit.

### Submodule Responsibilities

| Module | Exports | Key Dependencies |
|--------|---------|------------------|
| **runtime.py** | `ArticleGraphState`, `ArticleRuntime`, `RunOptions`, `ContextCallback`, `OutlineCallback`, `runtime_from`, `mark_llm`, `resolve_run_options`, `build_initial_state`, `result_from_state` | config, article, schemas |
| **search.py** | `run_pending_queries`, `apply_moc_boost`, `expand_extra_hops`, `snapshot_retrieval_params` | config, state, retrieval, graph, article |
| **nodes.py** | 13 `node_*` functions, `route_after_context`, `route_after_outline`, `route_after_judge` | runtime, search, article, schemas |
| **graph.py** | `build_article_graph`, `run_article_graph` | nodes, runtime, usage, langgraph |

### Node-Level Decomposition

`node_vector_search_merge` is the only node with real logic. It is decomposed into four pure
functions in `search.py` that take explicit arguments instead of a `RunnableConfig`:

| Function | Replaces (old line range) |
|---|---|
| `run_pending_queries(...)` | 139–161 — multi-query search loop with progress logging |
| `apply_moc_boost(...)` | 163–174 — one-time MOC neighbourhood boost |
| `expand_extra_hops(...)` | 176–211 — extra BFS hops, neighbour hydration, re-ranking |
| `snapshot_retrieval_params(...)` | 213–230 — the 20-key parameter snapshot |

The node itself becomes ~25 lines of composition. Being free of `RunnableConfig`, these functions are
directly unit-testable without constructing a graph.

`run_article_graph` sheds its two densest blocks to `runtime.py`: `resolve_run_options()` (old lines
634–649: the auto-approve-outline default, the force-skip of context review, and
`skip_judge or outline_only`) and `build_initial_state()` (old lines 665–690: the 25-key literal).
What remains is ~45 lines of readable sequence.

### Import Rules

**Submodules must:**
- Import siblings via relative imports: `from . import search`
- Import external modules via absolute imports: `from zettel.config import AppConfig`
- Never import back from `graph.py` — the DAG is `runtime` to `search` to `nodes` to `graph`
- Never expose private helpers in `__init__.py`

**All LLM calls must continue to route through `zettel/article.py`.** No module in this package may
import `call_llm` or `get_llm` from `zettel/llm.py` directly. `zettel/article.py` holds those as
module globals and the test suite monkeypatches them there; a direct import would silently bypass
every LLM test seam.

## Consequences

### Advantages

✓ **Topology is findable**: `graph.py` opens with the pipeline diagram and the node wiring  
✓ **Progressive disclosure**: an agent editing one node loads ~250 lines, not 716  
✓ **Testability**: the four `search.py` functions are pure and testable without a compiled graph  
✓ **Readable state**: `ArticleGraphState`'s 39 keys are grouped by phase with section comments  
✓ **Clean seams**: the two `# noqa: SLF001` accesses into `zettel/article.py` internals are removed  
✓ **Consistency**: same package pattern as `zettel/harvester/` (ADR-027)

### Trade-offs

⚠ **One test target moves**: `tests/test_article_graph.py` monkeypatches
`zettel.article_graph.build_article_graph`; because `run_article_graph` resolves that name as a
module global, the patch target becomes `zettel.article_graph.graph.build_article_graph`. This is the
same "test migration" trade-off ADR-027 accepted.  
⚠ **More files to navigate** for a reader who wanted one file  
⚠ **Circular import risk**: low, mitigated by the one-directional DAG and relative sibling imports

### No Architectural Changes

Behaviour and public API are identical:
- `from zettel.article_graph import run_article_graph` still works
- `run_article_graph` keeps its full signature, defaults, and semantics
- Graph topology is unchanged: same 13 nodes, same node names, same edges, same routers
- `zettel/cli.py` is untouched; the Rich `_hitl` handler stays where it is
- No compatibility shims or legacy aliases are introduced

### Companion Change in `zettel/article.py`

Two private names reached from the graph are promoted to the public surface, removing both
`# noqa: SLF001` suppressions:

- `_merge_moc_notes` becomes `merge_moc_notes` (no other callers)
- `_NO_EVIDENCE` becomes `NO_EVIDENCE` (3 references)

`zettel/article.py` is otherwise unchanged; splitting it is tracked separately as backlog.

### File Deletion Order (Critical on Windows)

A package directory takes precedence over a same-named module, so the two can coexist during the
transition:

```bash
# Step 1: Commit the complete package first
git add zettel/article_graph/
git commit -m "Extract article_graph package modules"

# Step 2: Update the monkeypatch target
git add tests/test_article_graph.py
git commit -m "Update article_graph test patch target for package layout"

# Step 3: Only then delete the old monolithic module
git rm zettel/article_graph.py
git commit -m "Remove monolithic article_graph.py (migrated to package)"
```

Reason: Windows file locks; git tracking clarity.

## Alternatives Considered

1. **Keep the monolith, add section comments** (~716 lines)  
   - Rejected: does not reduce what an agent must load, and the topology stays buried

2. **Flat sibling modules** (`article_nodes.py`, `article_state.py`, `article_search.py`)  
   - Rejected: ADR-027 already evaluated and rejected this shape for the same reason — a flat
     namespace does not scale and invites circular imports. It would also scatter five `article*`
     files across the top level of `zettel/`.

3. **Merge the nodes into `zettel/article.py`**  
   - Rejected: `article.py` is already 1163 lines, and it would put orchestration and domain logic
     back in one file — the opposite of ADR-028's separation

4. **Split `article.py` in the same change**  
   - Rejected for this ADR: doubles the blast radius and touches the `call_llm` / `get_llm` test
     seam. Tracked as separate backlog.

## Acceptance Criteria

- [ ] `zettel/article_graph.py` deleted; `zettel/article_graph/` package exists with 5 files
- [ ] No file in the package exceeds ~250 lines
- [ ] Public API maintained: `from zettel.article_graph import run_article_graph, build_article_graph` works
- [ ] `run_article_graph` signature, defaults and behaviour unchanged; `zettel/cli.py` untouched
- [ ] Graph topology byte-equivalent: same 13 node names, same edges, same 3 routers
- [ ] `node_vector_search_merge` reduced to composition over `search.py`
- [ ] No module in the package imports `call_llm` / `get_llm` from `zettel/llm.py`
- [ ] Both `# noqa: SLF001` suppressions removed; `merge_moc_notes` / `NO_EVIDENCE` are public
- [ ] `tests/test_article_graph.py` patch target updated; all article tests pass
- [ ] Full suite passes; `ruff check zettel/article_graph/` clean
- [ ] No circular imports; DAG validated
- [ ] CLAUDE.md, RUNBOOK.md and code-review-adr-checklist.md describe the package

## Related ADRs

- **ADR-028** (LangGraph orchestration): the decision this package structures; `graph.py` is its home
- **ADR-027** (harvest package): the precedent — same problem, same solution, same import rules
- **ADR-003 / ADR-009 / ADR-010** (hybrid retrieval, graph expansion, hits vs candidates): the
  behaviour `search.py` composes; thresholds and floors are unchanged by this refactor
- **ADR-024 / ADR-025** (multi-provider LLM, prompt cache split): preserved by the rule that all LLM
  calls route through `zettel/article.py`

## Timeline

- Phase 1 (`runtime.py` + `search.py`): ~2 hours
- Phase 2 (`nodes.py`): ~1.5 hours
- Phase 3 (`graph.py` + `__init__.py` + delete monolith): ~1 hour
- Phase 4 (tests, docs, index updates): ~1.5 hours
- **Total: ~6 hours**

---

**Decided by**: Ricardo Medeiros  
**Date**: 2026-09-01
