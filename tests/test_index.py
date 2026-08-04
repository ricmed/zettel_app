"""Tests for VectorIndex embedding safety and helpers — Fase 2 + embedding swap."""

import pytest

from zettel.index import (
    EmbeddingSpaceMismatch,
    VectorIndex,
    _sanitize_metadata,
    peek_stored_embedding_identity,
)


def test_fail_fast_without_api_key(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CHROMA_OPENAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        VectorIndex(tmp_path / "chroma", "openai", "text-embedding-3-small", allow_fallback=False)


def test_unknown_provider_fails_fast(tmp_path):
    with pytest.raises(ValueError, match="openai"):
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


def test_peek_empty_store(tmp_path):
    assert peek_stored_embedding_identity(tmp_path / "missing") == (None, None)


def test_embedding_space_matches_empty_and_same(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CHROMA_OPENAI_API_KEY", raising=False)
    path = tmp_path / "chroma"
    idx = VectorIndex(path, "provider-invalido", "modelo-a", allow_fallback=True)
    assert idx.embedding_space_matches() is True
    assert idx.get_stored_embedding_identity() == ("provider-invalido", "modelo-a")
    assert peek_stored_embedding_identity(path) == ("provider-invalido", "modelo-a")


def test_embedding_space_mismatch_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CHROMA_OPENAI_API_KEY", raising=False)
    path = tmp_path / "chroma"
    VectorIndex(path, "provider-invalido", "modelo-a", allow_fallback=True)

    with pytest.raises(EmbeddingSpaceMismatch) as ei:
        VectorIndex(path, "provider-invalido", "modelo-b", allow_fallback=True)
    exc = ei.value
    assert exc.stored_model == "modelo-a"
    assert exc.current_model == "modelo-b"


def test_embedding_space_reset_mismatched(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CHROMA_OPENAI_API_KEY", raising=False)
    path = tmp_path / "chroma"
    idx_a = VectorIndex(path, "provider-invalido", "modelo-a", allow_fallback=True)
    idx_a.upsert_chunk("c1", "texto", {"source_id": "@S"})
    assert idx_a.chunks.count() == 1

    idx_b = VectorIndex(
        path, "provider-invalido", "modelo-b",
        allow_fallback=True, reset_mismatched=True,
    )
    assert idx_b.embedding_space_matches() is True
    assert idx_b.get_stored_embedding_identity() == ("provider-invalido", "modelo-b")
    # Collections were reset — old vectors gone.
    assert idx_b.chunks.count() == 0


def test_ollama_embedding_fn_builds_without_server(tmp_path):
    """OpenAI-compatible EF is constructed at init; no server call until upsert."""
    idx = VectorIndex(
        tmp_path / "chroma",
        "ollama",
        "qwen3-embedding",
        base_url="http://localhost:11434/v1",
    )
    assert idx.embedding_fn is not None
    assert idx.get_stored_embedding_identity() == ("ollama", "qwen3-embedding")


def test_ollama_base_url_appends_v1_suffix(tmp_path):
    idx = VectorIndex(
        tmp_path / "chroma",
        "ollama",
        "nomic-embed-text",
        base_url="http://127.0.0.1:11434",
    )
    assert idx.embedding_provider == "ollama"
    # api_base should include /v1 (stored on the EF when available).
    api_base = getattr(idx.embedding_fn, "_api_base", None) or getattr(
        idx.embedding_fn, "api_base", None
    )
    if api_base:
        assert api_base.rstrip("/").endswith("/v1")


def test_sanitize_metadata_types():
    out = _sanitize_metadata({
        "s": "x", "i": 3, "f": 1.5, "b": True,
        "none": None, "list": ["a", "b"], "obj": object(),
    })
    assert out["s"] == "x" and out["i"] == 3 and out["f"] == 1.5 and out["b"] is True
    assert "none" not in out          # None dropped
    assert out["list"] == "a, b"      # list joined
    assert isinstance(out["obj"], str)
