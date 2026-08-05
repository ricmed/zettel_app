"""Tests for CostTracker aggregation."""

from __future__ import annotations

from zettel.usage import (
    begin_run,
    get_tracker,
    record_cache_hit,
    record_embed,
    record_llm,
    reset,
    set_progress,
    set_source,
)


def setup_function() -> None:
    reset()


def teardown_function() -> None:
    reset()


def test_tracker_aggregates_by_source():
    begin_run(1)
    set_source("@A")
    record_llm(model="gpt-4o-mini", tokens_in=100, tokens_out=20, cost_usd=0.01, label="a")
    record_embed(model="text-embedding-3-small", tokens=50, cost_usd=0.002, label="e")
    set_source("@B")
    record_llm(model="gpt-4o-mini", tokens_in=10, tokens_out=5, cost_usd=0.001, label="b")
    record_cache_hit(label="hit")

    tracker = get_tracker()
    assert tracker is not None
    total = tracker.summary()
    assert total.llm_calls == 2
    assert total.cache_hits == 1
    assert total.embed_calls == 1
    assert abs(total.cost_usd_total - 0.013) < 1e-9
    assert abs(total.cost_usd_llm - 0.011) < 1e-9
    assert abs(total.cost_usd_embedding - 0.002) < 1e-9

    a = tracker.summary_for_source("@A")
    assert a.llm_calls == 1
    assert a.tokens_prompt == 100
    assert abs(a.cost_usd_total - 0.012) < 1e-9

    b = tracker.summary_for_source("@B")
    assert b.llm_calls == 1
    assert b.cache_hits == 1
    assert abs(b.cost_usd_llm - 0.001) < 1e-9


def test_cost_log_includes_progress(caplog):
    import logging
    begin_run()
    set_progress(3, 10, "chunk")
    with caplog.at_level(logging.INFO, logger="zettel.usage"):
        record_llm(model="m", tokens_in=1, tokens_out=1, cost_usd=0.0, label="x")
    assert any("COST llm [chunk 3/10]" in r.message for r in caplog.records)


def test_cache_hit_is_zero_cost():
    begin_run()
    record_cache_hit(label="x")
    s = get_tracker().summary()
    assert s.cache_hits == 1
    assert s.cost_usd_total == 0.0
    assert s.llm_calls == 0
