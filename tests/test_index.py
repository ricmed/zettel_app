"""Tests for VectorIndex embedding safety and helpers — Fase 2 + embedding swap."""

from unittest.mock import MagicMock, patch

import pytest
from zettel.index import (
    EmbeddingSpaceMismatch,
    VectorIndex,
    _LangChainOllamaChromaEF,
    _normalize_ollama_base_url,
    _sanitize_metadata,
    peek_stored_embedding_identity,
)


def test_fail_fast_without_api_key(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CHROMA_OPENAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        VectorIndex(
            tmp_path / "chroma",
            "openai",
            "text-embedding-3-small",
            allow_fallback=False,
        )


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
    idx = VectorIndex(
        tmp_path / "chroma",
        "provider-invalido",
        "meu-modelo",
        allow_fallback=True,
        dimensions=1024,
    )
    meta = idx._collection_metadata()
    assert meta["embedding_provider"] == "provider-invalido"
    assert meta["embedding_model"] == "meu-modelo"
    assert meta["embedding_dimensions"] == 1024


def test_peek_empty_store(tmp_path):
    assert peek_stored_embedding_identity(tmp_path / "missing") == (None, None, None)


def test_embedding_space_matches_empty_and_same(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CHROMA_OPENAI_API_KEY", raising=False)
    path = tmp_path / "chroma"
    idx = VectorIndex(path, "provider-invalido", "modelo-a", allow_fallback=True)
    assert idx.embedding_space_matches() is True
    assert idx.get_stored_embedding_identity() == (
        "provider-invalido",
        "modelo-a",
        None,
    )
    assert peek_stored_embedding_identity(path) == (
        "provider-invalido",
        "modelo-a",
        None,
    )


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


def test_embedding_dimensions_mismatch_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CHROMA_OPENAI_API_KEY", raising=False)
    path = tmp_path / "chroma"
    VectorIndex(
        path,
        "provider-invalido",
        "modelo-a",
        allow_fallback=True,
        dimensions=1024,
    )
    with pytest.raises(EmbeddingSpaceMismatch) as ei:
        VectorIndex(
            path,
            "provider-invalido",
            "modelo-a",
            allow_fallback=True,
            dimensions=512,
        )
    assert ei.value.stored_dimensions == 1024
    assert ei.value.current_dimensions == 512


def test_embedding_space_reset_mismatched(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CHROMA_OPENAI_API_KEY", raising=False)
    path = tmp_path / "chroma"
    idx_a = VectorIndex(path, "provider-invalido", "modelo-a", allow_fallback=True)
    idx_a.upsert_chunk("c1", "texto", {"source_id": "@S"})
    assert idx_a.chunks.count() == 1

    idx_b = VectorIndex(
        path,
        "provider-invalido",
        "modelo-b",
        allow_fallback=True,
        reset_mismatched=True,
    )
    assert idx_b.embedding_space_matches() is True
    assert idx_b.get_stored_embedding_identity() == (
        "provider-invalido",
        "modelo-b",
        None,
    )
    # Collections were reset — old vectors gone.
    assert idx_b.chunks.count() == 0


def test_normalize_ollama_base_url_strips_v1():
    assert _normalize_ollama_base_url(None) == "http://localhost:11434"
    assert _normalize_ollama_base_url("http://localhost:11434/v1") == "http://localhost:11434"
    assert _normalize_ollama_base_url("http://127.0.0.1:11434/v1/") == "http://127.0.0.1:11434"
    assert _normalize_ollama_base_url("http://127.0.0.1:11434") == "http://127.0.0.1:11434"


def test_ollama_embedding_fn_builds_without_server(tmp_path):
    """langchain_ollama EF is constructed at init; no server call until upsert."""
    idx = VectorIndex(
        tmp_path / "chroma",
        "ollama",
        "qwen3-embedding",
        base_url="http://localhost:11434/v1",
        dimensions=1024,
    )
    assert idx.embedding_fn is not None
    assert isinstance(idx.embedding_fn, _LangChainOllamaChromaEF)
    assert idx.dimensions == 1024
    assert idx.embedding_fn.dimensions == 1024
    assert idx.get_stored_embedding_identity() == ("ollama", "qwen3-embedding", 1024)


def test_ollama_dimensions_forwarded_via_langchain(tmp_path):
    """OllamaEmbeddings receives dimensions; adapter returns float32 vectors of that size."""
    import numpy as np

    fake_emb = MagicMock()
    fake_emb.dimensions = 1024
    fake_emb.model = "qwen3-embedding"
    fake_emb.base_url = "http://localhost:11434"
    fake_emb.embed_documents.return_value = [[0.1] * 1024]

    with patch("langchain_ollama.OllamaEmbeddings", return_value=fake_emb) as ctor:
        idx = VectorIndex(
            tmp_path / "chroma",
            "ollama",
            "qwen3-embedding",
            base_url="http://localhost:11434/v1",
            dimensions=1024,
        )
        ctor.assert_called_once()
        kwargs = ctor.call_args.kwargs
        assert kwargs["model"] == "qwen3-embedding"
        assert kwargs["dimensions"] == 1024
        assert kwargs["base_url"] == "http://localhost:11434"

    out = idx.embedding_fn(["texto de teste"])
    fake_emb.embed_documents.assert_called_with(["texto de teste"])
    assert len(out) == 1
    assert len(out[0]) == 1024
    assert out[0].dtype == np.float32

    out_q = idx.embedding_fn.embed_query(["query de teste"])
    assert len(out_q[0]) == 1024


def test_ollama_base_url_strips_v1_for_native_client(tmp_path):
    with patch("langchain_ollama.OllamaEmbeddings") as ctor:
        fake = MagicMock()
        fake.dimensions = None
        fake.model = "nomic-embed-text"
        fake.base_url = "http://127.0.0.1:11434"
        ctor.return_value = fake
        idx = VectorIndex(
            tmp_path / "chroma",
            "ollama",
            "nomic-embed-text",
            base_url="http://127.0.0.1:11434",
        )
        assert idx.embedding_provider == "ollama"
        assert ctor.call_args.kwargs["base_url"] == "http://127.0.0.1:11434"


def test_ollama_build_from_config_roundtrip():
    """Chroma reconstrói o EF via build_from_config; deve incluir dimensions e base_url."""
    with patch("langchain_ollama.OllamaEmbeddings") as ctor:
        fake = MagicMock()
        fake.dimensions = 1024
        fake.model = "qwen3-embedding"
        fake.base_url = "http://localhost:11434"
        ctor.return_value = fake

        cfg = {
            "model": "qwen3-embedding",
            "dimensions": 1024,
            "base_url": "http://localhost:11434/v1",
        }
        ef = _LangChainOllamaChromaEF.build_from_config(cfg)
        assert isinstance(ef, _LangChainOllamaChromaEF)
        ctor.assert_called_once()
        kwargs = ctor.call_args.kwargs
        assert kwargs["model"] == "qwen3-embedding"
        assert kwargs["dimensions"] == 1024
        assert kwargs["base_url"] == "http://localhost:11434"

        out_cfg = ef.get_config()
        assert out_cfg["model"] == "qwen3-embedding"
        assert out_cfg["dimensions"] == 1024
        assert out_cfg["base_url"] == "http://localhost:11434"


def test_sanitize_metadata_types():
    out = _sanitize_metadata(
        {
            "s": "x",
            "i": 3,
            "f": 1.5,
            "b": True,
            "none": None,
            "list": ["a", "b"],
            "obj": object(),
        }
    )
    assert out["s"] == "x" and out["i"] == 3 and out["f"] == 1.5 and out["b"] is True
    assert "none" not in out  # None dropped
    assert out["list"] == "a, b"  # list joined
    assert isinstance(out["obj"], str)
