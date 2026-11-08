"""The Mesa's conversation (#384): what the typed graph says about the desk, no LLM."""

from __future__ import annotations

import pytest
from zettel.config import AppConfig
from zettel.project_file import parse_body
from zettel.project_suggest import converse, section_query
from zettel.state import StateDB

A, B, C, D, E, F = (f"01J{letter * 23}" for letter in "ABCDEF")


@pytest.fixture
def db(tmp_path):
    database = StateDB(tmp_path / "state.db")
    for note_id, source in ((A, "@S1"), (B, "@S1"), (C, "@S1"), (D, "@S2"), (E, "@S3"), (F, "@S1")):
        database.upsert_note(
            note_id,
            source,
            None,
            title=f"Nota {note_id[3]}",
            body=f"> **Tese**: tese {note_id[3]}.",
        )
    yield database
    database.close()


def _desk() -> str:
    return f"""## Modelos
- [[ZTL - {A} - a]]
- [[ZTL - {B} - b]]
- [[ZTL - {C} - c]]
"""


def test_tensions_frontier_corroborations_and_isolated_cards(tmp_path, db):
    db.upsert_note_connection(A, B, "contradicts", "discordam do limiar")
    db.upsert_note_connection(A, D, "extends", "D amplia A", origin="derived")
    db.upsert_note_connection(B, D, "supports", "D apoia B", origin="manual")
    db.upsert_note_connection(E, A, "corroborates", "outra fonte", origin="derived")
    db.upsert_note_connection(F, C, "contradicts", "F discorda de C", origin="manual")
    # Not walkable: an LLM edge waiting for review, and a rejected one.
    db.upsert_note_connection(A, F, "exemplifies", "x", status="proposed")
    db.upsert_note_connection(C, D, "related", "y", status="rejected")

    talk = converse(AppConfig(), db, parse_body(_desk()))

    tension_pairs = {(t.desk_note, t.other_note, t.label) for t in talk.tensions}
    assert (A, B, "contradiz") in tension_pairs
    assert (C, F, "contradiz") in tension_pairs
    # D touches two cards and outweighs F; the proposed A->F edge adds nothing.
    assert [n.note_id for n in talk.frontier] == [D, F]
    assert len(talk.frontier[0].links) == 2
    assert {link.label for link in talk.frontier[0].links} == {"estende", "sustenta"}
    assert [(c.desk_note, c.other_note, c.label) for c in talk.corroborations] == [
        (A, E, "corroborado por")
    ]
    assert talk.isolated == [(1, C)]
    assert talk.single_source == [(1, "@S1")]
    assert talk.theses[D] == "tese D."


def test_an_empty_desk_has_nothing_to_say(db):
    talk = converse(AppConfig(), db, parse_body("## Seção\n"))
    assert talk.empty and not talk.single_source


def test_section_query_reads_the_title_and_the_cards_theses():
    body = parse_body(_desk())
    query = section_query(body, 1, {A: "boosting supera", C: "viés amostral"}, "Mesa")
    assert query == "Modelos boosting supera viés amostral"
