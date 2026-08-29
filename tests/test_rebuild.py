"""Tests for reindex + vault rebuild from SQLite — Fases 2 and 5."""

import json

import pytest

from zettel.config import AppConfig
from zettel.rebuild import _moc_summary_from_body, run_rebuild_vault, run_reindex
from zettel.state import StateDB


class FakeIndex:
    """Records upserts per collection; enough for the rebuild code paths."""

    def __init__(self):
        self.store: dict[str, set[str]] = {
            "sources": set(), "chunks": set(), "permanent_notes": set(), "mocs": set()
        }
        self.reset_calls: list[str] = []

    def reset_collection(self, name):
        self.reset_calls.append(name)
        self.store[name] = set()

    def existing_ids(self, name, ids):
        return {i for i in ids if i in self.store[name]}

    def upsert_source(self, sid, summary, meta):
        self.store["sources"].add(sid)

    def upsert_chunk(self, cid, text, meta, **kwargs):
        self.store["chunks"].add(cid)

    def upsert_permanent_note(self, nid, text, meta):
        self.store["permanent_notes"].add(nid)

    def upsert_moc(self, mid, text, meta):
        self.store["mocs"].add(mid)


@pytest.fixture
def db(tmp_path):
    d = StateDB(tmp_path / "state.db")
    yield d
    d.close()


def _seed(db):
    db.upsert_source("@S", "S2024", "Titulo Fonte", ["Autor Um"], 2024, "fc", "/p/f.md", "md")
    db.update_source_texts("@S", extracted_text="texto", lit_body="---\ntype: literature\n---\ncorpo lit")
    db.upsert_chapter("@S::ch000", "@S", "Cap", "chk")
    db.upsert_chunk("@S::ch000::a", "@S", "@S::ch000", "chunk text", "cka", section_path="Cap > A")
    db.upsert_note(
        "n1", "@S", None, "Nota Permanente",
        body="> **Tese**: algo\n\n## Definicao\n\ntexto",
        frontmatter_json=json.dumps({"type": "permanent", "note_id": "n1", "tags": ["t1", "t2"]}),
    )
    db.upsert_moc(
        "m1", "Topico X", None, "sig",
        body="# Topico X\n\nResumo do topico.\n\n## Sub\n\n- item",
        frontmatter_json=json.dumps({"type": "moc", "moc_id": "m1", "topic": "Topico X"}),
    )


def test_moc_summary_extraction():
    body = "# Topico\n\nLinha de resumo.\n\n## Subsecao\n\n- nota"
    assert _moc_summary_from_body(body) == "Linha de resumo."


def test_reindex_populates_all_collections(db):
    _seed(db)
    idx = FakeIndex()
    stats = run_reindex(AppConfig(), db, idx)
    assert stats["sources"] == 1
    assert stats["chunks"] == 1
    assert stats["permanent_notes"] == 1
    assert stats["mocs"] == 1
    assert idx.store["chunks"] == {"@S::ch000::a"}
    assert idx.store["permanent_notes"] == {"n1"}
    # embedding_input_hash backfilled so a re-embed can be skipped next time.
    assert db.get_note("n1")["embedding_input_hash"]


def test_reindex_single_collection_with_force(db):
    _seed(db)
    idx = FakeIndex()
    stats = run_reindex(AppConfig(), db, idx, collection="chunks", force=True)
    assert set(stats.keys()) == {"chunks"}
    assert "chunks" in idx.reset_calls


def test_reindex_unknown_collection_raises(db):
    with pytest.raises(ValueError):
        run_reindex(AppConfig(), db, FakeIndex(), collection="bogus")


def test_reindex_force_after_embedding_swap(db, tmp_path, monkeypatch):
    """Troca de modelo com force regenera sources/chunks (sem force, ficariam stale)."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CHROMA_OPENAI_API_KEY", raising=False)
    from zettel.index import EmbeddingSpaceMismatch, VectorIndex

    _seed(db)
    chroma = tmp_path / "chroma"
    cfg = AppConfig(
        embedding={"provider": "openai", "model": "modelo-a", "allow_fallback": True},
        chroma_path=chroma,
    )
    # Bypass Literal validation by constructing VectorIndex directly for markers.
    idx_a = VectorIndex(chroma, "provider-invalido", "modelo-a", allow_fallback=True)
    run_reindex(cfg, db, idx_a, force=True)
    assert idx_a.chunks.count() == 1
    assert idx_a.get_stored_embedding_identity() == ("provider-invalido", "modelo-a", None)

    with pytest.raises(EmbeddingSpaceMismatch):
        VectorIndex(chroma, "provider-invalido", "modelo-b", allow_fallback=True)

    idx_b = VectorIndex(
        chroma, "provider-invalido", "modelo-b",
        allow_fallback=True, reset_mismatched=True,
    )
    stats = run_reindex(cfg, db, idx_b, force=True)
    assert stats["chunks"] == 1
    assert stats["sources"] == 1
    assert idx_b.get_stored_embedding_identity() == ("provider-invalido", "modelo-b", None)
    assert idx_b.chunks.count() == 1


def test_embedding_config_rejects_unknown_provider():
    from pydantic import ValidationError
    from zettel.config import EmbeddingConfig

    with pytest.raises(ValidationError):
        EmbeddingConfig(provider="provider-invalido", model="x")


def test_embedding_config_accepts_ollama():
    from zettel.config import EmbeddingConfig

    cfg = EmbeddingConfig(provider="ollama", model="qwen3-embedding", dimensions=1024)
    assert cfg.provider == "ollama"
    assert cfg.base_url is None
    assert cfg.dimensions == 1024


def test_embedding_config_rejects_non_positive_dimensions():
    from pydantic import ValidationError
    from zettel.config import EmbeddingConfig

    with pytest.raises(ValidationError):
        EmbeddingConfig(provider="ollama", model="qwen3-embedding", dimensions=0)


def test_rebuild_vault_writes_from_db(db, tmp_path):
    _seed(db)
    cfg = AppConfig(vault_path=tmp_path / "vault")
    stats = run_rebuild_vault(cfg, db)
    assert stats["sources"] == 1
    assert stats["literature"] == 1
    assert stats["permanent"] == 1
    assert stats["mocs"] == 1

    # ZTL file recreated with persisted body.
    ztl_files = list((cfg.vault_path / "30_Permanent").glob("*.md"))
    assert ztl_files
    assert "algo" in ztl_files[0].read_text(encoding="utf-8")
    lit_files = list((cfg.vault_path / "20_Literature").glob("*.md"))
    assert "corpo lit" in lit_files[0].read_text(encoding="utf-8")


def test_rebuild_vault_dry_run_writes_nothing(db, tmp_path):
    _seed(db)
    cfg = AppConfig(vault_path=tmp_path / "vault")
    stats = run_rebuild_vault(cfg, db, dry_run=True)
    assert stats["written"] > 0
    assert not (cfg.vault_path / "30_Permanent").exists() or \
        not list((cfg.vault_path / "30_Permanent").glob("*.md"))


def test_rebuild_vault_does_not_overwrite_existing_without_force(db, tmp_path):
    _seed(db)
    cfg = AppConfig(vault_path=tmp_path / "vault")
    run_rebuild_vault(cfg, db)
    ztl = list((cfg.vault_path / "30_Permanent").glob("*.md"))[0]
    ztl.write_text("EDICAO MANUAL", encoding="utf-8")

    # Without force, existing files are skipped.
    run_rebuild_vault(cfg, db)
    assert ztl.read_text(encoding="utf-8") == "EDICAO MANUAL"


def test_rebuild_vault_force_preserves_manual_origin(db, tmp_path):
    _seed(db)
    # Mark the note as manual: force must still not overwrite it.
    db.upsert_note("n1", "@S", None, "Nota Permanente",
                   body="corpo novo", frontmatter_json=json.dumps({"type": "permanent"}),
                   origin="manual")
    cfg = AppConfig(vault_path=tmp_path / "vault")
    run_rebuild_vault(cfg, db)
    ztl = list((cfg.vault_path / "30_Permanent").glob("*.md"))[0]
    ztl.write_text("EDICAO MANUAL", encoding="utf-8")

    run_rebuild_vault(cfg, db, force=True)
    assert ztl.read_text(encoding="utf-8") == "EDICAO MANUAL"
