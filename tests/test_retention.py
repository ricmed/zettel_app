"""Retention of full vault markdown and rebuild of missing files (#411)."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pytest
import yaml
from zettel.config import AppConfig
from zettel.project_file import render_body
from zettel.projects import create_project, load_project
from zettel.rebuild import run_rebuild_vault
from zettel.retention import flush, whole_file_sha256
from zettel.state import StateDB
from zettel.state.schema import SCHEMA_SQL
from zettel.sync import run_sync_manual

from tests.fakes import FakeVectorIndex, calls_to


@pytest.fixture
def db(tmp_path: Path):
    database = StateDB(tmp_path / "state.db")
    yield database
    database.close()


@pytest.fixture
def cfg(tmp_path: Path):
    config = AppConfig(vault_path=tmp_path / "vault")
    for name in ("10_Sources", "20_Literature", "30_Permanent", "40_MOCs", "50_Projetos"):
        (config.vault_path / name).mkdir(parents=True)
    return config


def _write(path: Path, meta: dict, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    dumped = yaml.dump(meta, allow_unicode=True, sort_keys=False)
    path.write_text(f"---\n{dumped}---\n\n{body}", encoding="utf-8", newline="\n")


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _old_schema() -> str:
    """SCHEMA_SQL as a database from before the retention columns and projects table."""
    sql = re.sub(
        r"CREATE TABLE IF NOT EXISTS projects \(.*?\);\n",
        "",
        SCHEMA_SQL,
        count=1,
        flags=re.DOTALL,
    )
    kept: list[str] = []
    for line in sql.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith(("card_body", "literature_body")):
            continue
        if stripped.startswith("file_checksum") and "NOT NULL" not in stripped:
            continue
        kept.append(line)
    return "".join(kept)


def test_existing_database_gains_retention_columns_without_reset(tmp_path: Path):
    path = tmp_path / "old.db"
    sql = _old_schema()
    conn = sqlite3.connect(path)
    conn.executescript(sql)
    assert "card_body" not in _columns(conn, "sources")
    assert "literature_body" not in _columns(conn, "chunks")
    assert "file_checksum" not in _columns(conn, "notes")
    assert "file_checksum" not in _columns(conn, "mocs")
    assert "projects" not in _tables(conn)
    conn.close()

    database = StateDB(path)
    try:
        assert "card_body" in _columns(database.conn, "sources")
        assert "literature_body" in _columns(database.conn, "chunks")
        assert "file_checksum" in _columns(database.conn, "notes")
        assert "file_checksum" in _columns(database.conn, "mocs")
        assert "projects" in _tables(database.conn)
        project_cols = _columns(database.conn, "projects")
    finally:
        database.close()
    assert {"project_id", "path", "body", "frontmatter_json", "file_checksum"} <= project_cols


def test_sync_flush_stores_full_manual_lit_and_src_resumo(cfg, db):
    source_id = "@Autor2024"
    chunk_id = f"{source_id}::manual::0001"
    _write(
        cfg.vault_path / "10_Sources" / "SRC - Autor2024 - livro.md",
        {
            "type": "source",
            "source_id": source_id,
            "title": "Livro",
            "author": "Autor",
            "year": 2024,
            "origin": "manual",
        },
        "## Resumo\n\nResumo da ficha.\n\n## Impressões\n\nGostei muito.\n",
    )
    lit = cfg.vault_path / "20_Literature" / "Autor2024" / "LIT - leitura.md"
    _write(
        lit,
        {
            "type": "literature",
            "origin": "manual",
            "source_id": source_id,
            "chunk_id": chunk_id,
            "title": "Nota de leitura",
        },
        "## Resumo\n\nResumo da lit.\n\n## Caderno\n\nFrase exclusiva do arquivo.\n",
    )

    stats = run_sync_manual(cfg, db, None)
    card = db.get_source(source_id)["card_body"]
    assert card.startswith("---")
    assert "## Resumo" in card
    assert "Resumo da ficha." in card
    assert "## Impressões" in card
    assert "Gostei muito." in card
    chunk = db.get_chunk(chunk_id)
    assert "type: literature" in chunk["literature_body"]
    assert "Frase exclusiva do arquivo." in chunk["literature_body"]
    assert chunk["status"] == "persisted"
    assert stats["retention"]["sources"] >= 1
    assert stats["retention"]["literature"] >= 1

    again = flush(cfg, db)
    assert again["literature"] == 0
    assert again["sources"] == 0
    assert again["skipped"] >= 2

    db.upsert_concept(f"{chunk_id}::concept::x", source_id, chunk_id, status="approved")
    lit.write_text(
        lit.read_text(encoding="utf-8") + "Linha nova.\n", encoding="utf-8", newline="\n"
    )
    flush(cfg, db)
    assert db.get_concepts_for_chunk(chunk_id)[0]["status"] == "approved"
    assert db.get_chunk(chunk_id)["status"] == "persisted"
    assert "Linha nova." in db.get_chunk(chunk_id)["literature_body"]


def test_flush_updates_pipeline_ztl_when_only_frontmatter_changes(cfg, db):
    source_id = "@Pipe2024"
    chunk_id = f"{source_id}::ch000::a"
    note_id = "N-PIPE"
    db.upsert_source(source_id, "Pipe2024", "Paper", ["Autor"], 2024, "h", "/x.pdf", "pdf")
    db.upsert_chapter(f"{source_id}::ch000", source_id, "Cap", "chk")
    db.upsert_chunk(chunk_id, source_id, f"{source_id}::ch000", "trecho", "ck", status="persisted")
    db.upsert_concept(f"{chunk_id}::concept::p", source_id, chunk_id, status="approved")
    body = "> **Tese**: tese estavel\n\ntexto\n"
    path = cfg.vault_path / "30_Permanent" / "ZTL - pipe.md"
    _write(
        path,
        {
            "type": "permanent",
            "note_id": note_id,
            "title": "Pipe",
            "origin": "pipeline",
            "tags": ["depois"],
        },
        body,
    )
    db.upsert_note(
        note_id,
        source_id,
        str(path),
        "Pipe",
        body=body,
        frontmatter_json=json.dumps({"type": "permanent", "note_id": note_id, "tags": ["antes"]}),
        origin="pipeline",
        note_semantic_checksum="fixo",
    )
    idx = FakeVectorIndex()

    run_sync_manual(cfg, db, idx)

    note = db.get_note(note_id)
    assert "depois" in note["frontmatter_json"]
    assert "antes" not in note["frontmatter_json"]
    assert note["note_semantic_checksum"] == "fixo"
    assert note["origin"] == "pipeline"
    assert note["file_checksum"] == whole_file_sha256(path)
    assert db.get_concepts_for_chunk(chunk_id)[0]["status"] == "approved"
    assert calls_to(idx, "upsert_permanent_note") == []


def test_flush_updates_pipeline_note_when_only_a_managed_block_changes(cfg, db):
    note_id = "N-BLOCO"
    body = (
        "> **Tese**: tese estavel\n\ntexto\n\n"
        "<!-- zettel:auto-connections:start -->\n"
        "- [[ZTL - ligacao nova]]\n"
        "<!-- zettel:auto-connections:end -->\n"
    )
    path = cfg.vault_path / "30_Permanent" / "ZTL - bloco.md"
    meta = {"type": "permanent", "note_id": note_id, "origin": "pipeline", "title": "Bloco"}
    _write(path, meta, body)
    db.upsert_note(
        note_id,
        None,
        str(path),
        "Bloco",
        body="> **Tese**: tese estavel\n\ntexto\n",
        frontmatter_json=json.dumps(meta),
        origin="pipeline",
        note_semantic_checksum="fixo",
    )

    flush(cfg, db)

    note = db.get_note(note_id)
    assert "ligacao nova" in note["body"]
    assert note["note_semantic_checksum"] == "fixo"
    assert note["origin"] == "pipeline"
    stamp = note["updated_at"]
    assert note["file_checksum"] == whole_file_sha256(path)

    skipped = flush(cfg, db)
    assert skipped["notes"] == 0
    assert skipped["skipped"] >= 1
    assert db.get_note(note_id)["updated_at"] == stamp


def test_flush_updates_pipeline_moc_without_changing_its_signature(cfg, db):
    moc_id = "M1"
    path = cfg.vault_path / "40_MOCs" / "MOC - tema.md"
    _write(
        path,
        {"type": "moc", "moc_id": moc_id, "topic": "Tema", "origin": "pipeline"},
        "# Tema\n\n<!-- zettel:auto-moc-backrefs:start -->\nantigo\n"
        "<!-- zettel:auto-moc-backrefs:end -->\n",
    )
    db.upsert_moc(
        moc_id,
        "Tema",
        str(path),
        "assinatura-estavel",
        body="# Tema\n",
        frontmatter_json="{}",
        origin="pipeline",
    )

    flush(cfg, db)

    row = db.get_moc(moc_id)
    assert "antigo" in row["body"]
    assert row["cluster_signature"] == "assinatura-estavel"
    assert row["origin"] == "pipeline"
    assert row["file_checksum"] == whole_file_sha256(path)


def test_rebuild_recreates_missing_manual_ztl_and_keeps_an_existing_one(cfg, db):
    note_id = "N-MANUAL"
    path = cfg.vault_path / "30_Permanent" / "ZTL - manual.md"
    body = "corpo que so existe no banco\n"
    meta = {"type": "permanent", "note_id": note_id, "title": "Manual", "origin": "manual"}
    db.upsert_note(
        note_id,
        None,
        str(path),
        "Manual",
        body=body,
        frontmatter_json=json.dumps(meta),
        origin="manual",
    )

    run_rebuild_vault(cfg, db)

    assert path.is_file()
    assert "corpo que so existe no banco" in path.read_text(encoding="utf-8")
    path.write_text("EDICAO MANUAL", encoding="utf-8", newline="\n")
    run_rebuild_vault(cfg, db, force=True)
    assert path.read_text(encoding="utf-8") == "EDICAO MANUAL"


def test_rebuild_writes_a_relative_note_inside_the_current_vault(cfg, db, tmp_path: Path):
    relative = "30_Permanent/ZTL - relativa.md"
    outside = tmp_path / "maquina-antiga" / "ZTL - relativa.md"
    meta = {"type": "permanent", "note_id": "N-REL", "title": "Relativa", "origin": "manual"}
    db.upsert_note(
        "N-REL",
        None,
        relative,
        "Relativa",
        body="texto relativo\n",
        frontmatter_json=json.dumps(meta),
        origin="manual",
    )

    run_rebuild_vault(cfg, db)

    written = cfg.vault_path / relative
    assert written.is_file()
    assert "texto relativo" in written.read_text(encoding="utf-8")
    assert not outside.exists()


def test_rebuild_does_not_follow_an_absolute_note_path_outside_the_vault(cfg, db, tmp_path: Path):
    outside = tmp_path / "maquina-antiga" / "ZTL - fora.md"
    meta = {"type": "permanent", "note_id": "N-FORA", "title": "Fora", "origin": "manual"}
    db.upsert_note(
        "N-FORA",
        None,
        str(outside),
        "Fora",
        body="texto de fora\n",
        frontmatter_json=json.dumps(meta),
        origin="manual",
    )

    run_rebuild_vault(cfg, db)

    assert not outside.exists()
    found = [
        path
        for path in (cfg.vault_path / "30_Permanent").glob("*.md")
        if "texto de fora" in path.read_text(encoding="utf-8")
    ]
    assert len(found) == 1
    assert found[0].is_relative_to(cfg.vault_path)


def test_rebuild_recreates_missing_project_and_leaves_an_existing_one(cfg, db):
    project = create_project(cfg, "Mesa de teste")
    flush(cfg, db)
    assert db.conn.execute(
        "SELECT file_checksum FROM projects WHERE project_id=?", (project.project_id,)
    ).fetchone()[0] == whole_file_sha256(project.path)

    project.path.unlink()
    run_rebuild_vault(cfg, db)
    restored = project.path.read_text(encoding="utf-8")
    assert "Introdução" in restored

    project.path.write_text(restored + "Linha da mao.\n", encoding="utf-8", newline="\n")
    run_rebuild_vault(cfg, db, force=True)
    assert "Linha da mao." in project.path.read_text(encoding="utf-8")


def test_mesa_reads_the_file_not_the_retention_row(cfg, db):
    project = create_project(cfg, "Mesa viva")
    flush(cfg, db)
    db.conn.execute(
        "UPDATE projects SET body=? WHERE project_id=?",
        ("## Sumiu\n", project.project_id),
    )
    db.conn.commit()

    loaded = load_project(cfg, project.project_id)
    text = render_body(loaded.body)
    assert "Introdução" in text
    assert "Sumiu" not in text


def test_rebuild_refuses_a_project_path_outside_the_vault(cfg, db, tmp_path: Path):
    outside = tmp_path / "fora.md"
    db.conn.execute(
        """INSERT INTO projects (project_id, path, body, frontmatter_json, file_checksum)
           VALUES (?, ?, ?, ?, ?)""",
        ("P-FORA", str(outside), "## Fora\n", '{"type": "project"}', "abc"),
    )
    db.conn.commit()

    run_rebuild_vault(cfg, db)
    assert not outside.exists()


def test_rebuild_uses_literature_body_and_falls_back_to_synthesis(cfg, db):
    source_id = "@Lit2024"
    db.upsert_source(source_id, "Lit2024", "Fonte", ["Autor"], 2024, "h", "/x.md", "md")
    db.upsert_chapter(f"{source_id}::ch000", source_id, "Cap", "chk")
    retained_id = f"{source_id}::ch000::full"
    synthesized_id = f"{source_id}::ch000::syn"
    db.upsert_chunk(
        retained_id,
        source_id,
        f"{source_id}::ch000",
        "trecho",
        "c1",
        status="persisted",
        chunk_index=0,
        summary_json=json.dumps({"summary": "NAO USAR"}),
    )
    db.conn.execute(
        "UPDATE chunks SET literature_body=? WHERE chunk_id=?",
        ("---\ntype: literature\n---\n\nCORPO INTEGRAL\n", retained_id),
    )
    db.upsert_chunk(
        synthesized_id,
        source_id,
        f"{source_id}::ch000",
        "trecho",
        "c2",
        status="persisted",
        chunk_index=1,
        summary_json=json.dumps({"summary": "SO A SINTESE"}),
    )
    db.conn.commit()

    run_rebuild_vault(cfg, db)

    folder = cfg.vault_path / "20_Literature" / "Lit2024"
    texts = {path.name: path.read_text(encoding="utf-8") for path in folder.glob("*.md")}
    integral = next(text for text in texts.values() if "CORPO INTEGRAL" in text)
    assert "NAO USAR" not in integral
    synthesized = next(text for text in texts.values() if "SO A SINTESE" in text)
    assert "CORPO INTEGRAL" not in synthesized


def test_rebuild_restores_src_card_and_does_not_overwrite_a_manual_file(cfg, db):
    source_id = "@Card2024"
    db.upsert_source(
        source_id,
        "Card2024",
        "Ficha",
        ["Autor"],
        2024,
        "h",
        "/x.md",
        "md",
        origin="manual",
    )
    card = (
        "---\ntype: source\norigin: manual\n---\n\n"
        "## Resumo\n\nImpressao exclusiva do autor.\n\n## Impressões\n\nFicou.\n"
    )
    db.conn.execute("UPDATE sources SET card_body=? WHERE source_id=?", (card, source_id))
    db.conn.commit()

    run_rebuild_vault(cfg, db)

    src = next((cfg.vault_path / "10_Sources").glob("*.md"))
    assert "Impressao exclusiva do autor." in src.read_text(encoding="utf-8")
    assert "## Impressões" in src.read_text(encoding="utf-8")
    src.write_text("NAO TOCAR", encoding="utf-8", newline="\n")
    run_rebuild_vault(cfg, db, force=True)
    assert src.read_text(encoding="utf-8") == "NAO TOCAR"
