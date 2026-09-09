"""Tests for the hybrid Retriever (vector + BM25 RRF + graph expansion)."""

import pytest
from zettel.config import AppConfig
from zettel.retrieval import RetrievedNote, Retriever
from zettel.state import StateDB


class FakeIndex:
    """Stub VectorIndex returning a fixed ranked list of note ids.

    ``distances`` lets a test control the per-note vector distance (default
    0.1 -> similarity 0.95, comfortably above the default 0.70 floor).
    """

    def __init__(self, note_ids=None, chunk_ids=None, distances=None):
        self._note_ids = note_ids or []
        self._chunk_ids = chunk_ids or []
        self._distances = distances or {}

    def query_similar_notes(self, query_text, n_results=5, exclude_id=None):
        out = []
        for nid in self._note_ids:
            if exclude_id and nid == exclude_id:
                continue
            out.append(
                {
                    "id": nid,
                    "document": f"doc {nid}",
                    "metadata": {"title": f"T {nid}"},
                    "distance": self._distances.get(nid, 0.1),
                }
            )
            if len(out) >= n_results:
                break
        return out

    def find_similar_chunks(self, texts, n_results=3):
        return [
            {"id": cid, "document": f"chunk {cid}", "metadata": {}, "distance": 0.1}
            for cid in self._chunk_ids[:n_results]
        ]


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
    idx = FakeIndex(note_ids=["n1", "n3"])  # vector doesn't surface n2
    r = Retriever(_cfg(), db, idx)
    res = r.search_notes("grafo", topk=5, expand_graph=False).hits
    ids = [x.note_id for x in res]
    assert "n2" in ids  # surfaced purely by BM25
    assert "n1" in ids  # surfaced purely by vector


def test_vector_only_mode_ignores_fts(db):
    if not db.fts_enabled:
        pytest.skip("SQLite build sem FTS5")
    _seed_notes(db, ["n1"])
    db.upsert_note("n2", "@S", "/p/n2.md", "Titulo n2", body="grafo")
    idx = FakeIndex(note_ids=["n1"])
    r = Retriever(_cfg(), db, idx)
    res = r.search_notes("grafo", topk=5, mode="vector", expand_graph=False).hits
    ids = [x.note_id for x in res]
    assert ids == ["n1"]  # n2 (bm25-only) excluded in vector mode


def test_degrades_when_fts_disabled(db, monkeypatch):
    _seed_notes(db, ["n1"])
    monkeypatch.setattr(db, "fts_enabled", False)
    idx = FakeIndex(note_ids=["n1"])
    r = Retriever(_cfg(), db, idx)
    res = r.search_notes("qualquer", topk=5, mode="hybrid", expand_graph=False).hits
    assert [x.note_id for x in res] == ["n1"]


def test_hydration_fills_bm25_only_note(db):
    if not db.fts_enabled:
        pytest.skip("SQLite build sem FTS5")
    db.upsert_note("n2", "@S", "/p/n2.md", "Titulo Real", body="conteudo indexado")
    idx = FakeIndex(note_ids=[])  # nothing from vector; only BM25 finds n2
    r = Retriever(_cfg(), db, idx)
    res = r.search_notes("conteudo", topk=5, expand_graph=False).hits
    assert res and res[0].note_id == "n2"
    assert res[0].title == "Titulo Real"
    assert "conteudo" in res[0].document


def test_exclude_id(db):
    if not db.fts_enabled:
        pytest.skip("SQLite build sem FTS5")
    _seed_notes(db, ["n1", "n2"])
    idx = FakeIndex(note_ids=["n1", "n2"])
    r = Retriever(_cfg(), db, idx)
    res = r.search_notes("corpo", topk=5, exclude_id="n1", expand_graph=False).hits
    assert all(x.note_id != "n1" for x in res)


def test_graph_expansion_adds_neighbors(db):
    if not db.fts_enabled:
        pytest.skip("SQLite build sem FTS5")
    _seed_notes(db, ["n1", "n2"])
    db.upsert_note_connection("n1", "n2", "contradicts", "tensiona a tese")
    idx = FakeIndex(note_ids=["n1"])  # only n1 is a search seed
    r = Retriever(_cfg(), db, idx)
    res = r.search_notes("corpo", topk=1, expand_graph=True).hits
    ids = [x.note_id for x in res]
    assert "n1" in ids and "n2" in ids
    neigh = next(x for x in res if x.note_id == "n2")
    assert neigh.hop == 1
    assert neigh.via and neigh.via[-1]["relation_type"] == "contradicts"


# ── Absolute relevance floor ────────────────────────────────────────────


def test_floor_rejects_low_similarity_vector_hit(db):
    """A vector hit far below the similarity floor must not end up in `hits`."""
    _seed_notes(db, ["n1"])
    # distance=0.7 -> similarity = 1 - 0.7/2 = 0.65, below the default 0.70 floor.
    idx = FakeIndex(note_ids=["n1"], distances={"n1": 0.7})
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
    idx = FakeIndex(note_ids=["n1"], distances={"n1": 0.2})
    r = Retriever(_cfg(), db, idx)
    result = r.search_notes("pergunta relevante", topk=5, mode="vector", expand_graph=False)
    assert [h.note_id for h in result.hits] == ["n1"]
    assert result.candidates[0].passed_floor is True


def test_floor_disabled_via_config_keeps_everything(db):
    _seed_notes(db, ["n1"])
    cfg = _cfg()
    cfg.retrieval.relevance_floor.enabled = False
    idx = FakeIndex(note_ids=["n1"], distances={"n1": 1.9})  # similarity ~0.05
    r = Retriever(cfg, db, idx)
    result = r.search_notes("qualquer coisa", topk=5, mode="vector", expand_graph=False)
    assert [h.note_id for h in result.hits] == ["n1"]


def test_floor_override_at_call_site(db):
    _seed_notes(db, ["n1"])
    idx = FakeIndex(note_ids=["n1"], distances={"n1": 0.7})  # similarity 0.65
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
    idx = FakeIndex(note_ids=[])  # vector search finds nothing
    r = Retriever(_cfg(), db, idx)
    result = r.search_notes("termo tecnico especifico", topk=5, expand_graph=False)
    assert any(h.note_id == "n2" for h in result.hits)


def test_no_hits_when_everything_below_floor_but_candidates_shown(db):
    """Simulates the 'chuva' scenario: nothing relevant, but top-k still surfaced."""
    _seed_notes(db, ["n1", "n2"])
    idx = FakeIndex(
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
    hit = RetrievedNote(note_id="n1", score=0.01, vector_distance=0.7, bm25_rank=2)  # sim=0.65
    _floor(cfg, hit)
    assert hit.passed_floor is True
    assert "forte" in hit.floor_reason


def test_weak_bm25_rank_does_not_bypass():
    cfg = _cfg()  # default bm25_bypass_max_rank=5
    hit = RetrievedNote(note_id="n1", score=0.01, vector_distance=0.7, bm25_rank=8)  # sim=0.65
    _floor(cfg, hit)
    # Rank 8 > max_rank 5 -> falls through to the similarity check, which fails.
    assert hit.passed_floor is False
    assert "abaixo do piso" in hit.floor_reason


def test_bm25_bypass_max_rank_is_configurable():
    cfg = _cfg()
    cfg.retrieval.relevance_floor.bm25_bypass_max_rank = 10
    hit = RetrievedNote(note_id="n1", score=0.01, vector_distance=0.7, bm25_rank=8)  # sim=0.65
    _floor(cfg, hit)
    assert hit.passed_floor is True  # now within the widened rank window


def test_absolute_min_similarity_blocks_even_strong_bm25_bypass():
    """A hard backstop: an embedding-orthogonal note can't be rescued by BM25."""
    cfg = _cfg()  # default absolute_min_similarity=0.15
    # distance=1.8 -> similarity = 1 - 1.8/2 = 0.10, below the 0.15 hard floor.
    hit = RetrievedNote(note_id="n1", score=0.01, vector_distance=1.8, bm25_rank=1)
    _floor(cfg, hit)
    assert hit.passed_floor is False
    assert "minimo absoluto" in hit.floor_reason


def test_absolute_min_similarity_does_not_block_legitimate_rescue():
    """A jargon/acronym rescue (moderate similarity, strong bm25) still works."""
    cfg = _cfg()
    # similarity 0.40 -- well above absolute_min_similarity (0.15) and below
    # min_vector_similarity (0.70), but rescued by a strong bm25 rank.
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
    )  # sim=0.65, below the 0.70 floor
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
    )  # sim=0.90, well above the floor
    _floor(cfg, hit)
    assert hit.passed_floor is True
    assert "similaridade 0.90" in hit.floor_reason


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
    r = Retriever(_cfg(), db=db, idx=FakeIndex())

    hits = {h["note_id"]: h for h in r._bm25_notes("tendencia e sazonalidade", 20, None)}
    assert hits["n1"]["coverage"] == 1.0  # both terms present
    assert hits["n2"]["coverage"] == 0.5  # only "tendencia"


def test_coverage_ignores_stopwords_like_the_match_expression(db):
    """The denominator is the term set FTS actually searched, not every word."""
    if not db.fts_enabled:
        pytest.skip("SQLite build sem FTS5")
    db.upsert_note("n1", "@S", "/p/n1.md", "Sazonalidade", body="sazonalidade")
    r = Retriever(_cfg(), db=db, idx=FakeIndex())
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
    r = Retriever(cfg, db=db, idx=FakeIndex())

    hits = r._bm25_notes("sazonalidade", 20, None)
    assert len(hits) == 8
    assert all(h["coverage"] is not None for h in hits[:3])
    assert all(h["coverage"] is None for h in hits[3:])
