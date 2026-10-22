"""Tests for hub-anchored MOC gardener."""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from zettel.config import AppConfig, HubMocsConfig
from zettel.gardener_hub import (
    build_hub_neighborhood,
    dedup_hub_neighborhoods,
    purge_hub_pipeline_mocs,
    rank_note_hubs,
)
from zettel.state import StateDB
from zettel.vault import safe_write_note


def _perm_path(note_id: str, slug: str) -> str:
    return str(Path("vault") / "30_Permanent" / f"ZTL - {note_id} - {slug}.md")


def _setup_graph_db(tmp_path: Path) -> StateDB:
    """Build a small permanent-note graph: HUB is highly connected."""
    db = StateDB(tmp_path / "test.db")
    notes = [
        ("HUB", "Nota Central"),
        ("A", "Nota A"),
        ("B", "Nota B"),
        ("C", "Nota C"),
        ("D", "Nota D"),
        ("E", "Nota E"),
    ]
    for nid, title in notes:
        path = _perm_path(nid, title.lower().replace(" ", "-"))
        db.upsert_note(nid, "SRC", path, title, body=f"Corpo {title}")

    edges = [
        ("HUB", "A", "extends"),
        ("HUB", "B", "supports"),
        ("HUB", "C", "supports"),
        ("HUB", "D", "extends"),
        ("HUB", "E", "related"),
        ("A", "B", "related"),
        ("C", "D", "depends_on"),
    ]
    for src, tgt, rel in edges:
        db.upsert_note_connection(src, tgt, rel, "")

    return db


def test_get_weighted_note_degrees(tmp_path):
    db = _setup_graph_db(tmp_path)
    from zettel.config import DEFAULT_RELATION_WEIGHTS

    degrees = db.get_weighted_note_degrees(DEFAULT_RELATION_WEIGHTS)
    assert degrees["HUB"] > degrees["A"]
    assert degrees["HUB"] > degrees["E"]
    db.close()


def test_rank_note_hubs_percentile(tmp_path):
    db = _setup_graph_db(tmp_path)
    cfg = HubMocsConfig(selection_mode="percentile", hub_percentile=0.5, top_n_hubs=10)

    ranked = rank_note_hubs(db, cfg)
    assert ranked[0][0] == "HUB"
    db.close()


def test_rank_note_hubs_absolute(tmp_path):
    db = _setup_graph_db(tmp_path)
    cfg = HubMocsConfig(selection_mode="absolute", min_weighted_degree=2.0, top_n_hubs=10)

    ranked = rank_note_hubs(db, cfg)
    hub_ids = [nid for nid, _ in ranked]
    assert "HUB" in hub_ids
    db.close()


def test_build_hub_neighborhood(tmp_path):
    db = _setup_graph_db(tmp_path)
    cfg = HubMocsConfig(max_hops=2, max_neighbors=10, min_neighbor_weight=0.1)

    neighborhood = build_hub_neighborhood(db, "HUB", cfg)
    assert neighborhood[0] == "HUB"
    assert len(neighborhood) >= 4
    db.close()


def test_dedup_hub_neighborhoods():
    hubs = [
        ("HUB", 10.0, ["HUB", "A", "B", "C", "D"]),
        ("A", 3.0, ["A", "B"]),
    ]
    result = dedup_hub_neighborhoods(hubs, threshold=0.8)
    assert len(result) == 1
    assert result[0][0] == "HUB"


def test_dedup_keeps_distinct_neighborhoods():
    hubs = [
        ("HUB", 10.0, ["HUB", "A", "B"]),
        ("C", 5.0, ["C", "D", "E"]),
    ]
    result = dedup_hub_neighborhoods(hubs, threshold=0.8)
    assert len(result) == 2


def test_find_moc_by_hub_note_id(tmp_path):
    db = _setup_graph_db(tmp_path)
    meta = json.dumps({"hub_note_id": "HUB", "origin": "hub_pipeline"})
    db.upsert_moc(
        "MOC_HUB",
        "Tema Hub",
        "/p/moc.md",
        "sig",
        frontmatter_json=meta,
        origin="hub_pipeline",
    )

    found = db.find_moc_by_hub_note_id("HUB")
    assert found is not None
    assert found["moc_id"] == "MOC_HUB"
    assert db.find_moc_by_hub_note_id("MISSING") is None
    db.close()


def test_purge_hub_pipeline_mocs_keeps_taxonomy(tmp_path):
    db = _setup_graph_db(tmp_path)
    cfg = AppConfig(vault_path=tmp_path / "vault")
    moc_dir = cfg.vault_path / "40_MOCs"
    moc_dir.mkdir(parents=True, exist_ok=True)

    hub_file = moc_dir / "MOC - HUB001 - hub-tema.md"
    tax_file = moc_dir / "MOC - TAX001 - taxonomia.md"
    safe_write_note(hub_file, {"type": "moc", "moc_id": "HUB001"}, "# Hub")
    safe_write_note(tax_file, {"type": "moc", "moc_id": "TAX001"}, "# Tax")

    db.upsert_moc("HUB001", "Hub", str(hub_file), "s1", origin="hub_pipeline")
    db.upsert_moc("TAX001", "Tax", str(tax_file), "s2", origin="pipeline")
    db.upsert_moc("MAN001", "Manual", str(tax_file), "s3", origin="manual")

    MagicMock()
    removed = purge_hub_pipeline_mocs(cfg, db)

    assert removed == 1
    assert not hub_file.exists()
    assert tax_file.exists()
    assert db.get_moc("HUB001") is None
    assert db.get_moc("TAX001") is not None
    assert db.get_moc("MAN001") is not None
    db.close()


def test_process_hub_cluster_routes_to_incremental(tmp_path):
    """Existing hub MOC triggers incremental path (single LLM call)."""
    db = _setup_graph_db(tmp_path)
    cfg = AppConfig(vault_path=tmp_path / "vault")
    moc_dir = cfg.vault_path / "40_MOCs"
    moc_dir.mkdir(parents=True, exist_ok=True)
    moc_file = moc_dir / "MOC - MOC001 - tema-hub.md"

    meta = {
        "type": "moc",
        "moc_id": "MOC001",
        "topic": "Tema Hub",
        "hub_note_id": "HUB",
        "origin": "hub_pipeline",
    }
    body = (
        "# Tema Hub\n\nResumo.\n\n"
        "## Porta de entrada\n\n"
        "- [[ZTL - HUB - nota-central]]\n\n"
        "## Vizinhos\n\n"
        "- [[ZTL - A - nota-a]]\n"
    )
    safe_write_note(moc_file, meta, body)
    db.upsert_moc(
        "MOC001",
        "Tema Hub",
        str(moc_file),
        "old_sig",
        body=body,
        frontmatter_json=json.dumps(meta),
        origin="hub_pipeline",
    )

    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "moc_hub_incremental.md").write_text(
        "Hub: {hub_note_title} {moc_topic} {moc_summary} {existing_subsections} {new_notes_list}",
        encoding="utf-8",
    )
    cfg.prompts_path = prompts_dir

    incremental_response = MagicMock()
    incremental_response.content = json.dumps(
        {
            "placements": [{"note_id": "N1", "subsection": "Vizinhos", "reason": "teste"}],
            "new_subsections": [],
        }
    )
    llm = MagicMock()
    llm.invoke.return_value = incremental_response
    MagicMock()

    from zettel.gardener_hub import _HubGardenStats, _process_hub_cluster

    stats = _HubGardenStats()
    result = _process_hub_cluster(
        cfg,
        db,
        llm,
        "HUB",
        ["HUB", "A", "B", "C"],
        5.0,
        stats,
    )

    assert result == "MOC001"
    assert llm.invoke.call_count == 1
    assert stats.incremental == 1
    db.close()


def test_hub_incremental_lists_notes_already_in_the_moc(tmp_path):
    """Existing subsections carry the wikilinks, like the taxonomy incremental.

    Without them the model only sees subsection titles and re-files notes that are
    already placed.
    """
    db = _setup_graph_db(tmp_path)
    cfg = AppConfig(vault_path=tmp_path / "vault")
    moc_dir = cfg.vault_path / "40_MOCs"
    moc_dir.mkdir(parents=True, exist_ok=True)
    moc_file = moc_dir / "MOC - MOC002 - tema-hub.md"

    meta = {
        "type": "moc",
        "moc_id": "MOC002",
        "topic": "Tema Hub",
        "hub_note_id": "HUB",
        "origin": "hub_pipeline",
    }
    body = (
        "# Tema Hub\n\nResumo.\n\n"
        "## Porta de entrada\n\n"
        "- [[ZTL - HUB - nota-central]]\n\n"
        "## Vizinhos\n\n"
        "- [[ZTL - A - nota-a]]\n"
    )
    safe_write_note(moc_file, meta, body)
    db.upsert_moc(
        "MOC002",
        "Tema Hub",
        str(moc_file),
        "old_sig",
        body=body,
        frontmatter_json=json.dumps(meta),
        origin="hub_pipeline",
    )

    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "moc_hub_incremental.md").write_text(
        "Hub: {hub_note_title} {moc_topic} {moc_summary}\n"
        "SUBSECOES:\n{existing_subsections}\nNOVAS:\n{new_notes_list}",
        encoding="utf-8",
    )
    cfg.prompts_path = prompts_dir

    response = MagicMock()
    response.content = json.dumps({"placements": [], "new_subsections": []})
    llm = MagicMock()
    llm.invoke.return_value = response

    from zettel.gardener_hub import _update_hub_moc

    _update_hub_moc(
        cfg,
        db,
        llm,
        {"moc_id": "MOC002", "topic": "Tema Hub", "path": str(moc_file), "origin": "hub_pipeline"},
        "HUB",
        ["B", "C"],
        "new_sig",
    )

    sent = str(llm.invoke.call_args[0][0][-1].content)
    subsections = sent.split("SUBSECOES:")[1].split("NOVAS:")[0]
    assert "[[ZTL - HUB - nota-central]]" in subsections
    assert "[[ZTL - A - nota-a]]" in subsections
    db.close()


# ── Proposed edges and configured weights (#299) ─────────────────────


def _proposed_graph_db(tmp_path: Path) -> StateDB:
    """HUB linked to A..E only by LLM edges still waiting for review."""
    db = StateDB(tmp_path / "proposed.db")
    for nid in ("HUB", "A", "B", "C", "D", "E"):
        db.upsert_note(nid, "SRC", _perm_path(nid, nid.lower()), nid, body=nid)
    for tgt in ("A", "B", "C", "D", "E"):
        db.upsert_note_connection("HUB", tgt, "supports", "", status="proposed")
    db.upsert_note_connection("A", "B", "related", "", status="rejected")
    return db


def test_edge_policy_weighs_proposed_edges_by_the_factor():
    from zettel.state.connections import EdgePolicy

    policy = EdgePolicy({"supports": 0.8}, proposed_factor=0.5)

    assert policy.weight({"relation_type": "supports", "status": "accepted"}) == 0.8
    assert policy.weight({"relation_type": "supports", "status": "proposed"}) == 0.4
    assert policy.weight({"relation_type": "supports", "status": "rejected"}) == 0.0
    walkable = EdgePolicy({"supports": 0.8})
    assert walkable.weight({"relation_type": "supports", "status": "proposed"}) == 0.0


def test_proposed_edges_count_in_hub_degree_and_bfs_only_with_a_factor(tmp_path):
    from zettel.state.connections import EdgePolicy

    db = _proposed_graph_db(tmp_path)
    hcfg = HubMocsConfig(
        selection_mode="absolute", min_weighted_degree=1.0, min_neighbor_weight=0.1
    )
    weights = {"supports": 0.8, "related": 0.5}

    assert rank_note_hubs(db, hcfg, EdgePolicy(weights)) == []
    assert build_hub_neighborhood(db, "HUB", hcfg, EdgePolicy(weights)) == ["HUB"]

    policy = EdgePolicy(weights, proposed_factor=0.5)
    assert rank_note_hubs(db, hcfg, policy) == [("HUB", pytest.approx(2.0))]
    assert sorted(build_hub_neighborhood(db, "HUB", hcfg, policy)[1:]) == list("ABCDE")
    db.close()


def test_configured_relation_weights_change_hub_degree(tmp_path):
    from zettel.gardener_assign import garden_edge_policy

    db = _setup_graph_db(tmp_path)
    cfg = AppConfig()
    hcfg = HubMocsConfig(selection_mode="absolute", min_weighted_degree=0.0, top_n_hubs=10)
    before = dict(rank_note_hubs(db, hcfg, garden_edge_policy(cfg)))

    cfg.retrieval.graph_expansion.relation_weights["depends_on"] = 5.0
    after = dict(rank_note_hubs(db, hcfg, garden_edge_policy(cfg)))

    assert after["C"] == pytest.approx(before["C"] + 5.0 - 0.9)
    assert after["HUB"] == pytest.approx(before["HUB"])
    db.close()


def test_hubs_on_an_unreviewed_graph_warn_instead_of_failing_silently(tmp_path, monkeypatch):
    import zettel.gardener_hub as hub_mod

    db = _proposed_graph_db(tmp_path)
    cfg = AppConfig()
    cfg.vault_path = tmp_path / "vault"
    cfg.gardener.proposed_edge_factor = 0.0
    monkeypatch.setattr(hub_mod, "get_llm", lambda cfg, phase: MagicMock())

    assert hub_mod.run_garden_hubs(cfg, db) == []

    report = db.latest_garden_report("garden_hubs")
    assert report["actions"] == []
    assert report["traversable_edges"] == 0 and report["proposed_edges"] == 5
    assert any("zettel links" in w for w in report["warnings"])
    db.close()


# -- a new hub MOC (#329): the creation path had never run in a test ---------

REPO_PROMPTS = Path(__file__).resolve().parents[1] / "prompts"
_HUB_NOTES = [("HUB", "Nota Central"), ("A", "Nota A"), ("B", "Nota B"), ("C", "Nota C")]


def _hub_vault(tmp_path: Path) -> tuple[AppConfig, StateDB]:
    """The hub graph of ``_setup_graph_db`` with real permanent-note files in a tmp vault."""
    cfg = AppConfig(vault_path=tmp_path / "vault", prompts_path=REPO_PROMPTS)
    cfg.hub_mocs = HubMocsConfig(
        selection_mode="absolute",
        min_weighted_degree=2.0,
        min_neighbors=3,
        min_neighbor_weight=0.1,
    )
    db = StateDB(tmp_path / "hub.db")
    for nid, title in _HUB_NOTES:
        path = (
            cfg.vault_path / "30_Permanent" / f"ZTL - {nid} - {title.lower().replace(' ', '-')}.md"
        )
        safe_write_note(
            path, {"type": "permanent", "note_id": nid}, f"# {title}\n\nCorpo {title}.\n"
        )
        db.upsert_note(nid, "SRC", str(path), title, body=f"Corpo {title}")
    for src, tgt, rel in [
        ("HUB", "A", "extends"),
        ("HUB", "B", "supports"),
        ("HUB", "C", "supports"),
    ]:
        db.upsert_note_connection(src, tgt, rel, "")
    return cfg, db


def _llm_answering(content: str) -> MagicMock:
    llm = MagicMock()
    llm.invoke.return_value = MagicMock(content=content)
    return llm


def test_a_hub_without_a_moc_gets_one_with_the_hub_as_its_door(tmp_path):
    from zettel.gardener_hub import _HubGardenStats, _process_hub_cluster
    from zettel.vault import parse_frontmatter

    cfg, db = _hub_vault(tmp_path)
    answer = {
        "topic": "Tema central",
        "summary": "Resumo do tema.",
        "hub_role": "Concentra as ligacoes do tema.",
        "subsections": [
            {
                "title": "Vizinhos",
                "description": "Desdobramentos.",
                # N1 is the hub, N2 = A, N3 = B; C (N4) is left out on purpose.
                "entries": [
                    {"note_id": "N2", "orientation": "comece por A"},
                    {"note_id": "N3", "orientation": "depois B"},
                ],
            }
        ],
    }
    llm = _llm_answering(json.dumps(answer))
    stats = _HubGardenStats()
    try:
        moc_id = _process_hub_cluster(cfg, db, llm, "HUB", ["HUB", "A", "B", "C"], 5.0, stats)

        assert moc_id and stats.created == 1 and llm.invoke.call_count == 1
        assert db.find_moc_by_hub_note_id("HUB")["moc_id"] == moc_id
        (moc_file,) = (cfg.vault_path / "40_MOCs").glob("HUB - *.md")
        meta, body = parse_frontmatter(moc_file.read_text(encoding="utf-8"))
        assert meta["origin"] == "hub_pipeline" and meta["hub_note_id"] == "HUB"
        door = body.split("## Porta de entrada", 1)[1]
        assert "ZTL - HUB - nota-central" in door.split("##", 1)[0]
        assert "comece por A" in body and "depois B" in body
        assert "ZTL - C - nota-c" not in body
        # The note the LLM did not place is kept out of this MOC's next offers.
        assert set(db.moc_discard_checksums(moc_id)) == {"C"}
        backrefs = (cfg.vault_path / "30_Permanent" / "ZTL - A - nota-a.md").read_text(
            encoding="utf-8"
        )
        assert "zettel:auto-moc-backrefs:start" in backrefs and moc_file.stem in backrefs
    finally:
        db.close()


def test_an_unparseable_hub_answer_is_recorded_and_writes_no_moc(tmp_path):
    from zettel.gardener_hub import _HubGardenStats, _process_hub_cluster

    cfg, db = _hub_vault(tmp_path)
    try:
        moc_id = _process_hub_cluster(
            cfg,
            db,
            _llm_answering("isto nao e json"),
            "HUB",
            ["HUB", "A", "B"],
            5.0,
            _HubGardenStats(),
        )
        assert moc_id is None
        assert db.find_moc_by_hub_note_id("HUB") is None
        assert not list((cfg.vault_path / "40_MOCs").glob("*.md"))
        assert any(row["phase"] == "garden" for row in db.llm_failure_counts())
    finally:
        db.close()


def test_plan_garden_hubs_calls_no_llm_and_writes_nothing(tmp_path, monkeypatch):
    import zettel.gardener_hub as hub_mod

    cfg, db = _hub_vault(tmp_path)
    monkeypatch.setattr(
        hub_mod, "get_llm", lambda *_a, **_k: pytest.fail("plan must not build the LLM")
    )
    try:
        report = hub_mod.plan_garden_hubs(cfg, db)
        assert report.planned is True
        assert [(a.category, a.action) for a in report.actions] == [("HUB", "created")]
        assert db.find_moc_by_hub_note_id("HUB") is None
        assert not (cfg.vault_path / "40_MOCs").exists()
    finally:
        db.close()
