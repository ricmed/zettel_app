"""The 13 node_* functions and 3 route_after_* routers of the article graph.

Nearly all nodes are thin adapters over domain helpers in ``zettel/article.py``.
``node_vector_search_merge`` is the exception — see :mod:`zettel.article_graph.search`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from langgraph.types import interrupt

from .. import article as art
from ..config import llm_phase
from ..retrieval import Retriever
from ..schemas import ArticleOutline
from . import search
from .runtime import ArticleGraphState, mark_llm, runtime_from

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig


def node_query_enricher(state: ArticleGraphState, config: RunnableConfig) -> dict:
    rt = runtime_from(config)
    cfg = rt.cfg
    extras = list(state.get("extra_queries") or [])
    # After HITL enrich, only search the new extras (don't re-expand everything)
    if extras and state.get("executed_queries"):
        queries = extras
        called = False
    else:
        queries, called = art.enrich_search_queries(
            cfg,
            rt.db,
            state["topic"],
            state["style"],  # type: ignore[arg-type]
            extra_queries=extras or None,
        )
    out = {
        "search_queries": queries,
        "extra_queries": [],
        **mark_llm(rt, called),
    }
    return out


def node_vector_search_merge(state: ArticleGraphState, config: RunnableConfig) -> dict:
    rt = runtime_from(config)
    cfg = rt.cfg
    art_cfg = cfg.retrieval.article
    gcfg = cfg.retrieval.graph_expansion
    topk = int(state.get("topk") or art_cfg.topk)
    mode = state.get("mode") or cfg.retrieval.mode
    use_graph = state.get("use_graph")
    if use_graph is None:
        use_graph = cfg.retrieval.graph_expansion.enabled

    retriever = Retriever(cfg, rt.db, rt.idx)
    existing = list(state.get("retrieved_notes") or [])
    executed = list(state.get("executed_queries") or [])
    queries = list(state.get("search_queries") or [])

    existing, executed = search.run_pending_queries(
        retriever,
        queries,
        executed,
        existing,
        topk=topk,
        mode=mode,
        use_graph=use_graph,
        max_context_notes=art_cfg.max_context_notes,
    )

    moc_ids = list(state.get("moc_ids") or [])
    existing, moc_ids = search.apply_moc_boost(
        rt.db, existing, state["topic"], moc_ids, art_cfg.max_context_notes
    )

    if use_graph and existing and art_cfg.max_hops > gcfg.max_hops:
        existing = search.expand_extra_hops(
            rt.db, existing, topk=topk, article_cfg=art_cfg, graph_cfg=gcfg
        )

    retrieval_params = search.snapshot_retrieval_params(
        cfg,
        mode=mode,
        topk=topk,
        use_graph=use_graph,
        moc_ids=moc_ids,
        executed_queries=executed,
    )

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
    rt = runtime_from(config)
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
    rt = runtime_from(config)
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
    rt = runtime_from(config)
    assert rt.catalog is not None
    feedback = state.get("outline_feedback") or None
    outline, called = art.generate_outline(rt.cfg, rt.db, rt.catalog, feedback=feedback)
    return {
        "outline": outline.model_dump(),
        "outline_feedback": "",
        **mark_llm(rt, called),
    }


def node_outline_review(state: ArticleGraphState, config: RunnableConfig) -> dict:
    rt = runtime_from(config)
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
    rt = runtime_from(config)
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
        **mark_llm(rt, called),
    }


def node_assemble(state: ArticleGraphState, config: RunnableConfig) -> dict:
    rt = runtime_from(config)
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
            "llm_model": llm_phase(rt.cfg, "article").model,
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
    rt = runtime_from(config)
    body = state.get("draft_body") or ""
    styled, called = art.apply_personality_rewrite(
        rt.cfg,
        rt.db,
        body,
        state.get("personality") or "neutral",
        custom_style_notes=state.get("custom_style_notes") or "",
    )
    return {"styled_body": styled, **mark_llm(rt, called)}


def node_judge(state: ArticleGraphState, config: RunnableConfig) -> dict:
    rt = runtime_from(config)
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
        **mark_llm(rt, called),
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
    rt = runtime_from(config)
    body = state.get("styled_body") or state.get("draft_body") or ""
    warnings = list(state.get("warnings") or [])
    scores = state.get("judge_scores") or {}
    if scores.get("verdict") == "REJECTED" and int(state.get("iteration_count") or 0) >= int(
        state.get("max_judge_iterations") or 3
    ):
        warnings.append(
            "Judge nao aprovou apos max_judge_iterations; salvando melhor rascunho disponivel."
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
            "final_body": art.NO_EVIDENCE,
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
