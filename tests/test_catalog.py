"""Catalog search: which sources treat a subject (ADR-047).

The properties that matter:

* both signals reach the answer — a chapter found through its permanent notes,
  and a chapter found only through its summary (yield zero, or no `connect` yet);
* every row says *why* it is there, and a rejected chapter stays visible in
  `candidates` with its reason (ADR-010's contract);
* the note count is the SQL aggregate, never a model's guess;
* no LLM is called, ever.
"""

from __future__ import annotations

import pytest
from zettel.catalog import run_catalog
from zettel.config import AppConfig
from zettel.retrieval import ChapterSearchResult, NoteSearchResult, RetrievedChapter, RetrievedNote
from zettel.state import StateDB

SID_A = "@Autor2024Livro"
SID_B = "@Outro2020Artigo"


@pytest.fixture
def cfg(tmp_path):
    return AppConfig(vault_path=tmp_path / "vault", state_db_path=tmp_path / "state.db")


@pytest.fixture
def db(tmp_path):
    database = StateDB(tmp_path / "state.db")
    yield database
    database.close()


def _seed_source(db: StateDB, source_id: str, citekey: str, title: str, chapters: int) -> None:
    db.upsert_source(source_id, citekey, title, ["Autor"], 2024, "h", f"/tmp/{citekey}", "pdf")
    for ci in range(chapters):
        chapter_id = f"{source_id}::ch{ci:03d}"
        db.upsert_chapter(chapter_id, source_id, f"Capitulo {ci}", f"chk-{citekey}-{ci}")
        db.upsert_chunk(
            f"{chapter_id}::c0",
            source_id,
            chapter_id,
            "texto",
            f"cs-{citekey}-{ci}",
            chunk_index=ci,
            page_in_book=10 + ci,
        )


def _stub_retriever(monkeypatch, *, notes=None, chapter_hits=None, chapter_candidates=None):
    """Replace both retrieval paths; nothing touches Chroma or an LLM."""
    monkeypatch.setattr(
        "zettel.retrieval.Retriever.search_notes",
        lambda self, query, **kw: NoteSearchResult(hits=list(notes or []), candidates=[]),
    )
    monkeypatch.setattr(
        "zettel.retrieval.Retriever.search_chapter_summaries",
        lambda self, query, **kw: ChapterSearchResult(
            hits=list(chapter_hits or []),
            candidates=list(
                chapter_candidates if chapter_candidates is not None else (chapter_hits or [])
            ),
        ),
    )


def _chapter_hit(chapter_id, source_id, *, score=0.5, distance=0.4, passed=True, reason="ok"):
    return RetrievedChapter(
        chapter_id=chapter_id,
        score=score,
        source_id=source_id,
        chapter_title=chapter_id.rsplit("::", 1)[-1],
        vector_distance=distance,
        passed_floor=passed,
        floor_reason=reason,
    )


# ── Signal A: chapters found through their permanent notes ─────────────


def test_note_hit_surfaces_its_chapter_and_source(cfg, db, monkeypatch):
    _seed_source(db, SID_A, "Autor2024Livro", "Um Livro", 2)
    db.upsert_note("n1", SID_A, "/v/ZTL - n1 - a.md", "Nota A")
    db.upsert_concept("cp1", SID_A, f"{SID_A}::ch001::c0", note_id="n1")
    _stub_retriever(monkeypatch, notes=[RetrievedNote(note_id="n1", score=0.9)])

    result = run_catalog(cfg, db, None, "assunto")

    assert [s.source_id for s in result.sources] == [SID_A]
    chapter = result.sources[0].chapters[0]
    assert chapter.chapter_id == f"{SID_A}::ch001"
    assert chapter.via_notes == ["n1"]
    assert chapter.via_summary is False
    assert "notas permanentes" in chapter.floor_reason


def test_a_chapter_found_by_notes_never_faces_the_chapter_floor(cfg, db, monkeypatch):
    """Its evidence is a note that already cleared the *note* floor."""
    _seed_source(db, SID_A, "Autor2024Livro", "Um Livro", 1)
    db.upsert_note("n1", SID_A, "/v/ZTL - n1 - a.md", "Nota A")
    db.upsert_concept("cp1", SID_A, f"{SID_A}::ch000::c0", note_id="n1")
    # The summary search rejects the very same chapter.
    rejected = _chapter_hit(f"{SID_A}::ch000", SID_A, passed=False, reason="abaixo do piso")
    _stub_retriever(
        monkeypatch,
        notes=[RetrievedNote(note_id="n1", score=0.9)],
        chapter_hits=[],
        chapter_candidates=[rejected],
    )

    result = run_catalog(cfg, db, None, "assunto")
    assert result.sources and result.sources[0].chapters[0].passed_floor is True


def test_more_matching_notes_ranks_a_chapter_higher(cfg, db, monkeypatch):
    _seed_source(db, SID_A, "Autor2024Livro", "Um Livro", 2)
    for i, chapter in enumerate((0, 0, 1)):
        note_id = f"n{i}"
        db.upsert_note(note_id, SID_A, f"/v/ZTL - {note_id} - x.md", "Nota")
        db.upsert_concept(f"cp{i}", SID_A, f"{SID_A}::ch{chapter:03d}::c0", note_id=note_id)
    _stub_retriever(
        monkeypatch,
        notes=[RetrievedNote(note_id=f"n{i}", score=0.9) for i in range(3)],
    )

    result = run_catalog(cfg, db, None, "assunto")
    chapters = result.sources[0].chapters
    assert chapters[0].chapter_id == f"{SID_A}::ch000"
    assert chapters[0].matched_notes == 2


def test_graph_neighbours_are_not_evidence_for_their_chapter(cfg, db, monkeypatch):
    """A neighbour rides in on its seed's relevance, not its own.

    `search_notes` adds graph neighbours to `hits` as additive context — right
    for RAG, wrong here: a neighbour treats something *related* to the question,
    which says nothing about its chapter. Counting them let one passing seed drag
    in `max_neighbors` chapters, so an off-domain query "found" the whole vault.
    """
    _seed_source(db, SID_A, "Autor2024Livro", "Um Livro", 2)
    for i, chapter in enumerate((0, 1)):
        note_id = f"n{i}"
        db.upsert_note(note_id, SID_A, f"/v/ZTL - {note_id} - x.md", "Nota")
        db.upsert_concept(f"cp{i}", SID_A, f"{SID_A}::ch{chapter:03d}::c0", note_id=note_id)

    captured: dict = {}

    def fake_search(self, query, **kw):
        captured.update(kw)
        return NoteSearchResult(
            hits=[
                RetrievedNote(note_id="n0", score=0.9, hop=0),
                RetrievedNote(note_id="n1", score=0.4, hop=2),  # graph neighbour
            ],
            candidates=[],
        )

    monkeypatch.setattr("zettel.retrieval.Retriever.search_notes", fake_search)
    monkeypatch.setattr(
        "zettel.retrieval.Retriever.search_chapter_summaries",
        lambda self, query, **kw: ChapterSearchResult(),
    )

    result = run_catalog(cfg, db, None, "assunto")
    found = {c.chapter_id for s in result.sources for c in s.chapters}
    assert found == {f"{SID_A}::ch000"}, "o vizinho de grafo nao pode entrar"
    # And the traversal is not paid for in the first place.
    assert captured.get("expand_graph") is False


# ── Signal B: chapters found only through their summary ────────────────


def test_chapter_with_no_notes_is_still_found_by_its_summary(cfg, db, monkeypatch):
    """The reason signal B exists: extract yield zero, or no `connect` yet."""
    _seed_source(db, SID_B, "Outro2020Artigo", "Um Artigo", 1)
    db.update_chapter_summary(
        f"{SID_B}::ch000", "Trata do assunto.", ["assunto"], "chk-Outro2020Artigo-0", "m", "t"
    )
    _stub_retriever(monkeypatch, notes=[], chapter_hits=[_chapter_hit(f"{SID_B}::ch000", SID_B)])

    result = run_catalog(cfg, db, None, "assunto")
    chapter = result.sources[0].chapters[0]
    assert chapter.via_summary is True
    assert chapter.note_count == 0
    assert chapter.summary == "Trata do assunto."


def test_both_signals_fuse_into_one_row(cfg, db, monkeypatch):
    _seed_source(db, SID_A, "Autor2024Livro", "Um Livro", 1)
    db.upsert_note("n1", SID_A, "/v/ZTL - n1 - a.md", "Nota A")
    db.upsert_concept("cp1", SID_A, f"{SID_A}::ch000::c0", note_id="n1")
    _stub_retriever(
        monkeypatch,
        notes=[RetrievedNote(note_id="n1", score=0.9)],
        chapter_hits=[_chapter_hit(f"{SID_A}::ch000", SID_A)],
    )

    result = run_catalog(cfg, db, None, "assunto")
    chapters = result.sources[0].chapters
    assert len(chapters) == 1, "o capitulo nao pode aparecer duas vezes"
    assert chapters[0].via_notes and chapters[0].via_summary
    assert chapters[0].origin_label == "1 nota(s) + resumo"


# ── Grouping, counts and transparency ──────────────────────────────────


def test_sources_are_ranked_and_carry_their_note_totals(cfg, db, monkeypatch):
    _seed_source(db, SID_A, "Autor2024Livro", "Um Livro", 1)
    _seed_source(db, SID_B, "Outro2020Artigo", "Um Artigo", 1)
    db.upsert_note("n1", SID_A, "/v/ZTL - n1 - a.md", "Nota A")
    db.upsert_concept("cp1", SID_A, f"{SID_A}::ch000::c0", note_id="n1")
    _stub_retriever(
        monkeypatch,
        notes=[RetrievedNote(note_id="n1", score=0.9)],
        chapter_hits=[_chapter_hit(f"{SID_B}::ch000", SID_B, score=0.001)],
    )

    result = run_catalog(cfg, db, None, "assunto")
    assert [s.source_id for s in result.sources] == [SID_A, SID_B]
    assert result.sources[0].total_notes == 1
    assert result.sources[1].total_notes == 0
    assert result.sources[0].citekey == "Autor2024Livro"


def test_rejected_chapter_stays_visible_in_candidates(cfg, db, monkeypatch):
    """An over-strict floor must be observable, not silent (ADR-010)."""
    _seed_source(db, SID_B, "Outro2020Artigo", "Um Artigo", 1)
    rejected = _chapter_hit(
        f"{SID_B}::ch000", SID_B, passed=False, reason="similaridade 0.31 abaixo do piso (0.60)"
    )
    _stub_retriever(monkeypatch, notes=[], chapter_hits=[], chapter_candidates=[rejected])

    result = run_catalog(cfg, db, None, "assunto")
    assert result.sources == []
    assert len(result.candidates) == 1
    assert result.candidates[0].passed_floor is False
    assert "abaixo do piso" in result.candidates[0].floor_reason


def test_empty_corpus_returns_empty_not_an_error(cfg, db, monkeypatch):
    _stub_retriever(monkeypatch, notes=[], chapter_hits=[])
    result = run_catalog(cfg, db, None, "assunto")
    assert result.sources == []
    assert result.candidates == []


def test_stale_summary_is_flagged(cfg, db, monkeypatch):
    _seed_source(db, SID_B, "Outro2020Artigo", "Um Artigo", 1)
    db.update_chapter_summary(f"{SID_B}::ch000", "Resumo antigo.", [], "checksum-velho", "m", "t")
    _stub_retriever(monkeypatch, notes=[], chapter_hits=[_chapter_hit(f"{SID_B}::ch000", SID_B)])

    result = run_catalog(cfg, db, None, "assunto")
    assert result.sources[0].chapters[0].summary_stale is True


def test_page_range_comes_from_the_chunks(cfg, db, monkeypatch):
    _seed_source(db, SID_A, "Autor2024Livro", "Um Livro", 1)
    db.upsert_chunk(
        f"{SID_A}::ch000::c1",
        SID_A,
        f"{SID_A}::ch000",
        "t",
        "cs-extra",
        chunk_index=5,
        page_in_book=27,
    )
    _stub_retriever(monkeypatch, notes=[], chapter_hits=[_chapter_hit(f"{SID_A}::ch000", SID_A)])

    chapter = run_catalog(cfg, db, None, "assunto").sources[0].chapters[0]
    assert (chapter.page_start, chapter.page_end) == (10, 27)
    assert chapter.page_label == "p. 10-27"


def test_retrieval_params_snapshot_the_chapter_floor(cfg, db, monkeypatch):
    """The reader must be able to see the rules a run was judged against."""
    _stub_retriever(monkeypatch, notes=[], chapter_hits=[])
    params = run_catalog(cfg, db, None, "assunto").retrieval_params
    floor = cfg.retrieval.chapter_floor
    assert params["chapter_min_vector_similarity"] == floor.min_vector_similarity
    assert params["chapter_absolute_min_similarity"] == floor.absolute_min_similarity
    assert params["chapter_floor_enabled"] is floor.enabled
    assert "note_topk" in params and "summary_topk" in params


def test_catalog_never_calls_an_llm(cfg, db, monkeypatch):
    """A catalog answer is a table plus a count — there is nothing to generate."""

    def explode(*a, **kw):
        raise AssertionError("catalog nao pode chamar LLM")

    monkeypatch.setattr("zettel.llm.call_llm", explode)
    monkeypatch.setattr("zettel.llm.get_llm", explode)
    _seed_source(db, SID_A, "Autor2024Livro", "Um Livro", 1)
    _stub_retriever(monkeypatch, notes=[], chapter_hits=[_chapter_hit(f"{SID_A}::ch000", SID_A)])

    assert run_catalog(cfg, db, None, "assunto").sources


# ── The ADR-043 boundary: routing, never evidence ──────────────────────


def test_chapter_summaries_are_not_reachable_from_ask():
    """A summary says *where to look*; it can never be quoted as support.

    This is what keeps an LLM-authored artifact out of the territory ADR-043
    gates. `ask` builds its context from `AskResult.sources`, which are
    permanent notes hydrated by `db.get_note` — a chapter id would find nothing
    there. The guard is structural: `run_ask` must reach only the note search.
    """
    import ast
    import inspect

    from zettel import ask as ask_module

    tree = ast.parse(inspect.getsource(ask_module))
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "search_notes" in called, "sanity: run_ask usa a busca de notas"
    assert "search_chapter_summaries" not in called
    assert "query_chapter_summaries" not in called

    source = inspect.getsource(ask_module)
    assert "chapter_summaries" not in source
    assert "chapter_floor" not in source


def test_ask_retrieval_params_do_not_leak_the_chapter_floor():
    """`ask` is judged against the note floor only; mixing the two would make
    `--show-context` report a threshold that never applied."""
    import inspect

    from zettel import ask as ask_module

    assert "chapter" not in inspect.getsource(ask_module.run_ask)
