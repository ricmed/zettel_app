"""A estante antiga vira o SRC e só então some."""

from __future__ import annotations

from zettel.library import book_file as bf
from zettel.library.migrate import migrate_shelf
from zettel.vault import compose_note

from tests.library_support import library_config, library_db


def _liv(cfg, name: str, meta: dict, body: str) -> object:
    folder = cfg.vault_path / "60_Estante"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(compose_note(meta, body), encoding="utf-8", newline="\n")
    return path


def _tables(db) -> set[str]:
    return {row[0] for row in db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_a_linked_note_is_deleted_only_after_the_copy_checks_out(tmp_path):
    cfg = library_config(tmp_path)
    db = library_db(cfg)
    book = bf.create_book(cfg, {"title": "O Hobbit", "authors": ["Tolkien"]})
    stem = book.path.stem
    path = _liv(
        cfg,
        "LIV - 01TEST - hobbit.md",
        {
            "type": "book",
            "title": "O Hobbit",
            "authors": ["Tolkien"],
            "source_id": book.source_id,
            "genres": ["Fantasia"],
        },
        "## Resumo\n\nBilbo sai de casa.\n\n## Impressões\n\nGostei.\n",
    )
    report = migrate_shelf(cfg, db)
    assert report.ok, report.text()
    assert not path.exists()
    assert not (cfg.vault_path / "60_Estante").exists()
    kept = bf.load_book(cfg, book.source_id)
    assert kept.path.stem == stem
    assert kept.genres == ["Fantasia"]
    assert "Bilbo sai de casa." in kept.body
    assert "Gostei." in kept.body
    assert "books" not in _tables(db) and "book_readings" not in _tables(db)
    columns = {row[1] for row in db.conn.execute("PRAGMA table_info(sources)")}
    assert "book_id" not in columns
    db.close()


def test_a_note_without_a_source_becomes_a_src_before_it_disappears(tmp_path):
    cfg = library_config(tmp_path)
    db = library_db(cfg)
    _liv(
        cfg,
        "LIV - 01ORFA - avulsa.md",
        {"type": "book", "title": "Avulsa", "authors": ["Ana"], "status": "quero_ler"},
        "## Resumo\n\nTexto da ficha.\n",
    )
    report = migrate_shelf(cfg, db)
    assert report.ok, report.text()
    books = bf.list_books(cfg)
    assert len(books) == 1
    assert books[0].title == "Avulsa"
    assert "Texto da ficha." in books[0].body
    assert books[0].path.parent.name == "10_Sources"
    assert not (cfg.vault_path / "60_Estante").exists()
    db.close()


def test_a_different_title_stops_the_deletion_until_it_is_accepted(tmp_path):
    cfg = library_config(tmp_path)
    db = library_db(cfg)
    book = bf.create_book(cfg, {"title": "O Hobbit", "authors": ["Tolkien"]})
    path = _liv(
        cfg,
        "LIV - 01DIFF - outro.md",
        {
            "type": "book",
            "title": "Outro título",
            "authors": ["Tolkien"],
            "source_id": book.source_id,
        },
        "## Resumo\n\nDa ficha antiga.\n",
    )
    refused = migrate_shelf(cfg, db)
    assert not refused.ok
    assert path.exists()
    assert bf.load_book(cfg, book.source_id).title == "O Hobbit"
    assert "books" in _tables(db) or not db.shelf_migrated()

    accepted = migrate_shelf(cfg, db, accept=True)
    assert accepted.ok, accepted.text()
    assert not path.exists()
    assert bf.load_book(cfg, book.source_id).title == "O Hobbit"
    assert "books" not in _tables(db)
    db.close()


def test_two_orphan_copies_keep_two_sources(tmp_path):
    cfg = library_config(tmp_path)
    db = library_db(cfg)
    shared = {"type": "book", "title": "Duna", "authors": ["Frank Herbert"], "year": 1965}
    _liv(cfg, "LIV - digital.md", {**shared, "format": "digital"}, "## Resumo\n\nEdicao digital.\n")
    _liv(cfg, "LIV - fisico.md", {**shared, "format": "fisico"}, "## Resumo\n\nEdicao fisica.\n")

    report = migrate_shelf(cfg, db)
    assert report.ok, report.text()
    books = bf.list_books(cfg)
    assert len({book.source_id for book in books}) == 2
    bodies = "\n".join(book.body for book in books)
    assert "Edicao digital." in bodies and "Edicao fisica." in bodies
    db.close()
