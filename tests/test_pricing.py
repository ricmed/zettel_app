"""Tests for LiteLLM-backed cost estimation."""

from __future__ import annotations

from unittest.mock import patch

from zettel.pricing import (
    estimate_embed_cost,
    estimate_embed_tokens,
    estimate_llm_cost,
    reset_price_warnings,
)


def setup_function() -> None:
    reset_price_warnings()


def test_estimate_embed_tokens_min_and_ratio():
    assert estimate_embed_tokens("") == 0
    assert estimate_embed_tokens("abcd") == 1
    assert estimate_embed_tokens("a" * 40) == 10


def test_estimate_llm_cost_uses_litellm(monkeypatch):
    def fake_cost_per_token(*, model, prompt_tokens, completion_tokens):
        assert model == "gpt-4o-mini"
        assert prompt_tokens == 1000
        assert completion_tokens == 500
        return (0.00015, 0.0003)

    with patch("litellm.cost_per_token", fake_cost_per_token):
        cost = estimate_llm_cost("gpt-4o-mini", 1000, 500)
    assert abs(cost - 0.00045) < 1e-9


def test_estimate_llm_cost_unknown_model_returns_zero():
    def boom(**_kwargs):
        raise Exception("not found")

    with patch("litellm.cost_per_token", boom):
        assert estimate_llm_cost("totally-unknown-model-xyz", 10, 10) == 0.0


def test_local_provider_is_zero():
    assert estimate_llm_cost("qwen3.5:4b", 100, 50, provider="ollama") == 0.0
    assert estimate_embed_cost("qwen3-embedding", 100, provider="ollama") == 0.0
    assert estimate_embed_cost("x", 100, provider="sentence-transformers") == 0.0
