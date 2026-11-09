"""The LIV note (#389): sequential code, round trip, version check, author sections."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st
from zettel.library import book_file as bf
from zettel.vault import partition_vault_notes

from tests.library_support import library_config


def test_code_joins_the_universal_number_and_the_format_letter(tmp_path):
    cfg = library_config(tmp_path)
    first = bf.create_book(cfg, {"title": "O Hobbit", "format": "fisico"})
    second = bf.create_book(cfg, {"title": "Duna", "format": "digital"})
    third = bf.create_book(cfg, {"title": "Ensaio sobre a cegueira", "format": "audiolivro"})

    assert first.code == "0001 - F"
    assert second.code == "0002 - D"
    assert third.code == "0003 - A"


def test_changing_the_format_changes_only_the_letter(tmp_path):
    cfg = library_config(tmp_path)
    book = bf.create_book(cfg, {"title": "O Hobbit", "format": "fisico"})
    book.meta["format"] = "digital"
    saved = bf.save_book(cfg, book, expected_version=book.version)

    assert saved.code == "0001 - D"
    assert bf.load_book(cfg, book.book_id).meta["code_number"] == 1


def test_numbers_are_never_reused(tmp_path):
    cfg = library_config(tmp_path)
    bf.create_book(cfg, {"title": "A"})
    gone = bf.create_book(cfg, {"title": "B"})
    gone.path.unlink()
    # The highest number is kept as long as a file carries it; a later one goes past it.
    bf.create_book(cfg, {"title": "C"})
    numbers = sorted(b.meta["code_number"] for b in bf.list_books(cfg))
    assert numbers == [1, 2]


def test_book_is_an_author_note_kept_by_reset(tmp_path):
    cfg = library_config(tmp_path)
    book = bf.create_book(cfg, {"title": "O Hobbit"})
    pipeline, kept = partition_vault_notes(cfg.vault_path)
    assert book.path in kept and book.path not in pipeline
    assert book.path.parent.name == "60_Estante"
    assert book.path.name.startswith(f"LIV - {book.book_id} - o-hobbit")


def test_every_property_is_present_in_order(tmp_path):
    cfg = library_config(tmp_path)
    book = bf.create_book(cfg, {"title": "O Hobbit", "genres": ["Fantasia"]})
    meta = bf.load_book(cfg, book.book_id).meta
    assert list(meta)[: len(bf.FIELD_ORDER)] == list(bf.FIELD_ORDER)
    assert meta["genres"] == ["Fantasia"]
    assert meta["ratings"] == {}
    assert meta["status"] == cfg.library.initial_status


def test_save_refuses_a_file_changed_since_it_was_read(tmp_path):
    cfg = library_config(tmp_path)
    book = bf.create_book(cfg, {"title": "O Hobbit"})
    stale = book.version
    book.path.write_text(book.path.read_text(encoding="utf-8") + "\nNota no Obsidian.\n", "utf-8")
    with pytest.raises(bf.BookConflict):
        bf.save_book(cfg, book, expected_version=stale)


def test_author_keys_survive_a_save(tmp_path):
    cfg = library_config(tmp_path)
    book = bf.create_book(cfg, {"title": "O Hobbit"})
    text = book.path.read_text(encoding="utf-8").replace("type: book", "type: book\nmeu_campo: 7")
    book.path.write_text(text, encoding="utf-8")
    fresh = bf.read_book(book.path)
    fresh.meta["status"] = "lido"
    bf.save_book(cfg, fresh, expected_version=fresh.version)
    assert bf.load_book(cfg, book.book_id).meta["meu_campo"] == 7


def test_load_finds_a_file_renamed_in_obsidian(tmp_path):
    cfg = library_config(tmp_path)
    book = bf.create_book(cfg, {"title": "O Hobbit"})
    renamed = book.path.with_name("Meu Hobbit.md")
    book.path.rename(renamed)
    assert bf.load_book(cfg, book.book_id).path == renamed


def test_readings_and_ratings_tolerate_hand_written_yaml():
    meta = {
        "readings": [{"start": "2021-01-01", "end": None}, {"end": "2024-03-02"}, "lixo", {}],
        "ratings": {"plot": 4, "art": "4.5", "edition": None, "x": "abc"},
    }
    assert [(r.start, r.end) for r in bf.readings_of(meta)] == [
        ("2021-01-01", None),
        (None, "2024-03-02"),
    ]
    assert bf.ratings_of(meta) == {"plot": 4.0, "art": 4.5}


def test_replace_section_keeps_the_rest_of_the_body():
    body = bf.initial_body("O Hobbit")
    body = bf.replace_section(body, bf.SUMMARY_HEADING, "Bilbo sai de casa.")
    body = bf.replace_section(body, bf.IMPRESSIONS_HEADING, "Gostei.\n\n```\n## não é seção\n```")
    assert bf.section_text(body, bf.SUMMARY_HEADING) == "Bilbo sai de casa."
    assert "## não é seção" in bf.section_text(body, bf.IMPRESSIONS_HEADING)
    assert "<!-- zettel:auto-book-notes:start -->" in body
    assert bf.replace_section(body, bf.SUMMARY_HEADING, "Bilbo sai de casa.") == body


@given(st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=200))
def test_summary_round_trip(text):
    body = bf.replace_section(bf.initial_body("T"), bf.SUMMARY_HEADING, text)
    expected = text.strip("\n")
    # A line that is itself an H2 would open a section: the author can write it, it just splits.
    if "\n## " not in "\n" + expected:
        assert bf.section_text(body, bf.SUMMARY_HEADING) == expected.strip()
