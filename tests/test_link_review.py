"""LLM edges stay out of the graph until a human accepts them."""

from pathlib import Path

from zettel.config import AppConfig
from zettel.connector.links import decide_link
from zettel.graph import expand_notes
from zettel.state import StateDB
from zettel.vault import read_managed_block


def _note(db: StateDB, root: Path, note_id: str) -> None:
    path = root / "30_Permanent" / f"ZTL - {note_id} - nota.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    body = f"> **Tese**: tese de {note_id}\n"
    path.write_text(body, encoding="utf-8")
    db.upsert_note(note_id, "@S", str(path), title=note_id, body=body)


def test_proposed_llm_edge_is_not_traversed_until_accepted(tmp_path):
    db = StateDB(tmp_path / "state.db")
    root = tmp_path / "vault"
    _note(db, root, "A")
    _note(db, root, "B")
    db.upsert_note_connection("A", "B", "supports", "proposta", origin="llm", status="proposed")
    db.upsert_note_connection("A", "B", "corroborates", "codigo", origin="derived")
    assert set(expand_notes(db, ["A"], max_hops=1)) == {"B"}
    # Only the derived edge is walked. Drop it and the proposed one still does not count.
    db.delete_note_connection("A", "B", "corroborates", origin="derived")
    assert expand_notes(db, ["A"], max_hops=1) == {}
    assert db.get_stats()["links_proposed"] == 1

    cfg = AppConfig(vault_path=root)
    assert decide_link(cfg, db, "A", "B", "supports", "accept", surface="cli")
    assert "B" in expand_notes(db, ["A"], max_hops=1)
    assert db.get_stats()["links_proposed"] == 0
    events = db.list_review_events()
    assert events[-1]["decision"] == "accept_link"
    assert events[-1]["surface"] == "cli"
    target = (root / "30_Permanent" / "ZTL - B - nota.md").read_text(encoding="utf-8")
    assert "suportado por" in (read_managed_block(target, "auto-backlinks") or "")
    db.close()


def test_retype_stores_review_origin(tmp_path):
    db = StateDB(tmp_path / "state.db")
    root = tmp_path / "vault"
    _note(db, root, "A")
    _note(db, root, "B")
    db.upsert_note_connection("A", "B", "supports", "talvez", origin="llm", status="proposed")
    cfg = AppConfig(vault_path=root)
    assert decide_link(cfg, db, "A", "B", "supports", "retype", new_type="extends", surface="web")
    rows = db.list_outgoing_connections("A")
    assert len(rows) == 1
    assert rows[0]["relation_type"] == "extends"
    assert rows[0]["origin"] == "review"
    assert rows[0]["status"] == "accepted"
    assert db.list_review_events()[-1]["decision"] == "retype_link"
    db.close()


def test_reject_does_not_enter_the_graph(tmp_path):
    db = StateDB(tmp_path / "state.db")
    root = tmp_path / "vault"
    _note(db, root, "A")
    _note(db, root, "B")
    db.upsert_note_connection("A", "B", "contradicts", "nao", origin="llm", status="proposed")
    cfg = AppConfig(vault_path=root)
    assert decide_link(cfg, db, "A", "B", "contradicts", "reject", surface="cli")
    assert expand_notes(db, ["A"], max_hops=1) == {}
    assert db.get_stats()["links_proposed"] == 0
    db.close()
