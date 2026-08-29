"""Tests for MOC back-reference blocks on permanent notes."""

from pathlib import Path
from unittest.mock import MagicMock

from zettel.config import AppConfig
from zettel.gardener import purge_pipeline_mocs
from zettel.moc_backrefs import (
    MOC_BACKREFS_BLOCK,
    moc_link_line,
    sync_moc_backrefs,
)
from zettel.state import StateDB
from zettel.sync import run_sync_manual
from zettel.vault import read_managed_block, safe_write_note


def _make_config(vault_path: Path) -> AppConfig:
    return AppConfig(vault_path=vault_path)


def _write_permanent_note(
    vault: Path, db: StateDB, note_id: str, title: str,
) -> Path:
    note_dir = vault / "30_Permanent"
    note_dir.mkdir(parents=True, exist_ok=True)
    note_path = note_dir / f"ZTL - {note_id} - {title.lower().replace(' ', '-')}.md"
    meta = {
        "type": "permanent",
        "note_id": note_id,
        "title": title,
        "origin": "pipeline",
    }
    body = f"# {title}\n\nConteudo da nota.\n"
    safe_write_note(note_path, meta, body)
    db.upsert_note(note_id, "SRC001", str(note_path), title, body=body)
    return note_path


def _write_moc(
    vault: Path,
    db: StateDB,
    moc_id: str,
    topic: str,
    body: str,
    *,
    origin: str = "pipeline",
) -> Path:
    moc_dir = vault / "40_MOCs"
    moc_dir.mkdir(parents=True, exist_ok=True)
    slug = topic.lower().replace(" ", "-")
    moc_path = moc_dir / f"MOC - {moc_id} - {slug}.md"
    meta = {
        "type": "moc",
        "moc_id": moc_id,
        "topic": topic,
        "origin": origin,
    }
    safe_write_note(moc_path, meta, body)
    db.upsert_moc(moc_id, topic, str(moc_path), "sig", body=body, origin=origin)
    return moc_path


def test_sync_moc_backrefs_adds_links_to_permanent_notes(tmp_path):
    vault = tmp_path / "vault"
    db = StateDB(tmp_path / "state.db")
    note_path = _write_permanent_note(vault, db, "NOTE001", "Regressao Linear")
    moc_body = (
        "# Topico\n\nResumo.\n\n"
        "## Secao\n\n"
        "- [[ZTL - NOTE001 - regressao-linear]]\n"
    )
    moc_path = _write_moc(vault, db, "MOC001", "Topico", moc_body)

    sync_moc_backrefs(db, "MOC001", "Topico", moc_path)

    block = read_managed_block(note_path.read_text(encoding="utf-8"), MOC_BACKREFS_BLOCK)
    assert block is not None
    assert "MOC001" in block
    assert "[[MOC - MOC001 - topico]]" in block
    db.close()


def test_sync_moc_backrefs_removes_stale_links(tmp_path):
    vault = tmp_path / "vault"
    db = StateDB(tmp_path / "state.db")
    note_a = _write_permanent_note(vault, db, "NOTE001", "Nota A")
    _write_permanent_note(vault, db, "NOTE002", "Nota B")
    previous_body = (
        "# Topico\n\nResumo.\n\n"
        "## Secao\n\n"
        "- [[ZTL - NOTE001 - nota-a]]\n"
        "- [[ZTL - NOTE002 - nota-b]]\n"
    )
    new_body = (
        "# Topico\n\nResumo.\n\n"
        "## Secao\n\n"
        "- [[ZTL - NOTE001 - nota-a]]\n"
    )
    moc_path = _write_moc(vault, db, "MOC001", "Topico", previous_body)
    sync_moc_backrefs(db, "MOC001", "Topico", moc_path)

    sync_moc_backrefs(
        db, "MOC001", "Topico", moc_path,
        previous_body=previous_body, new_body=new_body,
    )

    block_a = read_managed_block(note_a.read_text(encoding="utf-8"), MOC_BACKREFS_BLOCK)
    block_b = read_managed_block(
        (vault / "30_Permanent" / "ZTL - NOTE002 - nota-b.md").read_text(encoding="utf-8"),
        MOC_BACKREFS_BLOCK,
    )
    assert block_a and "MOC001" in block_a
    assert block_b is None or "MOC001" not in block_b


def test_clear_moc_backrefs_on_purge(tmp_path):
    vault = tmp_path / "vault"
    db = StateDB(tmp_path / "state.db")
    note_path = _write_permanent_note(vault, db, "NOTE001", "Regressao Linear")
    moc_body = (
        "# Topico\n\nResumo.\n\n"
        "## Secao\n\n"
        "- [[ZTL - NOTE001 - regressao-linear]]\n"
    )
    moc_path = _write_moc(vault, db, "PIPE001", "Topico", moc_body, origin="pipeline")
    sync_moc_backrefs(db, "PIPE001", "Topico", moc_path)
    assert read_managed_block(note_path.read_text(encoding="utf-8"), MOC_BACKREFS_BLOCK)

    cfg = _make_config(vault)
    idx = MagicMock()
    removed = purge_pipeline_mocs(cfg, db, idx)

    assert removed == 1
    block = read_managed_block(note_path.read_text(encoding="utf-8"), MOC_BACKREFS_BLOCK)
    assert block is None or "PIPE001" not in block
    db.close()


def test_sync_manual_updates_moc_backrefs(tmp_path):
    vault = tmp_path / "vault"
    for folder in ("10_Sources", "20_Literature", "30_Permanent", "40_MOCs"):
        (vault / folder).mkdir(parents=True)
    (tmp_path / "prompts").mkdir()

    db = StateDB(tmp_path / "state.db")
    note_path = _write_permanent_note(vault, db, "NOTE001", "Nota Manual")
    moc_body = (
        "# Manual\n\nResumo.\n\n"
        "## Secao\n\n"
        "- [[ZTL - NOTE001 - nota-manual]]\n"
    )
    moc_path = vault / "40_MOCs" / "MOC - MANUAL01 - manual.md"
    safe_write_note(
        moc_path,
        {"type": "moc", "moc_id": "MANUAL01", "topic": "Manual", "origin": "manual"},
        moc_body,
    )

    cfg = _make_config(vault)
    idx = MagicMock()
    stats = run_sync_manual(cfg, db, idx)

    assert stats["mocs"] >= 1
    block = read_managed_block(note_path.read_text(encoding="utf-8"), MOC_BACKREFS_BLOCK)
    assert block is not None
    assert "MANUAL01" in block
    db.close()


def test_moc_link_line_uses_path_stem(tmp_path):
    path = tmp_path / "40_MOCs" / "HUB - HUB001 - tema-hub.md"
    line = moc_link_line("HUB001", "Tema Hub", path=path)
    assert line == "- [[HUB - HUB001 - tema-hub]]"
