"""The SQLite mirror of the bookshelf (#389): incremental, rebuilt from the files."""

from __future__ import annotations

import json
import os

from zettel.library import book_file as bf
from zettel.library.mirror import refresh

from tests.library_support import library_config, library_db


def _rows(cfg, db, **filters):
    return db.list_shelf_rows(list(cfg.library.document_types), **filters)


def test_refresh_reads_new_books_and_skips_unchanged_ones(tmp_path):
    cfg = library_config(tmp_path)
    db = library_db(cfg)
    bf.create_book(cfg, {"title": "O Hobbit", "genres": ["Fantasia"], "country": "Inglaterra"})
    bf.create_book(cfg, {"title": "Código Limpo", "genres": ["Técnico"]})

    assert refresh(db, cfg).read == 2
    assert refresh(db, cfg).read == 0

    hobbit = _rows(cfg, db, query="hobbit")[0]
    assert hobbit["nature"] == "ficcao"
    assert hobbit["reading_mode"] == "reflexao"
    assert hobbit["rating_profile"] == "ficcao_traduzida"
    assert json.loads(hobbit["genres_json"]) == ["Fantasia"]
    assert _rows(cfg, db, query="codigo")[0]["reading_mode"] == "conceito"


def test_an_obsidian_edit_is_picked_up(tmp_path):
    cfg = library_config(tmp_path)
    db = library_db(cfg)
    book = bf.create_book(cfg, {"title": "O Hobbit"})
    refresh(db, cfg)
    text = book.path.read_text(encoding="utf-8").replace("status: quero_ler", "status: lido")
    book.path.write_text(text, encoding="utf-8")
    st = book.path.stat()
    os.utime(book.path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))

    assert refresh(db, cfg).read == 1
    assert db.get_source(book.source_id)["status"] == "lido"


def test_a_deleted_file_leaves_the_mirror(tmp_path):
    cfg = library_config(tmp_path)
    db = library_db(cfg)
    book = bf.create_book(cfg, {"title": "O Hobbit"})
    refresh(db, cfg)
    book.path.unlink()
    # A linha da fonte fica; quem apaga o documento é delete-source.
    assert refresh(db, cfg).removed == 0
    assert db.get_source(book.source_id) is not None


def test_mirror_is_rebuilt_from_files_after_a_reset(tmp_path):
    cfg = library_config(tmp_path)
    db = library_db(cfg)
    book = bf.create_book(cfg, {"title": "O Hobbit", "genres": ["Fantasia"]})
    book.meta["ratings"] = {"plot": 5, "characters": 5}
    book.meta["readings"] = [{"start": "2021-01-01", "end": "2021-02-01"}]
    bf.save_book(cfg, book, expected_version=book.version)
    refresh(db, cfg)
    db.close()
    os.remove(cfg.state_db_path)

    fresh = library_db(cfg)
    assert refresh(fresh, cfg).read == 1
    row = _rows(cfg, fresh, query="hobbit")[0]
    assert row["score"] == 5.0
    assert row["last_end"] == "2021-02-01"


def test_duplicate_book_id_keeps_the_first_file(tmp_path):
    cfg = library_config(tmp_path)
    db = library_db(cfg)
    book = bf.create_book(cfg, {"title": "O Hobbit"})
    (book.path.parent / "zz copia.md").write_bytes(book.path.read_bytes())
    report = refresh(db, cfg)
    assert report.duplicate_ids == [book.source_id]
    assert db.get_source(book.source_id)["source_id"] == book.source_id


def test_filters(tmp_path):
    cfg = library_config(tmp_path)
    db = library_db(cfg)
    hobbit = bf.create_book(
        cfg, {"title": "O Hobbit", "genres": ["Fantasia"], "format": "fisico", "status": "lido"}
    )
    hobbit.meta["readings"] = [{"end": "2021-05-01"}]
    bf.save_book(cfg, hobbit, expected_version=hobbit.version)
    duna = bf.create_book(
        cfg, {"title": "Duna", "genres": ["Ficção Científica"], "format": "digital"}
    )
    refresh(db, cfg)

    assert [r["title"] for r in _rows(cfg, db, genre="fantasia")] == ["O Hobbit"]
    assert [r["title"] for r in _rows(cfg, db, format="digital")] == ["Duna"]
    assert [r["title"] for r in _rows(cfg, db, year=2021)] == ["O Hobbit"]
    assert [r["title"] for r in _rows(cfg, db, status="lido")] == ["O Hobbit"]
    assert [r["title"] for r in _rows(cfg, db, sort="code")] == ["Duna", "O Hobbit"]
    assert set(db.list_reflective_source_ids()) == {hobbit.source_id, duna.source_id}


def test_library_health_reports_repeated_codes(tmp_path):
    from zettel.library.mirror import library_health

    cfg = library_config(tmp_path)
    first = bf.create_book(cfg, {"title": "A"})
    ok, detail = library_health(cfg)
    assert ok and "1 item" in detail
    second = bf.create_book(cfg, {"title": "B"})
    second.meta["code_number"] = first.meta["code_number"]
    bf.save_book(cfg, second, expected_version=second.version)
    ok, detail = library_health(cfg)
    assert not ok and "0001" in detail


def test_library_health_reports_a_broken_vocabulary(tmp_path):
    from zettel.library.mirror import library_health

    cfg = library_config(tmp_path, rating_path=tmp_path / "nada.yaml")
    ok, detail = library_health(cfg)
    assert not ok and "nada.yaml" in detail
