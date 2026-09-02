"""Tests for manual sync across all vault folders + provenance — Fase 4."""

import pytest
import yaml

from zettel.config import AppConfig
from zettel.state import StateDB
from zettel.sync import (
    _extract_body_edges,
    rebuild_manual_edges,
    repair_permanent_links,
    run_sync_manual,
)


class FakeIndex:
    def __init__(self):
        self.sources: list[str] = []
        self.permanent: list[str] = []
        self.mocs: list[str] = []

    def upsert_source(self, sid, summary, meta):
        self.sources.append(sid)

    def upsert_permanent_note(self, nid, text, meta):
        self.permanent.append(nid)

    def upsert_moc(self, mid, text, meta):
        self.mocs.append(mid)

    def query_similar_notes(self, text, n_results=5, exclude_id=None):
        return []


@pytest.fixture
def db(tmp_path):
    d = StateDB(tmp_path / "state.db")
    yield d
    d.close()


@pytest.fixture
def cfg(tmp_path):
    c = AppConfig(vault_path=tmp_path / "vault")
    for d in ("10_Sources", "20_Literature", "30_Permanent", "40_MOCs"):
        (c.vault_path / d).mkdir(parents=True, exist_ok=True)
    return c


def _write(path, meta, body):
    fm = yaml.dump(meta, allow_unicode=True, sort_keys=False)
    path.write_text(f"---\n{fm}---\n\n{body}", encoding="utf-8")


def _frontmatter(path):
    content = path.read_text(encoding="utf-8")
    parts = content.split("---", 2)
    return yaml.safe_load(parts[1])


def test_manual_source_is_adopted(cfg, db):
    src = cfg.vault_path / "10_Sources" / "SRC - meu-artigo.md"
    _write(src, {"type": "source", "title": "Meu Artigo", "year": 2023}, "# Meu Artigo")
    idx = FakeIndex()
    stats = run_sync_manual(cfg, db, idx)

    assert stats["sources"] == 1
    # source_id + origin:manual injected into the file.
    fm = _frontmatter(src)
    assert fm["source_id"].startswith("@")
    assert fm["origin"] == "manual"
    # Registered in the DB with manual origin.
    stored = db.get_source(fm["source_id"])
    assert stored is not None
    assert stored["origin"] == "manual"
    assert idx.sources == [fm["source_id"]]


def test_manual_literature_links_and_persists_body(cfg, db):
    lit = cfg.vault_path / "20_Literature" / "LIT - Autor2023 - artigo.md"
    _write(
        lit,
        {
            "type": "literature_index",
            "title": "Artigo",
            "source_id": "@Autor2023",
            "citekey": "Autor2023",
        },
        "Resumo manual do artigo.",
    )
    idx = FakeIndex()
    stats = run_sync_manual(cfg, db, idx)

    assert stats["literature"] == 1
    # An orphan LIT creates a manual source it can attach to.
    src = db.get_source("@Autor2023")
    assert src is not None
    assert src["origin"] == "manual"
    assert "Resumo manual" in (src["lit_body"] or "")


def test_manual_permanent_gets_id_and_origin(cfg, db):
    ztl = cfg.vault_path / "30_Permanent" / "ZTL - nota-manual.md"
    _write(ztl, {"type": "permanent", "title": "Nota Manual"},
           "> **Tese**: uma tese manual\n\n## Definicao\n\ntexto")
    idx = FakeIndex()
    stats = run_sync_manual(cfg, db, idx)

    assert stats["permanent"] == 1
    fm = _frontmatter(ztl)
    assert "note_id" in fm
    assert fm["origin"] == "manual"
    note = db.get_note(fm["note_id"])
    assert note["origin"] == "manual"
    assert note["body"]  # body persisted
    assert idx.permanent == [fm["note_id"]]


def test_pipeline_note_stays_pipeline(cfg, db):
    ztl = cfg.vault_path / "30_Permanent" / "ZTL - 01ABC - pipe.md"
    _write(ztl, {"type": "permanent", "note_id": "01ABC", "title": "Pipe", "origin": "pipeline"},
           "> **Tese**: gerada pelo pipeline\n\n## Definicao\n\ntexto")
    idx = FakeIndex()
    run_sync_manual(cfg, db, idx)
    assert db.get_note("01ABC")["origin"] == "pipeline"


def test_resync_unchanged_is_skipped(cfg, db):
    ztl = cfg.vault_path / "30_Permanent" / "ZTL - nota.md"
    _write(ztl, {"type": "permanent", "title": "Nota"},
           "> **Tese**: tese estavel\n\n## Definicao\n\ntexto")
    idx = FakeIndex()
    run_sync_manual(cfg, db, idx)  # first pass: new
    stats2 = run_sync_manual(cfg, db, idx)  # second pass: unchanged
    assert stats2["skipped"] >= 1
    assert stats2["permanent"] == 0


def _seed_pipeline_granular_lit(cfg, db, *, path=None):
    """Approved pipeline LIT: vault says status:approved, chunk row is persisted."""
    source_id = "@Pipe2024"
    chunk_id = f"{source_id}::ch000::abcd"
    lit_id = "lit-pipe-1"
    db.upsert_source(
        source_id, "Pipe2024", "Paper", ["Autor"], 2024,
        "h", "/x.pdf", "pdf", origin="pipeline",
    )
    db.upsert_chapter(f"{source_id}::ch000", source_id, "Ch1", "chh")
    lit_dir = cfg.vault_path / "20_Literature" / "Pipe2024"
    lit_dir.mkdir(parents=True, exist_ok=True)
    lit = path or (lit_dir / "LIT - Pipe2024 - p001 - topic-0001.md")
    _write(
        lit,
        {
            "type": "literature",
            "origin": "pipeline",
            "source_id": source_id,
            "citekey": "Pipe2024",
            "chunk_id": chunk_id,
            "literature_id": lit_id,
            "status": "approved",
        },
        "Resumo gerado pelo pipeline.",
    )
    db.upsert_chunk(
        chunk_id, source_id, f"{source_id}::ch000",
        "excerpt", "ck",
        status="persisted",
        literature_note_path=str(lit),
        literature_id=lit_id,
    )
    return chunk_id, lit


def test_pipeline_granular_lit_unchanged_is_skipped(cfg, db):
    chunk_id, _ = _seed_pipeline_granular_lit(cfg, db)
    idx = FakeIndex()
    stats = run_sync_manual(cfg, db, idx)
    assert stats["literature"] == 0
    assert stats["skipped"] >= 1
    assert db.get_chunk(chunk_id)["status"] == "persisted"


def test_pipeline_granular_lit_does_not_overwrite_persisted_status(cfg, db):
    chunk_id, lit = _seed_pipeline_granular_lit(cfg, db)
    # Simulate the old bug: path already matches, frontmatter says approved.
    idx = FakeIndex()
    run_sync_manual(cfg, db, idx)
    assert db.get_chunk(chunk_id)["status"] == "persisted"
    assert db.get_chunk(chunk_id)["literature_note_path"] == str(lit)


def test_pipeline_granular_lit_moved_updates_path(cfg, db):
    chunk_id, old = _seed_pipeline_granular_lit(cfg, db)
    new = old.with_name("LIT - Pipe2024 - p001 - topic-renomeado.md")
    old.rename(new)
    db.update_chunk_review(chunk_id, literature_note_path=str(old))
    idx = FakeIndex()
    stats = run_sync_manual(cfg, db, idx)
    assert stats["literature"] == 1
    row = db.get_chunk(chunk_id)
    assert row["literature_note_path"] == str(new)
    assert row["status"] == "persisted"


# ── Graph edges from manual wikilinks (Etapa 6) ────────────────────────

# Valid ULID-shaped ids (Crockford base32, 26 chars).
_A = "01HAAAAAAAAAAAAAAAAAAAAAAA"
_B = "01HBBBBBBBBBBBBBBBBBBBBBBB"
_C = "01HCCCCCCCCCCCCCCCCCCCCCCC"


def _seed(db, *note_ids):
    for nid in note_ids:
        db.upsert_note(nid, "@S", f"/p/{nid}.md", f"Nota {nid}", body="corpo")


def test_body_wikilink_creates_related_edge(db):
    _seed(db, _A, _B)
    body = f"> **Tese**: x\n\n## Conexoes\n\n- [[ZTL - {_B} - nota-b]]: relacionada"
    created = _extract_body_edges(db, _A, body)
    assert created == 1
    edges = db.get_note_connections(_A)
    assert len(edges) == 1
    assert edges[0]["relation_type"] == "related"
    assert {edges[0]["source_note_id"], edges[0]["target_note_id"]} == {_A, _B}


def test_wikilink_in_managed_block_is_ignored(db):
    _seed(db, _A, _B, _C)
    body = (
        f"> **Tese**: x\n\n## Conexoes\n\n- [[ZTL - {_B} - nota-b]]\n\n"
        f"<!-- zettel:auto-connections:start -->\n"
        f"- [[ZTL - {_C} - nota-c]]\n"
        f"<!-- zettel:auto-connections:end -->\n"
    )
    _extract_body_edges(db, _A, body)
    edges = db.get_note_connections(_A)
    targets = {e["target_note_id"] for e in edges} | {e["source_note_id"] for e in edges}
    assert _B in targets       # body link accepted
    assert _C not in targets   # suggestion block link ignored


def test_self_link_ignored(db):
    _seed(db, _A)
    body = f"- [[ZTL - {_A} - eu-mesmo]]"
    assert _extract_body_edges(db, _A, body) == 0
    assert db.get_note_connections(_A) == []


def test_link_to_unknown_note_ignored(db):
    _seed(db, _A)  # _B not seeded
    body = f"- [[ZTL - {_B} - fantasma]]"
    assert _extract_body_edges(db, _A, body) == 0


def test_existing_typed_edge_not_downgraded(db):
    _seed(db, _A, _B)
    db.upsert_note_connection(_A, _B, "contradicts", "tensiona")
    body = f"- [[ZTL - {_B} - nota-b]]"
    created = _extract_body_edges(db, _A, body)
    assert created == 0
    edges = db.get_note_connections(_A)
    assert len(edges) == 1
    assert edges[0]["relation_type"] == "contradicts"  # preserved


def test_existing_reverse_edge_not_duplicated(db):
    _seed(db, _A, _B)
    db.upsert_note_connection(_B, _A, "extends", "")  # reverse direction
    body = f"- [[ZTL - {_B} - nota-b]]"
    assert _extract_body_edges(db, _A, body) == 0


def test_rebuild_manual_edges_backfills(db):
    _seed(db, _A, _C)
    # Overwrite _A's body to contain a link to _C.
    db.upsert_note(_A, "@S", f"/p/{_A}.md", "Nota A",
                   body=f"## Conexoes\n\n- [[ZTL - {_C} - nota-c]]")
    stats = rebuild_manual_edges(db)
    assert stats["edges_created"] == 1
    assert db.count_note_connections() == 1


def test_edited_moc_returns_updated(cfg, db):
    moc = cfg.vault_path / "40_MOCs" / "MOC - topico.md"
    _write(moc, {"type": "moc", "topic": "Topico"}, "# Topico\n\nResumo inicial.\n\n## Sub\n\n- a")
    idx = FakeIndex()
    run_sync_manual(cfg, db, idx)  # new (moc_id injected)

    moc_id = _frontmatter(moc)["moc_id"]
    # Edit the body → semantic checksum changes → must be 'updated', not 'new'.
    _write(moc, {"type": "moc", "topic": "Topico", "moc_id": moc_id},
           "# Topico\n\nResumo bem diferente agora.\n\n## Sub\n\n- a\n- b")
    stats = run_sync_manual(cfg, db, idx)
    assert stats["updated"] >= 1


def test_repair_permanent_links_rewrites_double_prefix_and_rebuilds_backlinks(cfg, db):
    _A = "01HAAAAAAAAAAAAAAAAAAAAAAA"
    _B = "01HBBBBBBBBBBBBBBBBBBBBBBB"
    path_a = cfg.vault_path / "30_Permanent" / f"ZTL - {_A} - analise-de-series-temporais.md"
    path_b = cfg.vault_path / "30_Permanent" / f"ZTL - {_B} - sazonalidade.md"
    _write(
        path_a,
        {"type": "permanent", "note_id": _A, "title": "Analise"},
        f"## Conexoes\n\n- [[ZTL - ZTL - {_B}]] (extends) -- contexto\n",
    )
    _write(
        path_b,
        {"type": "permanent", "note_id": _B, "title": "Sazonalidade"},
        (
            "## Conexoes\n\n"
            "<!-- zettel:auto-backlinks:start -->\n"
            "- [[ZTL - GHOST - fantasma]] (relacionado) -- morto\n"
            "<!-- zettel:auto-backlinks:end -->\n"
        ),
    )
    db.upsert_note(_A, "@S", str(path_a), "Analise", body="x")
    db.upsert_note(_B, "@S", str(path_b), "Sazonalidade", body="x")
    db.upsert_note_connection(_A, _B, "extends", "contexto")

    stats = repair_permanent_links(db)
    assert stats["wikilinks_rewritten"] >= 1

    body_a = path_a.read_text(encoding="utf-8")
    assert f"[[ZTL - {_B} - sazonalidade]]" in body_a
    assert f"ZTL - ZTL - {_B}" not in body_a

    from zettel.vault import read_managed_block
    block = read_managed_block(path_b.read_text(encoding="utf-8"), "auto-backlinks")
    assert block is not None
    assert "GHOST" not in block
    assert f"ZTL - {_A} - analise-de-series-temporais" in block
    assert "estendido por" in block
