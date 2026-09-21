"""Credential readiness for the configured embedding provider."""

from zettel.config import AppConfig, EmbeddingConfig
from zettel.credentials import embedding_credential_env, embedding_ready

_KEYS = ("GOOGLE_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY", "CHROMA_OPENAI_API_KEY")


def _cfg(provider: str) -> AppConfig:
    return AppConfig(embedding=EmbeddingConfig(provider=provider, model="m"))


def _clear(monkeypatch):
    for k in _KEYS:
        monkeypatch.delenv(k, raising=False)


def test_local_embedding_needs_no_credential(monkeypatch):
    _clear(monkeypatch)
    for provider in ("ollama", "sentence-transformers"):
        assert embedding_credential_env(_cfg(provider)) is None
        assert embedding_ready(_cfg(provider)) is True


def test_gemini_embedding_accepts_either_google_key(monkeypatch):
    _clear(monkeypatch)
    cfg = _cfg("gemini")
    assert embedding_ready(cfg) is False
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    assert embedding_ready(cfg) is True


def test_openai_embedding_requires_openai_key(monkeypatch):
    _clear(monkeypatch)
    cfg = _cfg("openai")
    assert embedding_ready(cfg) is False
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    assert embedding_ready(cfg) is True
