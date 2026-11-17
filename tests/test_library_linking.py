"""Bookshelf <-> sources (#391, #392): harvest creates the LIV, a reset keeps the link."""

from __future__ import annotations

import os

import pytest
from zettel.config import HarvestConfig
from zettel.harvester.pipeline import _process_file, run_harvest
from zettel.library import book_file as bf
from zettel.library.linking import source_notes_lines, sync_library
from zettel.library.mirror import refresh
from zettel.manual_lit import ensure_manual_chapter

from tests.fakes import FakeVectorIndex
from tests.library_support import REPO, library_db

ISBN = "978-85-333-0227-3"


@pytest.fixture
def cfg(tmp_path):
    from zettel.config import AppConfig

    vault = tmp_path / "vault"
    for folder in ("10_Sources", "20_Literature"):
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


def test_harvest_of_a_book_and_an_article_shelves_both_and_skips_a_chapter(cfg, db):
    _markdown(cfg, "hobbit.md", title="O Hobbit", author="J. R. R. Tolkien", doc_type="livro")
    _markdown(cfg, "artigo.md", title="Um artigo", author="Ana Silva", doc_type="artigo_periodico")
    _markdown(cfg, "cap.md", title="Um capitulo", author="Ana Silva", doc_type="capitulo_livro")
    outcome = run_harvest(cfg, db, FakeVectorIndex(), interactive=False, skip_biblio=True)

    assert outcome.library["shelved"] == 2
    titles = {book.title for book in bf.list_books(cfg)}
    assert titles == {"O Hobbit", "Um artigo"}
    assert list((cfg.vault_path / "10_Sources").glob("SRC - *.md"))
    assert not (cfg.vault_path / "60_Estante").exists()
    assert len(outcome.source_ids) == 3


def test_reset_keeps_the_src_and_the_next_harvest_reuses_it(cfg, db):
    path = _markdown(cfg, "h.md", title="O Hobbit", author="Tolkien", doc_type="livro", isbn=ISBN)
    sid = _harvest(cfg, db, path)
    book = bf.load_book(cfg, sid)
    book.body = bf.replace_section(book.body, bf.SUMMARY_HEADING, "Bilbo sai de casa.")
    book.meta["ratings"] = {"plot": 5}
    bf.save_book(cfg, book, expected_version=book.version)
    db.close()
    os.remove(cfg.state_db_path)

    fresh = library_db(cfg)
    new_sid = _harvest(cfg, fresh, path)
    files = list((cfg.vault_path / "10_Sources").glob("*.md"))
    kept = bf.load_book(cfg, sid)
    assert new_sid == sid
    assert len(files) == 1
    assert "Bilbo sai de casa." in kept.body
    assert kept.ratings == {"plot": 5.0}
    fresh.close()


def test_work_id_is_blank_until_set_and_survives_harvest(cfg, db):
    path = _markdown(cfg, "h.md", title="O Hobbit", author="Tolkien", doc_type="livro")
    sid = _harvest(cfg, db, path)
    book = bf.load_book(cfg, sid)
    assert not str(book.meta.get("work_id") or "").strip()
    book.meta["work_id"] = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    bf.save_book(cfg, book, expected_version=book.version)
    assert bf.load_book(cfg, sid).meta["work_id"] == "01ARZ3NDEKTSV4RRFFQ69G5FAV"

    path.write_text(path.read_text(encoding="utf-8") + "\n\nMais um paragrafo.", encoding="utf-8")
    cfg.harvest.modified_file_action = "update"
    assert _harvest(cfg, db, path) == sid
    kept = bf.load_book(cfg, sid)
    assert kept.meta["work_id"] == "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    refresh(db, cfg)
    assert db.get_source(sid)["work_id"] == "01ARZ3NDEKTSV4RRFFQ69G5FAV"


def test_book_notes_block_lists_the_source_notes(cfg, db):
    sid = _harvest(
        cfg, db, _markdown(cfg, "h.md", title="O Hobbit", author="Tolkien", doc_type="livro")
    )
    sync_library(cfg, db, [sid])
    book = bf.list_books(cfg)[0]
    text = book.path.read_text(encoding="utf-8")
    assert "Nenhuma nota de leitura ainda" in text
    assert source_notes_lines(db, None)[0].startswith("_Nenhuma")


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


def test_sync_keeps_a_code_that_was_stored_only_as_text(cfg, db):
    book = bf.create_book(cfg, {"title": "O Hobbit", "format": "fisico", "status": "lido"})
    text = book.path.read_text(encoding="utf-8")
    kept = "".join(
        line
        for line in text.splitlines(keepends=True)
        if not line.startswith(("code_number:", "format:"))
    )
    book.path.write_text(kept, encoding="utf-8", newline="\n")
    refresh(db, cfg)

    sync_library(cfg, db)
    after = book.path.read_text(encoding="utf-8")
    assert "0001 - F" in after
    assert "0001 - D" not in after


def test_shelf_codes_follow_one_sequence_including_articles(cfg, db):
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
    chapter = _harvest(
        cfg,
        db,
        _markdown(cfg, "c.md", title="Um capitulo", author="Ana", doc_type="capitulo_livro"),
    )
    sync_library(cfg, db)
    books = bf.list_books(cfg)
    ids = {b.source_id for b in books}
    assert {first, article, second} <= ids
    assert chapter not in ids
    assert all(b.code for b in books)
    assert sync_library(cfg, db).shelved == []
