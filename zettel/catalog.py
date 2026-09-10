"""Catalog search — which sources treat a subject, and where (ADR-047).

`zettel catalog "assunto"` answers a *library* question ("quais livros falam
sobre X?"), not a synthesis question. That difference is the whole design:

* **No LLM call.** The answer is a ranked table plus a SQL aggregate. Routing it
  through ``prompts/ask.md`` would ask a model to enumerate a count it could
  hallucinate. `ask` stays the place where prose answers come from.
* **Two signals, fused at the chapter level**, each hit declaring why it
  appeared, in the spirit of ADR-010's hits/candidates contract:

  - **Notes (signal A)** — the existing :meth:`Retriever.search_notes` pool,
    walked back through ``concepts`` to the chunk and therefore the chapter.
    This is the strongest evidence the vault has: notes are the human-approved,
    already-calibrated layer, and this signal costs nothing new.
  - **Summaries (signal B)** — :meth:`Retriever.search_chapter_summaries`.
    Covers the chapter that produced no permanent note at all: extract yield
    zero, or a source that has not been through ``connect`` yet.

This module is the named reader of the ``chapter_summaries`` collection. Without
it that collection would be write-only, which is exactly the defect that removed
``literature_notes`` (ADR-015 amendment).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from zettel.config import AppConfig
from zettel.index import VectorIndex
from zettel.retrieval import Retriever
from zettel.state import StateDB

logger = logging.getLogger(__name__)


@dataclass
class ChapterMatch:
    """One chapter in the answer, with the provenance of why it is there."""

    chapter_id: str
    source_id: str
    chapter_title: str
    score: float
    note_count: int = 0  # SQL aggregate, never from an LLM
    page_start: int | None = None
    page_end: int | None = None
    summary: str = ""
    summary_stale: bool = False
    # Provenance
    via_notes: list[str] = field(default_factory=list)
    matched_notes: int = 0
    via_summary: bool = False
    summary_similarity: float | None = None
    bm25_rank: int | None = None
    passed_floor: bool = True
    floor_reason: str = ""

    @property
    def page_label(self) -> str:
        if self.page_start is None:
            return ""
        if self.page_end is None or self.page_start == self.page_end:
            return f"p. {self.page_start}"
        return f"p. {self.page_start}-{self.page_end}"

    @property
    def origin_label(self) -> str:
        """Human-readable account of which signals found this chapter."""
        parts = []
        if self.via_notes:
            parts.append(f"{self.matched_notes} nota(s)")
        if self.via_summary:
            parts.append("resumo")
        return " + ".join(parts) or "-"


@dataclass
class SourceMatch:
    """A source (book, article, handout) and its matching chapters."""

    source_id: str
    citekey: str
    title: str
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    score: float = 0.0
    chapters: list[ChapterMatch] = field(default_factory=list)
    has_summary: bool = False

    @property
    def total_notes(self) -> int:
        return sum(c.note_count for c in self.chapters)


@dataclass
class CatalogResult:
    """Result of :func:`run_catalog`.

    ``sources`` is what cleared the floor, grouped by source. ``candidates`` is
    the raw chapter pool before the floor, so an over-strict threshold is
    visible instead of silently returning nothing.
    """

    query: str
    sources: list[SourceMatch] = field(default_factory=list)
    candidates: list[ChapterMatch] = field(default_factory=list)
    retrieval_params: dict = field(default_factory=dict)


def _rrf(rank: int, k: int) -> float:
    return 1.0 / (k + rank)


def _chapters_from_notes(
    db: StateDB, retriever: Retriever, query: str, topk: int, rrf_k: int
) -> tuple[dict[str, float], dict[str, list[str]]]:
    """Signal A: rank chapters by the permanent notes they produced.

    A chapter inherits the RRF contribution of each of its matching notes, so a
    chapter that produced three relevant notes outranks one that produced a
    single borderline note.

    **Graph expansion is off, and hop-0 seeds are the only input.** In
    `search_notes` a neighbour is *additive context* — it enters `hits` on the
    strength of the seed that pulled it in, not on its own relevance. That is
    right for RAG and wrong here: a neighbour treats something *related* to the
    question, which is no evidence that its chapter treats the question. Left on,
    one passing seed drags in up to `max_neighbors` chapters and an off-domain
    query "finds" the whole vault. Same reasoning as ADR-045, which derives
    corroboration from `hop == 0` seeds only.
    """
    result = retriever.search_notes(query, topk=topk, expand_graph=False)
    scores: dict[str, float] = {}
    note_ids: dict[str, list[str]] = {}
    for rank, hit in enumerate(result.hits, start=1):
        if hit.hop != 0:
            continue
        for chapter_id in db.get_chapters_for_note(hit.note_id):
            scores[chapter_id] = scores.get(chapter_id, 0.0) + _rrf(rank, rrf_k)
            note_ids.setdefault(chapter_id, []).append(hit.note_id)
    return scores, note_ids


def run_catalog(
    cfg: AppConfig,
    db: StateDB,
    idx: VectorIndex,
    query: str,
    *,
    note_topk: int | None = None,
    summary_topk: int | None = None,
) -> CatalogResult:
    """Find the sources whose chapters treat ``query``. No LLM call."""
    cat_cfg = cfg.retrieval.catalog
    note_topk = note_topk if note_topk is not None else cat_cfg.note_topk
    summary_topk = summary_topk if summary_topk is not None else cat_cfg.summary_topk
    rrf_k = cfg.retrieval.rrf_k

    retriever = Retriever(cfg, db, idx)
    note_scores, note_ids = _chapters_from_notes(db, retriever, query, note_topk, rrf_k)
    summary_result = retriever.search_chapter_summaries(query, topk=summary_topk)

    floor_cfg = cfg.retrieval.chapter_floor
    result = CatalogResult(
        query=query,
        retrieval_params={
            "mode": cfg.retrieval.mode,
            "note_topk": note_topk,
            "summary_topk": summary_topk,
            "rrf_k": rrf_k,
            "chapter_floor_enabled": floor_cfg.enabled,
            "chapter_min_vector_similarity": floor_cfg.min_vector_similarity,
            "chapter_absolute_min_similarity": floor_cfg.absolute_min_similarity,
            "chapter_bm25_bypass_max_rank": floor_cfg.bm25_bypass_max_rank,
            "max_sources": cat_cfg.max_sources,
            "max_chapters_per_source": cat_cfg.max_chapters_per_source,
        },
    )

    # Summary hits that cleared the floor, keyed by chapter.
    summary_hits = {h.chapter_id: h for h in summary_result.hits}
    all_summary = {c.chapter_id: c for c in summary_result.candidates}

    # A chapter enters the answer through either signal.
    chapter_ids = set(note_scores) | set(summary_hits)
    candidate_ids = chapter_ids | set(all_summary)

    matches: dict[str, ChapterMatch] = {}
    for chapter_id in candidate_ids:
        chapter = db.get_chapter(chapter_id)
        if not chapter:
            continue
        hit = all_summary.get(chapter_id)
        passed_summary = chapter_id in summary_hits
        note_score = note_scores.get(chapter_id, 0.0)
        summary_score = summary_hits[chapter_id].score if passed_summary else 0.0

        matches[chapter_id] = ChapterMatch(
            chapter_id=chapter_id,
            source_id=chapter["source_id"],
            chapter_title=chapter.get("title") or chapter_id,
            score=note_score + summary_score,
            summary=chapter.get("summary") or "",
            summary_stale=bool(
                chapter.get("summary")
                and chapter.get("summary_checksum") != chapter["chapter_checksum"]
            ),
            via_notes=note_ids.get(chapter_id, []),
            matched_notes=len(note_ids.get(chapter_id, [])),
            via_summary=passed_summary,
            summary_similarity=hit.similarity if hit else None,
            bm25_rank=hit.bm25_rank if hit else None,
            # A chapter found through its notes never faced the chapter floor —
            # its evidence is a note that already cleared the note floor.
            passed_floor=bool(note_score) or passed_summary,
            floor_reason=(
                "encontrado pelas notas permanentes do capitulo"
                if note_score and not passed_summary
                else (hit.floor_reason if hit else "")
            ),
        )

    _attach_counts_and_pages(db, matches.values())
    result.candidates = sorted(matches.values(), key=lambda m: m.score, reverse=True)
    result.sources = _group_by_source(db, matches, cat_cfg)
    return result


def _attach_counts_and_pages(db: StateDB, matches) -> None:
    """Fill the SQL-derived fields (note count, page range) per source."""
    by_source: dict[str, list[ChapterMatch]] = {}
    for m in matches:
        by_source.setdefault(m.source_id, []).append(m)
    for source_id, group in by_source.items():
        counts = db.get_chapter_note_counts(source_id)
        ranges = db.get_chapter_page_ranges(source_id)
        for m in group:
            m.note_count = counts.get(m.chapter_id, 0)
            page_range = ranges.get(m.chapter_id)
            if page_range:
                m.page_start, m.page_end = page_range


def _group_by_source(db: StateDB, matches: dict[str, ChapterMatch], cat_cfg) -> list[SourceMatch]:
    """Group passing chapters under their source, ranked by summed score."""
    grouped: dict[str, list[ChapterMatch]] = {}
    for m in matches.values():
        if m.passed_floor:
            grouped.setdefault(m.source_id, []).append(m)

    sources: list[SourceMatch] = []
    for source_id, chapters in grouped.items():
        src = db.get_source(source_id)
        if not src:
            continue
        chapters.sort(key=lambda c: c.score, reverse=True)
        sources.append(
            SourceMatch(
                source_id=source_id,
                citekey=src["citekey"],
                title=src["title"],
                authors=json.loads(src.get("authors") or "[]"),
                year=src.get("year"),
                score=sum(c.score for c in chapters),
                chapters=chapters[: cat_cfg.max_chapters_per_source],
                has_summary=bool(src.get("summary")),
            )
        )
    sources.sort(key=lambda s: s.score, reverse=True)
    return sources[: cat_cfg.max_sources]
