"""Tests for manual sync across all vault folders + provenance — Fase 4."""

import pytest
import yaml

from zettel.config import AppConfig
from zettel.state import StateDB
from zettel.sync import run_sync_manual


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
    lit = cfg.vault_path / "20_Literature" / "LIT - @Autor2023 - artigo.md"
    _write(lit, {"type": "literature", "title": "Artigo"}, "Resumo manual do artigo.")
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
