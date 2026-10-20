"""Garden run report and entry-point reachability (#298)."""

from unittest.mock import MagicMock

import pytest
from zettel.config import AppConfig
from zettel.garden_report import (
    ClusterAction,
    GardenReport,
    adjacency,
    fill_reachability,
    reach_shares,
    subjects_without_door,
)
from zettel.gardener_assign import ClusterReport
from zettel.state import StateDB


def _edges(*pairs):
    return [{"source_note_id": a, "target_note_id": b} for a, b in pairs]


def test_reach_shares_counts_hops_from_entries():
    adj = adjacency(_edges(("A", "B"), ("B", "C"), ("C", "D")))

    shares = reach_shares({"A"}, adj, ["A", "B", "C", "D", "E"])

    assert shares == {"r1": 0.4, "r2": 0.6}


def test_reach_shares_ignores_entries_outside_the_universe():
    adj = adjacency(_edges(("X", "A")))

    assert reach_shares({"X"}, adj, ["A", "B"]) == {"r1": 0.5, "r2": 0.5}


def test_subjects_without_door_needs_enough_notes_and_no_entry():
    buckets = {"Etica": ["a", "b", "c"], "Dados": ["d", "e", "f"], "Pouco": ["g"]}

    assert subjects_without_door(buckets, {"a"}, min_notes=3) == ["Dados"]


def test_report_round_trips_through_json():
    import json

    report = GardenReport(
        pipeline="garden",
        actions=[ClusterAction("Etica", 12, "created", "M1")],
        reach={"r1": 0.5, "r2": 0.9},
    )

    again = GardenReport.from_dict(json.loads(report.to_json()))

    assert again == report
    assert again.action_counts() == {"created": 1}


def _note(db: StateDB, note_id: str, source: str) -> None:
    db.upsert_note(note_id, source, f"/vault/30_Permanent/ZTL - {note_id} - x.md", title=note_id)


def test_fill_reachability_walks_only_traversable_edges(tmp_path):
    db = StateDB(tmp_path / "state.db")
    for nid, src in (("A", "@S1"), ("B", "@S1"), ("C", "@S2")):
        _note(db, nid, src)
    db.upsert_note_connection("A", "B", "supports", origin="llm", status="accepted")
    db.upsert_note_connection("A", "C", "supports", origin="llm", status="proposed")

    report = fill_reachability(GardenReport(pipeline="garden"), db, {"A"})

    assert report.entries == 1
    assert report.reach["r1"] == pytest.approx(2 / 3, abs=1e-4)
    assert (report.traversable_edges, report.total_edges) == (1, 2)
    assert report.reach_by_source["@S2"]["r2"] == 0.0
    db.close()


def _garden_env(tmp_path):
    cfg = AppConfig()
    cfg.vault_path = tmp_path / "vault"
    cfg.gardener.topics_path = None
    cfg.gardener.strict_topics = False
    return cfg, StateDB(tmp_path / "state.db")


def test_run_garden_persists_its_report(tmp_path, monkeypatch):
    import zettel.gardener as gardener_mod

    cfg, db = _garden_env(tmp_path)
    monkeypatch.setattr(gardener_mod, "get_llm", lambda cfg, phase: MagicMock())
    cluster_report = ClusterReport(backends={"umap+hdbscan"}, clusters=0, notes_total=3)
    monkeypatch.setattr(gardener_mod, "_cluster_pairs", lambda cfg, idx, obs: ([], cluster_report))

    gardener_mod.run_garden(cfg, db, MagicMock())

    saved = db.latest_garden_report("garden")
    assert saved is not None
    assert saved["pipeline"] == "garden"
    assert saved["backends"] == ["umap+hdbscan"]
    assert saved["notes_total"] == 3
    db.close()


def test_plan_garden_calls_no_llm_and_writes_nothing(tmp_path, monkeypatch):
    import zettel.gardener as gardener_mod

    cfg, db = _garden_env(tmp_path)

    def forbidden(*args, **kwargs):
        raise AssertionError("plan must not reach the LLM")

    monkeypatch.setattr(gardener_mod, "get_llm", forbidden)
    monkeypatch.setattr(gardener_mod, "call_llm", forbidden)
    monkeypatch.setattr(
        gardener_mod,
        "_cluster_pairs",
        lambda cfg, idx, obs: (
            [("Etica", ["A", "B", "C"])],
            ClusterReport(buckets={"Etica": ["A", "B", "C"], "Dados": ["D", "E", "F"]}),
        ),
    )
    cfg.gardener.min_notes_for_moc = 3

    report = gardener_mod.plan_garden(cfg, db, MagicMock())

    assert report.planned is True
    assert [(a.category, a.action) for a in report.actions] == [("Etica", "created")]
    assert report.subjects_without_door == ["Dados"]
    assert db.list_mocs() == []
    assert db.latest_garden_report("garden") is None
    db.close()


def test_estimate_garden_counts_one_call_per_create_or_update(tmp_path):
    from zettel.preflight import estimate_garden

    cfg, db = _garden_env(tmp_path)
    report = GardenReport(
        pipeline="garden",
        planned=True,
        actions=[
            ClusterAction("A", 10, "created"),
            ClusterAction("B", 12, "incremental", "M1"),
            ClusterAction("C", 11, "signature_skip", "M2"),
        ],
    )

    estimate = estimate_garden(cfg, db, report)

    assert estimate.items == 2
    assert estimate.input_tokens > 0
    db.close()
