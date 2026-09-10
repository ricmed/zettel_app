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
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from . import graph

if TYPE_CHECKING:
    from .config import AppConfig, RelevanceFloorConfig
    from .index import VectorIndex
    from .state import StateDB


class _FloorCandidate(Protocol):
    """What :meth:`Retriever._apply_relevance_floor` actually needs.

    Both :class:`RetrievedNote` and :class:`RetrievedChapter` satisfy it, so one
    floor implementation serves both without either knowing about the other.
    """

    vector_distance: float | None
    bm25_rank: int | None
    bm25_coverage: float | None
    passed_floor: bool
    floor_reason: str


logger = logging.getLogger(__name__)


@dataclass
class RetrievedNote:
    """A single retrieval result, carrying provenance for downstream rendering."""

    note_id: str
    score: float  # fused RRF score (+ graph boost)
    title: str = ""
    document: str = ""  # embeddable text / note body snippet
    metadata: dict = field(default_factory=dict)
    vector_rank: int | None = None
    bm25_rank: int | None = None
    # Fraction of the query's FTS terms actually present in this note. Only the
    # lexical bypass reads it; None when the hit never matched BM25.
    bm25_coverage: float | None = None
    vector_distance: float | None = None
    hop: int = 0  # 0 = search seed; >=1 = graph neighbour
    via: list[dict] = field(default_factory=list)  # graph path (see graph.py)
    passed_floor: bool = True  # absolute relevance floor (see _apply_relevance_floor)
    floor_reason: str = ""  # human-readable explanation of the floor verdict
    origin: str = "search"  # "search" | "topic_index" | "distant_analogy"


@dataclass
class RetrievedChapter:
    """A chapter-summary hit (ADR-047).

    Deliberately a separate type from :class:`RetrievedNote`: a chapter has no
    graph edges and is never evidence, so it carries neither ``hop``/``via`` nor
    anything a caller could mistake for a quotable note.
    """

    chapter_id: str
    score: float
    source_id: str = ""
    chapter_title: str = ""
    document: str = ""
    metadata: dict = field(default_factory=dict)
    vector_rank: int | None = None
    bm25_rank: int | None = None
    bm25_coverage: float | None = None
    vector_distance: float | None = None
    passed_floor: bool = True
    floor_reason: str = ""

    @property
    def similarity(self) -> float | None:
        """Cosine similarity, or None when the hit came only from BM25."""
        if self.vector_distance is None:
            return None
        return 1.0 - self.vector_distance / 2.0


@dataclass
class ChapterSearchResult:
    """Mirror of :class:`NoteSearchResult` for chapter summaries."""

    hits: list[RetrievedChapter] = field(default_factory=list)
    candidates: list[RetrievedChapter] = field(default_factory=list)


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

    def __init__(self, cfg: AppConfig, db: StateDB, idx: VectorIndex):
        self.cfg = cfg
        self.db = db
        self.idx = idx
        self._warned_no_fts = False

    # ── Public API ─────────────────────────────────────────────────────

    def search_notes(
        self,
        query: str,
        topk: int | None = None,
        exclude_id: str | None = None,
        mode: str | None = None,
        expand_graph: bool | None = None,
        relevance_floor: bool | None = None,
        min_vector_similarity: float | None = None,
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
        bm25_hits = self._bm25_notes(query, pool, exclude_id) if mode == "hybrid" else []
        vector_hits, topic_seed_ids = self._add_topic_index_seeds(query, vector_hits, exclude_id)

        fused = self._rrf_fuse_notes(vector_hits, bm25_hits)
        for hit in fused:
            if hit.note_id in topic_seed_ids:
                hit.origin = "topic_index"
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

    def search_chapter_summaries(
        self,
        query: str,
        topk: int | None = None,
        mode: str | None = None,
    ) -> ChapterSearchResult:
        """Retrieve chapter summaries for ``query`` (ADR-047).

        Same machinery as :meth:`search_notes` — dense + BM25 fused by RRF, then
        an absolute relevance floor — with two deliberate omissions: no graph
        expansion (a chapter has no edges) and no topic-index boost (the index
        routes to permanent notes, and ADR-036 keeps non-note targets
        unroutable).

        The floor is ``retrieval.chapter_floor``, not the note floor: the two
        measure different text distributions and their thresholds do not
        transfer.
        """
        topk = topk if topk is not None else self.cfg.retrieval.catalog.summary_topk
        mode = mode or self.cfg.retrieval.mode

        pool = max(topk * 3, 20)
        vector_hits = self._vector_chapters(query, pool)
        bm25_hits = self._bm25_chapters(query, pool) if mode == "hybrid" else []
        if not vector_hits and not bm25_hits:
            return ChapterSearchResult()

        fused = self._rrf_fuse_chapters(vector_hits, bm25_hits)
        self._apply_relevance_floor(fused, None, None, floor_cfg=self.cfg.retrieval.chapter_floor)
        candidates = fused[: max(topk, 10)]
        hits = [f for f in fused if f.passed_floor][:topk]
        return ChapterSearchResult(hits=hits, candidates=candidates)

    def _vector_chapters(self, query: str, pool: int) -> list[dict]:
        try:
            return self.idx.query_chapter_summaries(query, n_results=pool)
        except Exception as e:  # pragma: no cover - defensive, mirrors _vector_notes
            logger.warning("Busca vetorial de resumos de capitulo falhou: %s", e)
            return []

    def _bm25_chapters(self, query: str, pool: int) -> list[dict]:
        if not getattr(self.db, "fts_enabled", False):
            self._warn_no_fts()
            return []
        hits = self.db.search_chapter_summaries_fts(query, limit=pool)
        self._attach_coverage(
            query,
            hits,
            "chapter_id",
            self.db.get_chapter_summary_texts,
            self.cfg.retrieval.chapter_floor,
        )
        return hits

    def _rrf_fuse_chapters(
        self, vector_hits: list[dict], bm25_hits: list[dict]
    ) -> list[RetrievedChapter]:
        k = self.cfg.retrieval.rrf_k
        scores: dict[str, float] = {}
        merged: dict[str, RetrievedChapter] = {}

        for rank, hit in enumerate(vector_hits, start=1):
            cid = hit["id"]
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank)
            rc = merged.setdefault(cid, RetrievedChapter(chapter_id=cid, score=0.0))
            rc.vector_rank = rank
            rc.vector_distance = hit.get("distance")
            if hit.get("document"):
                rc.document = hit["document"]
            if hit.get("metadata"):
                rc.metadata = hit["metadata"]
                rc.source_id = rc.metadata.get("source_id", "")
                rc.chapter_title = rc.metadata.get("chapter_title", "")

        for rank, hit in enumerate(bm25_hits, start=1):
            cid = hit["chapter_id"]
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank)
            rc = merged.setdefault(cid, RetrievedChapter(chapter_id=cid, score=0.0))
            rc.bm25_rank = rank
            rc.bm25_coverage = hit.get("coverage")

        for cid, rc in merged.items():
            rc.score = scores[cid]

        results = sorted(merged.values(), key=lambda r: r.score, reverse=True)
        self._hydrate_chapters(results)
        return results

    def _hydrate_chapters(self, results: list[RetrievedChapter]) -> None:
        """Fill title/source for ids that arrived only through BM25."""
        for rc in results:
            if rc.chapter_title and rc.source_id:
                continue
            row = self.db.get_chapter(rc.chapter_id)
            if not row:
                continue
            rc.chapter_title = rc.chapter_title or (row.get("title") or "")
            rc.source_id = rc.source_id or row.get("source_id", "")
            if not rc.document:
                rc.document = row.get("summary") or ""

    def search_distant_analogies(
        self,
        query: str,
        *,
        exclude_id: str | None,
        exclude_ids: set[str],
        topk: int,
        min_vector_similarity: float,
    ) -> list[RetrievedNote]:
        """Secondary connect search: local floor, no graph, other-bucket notes.

        Does **not** touch ``RelevanceFloorConfig`` defaults — the lower
        similarity is an argument, local to this call (issue #161).
        """
        if topk <= 0:
            return []
        result = self.search_notes(
            query,
            topk=max(topk * 4, 16),
            exclude_id=exclude_id,
            expand_graph=False,
            min_vector_similarity=min_vector_similarity,
        )
        hits: list[RetrievedNote] = []
        for hit in result.hits:
            if hit.note_id in exclude_ids:
                continue
            hit.origin = "distant_analogy"
            hits.append(hit)
            if len(hits) >= topk:
                break
        return hits

    # ── Vector / BM25 source rankings ──────────────────────────────────

    def _vector_notes(self, query: str, pool: int, exclude_id: str | None) -> list[dict]:
        try:
            return self.idx.query_similar_notes(query, n_results=pool, exclude_id=exclude_id)
        except Exception as e:  # pragma: no cover - defensive around Chroma
            logger.warning("Busca vetorial de notas falhou: %s", e)
            return []

    def _add_topic_index_seeds(
        self,
        query: str,
        vector_hits: list[dict],
        exclude_id: str | None,
    ) -> tuple[list[dict], set[str]]:
        """Add notes the topic index routes this query to, as extra vector hits.

        The seed is added *as a vector hit*, with a real distance from a
        Chroma query restricted to those ids, so it goes through
        ``_apply_relevance_floor`` on exactly the same evidence as any other
        candidate. Injecting it without a distance would make it pass the floor
        by default — a repeat of the unconditional-BM25-bypass bug the floor's
        rank cutoff exists to prevent. Being in the index is a routing hint, not
        proof of relevance.

        Costs one extra embedding of the query, and only when a term matches.
        """
        from zettel.topic_index import fold

        if not self.cfg.retrieval.topic_index_boost:
            return vector_hits, set()

        already = {h["id"] for h in vector_hits}
        matches = self.db.match_topic_index(fold(query))
        wanted = [
            m["note_id"]
            for m in matches
            if m["note_id"] not in already and m["note_id"] != exclude_id
        ][: self.cfg.retrieval.topic_index_max_seeds]
        if not wanted:
            return vector_hits, set()

        try:
            extra = self.idx.query_notes_by_ids(query, wanted)
        except Exception as e:  # pragma: no cover - defensive around Chroma
            logger.warning("Boost por topic index falhou: %s", e)
            return vector_hits, set()

        logger.debug("Topic index: %d semente(s) extra para a consulta", len(extra))
        # Re-sort by real distance so a routed note gets its true vector rank
        # rather than being pinned to the tail of the pool.
        merged = sorted(
            vector_hits + extra,
            key=lambda h: h.get("distance") if h.get("distance") is not None else float("inf"),
        )
        return merged, {hit["id"] for hit in extra}

    def _bm25_notes(self, query: str, pool: int, exclude_id: str | None) -> list[dict]:
        if not getattr(self.db, "fts_enabled", False):
            self._warn_no_fts()
            return []
        hits = self.db.search_notes_fts(query, limit=pool)
        if exclude_id:
            hits = [h for h in hits if h["note_id"] != exclude_id]
        self._attach_coverage(
            query, hits, "note_id", self.db.get_note_texts, self.cfg.retrieval.relevance_floor
        )
        return hits

    def _attach_coverage(
        self,
        query: str,
        hits: list[dict],
        id_key: str,
        fetch_texts: Callable[[list[str]], dict[str, str]],
        floor_cfg: RelevanceFloorConfig,
    ) -> None:
        """Annotate each BM25 hit with the fraction of query terms it contains.

        BM25 joins the query's terms with ``OR``, so a hit is returned for
        matching *any* one of them, and ``bm25_bypass_max_rank`` only asks
        whether it ranked well *among those matches* — a relative measure. On a
        small corpus a query's match pool is often smaller than the cutoff, and
        then "top 5" means "everything that matched at all", so a note sharing a
        single common word bypasses the similarity floor. Coverage is the
        absolute counterpart: how much of what was asked is actually there.
        """
        from zettel.hashing import fold_for_match
        from zettel.state import fts_query_terms

        for hit in hits:
            hit["coverage"] = None
        # Only a hit inside `bm25_bypass_max_rank` can ever consult coverage —
        # the rank test is evaluated first — and `hits` arrives rank-ordered, so
        # folding the rest of the pool would be pure waste. Folding a note body
        # is the expensive part here, not the SQL.
        scoped = hits[: max(floor_cfg.bm25_bypass_max_rank, 0)]
        if not scoped:
            return
        terms = [t for t in (fold_for_match(x).strip() for x in fts_query_terms(query)) if t]
        if not terms:
            return
        haystacks = fetch_texts([h[id_key] for h in scoped])
        for hit in scoped:
            # fold_for_match collapses every non-alphanumeric run to a single
            # space, so padding both sides turns substring search into a
            # whole-word test without a regex.
            folded = f" {fold_for_match(haystacks.get(hit[id_key], ''))} "
            found = sum(1 for t in terms if f" {t} " in folded)
            hit["coverage"] = found / len(terms)

    def _apply_relevance_floor(
        self,
        fused: Sequence[_FloorCandidate],
        relevance_floor: bool | None,
        min_vector_similarity: float | None,
        floor_cfg: RelevanceFloorConfig | None = None,
    ) -> None:
        """Mark each hit's ``passed_floor``/``floor_reason`` in place.

        ``floor_cfg`` defaults to ``retrieval.relevance_floor`` (permanent
        notes). Chapter summaries pass ``retrieval.chapter_floor`` instead:
        the reasoning below is identical, but the calibrated numbers are not
        transferable between two different text distributions (ADR-047).

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
        3. A BM25 hit bypasses the similarity check entirely when it is both
           ranked within ``bm25_bypass_max_rank`` **and** contains at least
           ``bm25_bypass_min_coverage`` of the query's terms. Rank alone is a
           *relative* test — it asks "did you rank well among whoever matched",
           and BM25 ORs the query's terms, so on a small corpus the match pool
           is often smaller than the cutoff and "top 5" degenerates into
           "everything". Coverage is the *absolute* half: a note sharing one
           common word with the question is not a strong lexical match however
           it ranks. A weak match on either axis falls through to the
           similarity check like any other hit.
        4. Otherwise, gate on ``min_vector_similarity``.
        5. No similarity data at all and a bm25 hit too weak to bypass -> FAILS
           (insufficient evidence either way).
        """
        floor_cfg = floor_cfg or self.cfg.retrieval.relevance_floor
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
        min_cov = floor_cfg.bm25_bypass_min_coverage

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
                coverage = hit.bm25_coverage
                if coverage is None or coverage >= min_cov:
                    hit.passed_floor = True
                    cov_txt = "" if coverage is None else f", cobertura {coverage:.0%}"
                    hit.floor_reason = (
                        f"match lexical forte (bm25 rank {hit.bm25_rank} <= {max_rank}{cov_txt})"
                    )
                    continue
                # Ranked well among the matches, but the note only contains a
                # sliver of what was asked — fall through to the similarity
                # check instead of bypassing it.
                bypass_denied = (
                    f"cobertura lexical {coverage:.0%} < {min_cov:.0%} "
                    f"(bm25 rank {hit.bm25_rank}), sem bypass"
                )
            else:
                bypass_denied = ""

            if similarity is not None:
                hit.passed_floor = similarity >= min_sim
                verdict = (
                    f"similaridade {similarity:.2f} >= piso {min_sim:.2f}"
                    if hit.passed_floor
                    else f"similaridade {similarity:.2f} abaixo do piso ({min_sim:.2f})"
                )
                hit.floor_reason = f"{bypass_denied}; {verdict}" if bypass_denied else verdict
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
            logger.warning("FTS5 indisponivel — busca hibrida degradada para vetorial pura")
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
            rn.bm25_coverage = hit.get("coverage")

        for nid, rn in merged.items():
            rn.score = scores[nid]

        results = sorted(merged.values(), key=lambda r: r.score, reverse=True)
        self._hydrate_notes(results)
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

    # ── Graph expansion ────────────────────────────────────────────────

    def _expand_with_graph(
        self, seeds: list[RetrievedNote], exclude_id: str | None
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
