"""Edições da mesma obra (ADR-071): work_id, notas nas duas fichas, corroboração."""

from __future__ import annotations

import sqlite3

import pytest
from zettel.connector.links import corroborating_note_ids
from zettel.library import book_file as bf
from zettel.library.edition_match import compare_excerpt
from zettel.library.mirror import library_health, refresh
from zettel.library.works import edition_label, link_editions, sibling_source_ids, unlink_edition
from zettel.manual_lit import ensure_manual_chapter
from zettel.retrieval import RetrievedNote
from zettel.state import StateDB
from zettel.state.schema import SCHEMA_SQL

from tests.fakes import FakeVectorIndex
from tests.library_support import library_config, library_db


@pytest.fixture
def cfg(tmp_path):
    return library_config(tmp_path)


@pytest.fixture
def db(cfg):
    opened = library_db(cfg)
    yield opened
    opened.close()


def _book(cfg, db, title: str, *, fmt: str = "fisico", isbn: str = ""):
    fields = {"title": title, "authors": ["Ada Lovelace"], "format": fmt, "year": 2024}
    if isbn:
        fields["isbn"] = isbn
    book = bf.create_book(cfg, fields, db)
    refresh(db, cfg)
    return book


def _note(db, source_id: str, note_id: str, title: str) -> None:
    db.upsert_note(note_id, source_id, None, title=title, body="> **Tese**: uma ideia.")


def _chunk_with_note(db, source_id: str, chunk_id: str, note_id: str, title: str) -> None:
    chapter = ensure_manual_chapter(db, source_id)
    db.upsert_chunk(
        chunk_id=chunk_id,
        source_id=source_id,
        chapter_id=chapter,
        text="Um agente é um programa que age.",
        chunk_checksum="c",
        status="persisted",
    )
    _note(db, source_id, note_id, title)
    db.upsert_concept(
        f"{chunk_id}::concept::test",
        source_id,
        chunk_id,
        note_id=note_id,
        status="noted",
    )


def test_an_old_database_gains_work_id_on_open(tmp_path):
    path = tmp_path / "old.db"
    sql = SCHEMA_SQL.replace("    work_id              TEXT,\n", "")
    conn = sqlite3.connect(path)
    conn.executescript(sql)
    conn.close()

    db = StateDB(path)
    try:
        columns = {row[1] for row in db.conn.execute("PRAGMA table_info(sources)")}
    finally:
        db.close()
    assert "work_id" in columns


def test_labels_add_the_isbn_only_when_titles_match():
    same = edition_label({"title": "Duna", "format": "fisico", "isbn": "111"}, show_isbn=True)
    other = edition_label({"title": "Duna", "format": "digital", "isbn": "222"}, show_isbn=False)
    assert same == "Duna — Físico, ISBN 111"
    assert other == "Duna — Digital"


def test_link_unlink_and_merge_list_the_note_on_both_cards(cfg, db):
    portuguese = _book(cfg, db, "Construindo Aplicações", fmt="fisico")
    english = _book(cfg, db, "Building Applications", fmt="digital")
    _note(db, english.source_id, "N-en", "O que sao agentes")

    work_id = link_editions(cfg, db, portuguese.source_id, english.source_id)
    assert work_id
    assert bf.load_book(cfg, portuguese.source_id).meta["work_id"] == work_id
    assert bf.load_book(cfg, english.source_id).meta["work_id"] == work_id
    assert sibling_source_ids(db, portuguese.source_id) == [english.source_id]
    assert db.get_note("N-en")["source_id"] == english.source_id

    pt_text = bf.load_book(cfg, portuguese.source_id).path.read_text(encoding="utf-8")
    en_text = bf.load_book(cfg, english.source_id).path.read_text(encoding="utf-8")
    assert "Desta edição" in pt_text and "Desta edição" in en_text
    assert "N-en" in pt_text and "N-en" in en_text
    assert "Building Applications" in pt_text
    assert "Construindo Aplicações" in en_text

    third = _book(cfg, db, "Construyendo aplicaciones", fmt="fisico")
    fourth = _book(cfg, db, "Applications", fmt="audiolivro")
    other = link_editions(cfg, db, third.source_id, fourth.source_id)
    merged = link_editions(cfg, db, portuguese.source_id, third.source_id)
    assert merged == other
    group = {row["source_id"] for row in db.sources_by_work(merged)}
    assert group == {
        portuguese.source_id,
        english.source_id,
        third.source_id,
        fourth.source_id,
    }

    unlink_edition(cfg, db, portuguese.source_id)
    assert not str(bf.load_book(cfg, portuguese.source_id).meta.get("work_id") or "").strip()
    # Três ainda compartilham a obra. Desligar mais uma deixa duas.
    unlink_edition(cfg, db, english.source_id)
    unlink_edition(cfg, db, third.source_id)
    assert not str(bf.load_book(cfg, fourth.source_id).meta.get("work_id") or "").strip()
    assert not str(bf.load_book(cfg, third.source_id).meta.get("work_id") or "").strip()


def test_a_lonely_work_id_is_reported(cfg, db):
    book = _book(cfg, db, "Sozinho")
    book.meta["work_id"] = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    bf.save_book(cfg, book, expected_version=book.version)
    ok, detail = library_health(cfg)
    assert not ok
    assert "01ARZ3NDEKTSV4RRFFQ69G5FAV" in detail


def test_two_sources_corroborate_until_they_are_one_work(cfg, db):
    left = _book(cfg, db, "Building Applications", fmt="digital")
    right = _book(cfg, db, "Construindo Aplicações", fmt="fisico")
    _note(db, left.source_id, "N1", "Agentes")
    _note(db, right.source_id, "N2", "Agentes")
    db.upsert_note_connection("N1", "N2", "corroborates", origin="derived")
    db.upsert_note_connection("N1", "N2", "supports", origin="llm")
    hit = RetrievedNote("N2", 1.0, vector_distance=0.1, hop=0)

    assert corroborating_note_ids(cfg, db, [hit], left.source_id, "N1") == ["N2"]

    link_editions(cfg, db, left.source_id, right.source_id)
    assert corroborating_note_ids(cfg, db, [hit], left.source_id, "N1") == []
    edges = db.get_note_connections("N1")
    assert [e["relation_type"] for e in edges] == ["supports"]

    unlink_edition(cfg, db, left.source_id)
    assert [e["relation_type"] for e in db.get_note_connections("N1")] == ["supports"]


def test_a_translated_neighbour_below_the_floor_is_still_listed(cfg, db, monkeypatch):
    portuguese = _book(cfg, db, "Construindo Aplicações", fmt="fisico")
    english = _book(cfg, db, "Building Applications", fmt="digital")
    link_editions(cfg, db, portuguese.source_id, english.source_id)
    _chunk_with_note(db, english.source_id, "@En::c-near", "N-near", "Perto")
    _chunk_with_note(db, english.source_id, "@En::c-far", "N-far", "Abaixo")
    fake = FakeVectorIndex(chunks={"@En::c-near": 0.1, "@En::c-far": 0.8})
    fake.upsert_chunk("@En::c-near", "near", {"source_id": english.source_id})
    fake.upsert_chunk("@En::c-far", "far", {"source_id": english.source_id})
    monkeypatch.setattr("zettel.index.VectorIndex", lambda **kwargs: fake)
    monkeypatch.setattr("zettel.credentials.embedding_ready", lambda _cfg: True)

    excerpt = "Um agente é um programa que age."
    match = compare_excerpt(cfg, db, portuguese.source_id, excerpt)
    by_title = {item.title: item for item in match.passages}
    assert by_title["Perto"].probable
    assert not by_title["Abaixo"].probable
    assert calls_are_the_excerpt(fake, excerpt)
    before = len(fake.calls)
    missed = compare_excerpt(cfg, db, portuguese.source_id, "")
    assert missed.needs_excerpt and len(fake.calls) == before


def calls_are_the_excerpt(fake: FakeVectorIndex, excerpt: str) -> bool:
    from tests.fakes import calls_to

    return calls_to(fake, "find_similar_chunks") == [[excerpt]]
