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

    def fake_cost(
        model,
        prompt_tokens,
        completion_tokens,
        cache_read_tokens=0,
        cache_write_tokens=0,
        provider=None,
    ):
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


def test_cached_call_llm_stores_on_miss_and_skips_client_on_hit(tmp_path):
    from zettel.config import AppConfig
    from zettel.llm import cached_call_llm
    from zettel.state import StateDB

    begin_run(100)
    cfg = AppConfig(vault_path=tmp_path / "vault", state_db_path=tmp_path / "state.db")
    db = StateDB(tmp_path / "state.db")
    clients: list[str] = []
    calls: list[tuple[str, str | None]] = []

    def client():
        clients.append("built")
        return object()

    def fake_call(_llm, user, *, system=None, **_kwargs):
        calls.append((user, system))
        return f"resp:{user}"

    def run(user: str, temperature: float | None = None):
        return cached_call_llm(
            cfg,
            db,
            "ask",
            "tpl",
            "sys",
            user,
            get_client=client,
            call=fake_call,
            temperature=temperature,
        )

    try:
        assert run("q") == ("resp:q", False)
        assert run("q") == ("resp:q", True)
        assert clients == ["built"]
        assert calls == [("q", "sys")]
        assert get_tracker().summary().cache_hits == 1
        # Filled prompt and temperature are both part of the key.
        assert run("other") == ("resp:other", False)
        assert run("q", temperature=0.9) == ("resp:q", False)
        assert len(calls) == 3
    finally:
        db.close()


def test_call_llm_records_the_call_latency(monkeypatch):
    """The model call's wall time reaches the event, the tracker and the log line."""
    import itertools

    from zettel import llm as llm_module
    from zettel.logfmt import llm_extra

    clock = itertools.chain([10.0, 12.5], itertools.repeat(12.5))
    monkeypatch.setattr(llm_module.time, "perf_counter", lambda: next(clock))
    begin_run(99)
    call_llm(_FakeLLM(), "hello", label="unit")
    tracker = get_tracker()
    assert tracker.events[-1].latency_ms == 2500
    assert tracker.snapshot()["llm_latency_ms"] == 2500
    assert llm_extra(cache_local_hit=False, latency_ms=2500) == "cache local miss  2.5s"
    assert llm_extra(cache_local_hit=True) == "cache local hit"
