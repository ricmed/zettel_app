"""Bookshelf <-> sources (#391, #392): harvest creates the LIV, a reset keeps the link."""

from __future__ import annotations

import os

import pytest
from zettel.config import HarvestConfig
from zettel.harvester.pipeline import _process_file, run_harvest
from zettel.library import book_file as bf
from zettel.library.linking import book_notes_lines, ensure_books_for_sources, sync_library
from zettel.library.mirror import refresh
from zettel.manual_lit import ensure_manual_chapter

from tests.fakes import FakeVectorIndex
from tests.library_support import REPO, library_db

ISBN = "978-85-333-0227-3"


@pytest.fixture
def cfg(tmp_path):
    from zettel.config import AppConfig

    vault = tmp_path / "vault"
    for folder in ("10_Sources", "20_Literature", "60_Estante"):
        (vault / folder).mkdir(parents=True, exist_ok=True)
    (tmp_path / "inbox").mkdir()
    return AppConfig(
        vault_path=vault,
        inbox_path=tmp_path / "inbox",
        state_db_path=tmp_path / "state.db",
        harvest=HarvestConfig(biblio_llm_enabled=False),
        library={"rating_path": REPO / "config" / "library.yaml"},
    )


@pytest.fixture
def db(cfg):
    d = library_db(cfg)
    yield d
    d.close()


def _markdown(cfg, name: str, *, title: str, author: str, doc_type: str, isbn: str = "") -> object:
    path = cfg.inbox_path / name
    isbn_line = f"isbn: {isbn}\n" if isbn else ""
    path.write_text(
        f"---\ntitle: {title}\nauthor: {author}\nyear: 1937\n{isbn_line}"
        f"document_type: {doc_type}\n---\n\n# {title}\n\nNuma toca no chao vivia um hobbit. "
        "Nao era uma toca desagradavel, suja e umida.",
        encoding="utf-8",
    )
    return path


def _harvest(cfg, db, path) -> str:
    sid, _ = _process_file(
        cfg,
        db,
        FakeVectorIndex(),
        path,
        run_id=db.start_run("t"),
        interactive=False,
        skip_biblio=True,
    )
    return sid


def test_harvesting_a_book_creates_its_liv(cfg, db):
    _markdown(cfg, "hobbit.md", title="O Hobbit", author="J. R. R. Tolkien", doc_type="livro")
    outcome = run_harvest(cfg, db, FakeVectorIndex(), interactive=False, skip_biblio=True)

    assert outcome.library["books_created"] == 1
    (book,) = bf.list_books(cfg)
    assert book.source_id == outcome.source_ids[0]
    assert book.title == "O Hobbit"
    assert book.authors == ["J. R. R. Tolkien"]
    assert book.format == "digital" and book.code == "0001 - D"


def test_an_article_gets_no_book(cfg, db):
    path = _markdown(
        cfg, "a.md", title="Um artigo", author="Ana Silva", doc_type="artigo_periodico"
    )
    sid = _harvest(cfg, db, path)
    report = ensure_books_for_sources(cfg, db, [sid])
    assert report.created == [] and bf.list_books(cfg) == []


def test_a_shelved_book_is_linked_not_duplicated(cfg, db):
    book = bf.create_book(cfg, {"title": "O Hobbit", "authors": ["Tolkien"], "format": "fisico"})
    refresh(db, cfg)
    sid = _harvest(
        cfg,
        db,
        _markdown(cfg, "h.md", title="O Hobbit", author="J. R. R. Tolkien", doc_type="livro"),
    )
    report = ensure_books_for_sources(cfg, db, [sid])

    assert report.linked == [book.book_id] and report.created == []
    assert bf.load_book(cfg, book.book_id).source_id == sid
    assert bf.load_book(cfg, book.book_id).code == "0001 - F"


def test_reset_and_reharvest_relinks_the_liv_with_its_ratings(cfg, db):
    path = _markdown(cfg, "h.md", title="O Hobbit", author="Tolkien", doc_type="livro", isbn=ISBN)
    sid = _harvest(cfg, db, path)
    sync_library(cfg, db, [sid])
    book = bf.list_books(cfg)[0]
    book.meta["ratings"] = {"plot": 5}
    book.meta["source_id"] = "@Antigo1937Hobbit"
    bf.save_book(cfg, book, expected_version=book.version)
    db.close()
    os.remove(cfg.state_db_path)

    fresh = library_db(cfg)
    new_sid = _harvest(cfg, fresh, path)
    report = sync_library(cfg, fresh, [])
    relinked = bf.load_book(cfg, book.book_id)

    assert report.relinked == [book.book_id]
    assert relinked.source_id == new_sid
    assert relinked.ratings == {"plot": 5.0}
    assert len(bf.list_books(cfg)) == 1
    fresh.close()


def test_ambiguous_relink_is_reported_not_guessed(cfg, db):
    for name, year in (("a.md", "1937"), ("b.md", "1951")):
        path = _markdown(cfg, name, title="O Hobbit", author="Tolkien", doc_type="relatorio")
        text = path.read_text(encoding="utf-8").replace("1937", year) + f" Edicao de {year}."
        path.write_text(text, encoding="utf-8")
        _harvest(cfg, db, path)
    book = bf.create_book(cfg, {"title": "O Hobbit", "authors": ["Tolkien"]})
    book.meta["source_id"] = "@Sumiu"
    bf.save_book(cfg, book, expected_version=book.version)

    report = sync_library(cfg, db, [])
    assert report.relinked == []
    assert len(report.ambiguous[book.book_id]) == 2
    assert bf.load_book(cfg, book.book_id).source_id == "@Sumiu"


def test_book_notes_block_lists_the_source_notes(cfg, db):
    sid = _harvest(
        cfg, db, _markdown(cfg, "h.md", title="O Hobbit", author="Tolkien", doc_type="livro")
    )
    sync_library(cfg, db, [sid])
    book = bf.list_books(cfg)[0]
    text = book.path.read_text(encoding="utf-8")
    assert "Nenhuma nota de leitura ainda" in text
    assert book_notes_lines(db, None)[0].startswith("_Sem fonte")


def test_pdf_of_a_textless_source_is_extracted_into_it(cfg, db):
    """#392: a hand-made source found by ISBN gets the file's text, same citekey."""
    sid = "@Tolkien1937Hobbit"
    db.upsert_source(
        source_id=sid,
        citekey="Tolkien1937Hobbit",
        title="O Hobbit",
        authors=["Tolkien"],
        year=1937,
        file_checksum="",
        origin_path="",
        origin_type="md",
        origin="manual",
        document_type="livro",
        isbn=ISBN.replace("-", ""),
    )
    chapter = ensure_manual_chapter(db, sid)
    db.upsert_chunk(
        chunk_id=f"{sid}::manual::0001",
        source_id=sid,
        chapter_id=chapter,
        text="Trecho escrito a mao.",
        chunk_checksum="x",
        locator="",
        status="persisted",
    )
    db.conn.commit()

    path = _markdown(cfg, "h.md", title="O Hobbit", author="Tolkien", doc_type="livro", isbn=ISBN)
    assert _harvest(cfg, db, path) == sid

    chunks = db.get_chunks_for_source(sid)
    assert db.get_source(sid)["extracted_text"]
    assert any(c["chunk_id"] == f"{sid}::manual::0001" for c in chunks)
    assert any(c["chapter_id"] != chapter for c in chunks)
    assert len(list((cfg.vault_path / "10_Sources").glob("*.md"))) == 1


def _src_code(cfg, source_id: str):
    from zettel.library.sources import source_note_path
    from zettel.vault import parse_frontmatter

    meta, _ = parse_frontmatter(source_note_path(cfg, source_id).read_text(encoding="utf-8"))
    return meta.get("code")


def test_sync_gives_old_book_sources_a_liv_in_one_sequence(cfg, db):
    first = _harvest(
        cfg, db, _markdown(cfg, "h.md", title="O Hobbit", author="Tolkien", doc_type="livro")
    )
    bf.create_book(cfg, {"title": "Duna", "authors": ["Frank Herbert"], "format": "fisico"})
    refresh(db, cfg)
    article = _harvest(
        cfg,
        db,
        _markdown(cfg, "a.md", title="Um artigo", author="Ana Silva", doc_type="artigo_periodico"),
    )
    second = _harvest(
        cfg, db, _markdown(cfg, "s.md", title="O Silmarillion", author="Tolkien", doc_type="livro")
    )

    report = sync_library(cfg, db)

    codes = {b.source_id: b.code for b in bf.list_books(cfg)}
    assert len(report.created) == 2
    assert codes[first] == "0002 - D" and codes[second] == "0003 - D"
    assert article not in codes
    assert _src_code(cfg, first) == "0002 - D" and _src_code(cfg, article) is None
    assert sync_library(cfg, db).created == []  # a segunda vez não cria nada


def test_src_code_follows_the_format_and_leaves_with_the_liv(cfg, db):
    from zettel.library.sources import sync_source_code, sync_source_codes

    sid = _harvest(
        cfg, db, _markdown(cfg, "h.md", title="O Hobbit", author="Tolkien", doc_type="livro")
    )
    sync_library(cfg, db)
    (book,) = bf.list_books(cfg)
    assert _src_code(cfg, sid) == "0001 - D"

    book.meta["format"] = "fisico"
    book = bf.save_book(cfg, book, expected_version=book.version)
    sync_source_code(cfg, db, book)
    assert _src_code(cfg, sid) == "0001 - F"

    book.path.unlink()
    refresh(db, cfg)
    sync_source_codes(cfg, db)
    assert _src_code(cfg, sid) is None
