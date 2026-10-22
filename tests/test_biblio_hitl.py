"""The bibliographic review a harvest runs before writing a SRC note (#330).

``resolve_bibliography`` is the one place a human confirms what the file, the
frontmatter and the model said about a source. Interactive answers are scripted
by replacing Rich's ``Prompt.ask`` / ``Confirm.ask`` with queues, so every
branch runs without a terminal.
"""

from __future__ import annotations

from collections import deque
from pathlib import Path

import pytest
from zettel.bibliography import BibliographicMetadata
from zettel.config import AppConfig
from zettel.harvester.biblio_hitl import resolve_bibliography

BOOK = Path("livro.pdf")
LIVRO_TYPE = "1"  # the menu lists DOCUMENT_TYPES in order; checked below


def _book(**overrides) -> BibliographicMetadata:
    fields = {
        "document_type": "livro",
        "confidence": 0.9,
        "title": "O Capital",
        "authors": ["Karl Marx"],
        "year": 2013,
        "place": "Sao Paulo",
        "publisher": "Boitempo",
        "provenance": {"title": "user", "document_type": "heuristic"},
    }
    fields.update(overrides)
    return BibliographicMetadata(**fields)


@pytest.fixture
def answers(monkeypatch):
    """Script the review: ``answers.prompts`` and ``answers.confirms`` are consumed in order."""
    from rich.prompt import Confirm, Prompt

    script = type("Script", (), {"prompts": deque(), "confirms": deque()})()

    def prompt(*_a, **_k):
        return script.prompts.popleft()

    def confirm(*_a, **_k):
        return script.confirms.popleft()

    monkeypatch.setattr(Prompt, "ask", prompt)
    monkeypatch.setattr(Confirm, "ask", confirm)
    yield script
    assert not script.prompts and not script.confirms, "unused scripted answers"


def test_the_book_type_is_the_first_menu_entry():
    from zettel.bibliography import DOCUMENT_TYPES

    assert DOCUMENT_TYPES[int(LIVRO_TYPE) - 1] == "livro"


# -- non-interactive (web, --yes, no TTY) -------------------------------------


def test_a_complete_record_with_a_human_field_is_accepted_without_asking():
    biblio = _book()
    assert resolve_bibliography(BOOK, biblio, False, False, AppConfig()) is biblio


def test_a_complete_record_written_only_by_the_model_is_not_accepted_unasked():
    everything_llm = dict.fromkeys(
        ["document_type", "authors", "title", "place", "publisher", "year"], "llm"
    )
    biblio = _book(provenance=everything_llm)
    assert resolve_bibliography(BOOK, biblio, False, False, AppConfig()) is None
    # --skip-biblio is the explicit way through.
    assert resolve_bibliography(BOOK, biblio, False, True, AppConfig()) is biblio


def test_an_incomplete_record_is_skipped_unless_skip_biblio():
    biblio = _book(year=None)
    assert resolve_bibliography(BOOK, biblio, False, False, AppConfig()) is None
    assert resolve_bibliography(BOOK, biblio, False, True, AppConfig()) is biblio


# -- interactive ---------------------------------------------------------------


def test_a_complete_record_is_confirmed_and_kept(answers):
    answers.confirms.extend([True])
    meta = resolve_bibliography(BOOK, _book(), True, False, AppConfig())
    assert meta.title == "O Capital" and meta.year == 2013


def test_a_missing_field_is_asked_and_marked_as_the_users(answers):
    """Confidence below the limiar asks the type; the missing year is typed in."""
    answers.prompts.extend([LIVRO_TYPE, "2013"])
    answers.confirms.extend([False, True])  # optional fields? no; final confirm: yes
    meta = resolve_bibliography(BOOK, _book(year=None, confidence=0.3), True, False, AppConfig())
    assert meta.year == 2013
    assert meta.provenance["year"] == "user"
    assert meta.provenance["document_type"] == "user"
    assert meta.confidence >= AppConfig().harvest.biblio_confidence_threshold


def test_a_field_left_empty_can_be_abandoned(answers):
    answers.prompts.extend([LIVRO_TYPE, ""])
    answers.confirms.extend([False])  # continue with a required field missing? no
    assert (
        resolve_bibliography(BOOK, _book(year=None, confidence=0.3), True, False, AppConfig())
        is None
    )


def test_declining_the_final_confirmation_writes_nothing(answers):
    answers.prompts.extend([LIVRO_TYPE, "2013"])
    answers.confirms.extend([False, False])  # optional fields? no; final confirm: no
    assert (
        resolve_bibliography(BOOK, _book(year=None, confidence=0.3), True, False, AppConfig())
        is None
    )


def test_refusing_a_complete_record_opens_the_editor(answers):
    """Saying no to a complete record lets the user correct a field the model got wrong."""
    answers.confirms.extend(
        [
            False,  # complete: confirm? no -> edit
            False,  # change the document type? no
            True,  # review the required fields already filled? yes
            False,  # optional fields? no
            True,  # final confirm
        ]
    )
    # authors, title, place, publisher, year -- only the publisher changes
    answers.prompts.extend(["Karl Marx", "O Capital", "Sao Paulo", "Boitempo Editorial", "2013"])
    meta = resolve_bibliography(BOOK, _book(), True, False, AppConfig())
    assert meta.publisher == "Boitempo Editorial"
    assert meta.provenance["publisher"] == "user"


def test_optional_fields_are_filled_only_where_empty(answers):
    answers.confirms.extend([False, False, False, True, True])
    # no required review; then each empty optional field is asked once, in order
    from zettel.bibliography import BIBLIO_FRONTMATTER_FIELDS, REQUIRED_FIELDS

    biblio = _book()
    optional_empty = [
        f
        for f in BIBLIO_FRONTMATTER_FIELDS
        if f not in REQUIRED_FIELDS["livro"] and getattr(biblio, f, None) in (None, "", [])
    ]
    answers.prompts.extend(["2. ed."] + [""] * (len(optional_empty) - 1))
    meta = resolve_bibliography(BOOK, biblio, True, False, AppConfig())
    assert getattr(meta, optional_empty[0]) == "2. ed."
    assert meta.provenance[optional_empty[0]] == "user"
