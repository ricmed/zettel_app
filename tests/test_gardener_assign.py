"""Tests for hybrid gardener assignment and graph helpers."""

import numpy as np

from zettel.gardener_assign import (
    assign_notes_to_categories,
    extract_note_ids_from_moc_body,
    find_moc_by_note_overlap,
    graph_cohesion,
)
from zettel.state import StateDB


def test_assign_notes_to_categories():
    cat_a = np.array([1.0, 0.0])
    cat_b = np.array([0.0, 1.0])
    category_vectors = {"CatA": cat_a, "CatB": cat_b}
    embeddings_by_id = {
        "n1": np.array([0.9, 0.1]),
        "n2": np.array([0.1, 0.9]),
    }
    buckets = assign_notes_to_categories(
        ["n1", "n2"], embeddings_by_id, category_vectors,
    )
    assert buckets["CatA"] == ["n1"]
    assert buckets["CatB"] == ["n2"]


def test_extract_note_ids_from_moc_body():
    body = "- [[ZTL - NOTE001 - slug-a]]\n- [[ZTL - NOTE002 - slug-b]]"
    assert extract_note_ids_from_moc_body(body) == {"NOTE001", "NOTE002"}


def test_find_moc_by_note_overlap(tmp_path):
    db = StateDB(tmp_path / "test.db")
    body = "- [[ZTL - NOTE001 - a]]\n- [[ZTL - NOTE002 - b]]"
    db.upsert_moc(
        "MOC1", "Topic", "/p/moc.md", "sig",
        body=body, origin="pipeline",
    )
    db.upsert_moc(
        "MOC2", "Other", "/p/moc2.md", "sig2",
        body="- [[ZTL - NOTE999 - x]]", origin="pipeline",
    )

    match = find_moc_by_note_overlap(db, ["NOTE001", "NOTE003"], threshold=0.4)
    assert match is not None
    assert match["moc_id"] == "MOC1"

    no_match = find_moc_by_note_overlap(db, ["NOTE888", "NOTE777"], threshold=0.4)
    assert no_match is None
    db.close()


def test_graph_cohesion_internal_edges(tmp_path):
    db = StateDB(tmp_path / "test.db")
    for nid in ("A", "B", "C"):
        db.upsert_note(nid, "SRC", None, nid)
    db.upsert_note_connection("A", "B", "extends", "")
    db.upsert_note_connection("B", "C", "supports", "")

    score = graph_cohesion(db, ["A", "B", "C"])
    assert score > 0

    isolated = graph_cohesion(db, ["A", "B"])
    db.upsert_note_connection("A", "X", "related", "")
    assert graph_cohesion(db, ["A", "B"]) >= 0
    db.close()
