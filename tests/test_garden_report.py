"""Garden run report and entry-point reachability (#298)."""

from unittest.mock import MagicMock

import pytest
from zettel.config import AppConfig
from zettel.garden_report import (
    ClusterAction,
    GardenReport,
    adjacency,
    fill_reachability,
    graph_health,
    graph_warnings,
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


def test_plan_garden_splits_a_subject_above_the_cap(tmp_path, monkeypatch):
    import zettel.gardener as gardener_mod

    cfg, db = _garden_env(tmp_path)
    clusters = [[f"{p}{i}" for i in range(4)] for p in "ABCD"]
    monkeypatch.setattr(
        gardener_mod,
        "_cluster_pairs",
        lambda cfg, idx, obs: ([("Etica", ids) for ids in clusters], ClusterReport()),
    )

    report = gardener_mod.plan_garden(cfg, db, MagicMock())

    assert [a.action for a in report.actions] == ["moc_of_mocs"] + ["submoc"] * 4
    assert report.actions[0].size == 16
    assert all(a.moc_id is None for a in report.actions)
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
            ClusterAction("D", 40, "moc_of_mocs"),
            ClusterAction("D", 20, "submoc"),
            ClusterAction("E", 15, "home", "M3"),
        ],
    )

    estimate = estimate_garden(cfg, db, report)

    assert estimate.items == 5
    assert estimate.input_tokens > 0
    db.close()


# ── Graph warnings (#299) ────────────────────────────────────────────


def test_graph_warnings_flag_low_walkable_share_and_low_reach():
    cfg = AppConfig().gardener
    report = GardenReport(
        pipeline="garden",
        total_edges=100,
        traversable_edges=20,
        proposed_edges=80,
        entries=10,
        reach={"r1": 0.1, "r2": 0.3},
    )

    warnings = graph_warnings(report, cfg)

    assert len(warnings) == 3
    assert "20/100" in warnings[0] and "zettel links" in warnings[0]
    assert "30%" in warnings[1]
    assert "80 aresta(s) propostas" in warnings[2]
    assert len(graph_warnings(report, cfg, with_proposed_note=False)) == 2


def test_graph_warnings_are_quiet_on_a_healthy_graph():
    report = GardenReport(
        pipeline="garden", total_edges=10, traversable_edges=9, entries=5, reach={"r2": 0.9}
    )

    assert graph_warnings(report, AppConfig().gardener) == []
    assert (
        "rode `zettel connect`" in graph_warnings(GardenReport("garden"), AppConfig().gardener)[0]
    )


def test_graph_health_reads_current_edges_and_the_last_reach(tmp_path):
    cfg, db = _garden_env(tmp_path)
    run_id = db.start_run("garden")
    db.save_garden_report(
        run_id, "garden", GardenReport("garden", entries=3, reach={"r2": 0.2}).to_json()
    )
    db.upsert_note_connection("A", "B", "supports", status="proposed")
    db.upsert_note_connection("A", "C", "corroborates", origin="derived")

    report, problems = graph_health(db, cfg.gardener)

    assert (report.traversable_edges, report.total_edges, report.proposed_edges) == (1, 2, 1)
    assert len(problems) == 1 and "20%" in problems[0]
    db.close()
