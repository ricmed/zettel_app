"""Tests for VectorIndex embedding safety and helpers — Fase 2 + embedding swap."""

import math
from typing import get_args
from unittest.mock import MagicMock, patch

import pytest
from zettel.config import EmbeddingConfig
from zettel.index import (
    _EF_BUILDERS,
    EmbeddingSpaceMismatch,
    VectorIndex,
    _GeminiChromaEF,
    _normalize_ollama_base_url,
    _OllamaChromaEF,
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


def test_unknown_provider_with_fallback_leaves_the_embedding_to_chroma():
    """``allow_fallback`` hands an unknown provider to Chroma's own default.

    Built on the method alone: the default downloads an ONNX model on first use,
    which no test may do (#324).
    """
    from types import SimpleNamespace

    owner = SimpleNamespace(allow_fallback=True)
    assert VectorIndex._build_embedding_fn(owner, "provider-invalido", "m") is None


def test_existing_ids_empty_and_unknown_collection(tmp_path, token_hash_embedding):
    idx = VectorIndex(tmp_path / "chroma", token_hash_embedding, "m")
    assert idx.existing_ids("chunks", ["a", "b"]) == set()
    assert idx.existing_ids("chunks", []) == set()
    with pytest.raises(ValueError):
        idx.existing_ids("colecao-inexistente", ["a"])


def test_existing_ids_dedupes_duplicate_query_ids(tmp_path, token_hash_embedding):
    """ChromaDB get() rejects duplicate ids; existing_ids must dedupe first."""
    idx = VectorIndex(tmp_path / "chroma", token_hash_embedding, "m")
    idx.upsert_chunk("cid-a", "texto a", {"source_id": "@S"})
    # Same id twice in the query list must not raise DuplicateIDError.
    found = idx.existing_ids("chunks", ["cid-a", "cid-b", "cid-a"])
    assert found == {"cid-a"}


def test_chroma_cosine_space_distance_is_one_minus_cosine():
    """A real ephemeral collection in the declared space: d = 1 - cos (#267)."""
    import chromadb
    from zettel.similarity import HNSW_SPACE, cosine_from_distance

    client = chromadb.EphemeralClient()
    col = client.create_collection(
        "probe_cosine",
        configuration={"hnsw": {"space": HNSW_SPACE}},
    )
    left = [1.0, 0.0, 0.0]
    right = [0.6, 0.8, 0.0]
    col.add(ids=["left", "right"], embeddings=[left, right])
    result = col.query(query_embeddings=[left], n_results=2)
    by_id = dict(zip(result["ids"][0], result["distances"][0], strict=True))
    assert by_id["left"] == pytest.approx(0.0, abs=1e-5)
    assert by_id["right"] == pytest.approx(0.4, abs=1e-5)
    assert cosine_from_distance(by_id["right"], "cosine") == pytest.approx(0.6, abs=1e-5)


def test_query_similar_notes_filters_by_source_id(tmp_path, token_hash_embedding):
    """#271: a where filter keeps a nearer other-source note out of the dedupe pool."""
    idx = VectorIndex(tmp_path / "chroma", token_hash_embedding, "m")
    idx.upsert_permanent_note("near", "grafos de conhecimento e ligacoes", {"source_id": "@Other"})
    idx.upsert_permanent_note("same", "grafos de conhecimento e ligacoes", {"source_id": "@S"})
    hits = idx.query_similar_notes(
        "grafos de conhecimento e ligacoes",
        n_results=5,
        where={"source_id": "@S"},
    )
    assert [hit["id"] for hit in hits] == ["same"]


def test_chapter_summary_distance_uses_that_collections_space(tmp_path, token_hash_embedding):
    """A reindex of one collection must not convert its distances as another."""
    idx = VectorIndex(tmp_path / "chroma", token_hash_embedding, "m")
    idx.client.delete_collection("chapter_summaries")
    kwargs = idx._collection_kwargs()
    kwargs["configuration"] = {"hnsw": {"space": "l2"}}
    idx.chapter_summaries = idx.client.get_or_create_collection("chapter_summaries", **kwargs)
    assert idx.distance_space == "cosine"
    assert idx.collection_space(idx.chapter_summaries) == "l2"
    idx.upsert_chapter_summary("ch1", "resumo do capitulo sobre grafos", {"source_id": "@S"})
    hits = idx.query_chapter_summaries("resumo do capitulo sobre grafos", n_results=1)
    assert hits[0]["distance_space"] == "l2"
    idx.upsert_permanent_note("n1", "grafos de conhecimento", {"source_id": "@S"})
    notes = idx.query_similar_notes("grafos de conhecimento", n_results=1)
    assert notes[0]["distance_space"] == "cosine"


def test_vector_index_declares_cosine_space(tmp_path, token_hash_embedding):
    idx = VectorIndex(tmp_path / "chroma", token_hash_embedding, "m")
    assert idx.distance_space == "cosine"
    assert idx.permanent.configuration["hnsw"]["space"] == "cosine"
    assert idx.to_cosine(0.4) == pytest.approx(0.6)


def test_collection_metadata_marks_provider(tmp_path, token_hash_embedding):
    idx = VectorIndex(
        tmp_path / "chroma",
        token_hash_embedding,
        "meu-modelo",
        dimensions=1024,
    )
    meta = idx._collection_metadata()
    assert meta["embedding_provider"] == token_hash_embedding
    assert meta["embedding_model"] == "meu-modelo"
    assert meta["embedding_dimensions"] == 1024


def test_peek_empty_store(tmp_path):
    assert peek_stored_embedding_identity(tmp_path / "missing") == (None, None, None)


def test_embedding_space_matches_empty_and_same(tmp_path, token_hash_embedding):
    path = tmp_path / "chroma"
    idx = VectorIndex(path, token_hash_embedding, "modelo-a")
    assert idx.embedding_space_matches() is True
    assert idx.get_stored_embedding_identity() == (
        token_hash_embedding,
        "modelo-a",
        None,
    )
    assert peek_stored_embedding_identity(path) == (
        token_hash_embedding,
        "modelo-a",
        None,
    )


def test_embedding_space_mismatch_raises(tmp_path, token_hash_embedding):
    path = tmp_path / "chroma"
    VectorIndex(path, token_hash_embedding, "modelo-a")

    with pytest.raises(EmbeddingSpaceMismatch) as ei:
        VectorIndex(path, token_hash_embedding, "modelo-b")
    exc = ei.value
    assert exc.stored_model == "modelo-a"
    assert exc.current_model == "modelo-b"


def test_embedding_dimensions_mismatch_raises(tmp_path, token_hash_embedding):
    path = tmp_path / "chroma"
    VectorIndex(
        path,
        token_hash_embedding,
        "modelo-a",
        dimensions=1024,
    )
    with pytest.raises(EmbeddingSpaceMismatch) as ei:
        VectorIndex(
            path,
            token_hash_embedding,
            "modelo-a",
            dimensions=512,
        )
    assert ei.value.stored_dimensions == 1024
    assert ei.value.current_dimensions == 512


def test_embedding_space_reset_mismatched(tmp_path, token_hash_embedding):
    path = tmp_path / "chroma"
    idx_a = VectorIndex(path, token_hash_embedding, "modelo-a")
    idx_a.upsert_chunk("c1", "texto", {"source_id": "@S"})
    assert idx_a.chunks.count() == 1

    idx_b = VectorIndex(
        path,
        token_hash_embedding,
        "modelo-b",
        reset_mismatched=True,
    )
    assert idx_b.embedding_space_matches() is True
    assert idx_b.get_stored_embedding_identity() == (
        token_hash_embedding,
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
    assert isinstance(idx.embedding_fn, _OllamaChromaEF)
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
    fake_emb.embed_query.return_value = [0.2] * 1024

    with patch("langchain_ollama.OllamaEmbeddings", return_value=fake_emb) as ctor:
        idx = VectorIndex(
            tmp_path / "chroma",
            "ollama",
            "qwen3-embedding",
            base_url="http://localhost:11434/v1",
            dimensions=1024,
        )
        # Chroma re-instantiates the function when the collection declares hnsw.space.
        assert ctor.call_count >= 1
        for call in ctor.call_args_list:
            assert call.kwargs["model"] == "qwen3-embedding"
            assert call.kwargs["dimensions"] == 1024
            assert call.kwargs["base_url"] == "http://localhost:11434"

    out = idx.embedding_fn(["texto de teste"])
    fake_emb.embed_documents.assert_called_with(["texto de teste"])
    assert len(out) == 1
    assert len(out[0]) == 1024
    assert out[0].dtype == np.float32

    out_q = idx.embedding_fn.embed_query(["query de teste"])
    fake_emb.embed_query.assert_called_with("query de teste")
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
        ef = _OllamaChromaEF.build_from_config(cfg)
        assert isinstance(ef, _OllamaChromaEF)
        ctor.assert_called_once()
        kwargs = ctor.call_args.kwargs
        assert kwargs["model"] == "qwen3-embedding"
        assert kwargs["dimensions"] == 1024
        assert kwargs["base_url"] == "http://localhost:11434"

        out_cfg = ef.get_config()
        assert out_cfg["model"] == "qwen3-embedding"
        assert out_cfg["dimensions"] == 1024
        assert out_cfg["base_url"] == "http://localhost:11434"


def test_provider_registry_matches_config_literal():
    """Um provider aceito pelo schema tem builder, e vice-versa."""
    literal = EmbeddingConfig.model_fields["provider"].annotation
    assert set(get_args(literal)) == set(_EF_BUILDERS)


def _fake_gemini(doc_vectors, query_vector):
    fake = MagicMock()
    fake.embed_documents.return_value = doc_vectors
    fake.embed_query.return_value = query_vector
    return fake


def test_gemini_fails_fast_without_api_key(tmp_path, monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="GOOGLE_API_KEY"):
        VectorIndex(tmp_path / "chroma", "gemini", "gemini-embedding-001")


def test_gemini_builds_with_dimensions_and_marks_space(tmp_path, monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "chave-teste")
    fake = _fake_gemini([[3.0, 4.0]], [0.0, 2.0])
    with patch("langchain_google_genai.GoogleGenerativeAIEmbeddings", return_value=fake) as ctor:
        idx = VectorIndex(tmp_path / "chroma", "gemini", "gemini-embedding-001", dimensions=768)
    kwargs = ctor.call_args.kwargs
    assert kwargs["model"] == "gemini-embedding-001"
    assert kwargs["output_dimensionality"] == 768
    assert kwargs["google_api_key"] == "chave-teste"
    assert isinstance(idx.embedding_fn, _GeminiChromaEF)
    assert idx.get_stored_embedding_identity() == ("gemini", "gemini-embedding-001", 768)


def test_gemini_routes_query_side_and_normalizes(monkeypatch):
    """Documento via embed_documents, consulta via embed_query; ambos com norma 1."""
    import numpy as np

    monkeypatch.setenv("GOOGLE_API_KEY", "chave-teste")
    fake = _fake_gemini([[3.0, 4.0]], [0.0, 2.0])
    with patch("langchain_google_genai.GoogleGenerativeAIEmbeddings", return_value=fake):
        ef = _GeminiChromaEF.build_from_config({"model": "gemini-embedding-001"})

    docs = ef(["documento"])
    fake.embed_documents.assert_called_once_with(["documento"])
    assert docs[0].dtype == np.float32
    assert np.allclose(docs[0], [0.6, 0.8])

    queries = ef.embed_query(["pergunta"])
    fake.embed_query.assert_called_once_with("pergunta")
    assert math.isclose(float(np.linalg.norm(queries[0])), 1.0, rel_tol=1e-6)


def test_gemini_config_never_persists_the_key(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "segredo")
    with patch("langchain_google_genai.GoogleGenerativeAIEmbeddings") as ctor:
        ctor.return_value = _fake_gemini([], [])
        ef = _GeminiChromaEF.build_from_config(
            {"model": "gemini-embedding-001", "dimensions": 1536}
        )
        rebuilt = _GeminiChromaEF.build_from_config(ef.get_config())
    assert ef.get_config() == {"model": "gemini-embedding-001", "dimensions": 1536}
    assert "segredo" not in str(ef.get_config())
    assert rebuilt.dimensions == 1536
    assert ctor.call_args.kwargs["google_api_key"] == "segredo"


def _google_error(code: int) -> Exception:
    """What LangChain raises: GoogleGenerativeAIError wrapping the SDK's APIError."""
    from google.genai.errors import ClientError
    from langchain_google_genai._common import GoogleGenerativeAIError

    status = {429: "RESOURCE_EXHAUSTED", 400: "INVALID_ARGUMENT"}[code]
    cause = ClientError(code, {"error": {"code": code, "message": "x", "status": status}}, None)
    try:
        raise GoogleGenerativeAIError(f"Error embedding content ({status})") from cause
    except GoogleGenerativeAIError as wrapped:
        return wrapped


@pytest.fixture
def gemini_ef_no_wait(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "chave-teste")
    monkeypatch.setattr(_GeminiChromaEF, "retry_initial_wait", 0.0)
    monkeypatch.setattr(_GeminiChromaEF, "retry_jitter", 0.0)

    def build(fake):
        with patch("langchain_google_genai.GoogleGenerativeAIEmbeddings", return_value=fake):
            return _GeminiChromaEF.build_from_config({"model": "gemini-embedding-001"})

    return build


def test_gemini_retries_rate_limit_then_succeeds(gemini_ef_no_wait):
    """Um 429 no meio de um run longo (ex.: garden) nao pode aborta-lo."""
    fake = MagicMock()
    fake.embed_documents.side_effect = [_google_error(429), _google_error(429), [[1.0, 0.0]]]
    fake.embed_query.side_effect = [_google_error(429), [0.0, 1.0]]
    ef = gemini_ef_no_wait(fake)

    assert len(ef(["moc"])) == 1
    assert fake.embed_documents.call_count == 3
    assert len(ef.embed_query(["pergunta"])) == 1
    assert fake.embed_query.call_count == 2


def test_gemini_gives_up_after_retry_attempts(gemini_ef_no_wait):
    from langchain_google_genai._common import GoogleGenerativeAIError

    fake = MagicMock()
    fake.embed_documents.side_effect = _google_error(429)
    ef = gemini_ef_no_wait(fake)

    with pytest.raises(GoogleGenerativeAIError):
        ef(["moc"])
    assert fake.embed_documents.call_count == _GeminiChromaEF.retry_attempts


def test_gemini_does_not_retry_client_errors(gemini_ef_no_wait):
    """400 (entrada invalida) nao melhora com espera: falha na primeira tentativa."""
    from langchain_google_genai._common import GoogleGenerativeAIError

    fake = MagicMock()
    fake.embed_documents.side_effect = _google_error(400)
    ef = gemini_ef_no_wait(fake)

    with pytest.raises(GoogleGenerativeAIError):
        ef(["moc"])
    assert fake.embed_documents.call_count == 1


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


def test_embed_texts_records_its_own_embedding_usage(tmp_path, token_hash_embedding):
    """Every caller is charged, not only the one that remembered to record (#327).

    The intra-batch dedupe used to call the private ``_record_embed_usage`` itself,
    and only for part of what it embedded; the garden's category labels and
    connect's category assignment were never charged at all.
    """
    from zettel.usage import begin_run, get_tracker, reset

    idx = VectorIndex(tmp_path / "chroma", token_hash_embedding, "m")
    begin_run(1)
    try:
        idx.embed_texts(["grafos de conhecimento", "notas atomicas"], label="teste")
        summary = get_tracker().summary()
    finally:
        reset()
    assert summary.embed_calls == 2
    assert summary.tokens_embedding > 0
