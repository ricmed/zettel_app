"""Reflective notes in ask and article (#397, ADR-069): analogy in a blog, out of academic."""

from __future__ import annotations

import pytest
from zettel.article import (
    ArticleCatalog,
    CatalogNote,
    CatalogSource,
    _pack_section,
    literary_mention,
)
from zettel.article_graph.search import drop_sources, excluded_sources
from zettel.ask import _build_context
from zettel.library import book_file as bf
from zettel.retrieval import RetrievedNote, Retriever
from zettel.schemas import ArticleOutlineSection

from tests.fakes import FakeVectorIndex
from tests.library_support import library_config, library_db

NOVEL, ESSAY = "01ARZ3NDEKTSV4RRFFQ69G5FA1", "01ARZ3NDEKTSV4RRFFQ69G5FA2"
NEIGHBOUR = "01ARZ3NDEKTSV4RRFFQ69G5FA3"


@pytest.fixture
def shelf(tmp_path):
    """A novel read reflectively and a technical book, each with one note."""
    cfg = library_config(tmp_path)
    db = library_db(cfg)
    db.upsert_source(
        "@Tolkien1937", "Tolkien1937", "O Hobbit", ["J. R. R. Tolkien"], 1937, "", "x", "md"
    )
    db.upsert_source(
        "@Martin2008", "Martin2008", "Código Limpo", ["Robert Martin"], 2008, "", "y", "md"
    )
    db.upsert_note(
        NOVEL, "@Tolkien1937", "/p/n.md", "Coragem improvável", body="> **Tese**: coragem"
    )
    db.upsert_note(ESSAY, "@Martin2008", "/p/e.md", "Funções pequenas", body="> **Tese**: coragem")
    db.upsert_note(NEIGHBOUR, "@Tolkien1937", "/p/v.md", "Viagem", body="> **Tese**: viagem")
    db.upsert_note_connection(ESSAY, NEIGHBOUR, "related", "")
    for title, genres, sid in (
        ("O Hobbit", ["Fantasia"], "@Tolkien1937"),
        ("Código Limpo", ["Técnico"], "@Martin2008"),
    ):
        book = bf.create_book(cfg, {"title": title, "genres": genres})
        book.meta["source_id"] = sid
        bf.save_book(cfg, book, expected_version=book.version)
    yield cfg, db
    db.close()


def test_retriever_leaves_out_every_note_of_excluded_sources(shelf):
    cfg, db = shelf
    idx = FakeVectorIndex(notes={NOVEL: 0.1, ESSAY: 0.1})
    retriever = Retriever(cfg, db, idx)
    everything = retriever.search_notes("coragem", topk=5, expand_graph=True).hits
    assert {h.note_id for h in everything} >= {NOVEL, ESSAY, NEIGHBOUR}

    kept = retriever.search_notes(
        "coragem", topk=5, expand_graph=True, exclude_source_ids=["@Tolkien1937"]
    ).hits
    # Neither the seed nor the graph neighbour of the excluded source comes back.
    assert [h.note_id for h in kept] == [ESSAY]


def test_academic_excludes_reflective_sources_blog_keeps_them(shelf):
    cfg, db = shelf
    assert excluded_sources(cfg, db, "academic") == ["@Tolkien1937"]
    assert excluded_sources(cfg, db, "blog") == []
    cfg.retrieval.article.academic_include_reflective = True
    assert excluded_sources(cfg, db, "academic") == []

    existing = [{"note_id": NOVEL}, {"note_id": ESSAY}, {"note_id": NEIGHBOUR}]
    assert drop_sources(db, existing, ["@Tolkien1937"]) == [{"note_id": ESSAY}]


def test_blog_section_gets_the_literary_mention():
    catalog = ArticleCatalog(topic="t", style="blog")
    source = CatalogSource(
        source_id="@Tolkien1937",
        title="O Hobbit",
        authors=["J. R. R. Tolkien"],
        reflective=True,
    )
    catalog.sources[source.source_id] = source
    catalog.sources["@Martin2008"] = CatalogSource(source_id="@Martin2008", title="Código Limpo")
    note = CatalogNote(
        note_id=NOVEL,
        title="Coragem improvável",
        body="corpo",
        wiki_link="[[N]]",
        source_id="@Tolkien1937",
        characters=["Bilbo"],
    )
    catalog.notes[NOVEL] = note
    catalog.notes[ESSAY] = CatalogNote(
        note_id=ESSAY, title="Funções", body="corpo", wiki_link="[[E]]", source_id="@Martin2008"
    )
    evidence = _pack_section(
        catalog, ArticleOutlineSection(heading="S", goal="g", note_ids=[NOVEL, ESSAY])
    )["evidence"]

    assert literary_mention(note, source).startswith("como Bilbo em *O Hobbit*, de ")
    assert evidence.count("leitura: reflexiva") == 1
    assert "- mencao_literaria: como Bilbo em *O Hobbit*" in evidence


def test_ask_context_names_the_work_and_marks_reflective_notes(shelf):
    _cfg, db = shelf
    hits = [
        RetrievedNote(
            note_id=NOVEL,
            score=1,
            title="Coragem improvável",
            metadata={"source_id": "@Tolkien1937"},
        ),
        RetrievedNote(
            note_id=ESSAY, score=1, title="Funções pequenas", metadata={"source_id": "@Martin2008"}
        ),
    ]
    context = _build_context(db, hits, 500, reflective={"@Tolkien1937"})
    assert "- Obra: *O Hobbit*, de" in context and "leitura reflexiva" in context
    assert "- Obra: *Código Limpo*, de" in context
    assert context.count("leitura reflexiva") == 1
