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
from dataclasses import dataclass
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


def _interrupt_payload(result_state: dict) -> dict:
    ints = result_state.get("__interrupt__") or []
    payload = ints[0].value if ints and getattr(ints[0], "value", None) else {}
    return payload if isinstance(payload, dict) else {}


def _auto_resume(payload: dict) -> dict:
    """Approve an interrupt when no handler was supplied."""
    if payload.get("type") == "outline_review":
        return {"outline_decision": "approve", "outline_feedback": ""}
    return {"context_decision": "approve", "extra_queries": []}


def _pause_marker(_payload: dict) -> dict:
    """Present so ``resolve_run_options`` does not skip the reviews.

    The web drive returns the interrupt instead of calling this.
    """
    raise RuntimeError("a pausa do artigo devolve o interrupt; o marcador não é chamado")


@dataclass
class ArticleStep:
    """One hop of an article run: a finished result or a human pause."""

    result: art.ArticleResult | None = None
    interrupt: dict | None = None


class ArticleDrive:
    """One article graph that can stop at an interrupt and resume later.

    The compiled graph and its ``MemorySaver`` stay on this object, in this
    process. The pipeline run stays ``running`` across a pause and is finished
    only when the graph ends or ``abandon`` is called. The caller owns ``db``
    and closes it.
    """

    def __init__(
        self,
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
        pause_for_review: bool = False,
    ):
        from zettel.usage import begin_run

        self.db = db
        self._topic = topic
        self._style = style
        self._closed = False
        self._suspended = None
        handler = hitl_handler
        if handler is None and pause_for_review:
            handler = _pause_marker
        options = resolve_run_options(
            cfg,
            approve_outline=approve_outline,
            context_callback=context_callback,
            hitl_handler=handler,
            skip_context_review=skip_context_review,
            skip_judge=skip_judge,
            outline_only=outline_only,
            use_graph=use_graph,
        )
        self._runtime = ArticleRuntime(
            cfg=cfg,
            db=db,
            idx=idx,
            context_callback=context_callback,
            outline_callback=options.outline_callback,
        )
        self._graph = build_article_graph().compile(checkpointer=MemorySaver())
        self._config: dict[str, Any] = {
            "configurable": {
                "thread_id": f"article-{uuid.uuid4().hex[:12]}",
                "runtime": self._runtime,
            },
        }
        self._initial = build_initial_state(
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
        self.run_id = db.start_run("article")
        begin_run(self.run_id)
        self._started = False

    def start(self) -> ArticleStep:
        if self._started:
            raise RuntimeError("o artigo já foi iniciado")
        self._started = True
        return self._step(self._graph.invoke(self._initial, self._config))

    def resume(self, value: dict) -> ArticleStep:
        from zettel.usage import resume_run

        resume_run(self._suspended)
        self._suspended = None
        return self._step(self._graph.invoke(Command(resume=value), self._config))

    def abandon(self) -> None:
        """Finish the run as failed. Does not close ``db``."""
        self._finish(failed=True)

    def _step(self, result_state: dict) -> ArticleStep:
        from zettel.usage import suspend_run

        if result_state.get("__interrupt__"):
            self._suspended = suspend_run()
            return ArticleStep(interrupt=_interrupt_payload(result_state))
        result = result_from_state(result_state, self._runtime, self._topic, self._style)
        self._finish()
        return ArticleStep(result=result)

    def _finish(self, *, failed: bool = False) -> None:
        if self._closed:
            return
        self._closed = True
        from zettel.usage import finish_pipeline_run, resume_run

        if self._suspended is not None:
            resume_run(self._suspended)
            self._suspended = None
        finish_pipeline_run(self.db, self.run_id, status="failed" if failed else "completed")


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
    The loop is :class:`ArticleDrive`; the CLI and the web share it.
    """
    drive = ArticleDrive(
        cfg,
        db,
        idx,
        topic,
        style=style,
        topk=topk,
        use_graph=use_graph,
        mode=mode,
        outline_only=outline_only,
        approve_outline=approve_outline,
        personality=personality,
        custom_style_notes=custom_style_notes,
        skip_context_review=skip_context_review,
        skip_judge=skip_judge,
        max_judge_iterations=max_judge_iterations,
        context_callback=context_callback,
        hitl_handler=hitl_handler,
    )
    try:
        step = drive.start()
        while step.interrupt is not None:
            decision = (
                _auto_resume(step.interrupt)
                if hitl_handler is None
                else hitl_handler(step.interrupt)
            )
            step = drive.resume(decision)
        if step.result is None:
            raise RuntimeError("o artigo terminou sem resultado")
        return step.result
    except Exception:
        drive.abandon()
        raise
