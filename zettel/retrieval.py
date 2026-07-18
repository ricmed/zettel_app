"""Hybrid retrieval: dense vectors (Chroma) + BM25 lexical (FTS5) + graph.

Historically every RAG lookup in the pipeline went straight to Chroma's dense
nearest-neighbour search. This module adds a single composition point that fuses
that dense ranking with the BM25 lexical ranking from the SQLite FTS5 index
(Reciprocal Rank Fusion), then optionally expands the top results 1..N hops over
the typed note-connection graph. Every consumer that wants richer recall
(``ask``, ``connect``, ``sync``) goes through :class:`Retriever`; the
threshold-calibrated consumers (extractor dedupe, harvester layer-3) deliberately
stay on the raw vector distance and do not use this.

RRF is used instead of score normalisation because it only needs the *rank* of an
id in each list, so the incompatible scales of L2 distance and bm25 rank never
have to be reconciled. Ids are shared across Chroma and SQLite (same note_id /
chunk_id), so fusion is a direct dictionary merge.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from . import graph

if TYPE_CHECKING:
    from .config import AppConfig
    from .index import VectorIndex
    from .state import StateDB

logger = logging.getLogger(__name__)


@dataclass
class RetrievedNote:
    """A single retrieval result, carrying provenance for downstream rendering."""

    note_id: str
    score: float                          # fused RRF score (+ graph boost)
    title: str = ""
    document: str = ""                    # embeddable text / note body snippet
    metadata: dict = field(default_factory=dict)
    vector_rank: Optional[int] = None
    bm25_rank: Optional[int] = None
    vector_distance: Optional[float] = None
    hop: int = 0                          # 0 = search seed; >=1 = graph neighbour
    via: list[dict] = field(default_factory=list)  # graph path (see graph.py)


class Retriever:
    """Compose vector search, BM25 search and graph expansion behind one API."""

    def __init__(self, cfg: "AppConfig", db: "StateDB", idx: "VectorIndex"):
        self.cfg = cfg
        self.db = db
        self.idx = idx
        self._warned_no_fts = False

    # ── Public API ─────────────────────────────────────────────────────

    def search_notes(
        self,
        query: str,
        topk: Optional[int] = None,
        exclude_id: Optional[str] = None,
        mode: Optional[str] = None,
        expand_graph: Optional[bool] = None,
    ) -> list[RetrievedNote]:
        """Retrieve permanent notes for ``query``.

        Returns up to ``topk`` search seeds, plus (when graph expansion is on)
        their graph neighbours as additional entries with ``hop >= 1``, sorted by
        score descending. Neighbours are always additive context — their score is
        bounded above by the seed they came from, so seeds are never displaced by
        a weaker neighbour of a weaker seed.
        """
        topk = topk if topk is not None else self.cfg.linking.topk
        mode = mode or self.cfg.retrieval.mode
        if expand_graph is None:
            expand_graph = self.cfg.retrieval.graph_expansion.enabled

        pool = max(topk * 3, 20)
        vector_hits = self._vector_notes(query, pool, exclude_id)
        bm25_hits = (
            self._bm25_notes(query, pool, exclude_id) if mode == "hybrid" else []
        )

        fused = self._rrf_fuse_notes(vector_hits, bm25_hits)
        seeds = fused[:topk]
        if not expand_graph or not seeds:
            return seeds
        return self._expand_with_graph(seeds, exclude_id)

    def search_chunks(
        self,
        query: str,
        topk: Optional[int] = None,
        mode: Optional[str] = None,
    ) -> list[RetrievedNote]:
        """Retrieve source chunks for ``query`` (no graph expansion — graph is notes only).

        Result ``note_id`` field carries the ``chunk_id`` (same id space in Chroma
        and FTS). Used for optional raw-evidence retrieval in ``ask``.
        """
        topk = topk if topk is not None else self.cfg.linking.topk
        mode = mode or self.cfg.retrieval.mode
        pool = max(topk * 3, 20)

        vector_hits = self._vector_chunks(query, pool)
        bm25_hits = self._bm25_chunks(query, pool) if mode == "hybrid" else []
        fused = self._rrf_fuse_chunks(vector_hits, bm25_hits)
        return fused[:topk]

    # ── Vector / BM25 source rankings ──────────────────────────────────

    def _vector_notes(self, query: str, pool: int, exclude_id: Optional[str]) -> list[dict]:
        try:
            return self.idx.query_similar_notes(query, n_results=pool, exclude_id=exclude_id)
        except Exception as e:  # pragma: no cover - defensive around Chroma
            logger.warning("Busca vetorial de notas falhou: %s", e)
            return []

    def _vector_chunks(self, query: str, pool: int) -> list[dict]:
        try:
            return self.idx.find_similar_chunks([query], n_results=pool)
        except Exception as e:  # pragma: no cover
            logger.warning("Busca vetorial de chunks falhou: %s", e)
            return []

    def _bm25_notes(self, query: str, pool: int, exclude_id: Optional[str]) -> list[dict]:
        if not getattr(self.db, "fts_enabled", False):
            self._warn_no_fts()
            return []
        hits = self.db.search_notes_fts(query, limit=pool)
        if exclude_id:
            hits = [h for h in hits if h["note_id"] != exclude_id]
        return hits

    def _bm25_chunks(self, query: str, pool: int) -> list[dict]:
        if not getattr(self.db, "fts_enabled", False):
            self._warn_no_fts()
            return []
        return self.db.search_chunks_fts(query, limit=pool)

    def _warn_no_fts(self) -> None:
        if not self._warned_no_fts:
            logger.warning(
                "FTS5 indisponivel — busca hibrida degradada para vetorial pura"
            )
            self._warned_no_fts = True

    # ── RRF fusion ─────────────────────────────────────────────────────

    def _rrf_fuse_notes(
        self, vector_hits: list[dict], bm25_hits: list[dict]
    ) -> list[RetrievedNote]:
        k = self.cfg.retrieval.rrf_k
        scores: dict[str, float] = {}
        merged: dict[str, RetrievedNote] = {}

        for rank, hit in enumerate(vector_hits, start=1):
            nid = hit["id"]
            scores[nid] = scores.get(nid, 0.0) + 1.0 / (k + rank)
            rn = merged.setdefault(nid, RetrievedNote(note_id=nid, score=0.0))
            rn.vector_rank = rank
            rn.vector_distance = hit.get("distance")
            if hit.get("document"):
                rn.document = hit["document"]
            if hit.get("metadata"):
                rn.metadata = hit["metadata"]
                rn.title = rn.metadata.get("title", rn.title)

        for rank, hit in enumerate(bm25_hits, start=1):
            nid = hit["note_id"]
            scores[nid] = scores.get(nid, 0.0) + 1.0 / (k + rank)
            rn = merged.setdefault(nid, RetrievedNote(note_id=nid, score=0.0))
            rn.bm25_rank = rank

        for nid, rn in merged.items():
            rn.score = scores[nid]

        results = sorted(merged.values(), key=lambda r: r.score, reverse=True)
        self._hydrate_notes(results)
        return results

    def _rrf_fuse_chunks(
        self, vector_hits: list[dict], bm25_hits: list[dict]
    ) -> list[RetrievedNote]:
        k = self.cfg.retrieval.rrf_k
        scores: dict[str, float] = {}
        merged: dict[str, RetrievedNote] = {}

        for rank, hit in enumerate(vector_hits, start=1):
            cid = hit["id"]
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank)
            rn = merged.setdefault(cid, RetrievedNote(note_id=cid, score=0.0))
            rn.vector_rank = rank
            rn.vector_distance = hit.get("distance")
            if hit.get("document"):
                rn.document = hit["document"]
            if hit.get("metadata"):
                rn.metadata = hit["metadata"]

        for rank, hit in enumerate(bm25_hits, start=1):
            cid = hit["chunk_id"]
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank)
            rn = merged.setdefault(cid, RetrievedNote(note_id=cid, score=0.0))
            rn.bm25_rank = rank

        for cid, rn in merged.items():
            rn.score = scores[cid]

        results = sorted(merged.values(), key=lambda r: r.score, reverse=True)
        self._hydrate_chunks(results)
        return results

    # ── Hydration (fill title/document for ids that came only from BM25) ──

    def _hydrate_notes(self, results: list[RetrievedNote]) -> None:
        for rn in results:
            if rn.title and rn.document:
                continue
            row = self.db.get_note(rn.note_id)
            if not row:
                continue
            rn.title = rn.title or row.get("title", "")
            if not rn.document:
                rn.document = row.get("body") or ""
            rn.metadata.setdefault("source_id", row.get("source_id"))
            rn.metadata.setdefault("path", row.get("path"))

    def _hydrate_chunks(self, results: list[RetrievedNote]) -> None:
        for rn in results:
            if rn.document:
                continue
            row = self.db.get_chunk(rn.note_id)
            if not row:
                continue
            rn.document = row.get("text") or ""
            rn.metadata.setdefault("source_id", row.get("source_id"))
            rn.metadata.setdefault("locator", row.get("locator"))
            rn.metadata.setdefault("section_path", row.get("section_path"))

    # ── Graph expansion ────────────────────────────────────────────────

    def _expand_with_graph(
        self, seeds: list[RetrievedNote], exclude_id: Optional[str]
    ) -> list[RetrievedNote]:
        gcfg = self.cfg.retrieval.graph_expansion
        by_id = {s.note_id: s for s in seeds}
        neighbors = graph.expand_notes(
            self.db,
            seed_ids=list(by_id.keys()),
            max_hops=gcfg.max_hops,
            decay=gcfg.decay,
            relation_weights=gcfg.relation_weights,
            max_neighbors=gcfg.max_neighbors,
            seed_weights={s.note_id: s.score for s in seeds},
        )

        for nid, neigh in neighbors.items():
            if exclude_id and nid == exclude_id:
                continue
            if nid in by_id:
                # Already a seed: reinforce its score with the graph signal.
                by_id[nid].score += neigh.weight
                continue
            rn = RetrievedNote(
                note_id=nid,
                score=neigh.weight,
                hop=neigh.hop,
                via=neigh.via,
            )
            by_id[nid] = rn

        # Hydrate any pure-graph neighbours that have no title/body yet.
        self._hydrate_notes([r for r in by_id.values() if r.hop >= 1])
        return sorted(by_id.values(), key=lambda r: r.score, reverse=True)
