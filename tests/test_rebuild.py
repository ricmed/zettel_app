"""Tests for reindex + vault rebuild from SQLite — Fases 2 and 5."""

import json

import pytest
from zettel.config import AppConfig
from zettel.rebuild import run_rebuild_vault, run_reindex
from zettel.state import StateDB

from tests.fakes import FakeVectorIndex


@pytest.fixture
def db(tmp_path):
    d = StateDB(tmp_path / "state.db")
    yield d
    d.close()


def _seed(db):
    db.upsert_source("@S", "S2024", "Titulo Fonte", ["Autor Um"], 2024, "fc", "/p/f.md", "md")
    db.update_source_texts(
        "@S", extracted_text="texto", lit_body="---\ntype: literature\n---\ncorpo lit"
    )
    db.upsert_chapter("@S::ch000", "@S", "Cap", "chk")
    db.upsert_chunk("@S::ch000::a", "@S", "@S::ch000", "chunk text", "cka", section_path="Cap > A")
    db.upsert_note(
        "n1",
        "@S",
        None,
        "Nota Permanente",
        body="> **Tese**: algo\n\n## Definicao\n\ntexto",
        frontmatter_json=json.dumps({"type": "permanent", "note_id": "n1", "tags": ["t1", "t2"]}),
    )
    db.upsert_moc(
        "m1",
        "Topico X",
        None,
        "sig",
        body="# Topico X\n\nResumo do topico.\n\n## Sub\n\n- item",
        frontmatter_json=json.dumps({"type": "moc", "moc_id": "m1", "topic": "Topico X"}),
    )


def test_reindex_populates_all_collections(db):
    _seed(db)
    idx = FakeVectorIndex()
    # `chunks` is only reindexed while dedupe layer 5 is enabled (off by default).
    stats = run_reindex(AppConfig(harvest={"semantic_duplicate_enabled": True}), db, idx)
    assert stats["sources"] == 1
    assert stats["chunks"] == 1
    assert stats["permanent_notes"] == 1
    assert "mocs" not in stats
    assert set(idx.store["chunks"]) == {"@S::ch000::a"}
    assert set(idx.store["permanent_notes"]) == {"n1"}
    # embedding_input_hash backfilled so a re-embed can be skipped next time.
    assert db.get_note("n1")["embedding_input_hash"]


def test_reindex_single_collection_with_force(db):
    _seed(db)
    idx = FakeVectorIndex()
    cfg = AppConfig(harvest={"semantic_duplicate_enabled": True})
    stats = run_reindex(cfg, db, idx, collection="chunks", force=True)
    assert set(stats.keys()) == {"chunks"}
    assert "chunks" in idx.reset_calls


def test_reindex_leaves_chunks_untouched_while_layer5_is_off(db):
    """The flag means "the pipeline does not touch this collection" — not even reset.

    Resetting without repopulating would empty the collection as a side effect of
    a command whose contract is "rebuild".
    """
    _seed(db)
    idx = FakeVectorIndex()
    stats = run_reindex(AppConfig(), db, idx, collection="chunks", force=True)
    assert stats == {"chunks": 0}
    assert idx.reset_calls == []
    assert not idx.store["chunks"]


def test_reindex_unknown_collection_raises(db):
    with pytest.raises(ValueError):
        run_reindex(AppConfig(), db, FakeVectorIndex(), collection="bogus")


def test_reindex_force_after_embedding_swap(db, tmp_path, token_hash_embedding):
    """Troca de modelo com force regenera sources/chunks (sem force, ficariam stale)."""
    from zettel.index import EmbeddingSpaceMismatch, VectorIndex

    _seed(db)
    chroma = tmp_path / "chroma"
    cfg = AppConfig(
        embedding={"provider": "openai", "model": "modelo-a", "allow_fallback": True},
        chroma_path=chroma,
        harvest={"semantic_duplicate_enabled": True},
    )
    # Bypass Literal validation by constructing VectorIndex directly for markers.
    idx_a = VectorIndex(chroma, token_hash_embedding, "modelo-a")
    run_reindex(cfg, db, idx_a, force=True)
    assert idx_a.chunks.count() == 1
    assert idx_a.get_stored_embedding_identity() == (
        token_hash_embedding,
        "modelo-a",
        None,
    )

    with pytest.raises(EmbeddingSpaceMismatch):
        VectorIndex(chroma, token_hash_embedding, "modelo-b")

    idx_b = VectorIndex(
        chroma,
        token_hash_embedding,
        "modelo-b",
        reset_mismatched=True,
    )
    stats = run_reindex(cfg, db, idx_b, force=True)
    assert stats["chunks"] == 1
    assert stats["sources"] == 1
    assert idx_b.get_stored_embedding_identity() == (
        token_hash_embedding,
        "modelo-b",
        None,
    )
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
    assert not (cfg.vault_path / "30_Permanent").exists() or not list(
        (cfg.vault_path / "30_Permanent").glob("*.md")
    )


def test_rebuild_vault_does_not_overwrite_existing_without_force(db, tmp_path):
    _seed(db)
    cfg = AppConfig(vault_path=tmp_path / "vault")
    run_rebuild_vault(cfg, db)
    ztl = next(iter((cfg.vault_path / "30_Permanent").glob("*.md")))
    ztl.write_text("EDICAO MANUAL", encoding="utf-8")

    # Without force, existing files are skipped.
    run_rebuild_vault(cfg, db)
    assert ztl.read_text(encoding="utf-8") == "EDICAO MANUAL"


def test_rebuild_vault_force_preserves_manual_origin(db, tmp_path):
    _seed(db)
    # Mark the note as manual: force must still not overwrite it.
    db.upsert_note(
        "n1",
        "@S",
        None,
        "Nota Permanente",
        body="corpo novo",
        frontmatter_json=json.dumps({"type": "permanent"}),
        origin="manual",
    )
    cfg = AppConfig(vault_path=tmp_path / "vault")
    run_rebuild_vault(cfg, db)
    ztl = next(iter((cfg.vault_path / "30_Permanent").glob("*.md")))
    ztl.write_text("EDICAO MANUAL", encoding="utf-8")

    run_rebuild_vault(cfg, db, force=True)
    assert ztl.read_text(encoding="utf-8") == "EDICAO MANUAL"
