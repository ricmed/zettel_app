"""Tests for complete source deletion (purge_source)."""

import json

import pytest
import yaml
from zettel.config import AppConfig
from zettel.purge_source import normalize_source_id, purge_source
from zettel.state import StateDB
from zettel.vault import (
    build_literature_chunk_note,
    literature_chunk_filename_for_row,
    literature_index_filename,
    literature_source_dirname,
    safe_write_note,
    source_note_filename,
    source_note_stem,
    strip_matching_wikilinks,
)


class _FakeIndex:
    def __init__(self):
        self.chunk_deletes: list[str] = []
        self.lit_deletes: list[str] = []
        self.source_deletes: list[str] = []
        self.permanent_deletes: list[str] = []

    def delete_chunks(self, chunk_ids):
        self.chunk_deletes.extend(chunk_ids)

    def delete_literature_notes(self, ids):
        self.lit_deletes.extend(ids)

    def delete_sources(self, ids):
        self.source_deletes.extend(ids)

    def delete_permanent_notes(self, ids):
        self.permanent_deletes.extend(ids)

    def vacuum(self):
        pass


@pytest.fixture
def env(tmp_path):
    cfg = AppConfig(
        vault_path=tmp_path / "vault",
        cache_path=tmp_path / "cache",
        state_db_path=tmp_path / "state.db",
        chroma_path=tmp_path / "chroma",
    )
    for d in (
        "00_Inbox/Review",
        "10_Sources",
        "20_Literature",
        "30_Permanent",
        "40_MOCs",
        "90_Assets",
    ):
        (cfg.vault_path / d).mkdir(parents=True, exist_ok=True)
    db = StateDB(cfg.state_db_path)
    idx = _FakeIndex()
    yield cfg, db, idx
    db.close()


def _write_yaml(path, meta, body=""):
    fm = yaml.dump(meta, allow_unicode=True, sort_keys=False)
    path.write_text(f"---\n{fm}---\n\n{body}", encoding="utf-8")


def _seed_source(cfg, db, *, with_chunk=True, chunk_status="persisted"):
    db.upsert_source(
        "@Book2024",
        "Book2024",
        "Livro Teste",
        ["Autor"],
        2024,
        "h",
        "/x.pdf",
        "pdf",
    )
    db.upsert_chapter("@Book2024::ch000", "@Book2024", "Ch1", "chh")
    chunk = {
        "chunk_index": 3,
        "page_in_file": 20,
        "page_in_book": 10,
        "section_path": "Ch1 > Intro",
        "literature_id": "lit123",
        "summary_json": json.dumps({"summary": "Resumo curto"}),
    }
    if with_chunk:
        db.upsert_chunk(
            "@Book2024::ch000::abc",
            "@Book2024",
            "@Book2024::ch000",
            "texto do chunk",
            "ck",
            chunk_index=3,
            page_in_file=20,
            page_in_book=10,
            status=chunk_status,
            section_path="Ch1 > Intro",
            literature_id="lit123",
            summary_json=chunk["summary_json"],
        )
        db.upsert_concept(
            "concept1",
            "@Book2024",
            "@Book2024::ch000::abc",
            status="approved",
        )

    citekey = "Book2024"
    title = "Livro Teste"
    src_meta = {
        "type": "source",
        "source_id": "@Book2024",
        "citekey": citekey,
        "title": title,
    }
    _write_yaml(cfg.vault_path / "10_Sources" / source_note_filename(citekey, title), src_meta)

    lit_index = cfg.vault_path / "20_Literature" / literature_index_filename(citekey, title)
    _write_yaml(
        lit_index,
        {"type": "literature_index", "source_id": "@Book2024", "citekey": citekey},
        "# Indice\n",
    )

    lit_dir = cfg.vault_path / "20_Literature" / literature_source_dirname(citekey)
    lit_dir.mkdir(parents=True, exist_ok=True)
    if with_chunk:
        fname = literature_chunk_filename_for_row(citekey, chunk)
        meta, body = build_literature_chunk_note(
            source_id="@Book2024",
            citekey=citekey,
            title=title,
            chunk_id="@Book2024::ch000::abc",
            chunk_index=3,
            literature_id="lit123",
            summary="Resumo curto",
            key_concepts=["c"],
            candidates=[],
            section_path="Ch1 > Intro",
            page_in_book=10,
            status=chunk_status,
        )
        safe_write_note(lit_dir / fname, meta, body)

    review_dir = cfg.vault_path / "00_Inbox/Review" / literature_source_dirname(citekey)
    review_dir.mkdir(parents=True, exist_ok=True)
    _write_yaml(review_dir / "draft.md", {"type": "literature", "status": "awaiting_review"})

    db.upsert_file("/x.pdf", "h", "pdf", "@Book2024")
    return chunk


def test_normalize_source_id():
    assert normalize_source_id("Book2024") == "@Book2024"
    assert normalize_source_id("@Book2024") == "@Book2024"


def test_strip_matching_wikilinks_path_qualified():
    targets = {"Book2024/LIT - Book2024 - p010 - topic"}
    body = "Ver [[Book2024/LIT - Book2024 - p010 - topic|p. 10]] aqui."
    cleaned = strip_matching_wikilinks(body, targets)
    assert "[[" not in cleaned
    assert "Ver  aqui." in cleaned or "Ver aqui." in cleaned.replace("  ", " ")


def test_purge_source_removes_vault_sqlite_chroma(env):
    cfg, db, idx = env
    _seed_source(cfg, db)
    idx.upsert_source = lambda *a, **k: None  # not used; fake only deletes

    result = purge_source(cfg, db, idx, "@Book2024", compact=False)
    assert result["found"] is True
    assert db.get_source("@Book2024") is None
    assert db.get_chunks_for_source("@Book2024") == []
    assert db.get_chapters_for_source("@Book2024") == []
    assert db.get_concepts_for_chunk("@Book2024::ch000::abc") == []

    citekey = "Book2024"
    assert not (
        cfg.vault_path / "10_Sources" / source_note_filename(citekey, "Livro Teste")
    ).exists()
    assert not (
        cfg.vault_path / "20_Literature" / literature_index_filename(citekey, "Livro Teste")
    ).exists()
    assert not (cfg.vault_path / "20_Literature" / literature_source_dirname(citekey)).exists()
    assert not (cfg.vault_path / "00_Inbox/Review" / literature_source_dirname(citekey)).exists()

    assert "@Book2024::ch000::abc" in idx.chunk_deletes
    assert "lit123" in idx.lit_deletes
    assert "@Book2024" in idx.source_deletes


def test_purge_source_keeps_permanent_cleans_wikilinks(env):
    cfg, db, idx = env
    _seed_source(cfg, db)

    ztl_path = cfg.vault_path / "30_Permanent" / "ZTL - NOTE01 - conceito.md"
    lit_stem = literature_chunk_filename_for_row(
        "Book2024",
        {
            "chunk_index": 3,
            "page_in_book": 10,
            "section_path": "Ch1 > Intro",
            "summary_json": json.dumps({"summary": "Resumo curto"}),
        },
    ).removesuffix(".md")
    body = (
        f"## Fonte\n\n- Ref. literatura: [[Book2024/{lit_stem}]]\n"
        f"<!-- zettel:auto-backlinks:start -->\n"
        f"- [[{source_note_stem('Book2024', 'Livro Teste')}]]\n"
        f"<!-- zettel:auto-backlinks:end -->\n"
    )
    _write_yaml(
        ztl_path,
        {
            "type": "permanent",
            "note_id": "NOTE01",
            "source_id": "@Book2024",
            "title": "Conceito",
        },
        body,
    )
    db.upsert_note("NOTE01", "@Book2024", str(ztl_path), title="Conceito", body=body)

    other = cfg.vault_path / "30_Permanent" / "ZTL - OTHER - outra.md"
    _write_yaml(
        other,
        {"type": "permanent", "note_id": "OTHER", "title": "Outra"},
        f"Link: [[Book2024/{lit_stem}]]\n",
    )
    db.upsert_note(
        "OTHER",
        None,
        str(other),
        title="Outra",
        body=f"Link: [[Book2024/{lit_stem}]]\n",
    )

    result = purge_source(cfg, db, idx, "@Book2024", compact=False)
    assert result["permanent_deleted"] == 0
    assert "NOTE01" not in idx.permanent_deletes
    assert ztl_path.exists()

    ztl_content = ztl_path.read_text(encoding="utf-8")
    assert "[[Book2024/" not in ztl_content
    assert "[[SRC -" not in ztl_content
    assert db.get_note("NOTE01")["source_id"] is None

    other_content = other.read_text(encoding="utf-8")
    assert "[[Book2024/" not in other_content
    assert result["wikilinks_cleaned"] >= 2


def test_purge_source_delete_permanent(env):
    cfg, db, idx = env
    _seed_source(cfg, db)

    ztl_path = cfg.vault_path / "30_Permanent" / "ZTL - NOTE01 - conceito.md"
    _write_yaml(
        ztl_path,
        {"type": "permanent", "note_id": "NOTE01", "source_id": "@Book2024"},
        "Corpo\n",
    )
    db.upsert_note("NOTE01", "@Book2024", str(ztl_path), title="Conceito", body="Corpo\n")
    db.upsert_note_connection("NOTE01", "OTHER", "related", "test")

    result = purge_source(cfg, db, idx, "@Book2024", delete_permanent=True, compact=False)
    assert result["permanent_deleted"] == 1
    assert not ztl_path.exists()
    assert db.get_note("NOTE01") is None
    assert db.get_note_connections("OTHER") == []
    assert "NOTE01" in idx.permanent_deletes


def test_purge_source_not_found(env):
    cfg, db, idx = env
    result = purge_source(cfg, db, idx, "@Missing", compact=False)
    assert result["found"] is False
