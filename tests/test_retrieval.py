"""Tests for the hybrid Retriever (vector + BM25 RRF + graph expansion)."""

import pytest
from zettel.config import AppConfig
from zettel.retrieval import RetrievedNote, Retriever
from zettel.state import StateDB

from tests.fakes import FakeVectorIndex, calls_to


def _index(note_ids=None, chunk_ids=None, distances=None) -> FakeVectorIndex:
    """Canonical fake ranking ``note_ids`` in order; ``distances`` overrides the 0.1 default.

    Distance 0.1 is cosine 0.90, comfortably above the default 0.40 floor (ADR-058).
    """
    distances = distances or {}
    return FakeVectorIndex(
        notes={nid: distances.get(nid, 0.1) for nid in note_ids or []},
        chunks=dict.fromkeys(chunk_ids or [], 0.1),
    )


@pytest.fixture
def db(tmp_path):
    db = StateDB(tmp_path / "retr.db")
    yield db
    db.close()


def _cfg():
    return AppConfig()


def _seed_notes(db, ids):
    for nid in ids:
        db.upsert_note(nid, "@S", f"/p/{nid}.md", f"Titulo {nid}", body=f"corpo de {nid}")


def test_rrf_combines_vector_and_bm25(db):
    if not db.fts_enabled:
        pytest.skip("SQLite build sem FTS5")
    _seed_notes(db, ["n1", "n2", "n3"])
    # BM25 will match "grafo" only in n2's body.
    db.upsert_note("n2", "@S", "/p/n2.md", "Titulo n2", body="grafo de conhecimento")
    idx = _index(note_ids=["n1", "n3"])  # vector doesn't surface n2
    r = Retriever(_cfg(), db, idx)
    res = r.search_notes("grafo", topk=5, expand_graph=False).hits
    ids = [x.note_id for x in res]
    assert "n2" in ids  # surfaced purely by BM25
    assert "n1" in ids  # surfaced purely by vector


# -- the fusion itself (#328): RRF(d) = sum over lists of 1 / (k + rank_d) --


def _vector(*ids: str) -> list[dict]:
    return [{"id": nid, "distance": 0.1} for nid in ids]


def _bm25(*ids: str) -> list[dict]:
    return [{"note_id": nid} for nid in ids]


def test_rrf_scores_and_orders_by_the_formula(db):
    """Ranks picked by hand; the expected order and scores come from the formula."""
    cfg = _cfg()
    k = cfg.retrieval.rrf_k
    fused = Retriever(cfg, db, FakeVectorIndex())._rrf_fuse_notes(
        _vector("d1", "d2", "d3"), _bm25("d2", "d4", "d1")
    )
    expected = {
        "d1": 1 / (k + 1) + 1 / (k + 3),
        "d2": 1 / (k + 2) + 1 / (k + 1),
        "d3": 1 / (k + 3),
        "d4": 1 / (k + 2),
    }
    assert [hit.note_id for hit in fused] == ["d2", "d1", "d4", "d3"]
    assert {hit.note_id: hit.score for hit in fused} == pytest.approx(expected)
    assert {hit.note_id: (hit.vector_rank, hit.bm25_rank) for hit in fused} == {
        "d1": (1, 3),
        "d2": (2, 1),
        "d3": (3, None),
        "d4": (None, 2),
    }


@pytest.mark.parametrize(("k", "first"), [(0, "solo"), (60, "both")])
def test_rrf_k_decides_between_one_top_rank_and_two_middle_ones(db, k, first):
    """``solo`` is rank 1 in one list: 1/(k+1). ``both`` is rank 3 in two: 2/(k+3).

    The second wins exactly when k > 1, so the order proves ``rrf_k`` is read.
    """
    cfg = _cfg()
    cfg.retrieval.rrf_k = k
    fused = Retriever(cfg, db, FakeVectorIndex())._rrf_fuse_notes(
        _vector("solo", "x", "both"), _bm25("y", "z", "both")
    )
    assert fused[0].note_id == first


def test_rrf_ties_keep_the_vector_order_then_the_bm25_order(db):
    """Equal scores are not reordered: dense hits first, then BM25-only, each in rank order."""
    fused = Retriever(_cfg(), db, FakeVectorIndex())._rrf_fuse_notes(_vector("v1"), _bm25("b1"))
    assert [hit.note_id for hit in fused] == ["v1", "b1"]
    assert fused[0].score == fused[1].score


def test_chapter_rrf_uses_the_same_formula(db):
    """The catalog fuses chapter summaries the same way (ADR-047)."""
    cfg = _cfg()
    k = cfg.retrieval.rrf_k
    fused = Retriever(cfg, db, FakeVectorIndex())._rrf_fuse_chapters(
        _vector("c1", "c2"), [{"chapter_id": "c2"}, {"chapter_id": "c3"}]
    )
    assert [hit.chapter_id for hit in fused] == ["c2", "c1", "c3"]
    assert {hit.chapter_id: hit.score for hit in fused} == pytest.approx(
        {"c1": 1 / (k + 1), "c2": 1 / (k + 2) + 1 / (k + 1), "c3": 1 / (k + 2)}
    )


def test_vector_only_mode_ignores_fts(db):
    if not db.fts_enabled:
        pytest.skip("SQLite build sem FTS5")
    _seed_notes(db, ["n1"])
    db.upsert_note("n2", "@S", "/p/n2.md", "Titulo n2", body="grafo")
    idx = _index(note_ids=["n1"])
    r = Retriever(_cfg(), db, idx)
    res = r.search_notes("grafo", topk=5, mode="vector", expand_graph=False).hits
    ids = [x.note_id for x in res]
    assert ids == ["n1"]  # n2 (bm25-only) excluded in vector mode


def test_degrades_when_fts_disabled(db, monkeypatch):
    _seed_notes(db, ["n1"])
    monkeypatch.setattr(db, "fts_enabled", False)
    idx = _index(note_ids=["n1"])
    r = Retriever(_cfg(), db, idx)
    res = r.search_notes("qualquer", topk=5, mode="hybrid", expand_graph=False).hits
    assert [x.note_id for x in res] == ["n1"]


def test_hydration_fills_bm25_only_note(db):
    if not db.fts_enabled:
        pytest.skip("SQLite build sem FTS5")
    db.upsert_note("n2", "@S", "/p/n2.md", "Titulo Real", body="conteudo indexado")
    idx = _index(note_ids=[])  # nothing from vector; only BM25 finds n2
    r = Retriever(_cfg(), db, idx)
    res = r.search_notes("conteudo", topk=5, expand_graph=False).hits
    assert res and res[0].note_id == "n2"
    assert res[0].title == "Titulo Real"
    assert "conteudo" in res[0].document


def test_exclude_id(db):
    if not db.fts_enabled:
        pytest.skip("SQLite build sem FTS5")
    _seed_notes(db, ["n1", "n2"])
    idx = _index(note_ids=["n1", "n2"])
    r = Retriever(_cfg(), db, idx)
    res = r.search_notes("corpo", topk=5, exclude_id="n1", expand_graph=False).hits
    assert all(x.note_id != "n1" for x in res)


def test_graph_expansion_adds_neighbors(db):
    if not db.fts_enabled:
        pytest.skip("SQLite build sem FTS5")
    _seed_notes(db, ["n1", "n2"])
    db.upsert_note_connection("n1", "n2", "contradicts", "tensiona a tese")
    idx = _index(note_ids=["n1"])  # only n1 is a search seed
    r = Retriever(_cfg(), db, idx)
    res = r.search_notes("corpo", topk=1, expand_graph=True).hits
    ids = [x.note_id for x in res]
    assert "n1" in ids and "n2" in ids
    neigh = next(x for x in res if x.note_id == "n2")
    assert neigh.hop == 1
    assert neigh.via and neigh.via[-1]["relation_type"] == "contradicts"


N1, N2 = "01ARZ3NDEKTSV4RRFFQ69G5FAV", "01ARZ3NDEKTSV4RRFFQ69G5FAW"
_ZTL_BODY = (
    "> **Tese**: grafo tipado guia a busca\n\nArestas com tipo.\n\n"
    "<!-- zettel:auto-connections:start -->\n- [[ZTL - n9]]\n"
    "<!-- zettel:auto-connections:end -->\n\n"
    "## Fonte\n\n[[LIT - X]]\n\n"
    "<!-- zettel:auto-evidence:start -->\n> citacao literal\n"
    "<!-- zettel:auto-evidence:end -->\n"
)


def test_a_note_reads_the_same_by_every_retrieval_path(db):
    """#211: vector, BM25, graph and MOC hand a prompt one and the same text."""
    if not db.fts_enabled:
        pytest.skip("SQLite build sem FTS5")
    from zettel.article import merge_moc_notes
    from zettel.article_graph.search import expand_extra_hops
    from zettel.note_content import note_content

    db.upsert_note(N1, "@S", "/p/a.md", "Grafo", body=_ZTL_BODY)
    db.upsert_note(N2, "@S", "/p/b.md", "Outra", body="> **Tese**: outra\n\nX")
    db.upsert_note_connection(N2, N1, "supports", "")
    expected = note_content("Grafo", _ZTL_BODY)
    r = Retriever(_cfg(), db, _index(note_ids=[N1]))

    by_vector = r.search_notes("qualquer", topk=5, mode="vector", expand_graph=False).hits
    by_bm25 = Retriever(_cfg(), db, _index()).search_notes("tipado", expand_graph=False).hits
    by_graph = (
        Retriever(_cfg(), db, _index(note_ids=[N2]))
        .search_notes("qualquer", topk=1, mode="vector", expand_graph=True)
        .hits
    )
    by_moc = merge_moc_notes(db, [], {"body": f"- [[ZTL - {N1}|Grafo]]"})
    cfg = _cfg()
    by_hops = expand_extra_hops(
        db,
        [{"note_id": N2, "score": 1.0, "hop": 0}],
        topk=1,
        article_cfg=cfg.retrieval.article,
        graph_cfg=cfg.retrieval.graph_expansion,
    )

    documents = [
        next(h.document for h in by_vector if h.note_id == N1),
        next(h.document for h in by_bm25 if h.note_id == N1),
        next(h.document for h in by_graph if h.note_id == N1),
        next(h.document for h in by_moc if h.note_id == N1),
        next(d["document"] for d in by_hops if d["note_id"] == N1),
    ]
    assert documents == [expected] * 5
    assert "citacao literal" not in expected and "ZTL - n9" not in expected


# ── Absolute relevance floor ────────────────────────────────────────────


def test_floor_rejects_low_similarity_vector_hit(db):
    """A vector hit far below the similarity floor must not end up in `hits`."""
    _seed_notes(db, ["n1"])
    # distance=0.7 -> cosine 0.30, below the default 0.40 floor.
    idx = _index(note_ids=["n1"], distances={"n1": 0.7})
    r = Retriever(_cfg(), db, idx)
    result = r.search_notes("pergunta fora do tema", topk=5, mode="vector", expand_graph=False)
    assert result.hits == []
    # But it must still show up in the raw candidate pool, marked as rejected.
    assert len(result.candidates) == 1
    assert result.candidates[0].note_id == "n1"
    assert result.candidates[0].passed_floor is False


def test_floor_accepts_high_similarity_vector_hit(db):
    _seed_notes(db, ["n1"])
    # distance=0.2 -> similarity = 0.90, above the floor.
    idx = _index(note_ids=["n1"], distances={"n1": 0.2})
    r = Retriever(_cfg(), db, idx)
    result = r.search_notes("pergunta relevante", topk=5, mode="vector", expand_graph=False)
    assert [h.note_id for h in result.hits] == ["n1"]
    assert result.candidates[0].passed_floor is True


def test_floor_disabled_via_config_keeps_everything(db):
    _seed_notes(db, ["n1"])
    cfg = _cfg()
    cfg.retrieval.relevance_floor.enabled = False
    idx = _index(note_ids=["n1"], distances={"n1": 1.9})  # similarity ~0.05
    r = Retriever(cfg, db, idx)
    result = r.search_notes("qualquer coisa", topk=5, mode="vector", expand_graph=False)
    assert [h.note_id for h in result.hits] == ["n1"]


def test_floor_override_at_call_site(db):
    _seed_notes(db, ["n1"])
    idx = _index(note_ids=["n1"], distances={"n1": 0.7})  # cosine 0.30
    r = Retriever(_cfg(), db, idx)
    # Explicit override disables the floor for this call only.
    result = r.search_notes(
        "pergunta", topk=5, mode="vector", expand_graph=False, relevance_floor=False
    )
    assert [h.note_id for h in result.hits] == ["n1"]


def test_bm25_hit_bypasses_floor_even_without_vector_match(db):
    """A pure lexical match (no vector distance data) should pass the floor."""
    if not db.fts_enabled:
        pytest.skip("SQLite build sem FTS5")
    db.upsert_note("n2", "@S", "/p/n2.md", "Titulo n2", body="termo tecnico especifico")
    idx = _index(note_ids=[])  # vector search finds nothing
    r = Retriever(_cfg(), db, idx)
    result = r.search_notes("termo tecnico especifico", topk=5, expand_graph=False)
    assert any(h.note_id == "n2" for h in result.hits)


def test_no_hits_when_everything_below_floor_but_candidates_shown(db):
    """Simulates the 'chuva' scenario: nothing relevant, but top-k still surfaced."""
    _seed_notes(db, ["n1", "n2"])
    idx = _index(
        note_ids=["n1", "n2"],
        distances={
            "n1": 0.8,
            "n2": 0.9,
        },  # similarities 0.60 and 0.55 — both below floor
    )
    r = Retriever(_cfg(), db, idx)
    result = r.search_notes("pergunta totalmente fora do tema", topk=5, mode="vector")
    assert result.hits == []
    assert {c.note_id for c in result.candidates} == {"n1", "n2"}
    assert all(not c.passed_floor for c in result.candidates)


# ── Floor refinements: bm25_bypass_max_rank + absolute_min_similarity ──


def _floor(cfg, hit):
    """Run just the floor logic on a single synthetic hit (unit-level)."""
    r = Retriever(cfg, db=None, idx=None)
    r._apply_relevance_floor([hit], None, None)
    return hit


def test_strong_bm25_rank_bypasses_low_similarity():
    cfg = _cfg()  # default bm25_bypass_max_rank=5
    hit = RetrievedNote(note_id="n1", score=0.01, vector_distance=0.7, bm25_rank=2)  # cosine 0.30
    _floor(cfg, hit)
    assert hit.passed_floor is True
    assert "forte" in hit.floor_reason


def test_weak_bm25_rank_does_not_bypass():
    cfg = _cfg()  # default bm25_bypass_max_rank=5
    hit = RetrievedNote(note_id="n1", score=0.01, vector_distance=0.7, bm25_rank=8)  # cosine 0.30
    _floor(cfg, hit)
    # Rank 8 > max_rank 5 -> falls through to the similarity check, which fails.
    assert hit.passed_floor is False
    assert "abaixo do piso" in hit.floor_reason


def test_bm25_bypass_max_rank_is_configurable():
    cfg = _cfg()
    cfg.retrieval.relevance_floor.bm25_bypass_max_rank = 10
    hit = RetrievedNote(note_id="n1", score=0.01, vector_distance=0.7, bm25_rank=8)  # cosine 0.30
    _floor(cfg, hit)
    assert hit.passed_floor is True  # now within the widened rank window


def test_absolute_min_similarity_blocks_even_strong_bm25_bypass():
    """A hard backstop: an embedding-orthogonal note can't be rescued by BM25."""
    cfg = _cfg()  # default absolute_min_similarity=0.15
    # distance=1.8 -> cosine -0.80, below the -0.70 hard floor.
    hit = RetrievedNote(note_id="n1", score=0.01, vector_distance=1.8, bm25_rank=1)
    _floor(cfg, hit)
    assert hit.passed_floor is False
    assert "minimo absoluto" in hit.floor_reason


def test_absolute_min_similarity_does_not_block_legitimate_rescue():
    """A jargon/acronym rescue (moderate similarity, strong bm25) still works."""
    cfg = _cfg()
    # distance=1.2 -> cosine -0.20: above absolute_min_similarity (-0.70) and
    # below min_vector_similarity (0.40), but rescued by a strong bm25 rank.
    hit = RetrievedNote(note_id="n1", score=0.01, vector_distance=1.2, bm25_rank=1)
    _floor(cfg, hit)
    assert hit.passed_floor is True


def test_weak_bm25_only_hit_with_no_vector_data_fails():
    """A hit found only via a weak bm25 rank, with no vector data, is rejected."""
    cfg = _cfg()
    hit = RetrievedNote(note_id="n1", score=0.01, vector_distance=None, bm25_rank=9)
    _floor(cfg, hit)
    assert hit.passed_floor is False
    assert "fraco" in hit.floor_reason


def test_floor_reason_populated_when_disabled():
    cfg = _cfg()
    cfg.retrieval.relevance_floor.enabled = False
    hit = RetrievedNote(note_id="n1", score=0.01, vector_distance=1.9, bm25_rank=None)
    _floor(cfg, hit)
    assert hit.passed_floor is True
    assert hit.floor_reason == "piso desabilitado"


# ── Bypass coverage gate (ADR-003 addendum, 2026-09-09) ───────────────
#
# `bm25_bypass_max_rank` is a RELATIVE test: it asks whether a hit ranked well
# among whoever matched. BM25 ORs the query's terms, so on a small corpus the
# match pool is routinely smaller than the cutoff and "top 5" degenerates into
# "everything that matched at all" — a note sharing one common word with the
# question then bypasses the similarity floor. Coverage is the absolute half.


def test_low_coverage_denies_the_bypass():
    """One shared word out of four is not a strong lexical match, whatever its rank."""
    cfg = _cfg()
    hit = RetrievedNote(
        note_id="n1", score=0.01, vector_distance=0.7, bm25_rank=1, bm25_coverage=0.25
    )  # cosine 0.30, below the 0.40 floor
    _floor(cfg, hit)
    assert hit.passed_floor is False
    assert "cobertura lexical" in hit.floor_reason
    # The reason must name BOTH steps: why the bypass was denied AND the verdict
    # it then fell through to. A reader debugging retrieval needs the chain.
    assert "sem bypass" in hit.floor_reason
    assert "abaixo do piso" in hit.floor_reason


def test_full_coverage_still_bypasses():
    """The use case the bypass exists for: a one-term jargon query scores 1.00."""
    cfg = _cfg()
    hit = RetrievedNote(
        note_id="n1", score=0.01, vector_distance=0.7, bm25_rank=1, bm25_coverage=1.0
    )
    _floor(cfg, hit)
    assert hit.passed_floor is True
    assert "cobertura 100%" in hit.floor_reason


def test_coverage_exactly_at_threshold_passes():
    cfg = _cfg()  # default 0.5
    hit = RetrievedNote(
        note_id="n1", score=0.01, vector_distance=0.7, bm25_rank=1, bm25_coverage=0.5
    )
    _floor(cfg, hit)
    assert hit.passed_floor is True


def test_low_coverage_hit_still_passes_on_its_own_similarity():
    """Denying the bypass is not a rejection — the hit falls through, it is not dropped."""
    cfg = _cfg()
    hit = RetrievedNote(
        note_id="n1", score=0.01, vector_distance=0.2, bm25_rank=1, bm25_coverage=0.1
    )  # cosine 0.80, well above the 0.40 floor
    _floor(cfg, hit)
    assert hit.passed_floor is True
    assert "similaridade 0.80" in hit.floor_reason


def test_coverage_zero_disables_the_gate():
    """The escape hatch restores the pre-2026-09-09 rank-only behaviour."""
    cfg = _cfg()
    cfg.retrieval.relevance_floor.bm25_bypass_min_coverage = 0.0
    hit = RetrievedNote(
        note_id="n1", score=0.01, vector_distance=0.7, bm25_rank=1, bm25_coverage=0.0
    )
    _floor(cfg, hit)
    assert hit.passed_floor is True


def test_missing_coverage_does_not_deny_the_bypass():
    """`None` means "not measured" (FTS off, no usable term) — never a rejection."""
    cfg = _cfg()
    hit = RetrievedNote(
        note_id="n1", score=0.01, vector_distance=0.7, bm25_rank=1, bm25_coverage=None
    )
    _floor(cfg, hit)
    assert hit.passed_floor is True


def test_coverage_is_measured_against_the_real_note_text(db):
    """End to end through FTS: coverage counts query terms present in title+body."""
    if not db.fts_enabled:
        pytest.skip("SQLite build sem FTS5")
    db.upsert_note("n1", "@S", "/p/n1.md", "Sazonalidade", body="tendencia e sazonalidade")
    db.upsert_note("n2", "@S", "/p/n2.md", "Outra nota", body="fala apenas de tendencia")
    r = Retriever(_cfg(), db=db, idx=_index())

    hits = {h["note_id"]: h for h in r._bm25_notes("tendencia e sazonalidade", 20, None)}
    assert hits["n1"]["coverage"] == 1.0  # both terms present
    assert hits["n2"]["coverage"] == 0.5  # only "tendencia"


def test_coverage_ignores_stopwords_like_the_match_expression(db):
    """The denominator is the term set FTS actually searched, not every word."""
    if not db.fts_enabled:
        pytest.skip("SQLite build sem FTS5")
    db.upsert_note("n1", "@S", "/p/n1.md", "Sazonalidade", body="sazonalidade")
    r = Retriever(_cfg(), db=db, idx=_index())
    # "o", "que", "e", "a" are stopwords -> the only term is "sazonalidade".
    hits = {h["note_id"]: h for h in r._bm25_notes("o que e a sazonalidade", 20, None)}
    assert hits["n1"]["coverage"] == 1.0


def test_coverage_is_only_computed_for_bypass_eligible_hits(db):
    """Folding a note body is the expensive part; hits past the rank cutoff
    can never consult coverage, so they must not pay for it."""
    if not db.fts_enabled:
        pytest.skip("SQLite build sem FTS5")
    for i in range(8):
        db.upsert_note(f"n{i}", "@S", f"/p/n{i}.md", "Sazonalidade", body="sazonalidade")
    cfg = _cfg()
    cfg.retrieval.relevance_floor.bm25_bypass_max_rank = 3
    r = Retriever(cfg, db=db, idx=_index())

    hits = r._bm25_notes("sazonalidade", 20, None)
    assert len(hits) == 8
    assert all(h["coverage"] is not None for h in hits[:3])
    assert all(h["coverage"] is None for h in hits[3:])


def _seed(note_id: str, source_id: str) -> RetrievedNote:
    note = RetrievedNote(note_id=note_id, score=1.0, passed_floor=True)
    note.metadata["source_id"] = source_id
    return note


def test_source_quota_caps_the_candidate_source_when_others_cleared_the_floor():
    from zettel.retrieval import apply_source_quota

    eligible = [
        _seed("s1", "@A"),
        _seed("s2", "@A"),
        _seed("s3", "@A"),
        _seed("o1", "@B"),
        _seed("o2", "@C"),
    ]
    picked = apply_source_quota(eligible, 5, source_id="@A", same_source_max=2)
    sources = [hit.metadata["source_id"] for hit in picked]
    assert sources.count("@A") == 2
    assert "@B" in sources and "@C" in sources
    assert [hit.note_id for hit in picked[:2]] == ["s1", "s2"]


def test_source_quota_does_not_punch_a_hole_in_a_single_source_corpus():
    from zettel.retrieval import apply_source_quota

    eligible = [_seed(f"s{i}", "@A") for i in range(4)]
    picked = apply_source_quota(eligible, 3, source_id="@A", same_source_max=1)
    assert [hit.note_id for hit in picked] == ["s0", "s1", "s2"]


def test_source_quota_off_keeps_the_ranked_prefix():
    from zettel.retrieval import apply_source_quota

    eligible = [_seed("s1", "@A"), _seed("o1", "@B")]
    picked = apply_source_quota(eligible, 1, source_id="@A", same_source_max=0)
    assert [hit.note_id for hit in picked] == ["s1"]


def test_strict_search_reraises_an_error_that_is_not_an_outage(db):
    """Connect's strict mode converts only transport failures; a bug surfaces as itself (#329)."""
    broken = FakeVectorIndex(fail_on={"query_similar_notes": (1, KeyError("collection"))})
    retriever = Retriever(_cfg(), db, broken, strict_embedding=True)
    with pytest.raises(KeyError):
        retriever.search_notes("pergunta", mode="vector", expand_graph=False)


def test_tolerant_search_logs_and_returns_no_dense_hits(db):
    """Ask's tolerant mode: a dead embedding leaves the vector pool empty, not an exception."""
    down = FakeVectorIndex(fail_on={"query_similar_notes": (1, ConnectionError("refused"))})
    result = Retriever(_cfg(), db, down).search_notes("pergunta", mode="vector", expand_graph=False)
    assert result.hits == [] and result.candidates == []


def test_a_type_error_from_the_index_is_not_retried_without_arguments(db):
    """The search used to call again without ``purpose`` on any TypeError (#329).

    That fallback served fakes that took no ``purpose``; every index double now has
    the real signature (#327), so a TypeError is a real failure, handled like any other.
    """
    index = FakeVectorIndex(notes={"n1": 0.1}, fail_on={"query_similar_notes": (1, TypeError("x"))})
    result = Retriever(_cfg(), db, index).search_notes(
        "pergunta", mode="vector", expand_graph=False
    )
    assert result.candidates == []
    assert len(calls_to(index, "query_similar_notes")) == 1
