"""Tests for VectorIndex embedding safety and helpers — Fase 2."""

import pytest

from zettel.index import VectorIndex, _sanitize_metadata


def test_fail_fast_without_api_key(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CHROMA_OPENAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        VectorIndex(tmp_path / "chroma", "openai", "text-embedding-3-small", allow_fallback=False)


def test_unknown_provider_fails_fast(tmp_path):
    with pytest.raises(ValueError):
        VectorIndex(tmp_path / "chroma", "provider-invalido", "m", allow_fallback=False)


def test_existing_ids_empty_and_unknown_collection(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CHROMA_OPENAI_API_KEY", raising=False)
    # allow_fallback -> uses ChromaDB default embedding, works offline for empty gets.
    idx = VectorIndex(tmp_path / "chroma", "provider-invalido", "m", allow_fallback=True)
    assert idx.existing_ids("chunks", ["a", "b"]) == set()
    assert idx.existing_ids("chunks", []) == set()
    with pytest.raises(ValueError):
        idx.existing_ids("colecao-inexistente", ["a"])


def test_existing_ids_dedupes_duplicate_query_ids(tmp_path, monkeypatch):
    """ChromaDB get() rejects duplicate ids; existing_ids must dedupe first."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CHROMA_OPENAI_API_KEY", raising=False)
    idx = VectorIndex(tmp_path / "chroma", "provider-invalido", "m", allow_fallback=True)
    idx.upsert_chunk("cid-a", "texto a", {"source_id": "@S"})
    # Same id twice in the query list must not raise DuplicateIDError.
    found = idx.existing_ids("chunks", ["cid-a", "cid-b", "cid-a"])
    assert found == {"cid-a"}


def test_collection_metadata_marks_provider(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CHROMA_OPENAI_API_KEY", raising=False)
    idx = VectorIndex(tmp_path / "chroma", "provider-invalido", "meu-modelo", allow_fallback=True)
    meta = idx._collection_metadata()
    assert meta["embedding_provider"] == "provider-invalido"
    assert meta["embedding_model"] == "meu-modelo"


def test_sanitize_metadata_types():
    out = _sanitize_metadata({
        "s": "x", "i": 3, "f": 1.5, "b": True,
        "none": None, "list": ["a", "b"], "obj": object(),
    })
    assert out["s"] == "x" and out["i"] == 3 and out["f"] == 1.5 and out["b"] is True
    assert "none" not in out          # None dropped
    assert out["list"] == "a, b"      # list joined
    assert isinstance(out["obj"], str)
