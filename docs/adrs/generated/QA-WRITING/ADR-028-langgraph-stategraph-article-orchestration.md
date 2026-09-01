# ADR-028: LangGraph StateGraph for Article Orchestration

**Status:** Accepted
**Date:** 2026-09-01
**Depends on:** [ADR-003: Hybrid Dense+BM25 Retrieval with RRF Fusion and Absolute Relevance Floor](../INFRA/ADR-003-hybrid-dense-bm25-retrieval.md), [ADR-009: Graph-Based Note Discovery with Weighted BFS Expansion](../RETRIEVAL/ADR-009-graph-based-note-discovery-weighted-bfs.md), [ADR-010: Retrieval Result Transparency (Hits vs Candidates)](../RETRIEVAL/ADR-010-retrieval-result-transparency-hits-vs-candidates.md), [ADR-024: Pluggable Multi-Provider LLM Strategy](../LLM/ADR-024-multi-provider-llm-strategy.md), [ADR-025: System+Human Prompt Split for Provider-Agnostic Prompt Caching](../LLM/ADR-025-prompt-caching-system-human-split.md)
**Used by:** [ADR-029: Article Graph as Python Package](./ADR-029-article-graph-as-python-package.md)

## Context and Problem Statement

Every other phase of the pipeline (harvest, extract, review, connect, garden) is orchestrated by the
*staged pipeline* pattern: rows in SQLite carry a `status` column, each command selects the rows in
its input status, processes them forward, and checkpoints the new status. That pattern fits work that
moves in one direction over a large, resumable batch.

The `zettel article` command does not fit that shape. Producing one long-form article is a single
unit of work that must:

- **branch on human decisions** at two points — the operator reviews the retrieved context (and may
  inject extra search queries), then reviews the proposed outline (and may ask for a regeneration
  with feedback);
- **loop** — an LLM judge scores the assembled draft and, on `REJECTED`, sends feedback back into the
  drafting step for up to `max_judge_iterations` passes;
- **re-enter an earlier step with accumulated state** — the context-enrichment loop returns to the
  query enricher while keeping the notes already retrieved;
- **carry a wide, short-lived state** (~39 keys: queries, retrieved notes, outline, section bodies,
  judge scores, warnings) that has no value after the run and does not belong in SQLite.

Encoding loops, conditional re-entry, and mid-run human pauses as SQLite status transitions would
mean inventing a bespoke state machine, persisting intermediate artifacts that are never read again,
and hand-rolling the resume protocol.

## Decision Drivers

* Two human-in-the-loop pause points that must suspend execution and resume with operator input.
* A bounded feedback loop (draft to judge to draft) that the staged pipeline cannot express.
* Conditional re-entry into an earlier step (context enrichment) preserving accumulated state.
* Wide, ephemeral run state that should not be persisted as pipeline rows.
* The same orchestration must serve both an interactive CLI (Rich prompts) and non-interactive
  callers (tests, scripts) without two separate control flows.
* LangGraph was already an accepted dependency (`langgraph>=0.2.0` in `pyproject.toml`).

## Considered Options

* LangGraph `StateGraph` with `interrupt()` and a `MemorySaver` checkpointer (chosen)
* Staged pipeline with SQLite status fields, as used by harvest through garden
* A hand-rolled state machine or async generator yielding at each HITL point

## Decision Outcome

Chosen option: "LangGraph `StateGraph` with `interrupt()` and a `MemorySaver` checkpointer", because
the article's control flow is a graph with cycles and pause points, and LangGraph provides exactly
those primitives — conditional edges for branching, ordinary edges for loops, and `interrupt()` /
`Command(resume=...)` for suspending and resuming a run — without the project having to build and
test its own suspend/resume protocol.

The graph is defined in `build_article_graph()` with **13 nodes** and **3 conditional routers**:

```
START
  |
  v
query_enricher <----------------+
  |                             | enrich
  v                             |
vector_search_merge             |
  |                             |
  v                             |
context_review --- route_after_context (enrich | catalog | end)
  |                             |
  | catalog                     +--> abort --> END
  v
build_catalog
  |
  v
generate_outline <--------------+
  |                             | outline
  v                             |
outline_review --- route_after_outline (outline | draft | outline_only_finish | end)
  |                    |                 |
  | draft              |                 +--> outline_only_finish --> END
  v                    +--> abort --> END
draft_sections <----------------+
  |                             | redraft
  v                             |
assemble                        |
  |                             |
  v                             |
personality                     |
  |                             |
  v                             |
judge ------------- route_after_judge (redraft | finish | finish_with_warning)
                                |
                                +--> finish --> END
```

Both HITL nodes are dual-path by design: when a caller supplies a Python callback
(`context_callback` / `approve_outline`) the node calls it directly; otherwise it raises
`interrupt()` and the runner resolves it through a `hitl_handler`. This is what lets the interactive
CLI and non-interactive tests drive one graph.

A fresh `MemorySaver` and a per-run `thread_id` (`article-<uuid12>`) are created for each invocation.
The checkpointer exists to make `interrupt()` and resume work within a single process; article runs
are deliberately **not** persisted across processes.

Scope boundary: `zettel article` is **CLI-only**. The web interface does not enqueue article jobs —
`web.py` and `web_app.py` contain no article code path.

## Pros and Cons of the Options

### LangGraph StateGraph with interrupt() (chosen)

* Good, because loops, conditional branches, and HITL pauses are first-class primitives rather than
  hand-built control flow.
* Good, because `interrupt()` plus `Command(resume=...)` gives a tested suspend/resume protocol the
  project does not have to own.
* Good, because the topology is declared in one function, so the pipeline can be read as a graph
  instead of inferred from scattered status transitions.
* Good, because the callback/interrupt duality lets tests drive the same graph the CLI drives.
* Bad, because it introduces a second orchestration pattern in a codebase that otherwise uses the
  staged pipeline, so contributors must know both.
* Bad, because it adds a dependency whose pre-1.0 API has moved before (`interrupt()` and `Command`
  are relatively recent), so upgrades need care.

### Staged pipeline with SQLite status fields

* Good, because it reuses the pattern every other phase already uses — one orchestration model to learn.
* Good, because runs would be resumable across processes for free.
* Bad, because loops and conditional re-entry would need a bespoke state machine on top of status columns.
* Bad, because it would persist wide, ephemeral state (outline drafts, section bodies, judge scores)
  that is never read after the run.
* Bad, because suspending mid-run for human input would require inventing a resume protocol.

### Hand-rolled state machine or async generator

* Good, because it adds no dependency and the control flow is fully owned by the project.
* Good, because a generator yielding at each HITL point is a natural Python idiom.
* Bad, because suspend/resume, branching, and loop-bound logic would all become project-maintained code.
* Bad, because the topology would be implicit in the call structure rather than declared in one place.

## Consequences

The codebase now carries two orchestration patterns. The boundary is: **batch work that moves forward
over many rows uses the staged pipeline; a single interactive unit of work with loops and human
pauses uses the article graph.** This ADR is the reference for that boundary.

`langgraph` must be version-pinned with intent. `interrupt()`, `Command`, and `MemorySaver` are the
three surfaces the project depends on; an upgrade that changes any of them breaks `zettel article`
and nothing else.

Because `MemorySaver` is in-process and re-created per call, an article run cannot be resumed after
the process exits — an operator who abandons a run loses it. This is accepted: an article run is
minutes long and cheap to restart, unlike a harvest batch.

LLM cost accounting flows through the shared mechanisms: the runner opens a `runs` row
(`db.start_run("article")`, `begin_run`, `finish_pipeline_run`), and every node's LLM call goes
through `zettel/article.py` helpers that use `get_llm(cfg, "article")` per ADR-024 and the
deterministic `llm_cache` per ADR-025. The judge loop's redraft calls carry different feedback, so
they produce different call checksums and legitimately miss the cache.

Article output is written to `00_Inbox/ART - ....md` and is deliberately **not** indexed into
ChromaDB — an article is a derived artifact, not a note in the graph.

The node functions are thin adapters over domain helpers in `zettel/article.py`; the graph module
owns topology and state, not writing logic. ADR-029 formalizes that separation as a package.

## References

* zettel/article_graph.py — `build_article_graph()`, the 13-node topology and 3 conditional routers
* zettel/article_graph.py — `run_article_graph()`, compiles with `MemorySaver` and drives the interrupt/resume loop
* zettel/article_graph.py — `node_context_review` / `node_outline_review`, the two dual-path HITL nodes
* zettel/article_graph.py — `route_after_context`, `route_after_outline`, `route_after_judge`
* zettel/article.py — domain helpers (outline, drafting, assembly, personality, judge, verification)
* zettel/cli.py — `article` command and its Rich-based `_hitl` handler
* config/config.yaml — `retrieval.article` section (`max_judge_iterations`, `judge_min_score`, `max_sections`)
