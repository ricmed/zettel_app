"""Topology and runner for the `zettel article` LangGraph pipeline.

StateGraph: enrich queries -> incremental search -> context HITL -> catalog ->
outline HITL -> draft sections -> assemble -> personality -> judge loop -> verify.

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

See ADR-028 for the rationale and ADR-029 for this package's module layout.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from .. import article as art
from .nodes import (
    node_abort,
    node_assemble,
    node_build_catalog,
    node_context_review,
    node_draft_sections,
    node_finish,
    node_generate_outline,
    node_judge,
    node_outline_only_finish,
    node_outline_review,
    node_personality,
    node_query_enricher,
    node_vector_search_merge,
    route_after_context,
    route_after_judge,
    route_after_outline,
)
from .runtime import (
    ArticleGraphState,
    ArticleRuntime,
    ContextCallback,
    OutlineCallback,
    build_initial_state,
    resolve_run_options,
    result_from_state,
)

if TYPE_CHECKING:
    from ..config import AppConfig
    from ..index import VectorIndex
    from ..state import StateDB


def build_article_graph():
    g = StateGraph(ArticleGraphState)
    g.add_node("query_enricher", node_query_enricher)
    g.add_node("vector_search_merge", node_vector_search_merge)
    g.add_node("context_review", node_context_review)
    g.add_node("build_catalog", node_build_catalog)
    g.add_node("generate_outline", node_generate_outline)
    g.add_node("outline_review", node_outline_review)
    g.add_node("outline_only_finish", node_outline_only_finish)
    g.add_node("draft_sections", node_draft_sections)
    g.add_node("assemble", node_assemble)
    g.add_node("personality", node_personality)
    g.add_node("judge", node_judge)
    g.add_node("finish", node_finish)
    g.add_node("abort", node_abort)

    g.add_edge(START, "query_enricher")
    g.add_edge("query_enricher", "vector_search_merge")
    g.add_edge("vector_search_merge", "context_review")
    g.add_conditional_edges(
        "context_review",
        route_after_context,
        {"enrich": "query_enricher", "catalog": "build_catalog", "end": "abort"},
    )
    g.add_edge("build_catalog", "generate_outline")
    g.add_edge("generate_outline", "outline_review")
    g.add_conditional_edges(
        "outline_review",
        route_after_outline,
        {
            "outline": "generate_outline",
            "draft": "draft_sections",
            "outline_only_finish": "outline_only_finish",
            "end": "abort",
        },
    )
    g.add_edge("outline_only_finish", END)
    g.add_edge("draft_sections", "assemble")
    g.add_edge("assemble", "personality")
    g.add_edge("personality", "judge")
    g.add_conditional_edges(
        "judge",
        route_after_judge,
        {
            "redraft": "draft_sections",
            "finish": "finish",
            "finish_with_warning": "finish",
        },
    )
    g.add_edge("finish", END)
    g.add_edge("abort", END)
    return g


def run_article_graph(
    cfg: AppConfig,
    db: StateDB,
    idx: VectorIndex,
    topic: str,
    style: art.ArticleStyle = "blog",
    topk: int | None = None,
    use_graph: bool | None = None,
    mode: str | None = None,
    outline_only: bool = False,
    approve_outline: OutlineCallback | None = None,
    personality: str | None = None,
    custom_style_notes: str | None = None,
    skip_context_review: bool = False,
    skip_judge: bool = False,
    max_judge_iterations: int | None = None,
    context_callback: ContextCallback | None = None,
    hitl_handler: Callable[[dict], dict] | None = None,
) -> art.ArticleResult:
    """Compile and run the article StateGraph to completion.

    When ``hitl_handler`` is set, LangGraph interrupts are resolved by calling
    it with the interrupt payload and resuming with its return value.
    Callbacks (``context_callback`` / ``approve_outline``) bypass interrupts.
    """
    from zettel.usage import begin_run, finish_pipeline_run

    run_id = db.start_run("article")
    begin_run(run_id)

    options = resolve_run_options(
        cfg,
        approve_outline=approve_outline,
        context_callback=context_callback,
        hitl_handler=hitl_handler,
        skip_context_review=skip_context_review,
        skip_judge=skip_judge,
        outline_only=outline_only,
        use_graph=use_graph,
    )

    rt = ArticleRuntime(
        cfg=cfg,
        db=db,
        idx=idx,
        context_callback=context_callback,
        outline_callback=options.outline_callback,
    )

    graph = build_article_graph().compile(checkpointer=MemorySaver())
    thread_id = f"article-{uuid.uuid4().hex[:12]}"
    config: dict[str, Any] = {
        "configurable": {"thread_id": thread_id, "runtime": rt},
    }

    initial = build_initial_state(
        topic,
        style,
        cfg,
        options,
        personality=personality,
        custom_style_notes=custom_style_notes,
        topk=topk,
        mode=mode,
        outline_only=outline_only,
        max_judge_iterations=max_judge_iterations,
    )

    try:
        result_state: dict = graph.invoke(initial, config)

        # Resolve interrupts via HITL handler (CLI)
        while result_state.get("__interrupt__"):
            if hitl_handler is None:
                # Auto-approve if somehow interrupted without handler
                resume_val: dict = {"context_decision": "approve", "outline_decision": "approve"}
                ints = result_state["__interrupt__"]
                if ints and getattr(ints[0], "value", None):
                    itype = (ints[0].value or {}).get("type")
                    if itype == "outline_review":
                        resume_val = {"outline_decision": "approve", "outline_feedback": ""}
                    else:
                        resume_val = {"context_decision": "approve", "extra_queries": []}
                result_state = graph.invoke(Command(resume=resume_val), config)
                continue
            ints = result_state["__interrupt__"]
            payload = ints[0].value if ints and getattr(ints[0], "value", None) else {}
            resume_val = hitl_handler(payload if isinstance(payload, dict) else {})
            result_state = graph.invoke(Command(resume=resume_val), config)

        return result_from_state(result_state, rt, topic, style)
    finally:
        finish_pipeline_run(db, run_id)
