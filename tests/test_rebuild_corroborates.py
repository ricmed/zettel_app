"""Rebuild of derived corroborates edges (#280). No embedding, no LLM."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from rebuild_corroborates import execute, render_plan
from zettel.config import AppConfig
from zettel.connector.links import (
    CorroborationCandidate,
    build_corroboration_plan,
    corroborating_note_ids,
    rewrite_corroborates_section,
    select_corroborating_ids,
)
from zettel.retrieval import RetrievedNote
from zettel.state import StateDB
from zettel.vault import read_managed_block

NOTE = """> **Tese**: a mesma ideia

## Definição

o que delimita a ideia
"""


def _hit(note_id: str, distance: float, hop: int = 0) -> RetrievedNote:
    return RetrievedNote(note_id=note_id, score=1.0, vector_distance=distance, hop=hop)


def _note(db: StateDB, root: Path, note_id: str, source_id: str, body: str = NOTE) -> None:
    path = root / "30_Permanent" / f"ZTL - {note_id} - nota.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    db.upsert_note(note_id, source_id, str(path), title=f"Nota {note_id}", body=body)


def test_select_corroborating_ids_is_the_connect_rule():
    """Connect and the rebuild must accept the same ids for the same hits."""
    cfg = AppConfig()
    db_notes = {
        "SAME": {"source_id": "@A"},
        "OTHER": {"source_id": "@B"},
        "WEAK": {"source_id": "@C"},
    }

    class _DB:
        def get_note(self, note_id: str):
            return db_notes.get(note_id)

    hits = [_hit("SAME", 0.10), _hit("OTHER", 0.10), _hit("WEAK", 0.50), _hit("OTHER", 0.20)]
    via_connect = corroborating_note_ids(cfg, _DB(), hits, "@A", "SELF")
    candidates = [
        CorroborationCandidate("SAME", 0.90, "@A"),
        CorroborationCandidate("OTHER", 0.90, "@B"),
        CorroborationCandidate("WEAK", 0.50, "@C"),
        CorroborationCandidate("OTHER", 0.80, "@B"),
    ]
    via_rule = select_corroborating_ids(
        candidates,
        source_id="@A",
        note_id="SELF",
        min_similarity=cfg.linking.corroborates_min_similarity,
        max_edges=cfg.linking.corroborates_max_edges,
    )
    assert via_connect == via_rule == ["OTHER"]


def test_dry_run_lists_and_apply_is_idempotent(tmp_path, capsys):
    db = StateDB(tmp_path / "state.db")
    root = tmp_path / "vault"
    _note(db, root, "A", "@SrcA")
    _note(db, root, "B", "@SrcB")
    _note(db, root, "C", "@SrcC")
    # Above the cut in the fresh search, but not an edge yet.
    # C is an old edge the fresh search scores under the cut.
    db.upsert_note_connection("A", "C", "corroborates", "antiga", origin="derived")
    db.upsert_note_connection("A", "B", "supports", "nao mexe", origin="llm")

    def search(note_id: str, query: str):
        assert query
        if note_id == "A":
            return [_hit("B", 0.10), _hit("C", 0.50)]
        return []

    cfg = AppConfig(vault_path=root)
    plan = build_corroboration_plan(cfg, db, search)
    assert [c.target_note_id for c in plan.create] == ["B"]
    assert [c.target_note_id for c in plan.remove] == ["C"]
    assert plan.keep == ()
    assert plan.source_pair_counts() == [
        ("@SrcA", "@SrcB", 1, 0, 0),
        ("@SrcA", "@SrcC", 0, 1, 0),
    ]

    code = execute(cfg, db, apply=False, search=search)
    assert code == 0
    assert "dry-run" in capsys.readouterr().out
    still = {row["target_note_id"] for row in db.get_note_connections("A")}
    assert still == {"C", "B"}  # corroborates C plus supports B; nothing written

    execute(cfg, db, apply=True, search=search)
    edges = {
        (row["target_note_id"], row["relation_type"], row["origin"])
        for row in db.get_note_connections("A")
        if row["source_note_id"] == "A"
    }
    assert edges == {("B", "corroborates", "derived"), ("B", "supports", "llm")}
    source = (root / "30_Permanent" / "ZTL - A - nota.md").read_text(encoding="utf-8")
    assert "(corroborates)" in source
    assert "ZTL - B - nota" in source
    assert "ZTL - C - nota" not in source
    target = (root / "30_Permanent" / "ZTL - B - nota.md").read_text(encoding="utf-8")
    assert "corroborado por" in (read_managed_block(target, "auto-backlinks") or "")
    gone = (root / "30_Permanent" / "ZTL - C - nota.md").read_text(encoding="utf-8")
    assert read_managed_block(gone, "auto-backlinks") in (None, "")

    again = build_corroboration_plan(cfg, db, search)
    assert again.create == () and again.remove == () and len(again.keep) == 1
    execute(cfg, db, apply=True, search=search)
    outgoing = [row for row in db.get_note_connections("A") if row["source_note_id"] == "A"]
    assert len(outgoing) == 2
    db.close()


def test_empty_note_is_not_recalculated(tmp_path):
    db = StateDB(tmp_path / "state.db")
    root = tmp_path / "vault"
    path = root / "30_Permanent" / "ZTL - A - nota.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "sem tese e sem definicao\n"
    path.write_text(body, encoding="utf-8")
    db.upsert_note("A", "@SrcA", str(path), title="", body=body)
    _note(db, root, "C", "@SrcC")
    db.upsert_note_connection("A", "C", "corroborates", "antiga", origin="derived")

    called: list[str] = []

    def search(note_id: str, query: str):
        called.append(note_id)
        return []

    plan = build_corroboration_plan(AppConfig(vault_path=root), db, search)
    assert called == ["C"]
    assert plan.unresolved_note_ids == ("A",)
    assert plan.remove == ()
    assert plan.untouched_edges == 1
    db.close()


def test_rewrite_keeps_other_relations_and_drops_an_empty_heading():
    text = """# Titulo

## Conexões

- [[ZTL - B - nota]] (supports) -- amplia
- [[ZTL - C - nota]] (corroborates) -- antiga

## Fonte

- pagina
"""
    rewritten = rewrite_corroborates_section(
        text, ["- [[ZTL - D - nota]] (corroborates) -- Outra fonte sustenta a mesma ideia"]
    )
    assert "(supports)" in rewritten
    assert "ZTL - C - nota" not in rewritten
    assert "ZTL - D - nota" in rewritten
    assert rewritten.index("## Fonte") > rewritten.index("ZTL - D - nota")

    cleared = rewrite_corroborates_section(
        "## Conexões\n\n- [[ZTL - C - nota]] (corroborates) -- antiga\n", []
    )
    assert "Conexões" not in cleared

    fenced = "```\n## Conexões\n- [[ZTL - C - nota]] (corroborates)\n```\n"
    assert rewrite_corroborates_section(fenced, []) == fenced


def test_render_plan_names_the_mode():
    from zettel.connector.links import CorroborationPlan

    text = render_plan(CorroborationPlan(create=(), remove=(), keep=()), apply=False)
    assert "dry-run" in text
    assert "criar: 0" in text
