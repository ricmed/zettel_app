"""Retrieval logic for the article graph's one node with real business logic.

Four pure functions, each taking explicit arguments instead of a
``RunnableConfig``, so they are directly unit-testable without a compiled
graph. ``nodes.node_vector_search_merge`` composes them in order: pending
queries, MOC boost, extra graph hops, parameter snapshot. No threshold or
floor is reinterpreted here — see ADR-003 / ADR-009 / ADR-010.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .. import article as art
from .. import graph as note_graph

if TYPE_CHECKING:
    from ..config import AppConfig
    from ..retrieval import Retriever
    from ..state import StateDB

logger = logging.getLogger(__name__)


def run_pending_queries(
    retriever: Retriever,
    queries: list[str],
    executed: list[str],
    existing: list[dict],
    *,
    topk: int,
    mode: str,
    use_graph: bool,
    max_context_notes: int,
) -> tuple[list[dict], list[str]]:
    """Run every query not yet executed, merging hits into ``existing``."""
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
        existing = art.merge_retrieved_notes(existing, pool.hits, max_context_notes)
        executed.append(q)
        logger.info(
            "Busca [%d/%d] ok | hits=%d | pool acumulado=%d",
            i,
            total_q,
            len(pool.hits),
            len(existing),
        )
    return existing, executed


def apply_moc_boost(
    db: StateDB,
    existing: list[dict],
    topic: str,
    moc_ids: list[str],
    max_context_notes: int,
) -> tuple[list[dict], list[str]]:
    """Boost notes linked from a matching MOC, once (only when ``moc_ids`` is empty)."""
    if moc_ids:
        return existing, moc_ids
    moc = db.find_moc_by_topic(topic)
    if not moc:
        return existing, moc_ids
    moc_ids = [*moc_ids, moc["moc_id"]]
    hits = [art.dict_to_retrieved_note(d) for d in existing]
    hits = art.merge_moc_notes(db, hits, moc)
    existing = art.merge_retrieved_notes([], hits, max_context_notes)
    return existing, moc_ids


def expand_extra_hops(
    db: StateDB,
    existing: list[dict],
    *,
    topk: int,
    article_cfg,
    graph_cfg,
) -> list[dict]:
    """Extra BFS hops beyond the retriever's own graph expansion, article-specific."""
    seeds = [d for d in existing if int(d.get("hop") or 0) == 0] or existing[:topk]
    neighbors = note_graph.expand_notes(
        db,
        seed_ids=[s["note_id"] for s in seeds],
        max_hops=article_cfg.max_hops,
        decay=graph_cfg.decay,
        relation_weights=graph_cfg.relation_weights,
        max_neighbors=graph_cfg.max_neighbors,
        seed_weights={s["note_id"]: float(s.get("score") or 0) for s in seeds},
    )
    by_id = {d["note_id"]: d for d in existing}
    for nid, neigh in neighbors.items():
        if nid in by_id:
            continue
        row = db.get_note(nid)
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
    return sorted(
        by_id.values(), key=lambda x: float(x.get("score") or 0), reverse=True
    )[:article_cfg.max_context_notes]


def snapshot_retrieval_params(
    cfg: AppConfig,
    *,
    mode: str,
    topk: int,
    use_graph: bool,
    moc_ids: list[str],
    executed_queries: list[str],
) -> dict:
    """The parameter snapshot `--show-context` reads back as "Parametros de recuperacao"."""
    art_cfg = cfg.retrieval.article
    gcfg = cfg.retrieval.graph_expansion
    floor_cfg = cfg.retrieval.relevance_floor
    return {
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
        "executed_queries": list(executed_queries),
    }
