"""Tests for LiteLLM-backed cost estimation."""

from __future__ import annotations

from unittest.mock import patch

import pytest
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
    def fake_cost_per_token(
        *,
        model,
        prompt_tokens,
        completion_tokens,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    ):
        assert model == "gpt-4o-mini"
        assert prompt_tokens == 1000
        assert completion_tokens == 500
        assert (cache_read_input_tokens, cache_creation_input_tokens) == (0, 0)
        return (0.00015, 0.0003)

    with patch("litellm.cost_per_token", fake_cost_per_token):
        cost = estimate_llm_cost("gpt-4o-mini", 1000, 500)
    assert abs(cost - 0.00045) < 1e-9


def test_cache_reads_are_priced_at_the_cache_read_rate():
    """Anthropic: a read costs 10% of input, a write 125% (LiteLLM's bundled map)."""
    full = estimate_llm_cost("claude-haiku-4-5", 1000, 0)
    assert estimate_llm_cost("claude-haiku-4-5", 1000, 0, cache_read_tokens=800) == pytest.approx(
        full * (0.2 + 0.8 * 0.1)
    )
    assert estimate_llm_cost("claude-haiku-4-5", 1000, 0, cache_write_tokens=1000) == pytest.approx(
        full * 1.25
    )
    gemini_full = estimate_llm_cost("gemini/gemini-3.5-flash-lite", 1000, 0)
    gemini_cached = estimate_llm_cost(
        "gemini/gemini-3.5-flash-lite", 1000, 0, cache_read_tokens=800
    )
    assert gemini_cached < gemini_full


def test_cache_tokens_never_exceed_the_prompt():
    over = estimate_llm_cost("claude-haiku-4-5", 100, 0, cache_read_tokens=500)
    assert over == pytest.approx(
        estimate_llm_cost("claude-haiku-4-5", 100, 0, cache_read_tokens=100)
    )


def test_estimate_llm_cost_unknown_model_returns_zero():
    def boom(**_kwargs):
        raise Exception("not found")

    with patch("litellm.cost_per_token", boom):
        assert estimate_llm_cost("totally-unknown-model-xyz", 10, 10) == 0.0


def test_local_provider_is_zero():
    assert estimate_llm_cost("qwen3.5:4b", 100, 50, provider="ollama") == 0.0
    assert estimate_embed_cost("qwen3-embedding", 100, provider="ollama") == 0.0
    assert estimate_embed_cost("x", 100, provider="sentence-transformers") == 0.0


def test_gemini_embedding_is_priced():
    """gemini-embedding-001 resolve no mapa do LiteLLM sem prefixo de provider."""
    assert estimate_embed_cost("gemini-embedding-001", 1_000_000, provider="gemini") > 0.0
