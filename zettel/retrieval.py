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
    passed_floor: bool = True             # absolute relevance floor (see _apply_relevance_floor)
    floor_reason: str = ""                # human-readable explanation of the floor verdict


@dataclass
class NoteSearchResult:
    """Result of :meth:`Retriever.search_notes`.

    ``hits`` is what callers should actually use as evidence/context — it only
    contains candidates that cleared the absolute relevance floor (plus their
    graph neighbours). ``candidates`` is the raw ranked pool *before* the floor
    was applied, always populated, so a caller can still show "what was closest"
    for transparency even when nothing was relevant enough to answer from.
    """

    hits: list[RetrievedNote] = field(default_factory=list)
    candidates: list[RetrievedNote] = field(default_factory=list)


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
        relevance_floor: Optional[bool] = None,
        min_vector_similarity: Optional[float] = None,
    ) -> NoteSearchResult:
        """Retrieve permanent notes for ``query``.

        Returns a :class:`NoteSearchResult`:

        - ``hits``: up to ``topk`` seeds that cleared the absolute relevance
          floor, plus (when graph expansion is on) their graph neighbours with
          ``hop >= 1``. This is what callers should use as evidence/context. If
          nothing clears the floor, ``hits`` is empty — callers should treat
          that as "nothing relevant found" rather than forcing an answer from
          the closest-available-but-irrelevant candidates.
        - ``candidates``: the raw RRF-ranked pool *before* the floor, always
          populated (when the corpus is non-empty) so a caller can still show
          "what was closest" for transparency/debugging.

        Neighbours are always additive context — their score is bounded above
        by the seed they came from, so seeds are never displaced by a weaker
        neighbour of a weaker seed.
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
        self._apply_relevance_floor(fused, relevance_floor, min_vector_similarity)
        candidates = fused[: max(topk, 10)]

        seeds = [f for f in fused if f.passed_floor][:topk]
        if not seeds:
            return NoteSearchResult(hits=[], candidates=candidates)
        if not expand_graph:
            return NoteSearchResult(hits=seeds, candidates=candidates)
        return NoteSearchResult(
            hits=self._expand_with_graph(seeds, exclude_id), candidates=candidates
        )

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

    def _apply_relevance_floor(
        self,
        fused: list[RetrievedNote],
        relevance_floor: Optional[bool],
        min_vector_similarity: Optional[float],
    ) -> None:
        """Mark each hit's ``passed_floor``/``floor_reason`` in place.

        RRF's fused score is purely positional — the vector kNN side always
        returns the closest available notes regardless of whether any of them
        are actually relevant, so a totally off-topic query gets a similarly
        "confident-looking" score to a genuinely answerable one. This floor
        checks the raw vector similarity (or BM25 rank) instead of rank alone.

        Decision order per hit (see RelevanceFloorConfig for the tunables):

        1. Floor disabled -> always passes.
        2. Similarity present and below ``absolute_min_similarity`` -> FAILS,
           even if a lexical match exists. This is a hard backstop against a
           note that is embedding-wise near-orthogonal but happens to share an
           incidental term with the query; it is set well below
           ``min_vector_similarity`` so it doesn't undermine BM25's main use
           case (rescuing jargon/acronyms the embedding underrates).
        3. A BM25 hit ranked within ``bm25_bypass_max_rank`` bypasses the
           similarity check entirely -- a *strong* lexical match is evidence a
           kNN "closest available" hit isn't. A *weak* lexical match (found only
           deep in the pool) does not get this pass; it falls through to the
           similarity check like any other hit.
        4. Otherwise, gate on ``min_vector_similarity``.
        5. No similarity data at all and a bm25 hit too weak to bypass -> FAILS
           (insufficient evidence either way).
        """
        floor_cfg = self.cfg.retrieval.relevance_floor
        enabled = relevance_floor if relevance_floor is not None else floor_cfg.enabled
        if not enabled:
            for hit in fused:
                hit.passed_floor = True
                hit.floor_reason = "piso desabilitado"
            return

        min_sim = (
            min_vector_similarity
            if min_vector_similarity is not None
            else floor_cfg.min_vector_similarity
        )
        abs_min = floor_cfg.absolute_min_similarity
        max_rank = floor_cfg.bm25_bypass_max_rank

        for hit in fused:
            similarity = (
                1.0 - hit.vector_distance / 2.0 if hit.vector_distance is not None else None
            )

            if similarity is not None and similarity < abs_min:
                hit.passed_floor = False
                hit.floor_reason = (
                    f"similaridade {similarity:.2f} abaixo do minimo absoluto "
                    f"({abs_min:.2f}) — bypass do BM25 nao se aplica"
                )
                continue

            if (
                floor_cfg.bm25_hit_bypasses_floor
                and hit.bm25_rank is not None
                and hit.bm25_rank <= max_rank
            ):
                hit.passed_floor = True
                hit.floor_reason = f"match lexical forte (bm25 rank {hit.bm25_rank} <= {max_rank})"
                continue

            if similarity is not None:
                hit.passed_floor = similarity >= min_sim
                hit.floor_reason = (
                    f"similaridade {similarity:.2f} >= piso {min_sim:.2f}"
                    if hit.passed_floor
                    else f"similaridade {similarity:.2f} abaixo do piso ({min_sim:.2f})"
                )
                continue

            # No vector distance at all — only evidence is a bm25 hit that
            # didn't qualify for bypass (rank worse than max_rank).
            if hit.bm25_rank is not None:
                hit.passed_floor = False
                hit.floor_reason = (
                    f"match lexical fraco (bm25 rank {hit.bm25_rank} > {max_rank}), "
                    f"sem dado de similaridade vetorial"
                )
            else:
                # Shouldn't normally happen for a fused result (every hit comes
                # from vector_hits or bm25_hits), but fail safe rather than lose data.
                hit.passed_floor = True
                hit.floor_reason = "sem dados de similaridade (mantido por seguranca)"

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
