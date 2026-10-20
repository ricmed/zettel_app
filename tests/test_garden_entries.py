"""Entry-point selection for MOCs and hubs (#297, ADR-060)."""

import json

import numpy as np
from zettel.config import AppConfig, GardenerConfig
from zettel.garden_entries import internal_degrees, medoid_scores, select_entry_notes
from zettel.state import StateDB


def _db(tmp_path) -> StateDB:
    db = StateDB(tmp_path / "state.db")
    for i, nid in enumerate(["A", "B", "C", "D", "E"]):
        db.upsert_note(nid, "@S", f"/v/30_Permanent/ZTL - {nid} - x.md", title=nid)
        db.conn.execute(
            "UPDATE notes SET created_at=? WHERE note_id=?", (f"2026-01-0{5 - i}T00:00:00", nid)
        )
    db.conn.commit()
    # A is the centre of the cluster; E only links outside it.
    for tgt in ("B", "C", "D"):
        db.upsert_note_connection("A", tgt, "supports")
    db.upsert_note_connection("B", "C", "related")
    db.upsert_note_connection("E", "OUTSIDE", "supports")
    return db


def test_internal_degree_counts_only_edges_inside_the_cluster(tmp_path):
    db = _db(tmp_path)

    degree = internal_degrees(db, ["A", "B", "C", "D", "E"])

    assert degree["A"] == 3 * 0.8
    assert degree["E"] == 0.0
    db.close()


def test_select_entry_notes_keeps_the_cap_and_ranks_by_degree(tmp_path):
    db = _db(tmp_path)
    cfg = GardenerConfig(max_entries_per_moc=2)

    assert select_entry_notes(db, ["E", "D", "C", "B", "A"], cfg) == ["A", "B"]
    db.close()


def test_first_in_source_prefers_the_oldest_note(tmp_path):
    db = _db(tmp_path)
    cfg = GardenerConfig(entry_selection="first_in_source", max_entries_per_moc=1)

    assert select_entry_notes(db, ["A", "B", "C", "D", "E"], cfg) == ["E"]
    db.close()


def test_relevance_uses_the_concept_score(tmp_path):
    db = _db(tmp_path)
    db.conn.execute("PRAGMA foreign_keys=OFF")
    for nid, score in (("C", 5), ("A", 3)):
        db.upsert_concept(
            f"k-{nid}",
            "@S",
            f"chunk-{nid}",
            note_id=nid,
            candidate_json=json.dumps({"relevance_score": score}),
            status="noted",
        )
    cfg = GardenerConfig(entry_selection="relevance", max_entries_per_moc=1)

    assert select_entry_notes(db, ["A", "B", "C"], cfg) == ["C"]
    db.close()


def test_medoid_prefers_the_most_central_vector():
    vectors = {
        "A": np.array([1.0, 0.0]),
        "B": np.array([0.9, 0.1]),
        "C": np.array([0.0, 1.0]),
    }

    scores = medoid_scores(["A", "B", "C"], vectors)

    assert max(scores, key=scores.get) == "B"


def test_hub_entries_keep_the_hub_first_within_the_cap(tmp_path):
    from zettel.gardener_hub import hub_entry_notes

    db = _db(tmp_path)
    cfg = AppConfig()
    cfg.gardener.max_entries_per_moc = 3

    assert hub_entry_notes(db, "A", ["A", "B", "C", "D"], cfg) == ["A", "B", "C"]
    db.close()
