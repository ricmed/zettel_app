"""Tests for scripts/probe_moc_entry_points.py (#297)."""

import json
import sys
from pathlib import Path

import pytest
from zettel.state import StateDB

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import probe_moc_entry_points as probe


def _vault(tmp_path) -> Path:
    db_path = tmp_path / "state.db"
    db = StateDB(db_path)
    for nid in ("A", "B", "C", "D"):
        db.upsert_note(nid, "@S", f"/v/30_Permanent/ZTL - {nid} - x.md", title=nid, body="Tese.")
    for tgt in ("B", "C"):
        db.upsert_note_connection("A", tgt, "supports")
    db.upsert_note_connection("C", "D", "related")
    db.upsert_moc("M1", "Tema", None, "sig", body="- [[ZTL - A - x]]\n- [[ZTL - B - x]]\n")
    db.close()
    return db_path


def test_measure_reports_reachability_per_signal(tmp_path):
    conn, notes, edges, mocs = probe._load(_vault(tmp_path))
    graph = probe.ReadOnlyGraph(conn, edges)

    rows = probe.measure(graph, notes, edges, mocs, {}, [1])

    by_signal = {r["signal"]: r for r in rows}
    assert by_signal["internal_degree"]["entries"] == 1
    assert by_signal["internal_degree"]["all"] == {"r1": 0.75, "r2": 1.0}
    conn.close()


def test_export_sheet_is_blind_and_refuses_to_overwrite(tmp_path):
    conn, _notes, edges, mocs = probe._load(_vault(tmp_path))
    graph = probe.ReadOnlyGraph(conn, edges)
    out = tmp_path / "sheet"

    n = probe.export_sheet(graph, mocs, {}, 1, out, seed=1, force=False)

    sheet = (out / "portas-planilha.csv").read_text(encoding="utf-8-sig")
    key = json.loads((out / "portas-GABARITO-NAO-ABRIR.json").read_text(encoding="utf-8"))
    assert n == len(key)
    assert "internal_degree" not in sheet
    assert all(item["signals"] for item in key)
    with pytest.raises(SystemExit):
        probe.export_sheet(graph, mocs, {}, 1, out, seed=1, force=False)
    conn.close()
