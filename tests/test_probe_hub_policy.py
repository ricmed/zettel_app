"""Tests for scripts/probe_hub_policy.py (#299)."""

import sys
from pathlib import Path

from zettel.state import StateDB

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import probe_hub_policy as probe


def _source(tmp_path) -> Path:
    path = tmp_path / "source.db"
    db = StateDB(path)
    for nid in "ABCD":
        db.upsert_note(nid, "@S", f"/v/30_Permanent/ZTL - {nid} - x.md", title=nid)
    db.upsert_note_connection("A", "B", "supports")
    db.upsert_note_connection("B", "C", "extends")
    db.upsert_note_connection("C", "D", "corroborates", origin="derived")
    db.upsert_moc("M1", "Tema", None, "sig", body="- [[ZTL - A - x]]\n")
    db.close()
    return path


def test_unreviewed_regime_walks_only_derived_edges(tmp_path):
    result = probe.run(_source(tmp_path), [0.0, 1.0])

    reviewed, unreviewed = result["regimes"]["revisado"], result["regimes"]["nao_revisado"]
    assert (result["notes"], result["edges"], result["llm_edges"]) == (4, 3, 2)
    assert reviewed[0]["moc_reach"] == {"r1": 0.5, "r2": 0.75}
    assert unreviewed[0]["moc_reach"] == {"r1": 0.25, "r2": 0.25}
    # The factor changes what the garden weighs, never what the reader walks.
    assert unreviewed[1]["moc_reach"] == unreviewed[0]["moc_reach"]
    assert unreviewed[1]["moc_cohesion"]["max"] == reviewed[1]["moc_cohesion"]["max"]
