"""Tests for the hybrid Retriever (vector + BM25 RRF + graph expansion)."""

import pytest

from zettel.config import AppConfig
from zettel.retrieval import Retriever
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
            out.append({
                "id": nid, "document": f"doc {nid}", "metadata": {"title": f"T {nid}"},
                "distance": self._distances.get(nid, 0.1),
            })
            if len(out) >= n_results:
                break
        return out

    def find_similar_chunks(self, texts, n_results=3):
        return [{"id": cid, "document": f"chunk {cid}", "metadata": {}, "distance": 0.1}
                for cid in self._chunk_ids[:n_results]]


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
        distances={"n1": 0.8, "n2": 0.9},  # similarities 0.60 and 0.55 — both below floor
    )
    r = Retriever(_cfg(), db, idx)
    result = r.search_notes("pergunta totalmente fora do tema", topk=5, mode="vector")
    assert result.hits == []
    assert {c.note_id for c in result.candidates} == {"n1", "n2"}
    assert all(not c.passed_floor for c in result.candidates)


def test_search_chunks_hybrid(db):
    if not db.fts_enabled:
        pytest.skip("SQLite build sem FTS5")
    db.upsert_source("@S", "S", "T", [], None, "h", "/p", "md")
    db.upsert_chapter("@S::ch000", "@S", "Ch", "chk")
    db.upsert_chunk("@S::ch000::a", "@S", "@S::ch000", "atencao e transformers", "cka")
    idx = FakeIndex(chunk_ids=[])
    r = Retriever(_cfg(), db, idx)
    res = r.search_chunks("transformers", topk=5)
    assert any(x.note_id == "@S::ch000::a" for x in res)
    assert "atencao" in res[0].document
