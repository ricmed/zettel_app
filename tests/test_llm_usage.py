"""Tests for call_llm usage recording."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from zettel.llm import call_llm
from zettel.usage import begin_run, get_tracker, reset


def setup_function() -> None:
    reset()


def teardown_function() -> None:
    reset()


class _FakeLLM:
    model = "gpt-4o-mini"

    def invoke(self, _messages):
        return SimpleNamespace(
            content="ok",
            usage_metadata={"input_tokens": 12, "output_tokens": 3},
            response_metadata={},
        )


def test_call_llm_records_usage_and_cost():
    begin_run(99)

    def fake_cost(model, prompt_tokens, completion_tokens, provider=None):
        assert model == "gpt-4o-mini"
        assert prompt_tokens == 12
        assert completion_tokens == 3
        return 0.42

    with patch("zettel.pricing.estimate_llm_cost", fake_cost):
        text = call_llm(_FakeLLM(), "hello", label="unit")
    assert text == "ok"
    s = get_tracker().summary()
    assert s.llm_calls == 1
    assert s.tokens_prompt == 12
    assert s.tokens_completion == 3
    assert abs(s.cost_usd_llm - 0.42) < 1e-9
