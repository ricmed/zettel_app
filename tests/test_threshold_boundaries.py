"""Every similarity threshold, tested at its boundary and read from the config (#325).

The older tests checked a floor of 0.40 with points at 0.30 and 0.80: any floor
between 0.31 and 0.79 passed them, so a wrong comparison or a wrong knob went
unnoticed. Here each threshold is probed at ``theta - EPS``, ``theta`` and
``theta + EPS``, with ``theta`` taken from ``AppConfig`` -- re-measuring a floor
(#281) changes the config, never these tests. ``tests/test_config.py`` pins the
values themselves.

Equality runs on stubs carrying the similarity directly: ``1 - (1 - theta)`` is
not always ``theta`` in floating point. The ``+-EPS`` probes go through the real
distance-to-cosine conversion (ADR-058).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from zettel.config import AppConfig
from zettel.connector.links import CorroborationCandidate, select_corroborating_ids
from zettel.retrieval import Retriever
from zettel.state import StateDB

from tests.fakes import FakeVectorIndex

EPS = 0.005
CFG = AppConfig()


def _verdicts(floor_cfg, hits) -> list[bool]:
    Retriever(CFG, db=None, idx=None)._apply_relevance_floor(hits, None, None, floor_cfg=floor_cfg)
    return [hit.passed_floor for hit in hits]


def _hit(similarity, *, bm25_rank=None, coverage=None):
    return SimpleNamespace(
        similarity=similarity, bm25_rank=bm25_rank, bm25_coverage=coverage, passed_floor=None
    )


# -- relevance floor (notes) and chapter floor ------------------------------


@pytest.mark.parametrize("floor_name", ["relevance_floor", "chapter_floor"])
def test_min_vector_similarity_admits_from_theta_up(floor_name):
    floor_cfg = getattr(CFG.retrieval, floor_name)
    theta = floor_cfg.min_vector_similarity
    hits = [_hit(theta - EPS), _hit(theta), _hit(theta + EPS)]
    assert _verdicts(floor_cfg, hits) == [False, True, True]


def test_absolute_minimum_stops_even_a_strong_lexical_match():
    floor_cfg = CFG.retrieval.relevance_floor
    theta = floor_cfg.absolute_min_similarity
    strong = {"bm25_rank": 1, "coverage": 1.0}
    hits = [_hit(theta - EPS, **strong), _hit(theta, **strong), _hit(theta + EPS, **strong)]
    assert _verdicts(floor_cfg, hits) == [False, True, True]


def test_bm25_bypass_ends_at_the_configured_rank():
    floor_cfg = CFG.retrieval.relevance_floor
    below_floor = (floor_cfg.absolute_min_similarity + floor_cfg.min_vector_similarity) / 2
    max_rank = floor_cfg.bm25_bypass_max_rank
    hits = [
        _hit(below_floor, bm25_rank=max_rank, coverage=1.0),
        _hit(below_floor, bm25_rank=max_rank + 1, coverage=1.0),
    ]
    assert _verdicts(floor_cfg, hits) == [True, False]


def test_bm25_bypass_needs_the_configured_coverage():
    floor_cfg = CFG.retrieval.relevance_floor
    below_floor = (floor_cfg.absolute_min_similarity + floor_cfg.min_vector_similarity) / 2
    theta = floor_cfg.bm25_bypass_min_coverage
    hits = [
        _hit(below_floor, bm25_rank=1, coverage=theta - EPS),
        _hit(below_floor, bm25_rank=1, coverage=theta),
    ]
    assert _verdicts(floor_cfg, hits) == [False, True]


@pytest.mark.parametrize(("offset", "admitted"), [(-EPS, False), (EPS, True)])
def test_chapter_search_is_gated_by_the_chapter_floor_not_the_note_floor(
    tmp_path, offset, admitted
):
    """Two separately measured knobs (ADR-047): move one, the other must not decide."""
    note_theta = CFG.retrieval.relevance_floor.min_vector_similarity
    cfg = AppConfig(retrieval={"chapter_floor": {"min_vector_similarity": note_theta + 0.15}})
    theta = cfg.retrieval.chapter_floor.min_vector_similarity
    db = StateDB(tmp_path / "state.db")
    try:
        retriever = Retriever(cfg, db, FakeVectorIndex(chapters={"ch1": 1 - (theta + offset)}))
        result = retriever.search_chapter_summaries("pergunta", mode="vector")
    finally:
        db.close()
    assert bool(result.hits) is admitted
    assert [c.chapter_id for c in result.candidates] == ["ch1"]


# -- corroboration between sources -----------------------------------------


def test_corroboration_starts_at_the_configured_similarity():
    theta = CFG.linking.corroborates_min_similarity
    candidates = [
        CorroborationCandidate("below", theta - EPS, "@B"),
        CorroborationCandidate("at", theta, "@B"),
        CorroborationCandidate("above", theta + EPS, "@B"),
    ]
    chosen = select_corroborating_ids(
        candidates, source_id="@A", note_id="SELF", min_similarity=theta, max_edges=3
    )
    assert sorted(chosen) == ["above", "at"]


# -- distant analogies -------------------------------------------------------


def test_distant_analogies_use_their_own_local_floor(tmp_path):
    """``linking.distant_analogy_min_similarity`` decides, not the note floor (#161)."""
    theta = CFG.linking.distant_analogy_min_similarity
    db = StateDB(tmp_path / "state.db")
    try:
        for nid in ("below", "above"):
            db.upsert_note(nid, "@S", None, title=nid, body="corpo sem termos da pergunta")
        index = FakeVectorIndex(notes={"below": 1 - (theta - EPS), "above": 1 - (theta + EPS)})
        hits = Retriever(CFG, db, index).search_distant_analogies(
            "consulta",
            exclude_id=None,
            exclude_ids=set(),
            topk=5,
            min_vector_similarity=theta,
        )
    finally:
        db.close()
    assert [h.note_id for h in hits] == ["above"]
