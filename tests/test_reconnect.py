"""Reconnect replaces LLM edges and leaves manual and derived ones in place."""

from pathlib import Path

from zettel.config import AppConfig
from zettel.connector.note import ConnectSession
from zettel.connector.prompt import PromptParts
from zettel.connector.reconnect import (
    apply_relink,
    diff_llm_edges,
    run_reconnect,
    select_reconnect_notes,
)
from zettel.state import StateDB
from zettel.vault import read_managed_block

BODY = """> **Tese**: a tese que ja estava na nota

a definicao que nao pode mudar

## Conexões

- [[ZTL - OLD - nota]] (supports) -- antiga
"""


def _note(
    db: StateDB,
    root: Path,
    note_id: str,
    *,
    origin: str = "pipeline",
    body: str = BODY,
) -> None:
    path = root / "30_Permanent" / f"ZTL - {note_id} - nota.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    db.upsert_note(note_id, "@S", str(path), title=f"Nota {note_id}", body=body, origin=origin)


def _session(cfg: AppConfig, db: StateDB) -> ConnectSession:
    return ConnectSession(
        cfg=cfg,
        db=db,
        idx=object(),
        llm=object(),
        prompt_parts=PromptParts(system="", user_template="", has_split=False),
        retriever=object(),
        example_fields={},
        taxonomy=({}, {}),
        prompt_checksum="prompt-hash",
        model="gemini/gemini-embedding-test",
    )


def test_dry_run_lists_edges_and_writes_nothing(tmp_path):
    db = StateDB(tmp_path / "state.db")
    root = tmp_path / "vault"
    _note(db, root, "A")
    _note(db, root, "KEEP")
    _note(db, root, "NEW")
    db.upsert_note_connection("A", "KEEP", "supports", "antiga", origin="llm")
    db.upsert_note_connection("A", "KEEP", "corroborates", "derivada", origin="derived")
    db.upsert_note_connection("A", "KEEP", "related", "a mao", origin="manual")
    before = db.count_note_connections()
    cfg = AppConfig(vault_path=root)

    def link(note: dict) -> list[dict]:
        if note["note_id"] != "A":
            return []
        return [
            {
                "related_note_id": "NEW",
                "relation_type": "extends",
                "description": "ve uma nota posterior",
            }
        ]

    diffs = run_reconnect(
        cfg,
        db,
        _session(cfg, db),
        select_reconnect_notes(db),
        apply=False,
        link=link,
    )
    assert [(item.target_note_id, item.relation_type) for item in diffs[0].remove] == [
        ("KEEP", "supports")
    ]
    assert [(item.target_note_id, item.relation_type) for item in diffs[0].add] == [
        ("NEW", "extends")
    ]
    assert db.count_note_connections() == before
    text = (root / "30_Permanent" / "ZTL - A - nota.md").read_text(encoding="utf-8")
    assert "a tese que ja estava na nota" in text
    assert "ZTL - OLD - nota" in text
    db.close()


def test_apply_keeps_manual_and_derived_and_clears_old_backlinks(tmp_path):
    db = StateDB(tmp_path / "state.db")
    root = tmp_path / "vault"
    _note(db, root, "A")
    _note(db, root, "KEEP")
    _note(db, root, "NEW")
    _note(db, root, "HAND", origin="manual", body="nota manual\n")
    db.upsert_note_connection("A", "KEEP", "supports", "antiga", origin="llm")
    db.upsert_note_connection("A", "KEEP", "corroborates", "derivada", origin="derived")
    db.upsert_note_connection("A", "KEEP", "related", "a mao", origin="manual")
    # The old LLM edge is already rendered on the target, as connect would have left it.
    from zettel.connector.links import rebuild_auto_backlinks

    cfg = AppConfig(vault_path=root)
    rebuild_auto_backlinks(db, "KEEP", vault_timezone=cfg.vault_timezone)
    keep_before = (root / "30_Permanent" / "ZTL - KEEP - nota.md").read_text(encoding="utf-8")
    assert "suportado por" in (read_managed_block(keep_before, "auto-backlinks") or "")

    def link(_note: dict) -> list[dict]:
        return [{"related_note_id": "NEW", "relation_type": "extends", "description": "depois"}]

    run_reconnect(
        cfg,
        db,
        _session(cfg, db),
        select_reconnect_notes(db, note_id="A"),
        apply=True,
        link=link,
    )
    rows = {
        (row["target_note_id"], row["relation_type"], row["origin"])
        for row in db.get_note_connections("A")
        if row["source_note_id"] == "A"
    }
    assert ("KEEP", "supports", "llm") not in rows
    assert ("KEEP", "corroborates", "derived") in rows
    assert ("KEEP", "related", "manual") in rows
    assert ("NEW", "extends", "llm") in rows
    stored = next(
        row
        for row in db.list_outgoing_connections("A", origin="llm")
        if row["target_note_id"] == "NEW"
    )
    assert stored["prompt_checksum"] == "prompt-hash"
    assert stored["model"] == "gemini/gemini-embedding-test"
    assert stored["run_id"]
    text = (root / "30_Permanent" / "ZTL - A - nota.md").read_text(encoding="utf-8")
    assert "a tese que ja estava na nota" in text
    assert "a definicao que nao pode mudar" in text
    assert "(extends)" in text
    assert "(supports)" not in text
    assert "(corroborates)" in text
    keep = (root / "30_Permanent" / "ZTL - KEEP - nota.md").read_text(encoding="utf-8")
    assert "suportado por" not in (read_managed_block(keep, "auto-backlinks") or "")
    new = (root / "30_Permanent" / "ZTL - NEW - nota.md").read_text(encoding="utf-8")
    assert "estendido por" in (read_managed_block(new, "auto-backlinks") or "")
    # A hand-written note is not selected, so it gains no outgoing edge.
    assert db.list_outgoing_connections("HAND") == []
    db.close()


def test_select_is_deterministic_and_skips_manual_notes(tmp_path):
    db = StateDB(tmp_path / "state.db")
    root = tmp_path / "vault"
    _note(db, root, "B")
    _note(db, root, "A")
    _note(db, root, "M", origin="manual", body="manual\n")
    assert [row["note_id"] for row in select_reconnect_notes(db)] == ["A", "B"]
    assert [row["note_id"] for row in select_reconnect_notes(db, source_id="@OUTRA")] == []
    db.close()


def test_diff_names_edges_that_leave_and_arrive():
    diff = diff_llm_edges(
        "A",
        [{"target_note_id": "OLD", "relation_type": "supports", "description": ""}],
        [{"related_note_id": "NEW", "relation_type": "extends", "description": ""}],
    )
    assert diff.remove[0].target_note_id == "OLD"
    assert diff.add[0].target_note_id == "NEW"


def test_apply_relink_records_prompt_model_and_run(tmp_path):
    db = StateDB(tmp_path / "state.db")
    root = tmp_path / "vault"
    _note(db, root, "A")
    _note(db, root, "B")
    cfg = AppConfig(vault_path=root)
    note = db.get_note("A")
    apply_relink(
        cfg,
        db,
        note,
        [{"related_note_id": "B", "relation_type": "related", "description": "liga"}],
        provenance={"prompt_checksum": "abc", "model": "gemini/m", "run_id": 7},
    )
    row = db.list_outgoing_connections("A", origin="llm")[0]
    assert row["prompt_checksum"] == "abc"
    assert row["model"] == "gemini/m"
    assert row["run_id"] == 7
    db.close()
