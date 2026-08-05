"""LangGraph orchestration for `zettel article` (v2).

StateGraph: enrich queries -> incremental search -> context HITL -> catalog ->
outline HITL -> draft sections -> assemble -> personality -> judge loop -> verify.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Literal, Optional, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from . import article as art
from . import graph as note_graph
from .retrieval import Retriever
from .schemas import ArticleOutline

if TYPE_CHECKING:
    from .config import AppConfig
    from .index import VectorIndex
    from .state import StateDB

logger = logging.getLogger(__name__)

ContextCallback = Callable[[dict], dict]
OutlineCallback = art.ApproveOutlineFn


class ArticleGraphState(TypedDict, total=False):
    topic: str
    style: Literal["blog", "academic"]
    personality: str
    custom_style_notes: str
    topk: int
    use_graph: bool
    mode: str
    outline_only: bool
    skip_context_review: bool
    skip_judge: bool
    max_judge_iterations: int

    search_queries: list[str]
    executed_queries: list[str]
    extra_queries: list[str]
    retrieved_notes: list[dict]
    moc_ids: list[str]
    retrieval_params: dict

    context_decision: str
    outline_decision: str
    outline_feedback: str

    outline: dict
    section_bodies: list[str]
    used_note_ids: list[str]
    draft_body: str
    styled_body: str
    judge_feedback: str
    judge_scores: dict
    iteration_count: int

    final_body: str
    frontmatter: dict
    warnings: list[str]
    cited_source_ids: list[str]
    no_evidence: bool
    aborted: bool
    llm_called: bool


@dataclass
class ArticleRuntime:
    cfg: "AppConfig"
    db: "StateDB"
    idx: "VectorIndex"
    catalog: Optional[art.ArticleCatalog] = None
    context_callback: Optional[ContextCallback] = None
    outline_callback: Optional[OutlineCallback] = None
    llm_called: bool = False


def _rt(config: RunnableConfig) -> ArticleRuntime:
    return config["configurable"]["runtime"]  # type: ignore[index]


def _mark_llm(rt: ArticleRuntime, called: bool) -> dict:
    if called:
        rt.llm_called = True
        return {"llm_called": True}
    return {}


# ── Nodes ──────────────────────────────────────────────────────────────


def node_query_enricher(state: ArticleGraphState, config: RunnableConfig) -> dict:
    rt = _rt(config)
    cfg = rt.cfg
    extras = list(state.get("extra_queries") or [])
    # After HITL enrich, only search the new extras (don't re-expand everything)
    if extras and state.get("executed_queries"):
        queries = extras
        called = False
    else:
        queries, called = art.enrich_search_queries(
            cfg, rt.db, state["topic"], state["style"],  # type: ignore[arg-type]
            extra_queries=extras or None,
        )
    out = {
        "search_queries": queries,
        "extra_queries": [],
        **_mark_llm(rt, called),
    }
    return out


def node_vector_search_merge(state: ArticleGraphState, config: RunnableConfig) -> dict:
    rt = _rt(config)
    cfg = rt.cfg
    art_cfg = cfg.retrieval.article
    topk = int(state.get("topk") or art_cfg.topk)
    mode = state.get("mode") or cfg.retrieval.mode
    use_graph = state.get("use_graph")
    if use_graph is None:
        use_graph = cfg.retrieval.graph_expansion.enabled

    retriever = Retriever(cfg, rt.db, rt.idx)
    existing = list(state.get("retrieved_notes") or [])
    executed = list(state.get("executed_queries") or [])
    queries = list(state.get("search_queries") or [])

    pending = [q for q in queries if q and q not in executed]
    total_q = len(pending)
    for i, q in enumerate(pending, 1):
        logger.info(
            "Busca [%d/%d] notas | query=%s",
            i,
            total_q,
            art.clip_text(q),
        )
        pool = retriever.search_notes(
            q, topk=topk, mode=mode, expand_graph=bool(use_graph)
        )
        existing = art.merge_retrieved_notes(
            existing, pool.hits, art_cfg.max_context_notes
        )
        executed.append(q)
        logger.info(
            "Busca [%d/%d] ok | hits=%d | pool acumulado=%d",
            i,
            total_q,
            len(pool.hits),
            len(existing),
        )

    # MOC boost once when we have some hits
    moc_ids = list(state.get("moc_ids") or [])
    if not moc_ids:
        moc = rt.db.find_moc_by_topic(state["topic"])
        if moc:
            moc_ids.append(moc["moc_id"])
            hits = [art.dict_to_retrieved_note(d) for d in existing]
            hits = art._merge_moc_notes(rt.db, hits, moc)  # noqa: SLF001
            existing = art.merge_retrieved_notes(
                [], hits, art_cfg.max_context_notes
            )

    # Extra graph hops
    gcfg = cfg.retrieval.graph_expansion
    if use_graph and existing and art_cfg.max_hops > gcfg.max_hops:
        seeds = [d for d in existing if int(d.get("hop") or 0) == 0] or existing[:topk]
        neighbors = note_graph.expand_notes(
            rt.db,
            seed_ids=[s["note_id"] for s in seeds],
            max_hops=art_cfg.max_hops,
            decay=gcfg.decay,
            relation_weights=gcfg.relation_weights,
            max_neighbors=gcfg.max_neighbors,
            seed_weights={s["note_id"]: float(s.get("score") or 0) for s in seeds},
        )
        by_id = {d["note_id"]: d for d in existing}
        for nid, neigh in neighbors.items():
            if nid in by_id:
                continue
            row = rt.db.get_note(nid)
            if not row:
                continue
            by_id[nid] = {
                "note_id": nid,
                "score": neigh.weight,
                "title": row.get("title") or "",
                "document": row.get("body") or "",
                "metadata": {
                    "source_id": row.get("source_id"),
                    "path": row.get("path"),
                },
                "hop": neigh.hop,
                "via": neigh.via,
                "passed_floor": True,
                "floor_reason": "vizinho de grafo (article max_hops)",
            }
        existing = sorted(
            by_id.values(), key=lambda x: float(x.get("score") or 0), reverse=True
        )[: art_cfg.max_context_notes]

    floor_cfg = cfg.retrieval.relevance_floor
    retrieval_params = {
        "mode": mode,
        "topk": topk,
        "max_context_notes": art_cfg.max_context_notes,
        "rrf_k": cfg.retrieval.rrf_k,
        "relevance_floor_enabled": floor_cfg.enabled,
        "min_vector_similarity": floor_cfg.min_vector_similarity,
        "absolute_min_similarity": floor_cfg.absolute_min_similarity,
        "bm25_hit_bypasses_floor": floor_cfg.bm25_hit_bypasses_floor,
        "bm25_bypass_max_rank": floor_cfg.bm25_bypass_max_rank,
        "graph_expansion_used": bool(use_graph),
        "graph_max_hops": max(gcfg.max_hops, art_cfg.max_hops if use_graph else 0),
        "graph_decay": gcfg.decay,
        "graph_max_neighbors": gcfg.max_neighbors,
        "moc_boost": bool(moc_ids),
        "executed_queries": list(executed),
    }

    no_evidence = not existing
    return {
        "retrieved_notes": existing,
        "executed_queries": executed,
        "search_queries": [],
        "moc_ids": moc_ids,
        "retrieval_params": retrieval_params,
        "no_evidence": no_evidence,
    }


def node_context_review(state: ArticleGraphState, config: RunnableConfig) -> dict:
    rt = _rt(config)
    if state.get("no_evidence"):
        return {"context_decision": "abort", "aborted": True}
    if state.get("skip_context_review"):
        return {"context_decision": "approve", "extra_queries": []}
    if rt.context_callback is not None:
        return rt.context_callback(dict(state))

    payload = interrupt(
        {
            "type": "context_review",
            "notes": state.get("retrieved_notes") or [],
            "executed_queries": state.get("executed_queries") or [],
        }
    )
    if not isinstance(payload, dict):
        payload = {"context_decision": "approve", "extra_queries": []}
    return {
        "context_decision": payload.get("context_decision") or "approve",
        "extra_queries": list(payload.get("extra_queries") or []),
    }


def route_after_context(state: ArticleGraphState) -> str:
    if state.get("no_evidence") or state.get("aborted"):
        return "end"
    decision = state.get("context_decision") or "approve"
    if decision == "abort":
        return "end"
    if decision == "enrich" and state.get("extra_queries"):
        return "enrich"
    return "catalog"


def node_build_catalog(state: ArticleGraphState, config: RunnableConfig) -> dict:
    rt = _rt(config)
    catalog = art.catalog_from_retrieved(
        rt.cfg,
        rt.db,
        state["topic"],
        state["style"],  # type: ignore[arg-type]
        state.get("retrieved_notes") or [],
        moc_ids=state.get("moc_ids"),
        retrieval_params=state.get("retrieval_params"),
    )
    rt.catalog = catalog
    if not catalog.notes:
        return {"no_evidence": True, "aborted": True}
    return {"no_evidence": False}


def node_generate_outline(state: ArticleGraphState, config: RunnableConfig) -> dict:
    rt = _rt(config)
    assert rt.catalog is not None
    feedback = state.get("outline_feedback") or None
    outline, called = art.generate_outline(
        rt.cfg, rt.db, rt.catalog, feedback=feedback
    )
    return {
        "outline": outline.model_dump(),
        "outline_feedback": "",
        **_mark_llm(rt, called),
    }


def node_outline_review(state: ArticleGraphState, config: RunnableConfig) -> dict:
    rt = _rt(config)
    outline = ArticleOutline.model_validate(state["outline"])

    if state.get("outline_only"):
        return {"outline_decision": "approve"}

    if rt.outline_callback is not None:
        decision, feedback = rt.outline_callback(outline)
        return {
            "outline_decision": decision,
            "outline_feedback": feedback or "",
        }

    payload = interrupt(
        {
            "type": "outline_review",
            "outline": state["outline"],
            "preview": art.format_outline_for_display(outline),
        }
    )
    if not isinstance(payload, dict):
        payload = {"outline_decision": "approve"}
    return {
        "outline_decision": payload.get("outline_decision") or "approve",
        "outline_feedback": payload.get("outline_feedback") or "",
    }


def route_after_outline(state: ArticleGraphState) -> str:
    if state.get("outline_only"):
        return "outline_only_finish"
    decision = state.get("outline_decision") or "approve"
    if decision == "abort":
        return "end"
    if decision == "regenerate":
        return "outline"
    return "draft"


def node_outline_only_finish(state: ArticleGraphState, config: RunnableConfig) -> dict:
    outline = ArticleOutline.model_validate(state["outline"])
    body = art.format_outline_for_display(outline)
    return {
        "final_body": body,
        "draft_body": body,
        "styled_body": body,
        "frontmatter": {
            "type": "article_outline",
            "topic": state["topic"],
            "style": state["style"],
            "title": outline.title,
        },
        "warnings": [],
    }


def node_draft_sections(state: ArticleGraphState, config: RunnableConfig) -> dict:
    rt = _rt(config)
    assert rt.catalog is not None
    outline = ArticleOutline.model_validate(state["outline"])
    bodies, note_ids, called = art.draft_sections(
        rt.cfg,
        rt.db,
        rt.catalog,
        outline,
        judge_feedback=state.get("judge_feedback") or "",
    )
    return {
        "section_bodies": bodies,
        "used_note_ids": note_ids,
        **_mark_llm(rt, called),
    }


def node_assemble(state: ArticleGraphState, config: RunnableConfig) -> dict:
    rt = _rt(config)
    assert rt.catalog is not None
    outline = ArticleOutline.model_validate(state["outline"])
    frontmatter, body, cited, warnings = art.assemble_article(
        outline,
        list(state.get("section_bodies") or []),
        rt.catalog,
        rt.cfg.vault_path,
    )
    frontmatter.update(
        {
            "llm_model": rt.cfg.llm.model,
            "topic": state["topic"],
            "style": state["style"],
            "origin": "article",
            "type": "article",
            "personality": state.get("personality") or "neutral",
        }
    )
    return {
        "draft_body": body,
        "frontmatter": frontmatter,
        "cited_source_ids": cited,
        "warnings": warnings,
    }


def node_personality(state: ArticleGraphState, config: RunnableConfig) -> dict:
    rt = _rt(config)
    body = state.get("draft_body") or ""
    styled, called = art.apply_personality_rewrite(
        rt.cfg,
        rt.db,
        body,
        state.get("personality") or "neutral",
        custom_style_notes=state.get("custom_style_notes") or "",
    )
    return {"styled_body": styled, **_mark_llm(rt, called)}


def node_judge(state: ArticleGraphState, config: RunnableConfig) -> dict:
    rt = _rt(config)
    if state.get("skip_judge"):
        return {
            "judge_scores": {
                "verdict": "APPROVED",
                "average": 10.0,
                "feedback": "judge skipped",
            },
            "judge_feedback": "",
        }
    assert rt.catalog is not None
    body = state.get("styled_body") or state.get("draft_body") or ""
    scores, called = art.judge_article_body(rt.cfg, rt.db, rt.catalog, body)
    iteration = int(state.get("iteration_count") or 0)
    out: dict[str, Any] = {
        "judge_scores": scores,
        **_mark_llm(rt, called),
    }
    if scores.get("verdict") == "REJECTED":
        out["judge_feedback"] = scores.get("feedback") or ""
        out["iteration_count"] = iteration + 1
    else:
        out["judge_feedback"] = ""
    return out


def route_after_judge(state: ArticleGraphState) -> str:
    if state.get("skip_judge"):
        return "finish"
    scores = state.get("judge_scores") or {}
    if scores.get("verdict") != "REJECTED":
        return "finish"
    max_iter = int(state.get("max_judge_iterations") or 3)
    iteration = int(state.get("iteration_count") or 0)
    if iteration < max_iter:
        return "redraft"
    return "finish_with_warning"


def node_finish(state: ArticleGraphState, config: RunnableConfig) -> dict:
    rt = _rt(config)
    body = state.get("styled_body") or state.get("draft_body") or ""
    warnings = list(state.get("warnings") or [])
    scores = state.get("judge_scores") or {}
    if (
        scores.get("verdict") == "REJECTED"
        and int(state.get("iteration_count") or 0)
        >= int(state.get("max_judge_iterations") or 3)
    ):
        warnings.append(
            "Judge nao aprovou apos max_judge_iterations; "
            "salvando melhor rascunho disponivel."
        )

    catalog = rt.catalog
    if catalog is not None:
        warnings.extend(art.verify_article(body, catalog, rt.cfg.vault_path))

    frontmatter = dict(state.get("frontmatter") or {})
    if scores:
        frontmatter["judge_average"] = scores.get("average")
        frontmatter["judge_verdict"] = scores.get("verdict")
    frontmatter["executed_queries"] = list(state.get("executed_queries") or [])

    return {
        "final_body": body,
        "frontmatter": frontmatter,
        "warnings": warnings,
    }


def node_abort(state: ArticleGraphState, config: RunnableConfig) -> dict:
    if state.get("no_evidence"):
        return {
            "final_body": art._NO_EVIDENCE,  # noqa: SLF001
            "aborted": True,
            "no_evidence": True,
            "frontmatter": {},
            "warnings": [],
        }
    return {
        "final_body": "",
        "aborted": True,
        "frontmatter": {},
        "warnings": [],
    }


# ── Graph build / run ──────────────────────────────────────────────────


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


def _result_from_state(
    state: dict, rt: ArticleRuntime, topic: str, style: art.ArticleStyle
) -> art.ArticleResult:
    outline = None
    if state.get("outline"):
        outline = ArticleOutline.model_validate(state["outline"])
    body = state.get("final_body") or ""
    no_evidence = bool(state.get("no_evidence"))
    title = ""
    if outline:
        title = outline.title
    elif state.get("frontmatter"):
        title = str(state["frontmatter"].get("title") or "")

    return art.ArticleResult(
        topic=topic,
        style=style,
        title=title,
        body=body,
        answer=body,
        frontmatter=dict(state.get("frontmatter") or {}),
        outline=outline,
        warnings=list(state.get("warnings") or []),
        llm_called=bool(state.get("llm_called") or rt.llm_called),
        llm_model=rt.cfg.llm.model,
        note_ids=list(state.get("used_note_ids") or (
            list(rt.catalog.notes.keys()) if rt.catalog else []
        )),
        source_ids=list(state.get("cited_source_ids") or []),
        no_evidence=no_evidence,
        aborted=bool(state.get("aborted")),
    )


def run_article_graph(
    cfg: "AppConfig",
    db: "StateDB",
    idx: "VectorIndex",
    topic: str,
    style: art.ArticleStyle = "blog",
    topk: Optional[int] = None,
    use_graph: Optional[bool] = None,
    mode: Optional[str] = None,
    outline_only: bool = False,
    approve_outline: Optional[OutlineCallback] = None,
    personality: Optional[str] = None,
    custom_style_notes: Optional[str] = None,
    skip_context_review: bool = False,
    skip_judge: bool = False,
    max_judge_iterations: Optional[int] = None,
    context_callback: Optional[ContextCallback] = None,
    hitl_handler: Optional[Callable[[dict], dict]] = None,
) -> art.ArticleResult:
    """Compile and run the article StateGraph to completion.

    When ``hitl_handler`` is set, LangGraph interrupts are resolved by calling
    it with the interrupt payload and resuming with its return value.
    Callbacks (``context_callback`` / ``approve_outline``) bypass interrupts.
    """
    from zettel.usage import begin_run, finish_pipeline_run

    run_id = db.start_run("article")
    begin_run(run_id)

    art_cfg = cfg.retrieval.article
    if use_graph is None:
        use_graph = cfg.retrieval.graph_expansion.enabled

    # Tests / non-HITL: auto-approve outline when no callback provided
    outline_cb = approve_outline
    if outline_cb is None and hitl_handler is None:
        outline_cb = lambda _o: ("approve", None)  # noqa: E731

    # Callback-driven runs skip context review unless context_callback/HITL set
    effective_skip_context = skip_context_review
    if hitl_handler is None and context_callback is None:
        effective_skip_context = True

    effective_skip_judge = skip_judge or outline_only

    rt = ArticleRuntime(
        cfg=cfg,
        db=db,
        idx=idx,
        context_callback=context_callback,
        outline_callback=outline_cb,
    )

    graph = build_article_graph().compile(checkpointer=MemorySaver())
    thread_id = f"article-{uuid.uuid4().hex[:12]}"
    config: dict[str, Any] = {
        "configurable": {"thread_id": thread_id, "runtime": rt},
    }

    initial: ArticleGraphState = {
        "topic": topic,
        "style": style,
        "personality": personality or art_cfg.default_personality,
        "custom_style_notes": custom_style_notes or "",
        "topk": topk if topk is not None else art_cfg.topk,
        "use_graph": bool(use_graph),
        "mode": mode or cfg.retrieval.mode,
        "outline_only": outline_only,
        "skip_context_review": effective_skip_context,
        "skip_judge": effective_skip_judge,
        "max_judge_iterations": (
            max_judge_iterations
            if max_judge_iterations is not None
            else art_cfg.max_judge_iterations
        ),
        "search_queries": [],
        "executed_queries": [],
        "extra_queries": [],
        "retrieved_notes": [],
        "moc_ids": [],
        "iteration_count": 0,
        "judge_feedback": "",
        "llm_called": False,
        "warnings": [],
    }

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
            resume_val = hitl_handler(result_state)
            result_state = graph.invoke(Command(resume=resume_val), config)

        return _result_from_state(result_state, rt, topic, style)
    finally:
        finish_pipeline_run(db, run_id)
