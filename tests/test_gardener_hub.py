"""Tests for hub-anchored MOC gardener."""

import json
from pathlib import Path
from unittest.mock import MagicMock

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

    idx = MagicMock()
    removed = purge_hub_pipeline_mocs(cfg, db, idx)

    assert removed == 1
    assert not hub_file.exists()
    assert tax_file.exists()
    assert db.get_moc("HUB001") is None
    assert db.get_moc("TAX001") is not None
    assert db.get_moc("MAN001") is not None
    idx.delete_mocs.assert_called_once_with(["HUB001"])
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
    idx = MagicMock()

    from zettel.gardener_hub import _HubGardenStats, _process_hub_cluster

    stats = _HubGardenStats()
    result = _process_hub_cluster(
        cfg,
        db,
        idx,
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
        MagicMock(),
        llm,
        {"moc_id": "MOC002", "topic": "Tema Hub", "path": str(moc_file)},
        "HUB",
        ["B", "C"],
        "new_sig",
    )

    sent = str(llm.invoke.call_args[0][0][-1].content)
    subsections = sent.split("SUBSECOES:")[1].split("NOVAS:")[0]
    assert "[[ZTL - HUB - nota-central]]" in subsections
    assert "[[ZTL - A - nota-a]]" in subsections
    db.close()
