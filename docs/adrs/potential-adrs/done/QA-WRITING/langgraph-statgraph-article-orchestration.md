# Potential ADR: LangGraph StateGraph for Multi-Stage Article Orchestration

**Module**: QA-WRITING
**Category**: Orchestration Architecture / State Management
**Priority**: Consider (Score: 65)
**Date Identified**: 2026-08-30

---

## What Was Identified

The `zettel article` command implements a complex, multi-stage writing pipeline via a **LangGraph StateGraph** (13 nodes) rather than the simpler staged-pipeline pattern (SQLite status fields) used by phases 1-4 (harvest → extract → review → connect → garden).

### The Article State Machine

The graph flows through these nodes:
1. **Query Enricher** — expand topic into multiple search queries
2. **Incremental Vector Search** — run queries sequentially, merge results by note_id
3. **Context Review HITL** — human approves/modifies retrieved context
4. **Catalog Assembly** — build source/asset metadata from context
5. **Outline Generation** — LLM produces `ArticleOutline` (Pydantic schema)
6. **Outline Review HITL** — human approves/regenerates outline with feedback loop
7. **Per-Section Drafting** — LLM writes individual section bodies
8. **Section Assembly** — merge drafts, inject citations, format figures
9. **Personality Rewrite** — optional LLM call to apply writing style
10. **Judge Loop** — LLM scores article quality, collects feedback, max N iterations
11. **Final Assembly** — render frontmatter + body, compute checksums
12. **Verification** — pre-save validation

This pattern was introduced in commit 64c5346 (2026-08-04, "Add article generation capabilities and enhance retrieval process") and has been stable for 26 days with only minor enhancements (aborted state handling, prompt caching portable hints).

### Why LangGraph Over Staged Pipeline?

The article feature requires:
- **Complex branching**: HITL interrupts at outline and context stages
- **Iterative refinement**: Judge loop allows N passes over draft/feedback
- **State persistence**: MemorySaver checkpointer for resumable sessions
- **Async-friendly**: LangGraph works natively with async/await (used by web job queue)

The staged pipeline (SQL status fields) works well for *forward-moving* phases but is awkward for *loops* and *HITL interrupts*. LangGraph's `interrupt()` primitive is a natural fit for "pause here, get human input, resume with updated state."

---

## Why This Might Deserve an ADR

- **Architectural Divergence**: Introduces a new orchestration pattern distinct from the existing staged pipeline (all other phases use SQLite status fields + linear progression)
- **Team Knowledge Requirement**: Anyone extending the article feature must understand LangGraph semantics (nodes, edges, state, interrupts, MemorySaver)
- **Cost to Change**: Rewriting the orchestration layer would affect ~2,000 lines (article.py + article_graph.py) plus CLI interrupt handling and web job dispatch integration
- **Temporal Stability**: 26 days in production; only minor fixes since introduction (no regressions or rollbacks)
- **CLI Integration Complexity**: The `interrupt()` primitive integrates with Rich prompts for HITL, requiring careful signal handling

### Evidence Found in Codebase

#### Key Files
- `zettel/article_graph.py` (715 lines) — StateGraph definition, node implementations
- `zettel/article.py` (1161 lines) — domain helpers: outline generation, section drafting, assembly, judge loop
- `zettel/config.py` (ArticleConfig section) — tuning parameters (max_sections, chars_per_section_draft, max_judge_iterations)

#### Code Evidence: StateGraph Definition

From `article_graph.py:206-250` (graph construction):
```python
graph = StateGraph(ArticleGraphState)

graph.add_node("query_enricher", node_query_enricher)
graph.add_node("vector_search_merge", node_vector_search_merge)
graph.add_node("context_review", node_context_review)
graph.add_node("catalog", node_catalog)
graph.add_node("outline_generate", node_outline_generate)
graph.add_node("outline_review", node_outline_review)
graph.add_node("sections_draft", node_sections_draft)
graph.add_node("assemble_draft", node_assemble_draft)
graph.add_node("personality", node_personality)
graph.add_node("judge_loop", node_judge_loop)
graph.add_node("verify", node_verify)

# Edges define the flow and conditional routing
graph.add_edge(START, "query_enricher")
graph.add_conditional_edges("outline_review", _route_outline_decision, ...)
graph.add_conditional_edges("judge_loop", _judge_loop_router, ...)
# ... more edges
```

#### HITL Interrupt Pattern
From `article_graph.py:300-320` (context review node):
```python
def node_context_review(state: ArticleGraphState, config: RunnableConfig) -> dict:
    rt = _rt(config)
    if rt.context_callback:
        # Test/CLI path: callback-driven
        state_update = rt.context_callback({"catalog": rt.catalog})
    else:
        # Interactive path: pause execution, get human input
        interrupt(value={"please_review": rt.catalog})
        state_update = ...  # resumed by caller with updated state
    return state_update
```

This `interrupt()` call pauses graph execution, returns to the CLI, shows the catalog to the human, and resumes when the human provides feedback. This is a LangGraph-specific pattern not available in the staged pipeline approach.

#### Impact Analysis
- **Introduced**: 2026-08-04 16:31:38 (commit 64c5346)
- **Modified**: 5 times since introduction:
  - 2026-08-04 16:46:00 — Aborted state handling
  - 2026-08-05 00:00:32 — Literature review enhancements
  - 2026-08-13 20:38:12 — Prompt caching hints
  - 2026-08-25 19:31:51 — Pipeline enforcement (unrelated)
  - 2026-08-26 20:18:28 — MOC link fixes (unrelated)
- **Commit Themes**: Core pattern stable; enhancements focused on state handling and prompt caching

### Consumers
- **CLI** (`cli.py` article command) — uses `run_article()` with `approve_outline` callback + `skip_*` flags
- **Web** (`web_app.py`) — enqueues article job, converts HTTP params to graph config
- **Tests** (`test_article.py`, `test_article_graph.py`) — mock callbacks, test node logic, verify state transitions

---

## Questions to Address in ADR (if created)

1. **Why LangGraph specifically?** (vs. async generators, coroutines, explicit state machines)
2. **How does this differ from the staged pipeline?** (when is each pattern appropriate)
3. **What are the long-term maintenance implications?** (LangGraph API stability, version pinning)
4. **How do interrupts interact with web async/job queue architecture?**
5. **Could the judge loop be moved to a separate utility?** (to avoid deep nesting)

---

## Related Potential ADRs

- **RETRIEVAL/graph-based-note-discovery-weighted-bfs** — Graph expansion is used in article search phase
- **RETRIEVAL/retrieval-result-transparency-hits-vs-candidates** — Articles expose candidates for context HITL
- **INFRA/hybrid-dense-bm25-retrieval** — Articles use Retriever for search

---

## Additional Notes

- **Test coverage**: `test_article.py` covers outline generation, section drafting, and judge loop logic with 20+ test cases
- **Unresolved interaction**: How does the judge loop's iterative refinement interact with the prompt cache? (LLM responses cached per call checksum; refinements may have different checksums)
- **Bibliography integration**: ABNT citation formatting is handled within the assembly phase, not in the orchestration layer itself
