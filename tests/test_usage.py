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


def test_pipeline_phase_name_maps_harvest_json():
    from zettel.usage import pipeline_phase_name

    assert pipeline_phase_name('{"chunking": {}}') == "harvest"
    assert pipeline_phase_name("extract") == "extract"
    assert pipeline_phase_name("garden_hubs") == "garden_hubs"


def test_latest_pipeline_session_groups_run_all():
    from zettel.usage import latest_pipeline_session, pipeline_phase_name, sum_run_usage

    previous_garden = {
        "pipeline_signature": "garden",
        "started_at": "2026-08-01T10:00:00",
        "cost_usd_total": 9.0,
        "tokens_prompt": 9000,
        "llm_calls": 90,
    }
    harvest = {
        "pipeline_signature": '{"harvest": true}',
        "started_at": "2026-08-31T12:00:00",
        "cost_usd_total": 0.01,
        "cost_usd_llm": 0.01,
        "tokens_prompt": 100,
        "llm_calls": 2,
    }
    extract = {
        "pipeline_signature": "extract",
        "started_at": "2026-08-31T12:10:00",
        "cost_usd_total": 0.20,
        "cost_usd_llm": 0.20,
        "tokens_prompt": 2000,
        "llm_calls": 20,
    }
    garden = {
        "pipeline_signature": "garden",
        "started_at": "2026-08-31T12:40:00",
        "cost_usd_total": 0.000646,
        "cost_usd_llm": 0.000646,
        "tokens_prompt": 2146,
        "llm_calls": 1,
    }
    newest_first = [garden, extract, harvest, previous_garden]
    session = latest_pipeline_session(newest_first)
    assert [pipeline_phase_name(r["pipeline_signature"]) for r in session] == [
        "harvest",
        "extract",
        "garden",
    ]
    total = sum_run_usage(session)
    assert abs(total.cost_usd_total - 0.210646) < 1e-9
    assert total.llm_calls == 23


def test_latest_pipeline_session_isolated_extract_is_not_merged():
    from zettel.usage import latest_pipeline_session, pipeline_phase_name

    extract = {
        "pipeline_signature": "extract",
        "started_at": "2026-08-31T15:00:00",
        "cost_usd_total": 0.05,
    }
    garden = {
        "pipeline_signature": "garden",
        "started_at": "2026-08-31T12:40:00",
        "cost_usd_total": 0.001,
    }
    session = latest_pipeline_session([extract, garden])
    assert [pipeline_phase_name(r["pipeline_signature"]) for r in session] == ["extract"]


def test_latest_pipeline_session_ask_is_standalone():
    from zettel.usage import latest_pipeline_session

    ask = {"pipeline_signature": "ask", "started_at": "2026-08-31T16:00:00"}
    garden = {"pipeline_signature": "garden", "started_at": "2026-08-31T12:40:00"}
    session = latest_pipeline_session([ask, garden])
    assert session == [ask]


# ── #64: provider-side prompt cache tokens ────────────────────────────


def test_record_llm_aggregates_prompt_cache_tokens():
    begin_run(1)
    record_llm(
        model="gemini-3.5-flash-lite",
        tokens_in=5000,
        tokens_out=200,
        cost_usd=0.001,
        label="extract",
        cache_read_tokens=4800,
        cache_write_tokens=0,
    )
    record_llm(
        model="gemini-3.5-flash-lite",
        tokens_in=5100,
        tokens_out=210,
        cost_usd=0.001,
        label="extract",
        cache_read_tokens=4900,
        cache_write_tokens=100,
    )
    summary = get_tracker().summary().as_dict()
    assert summary["prompt_cache_read_tokens"] == 4800 + 4900
    assert summary["prompt_cache_write_tokens"] == 100


def test_usage_from_run_reads_prompt_cache_columns():
    from zettel.usage import usage_from_run

    row = {
        "tokens_prompt": 5000,
        "prompt_cache_read_tokens": 4800,
        "prompt_cache_write_tokens": 0,
    }
    u = usage_from_run(row)
    assert u.prompt_cache_read_tokens == 4800
    assert u.prompt_cache_write_tokens == 0


def test_usage_from_run_defaults_prompt_cache_to_zero_for_legacy_rows():
    """A run persisted before #64 has no prompt_cache_* columns at all."""
    from zettel.usage import usage_from_run

    row = {"tokens_prompt": 5000}
    u = usage_from_run(row)
    assert u.prompt_cache_read_tokens == 0
    assert u.prompt_cache_write_tokens == 0


def testfmt_prompt_cache_ratio():
    from zettel.cli.formatting import fmt_prompt_cache_ratio
    from zettel.usage import UsageSummary

    no_cache = UsageSummary(tokens_prompt=5000)
    assert fmt_prompt_cache_ratio(no_cache) == "-"

    with_cache = UsageSummary(
        tokens_prompt=5000, prompt_cache_read_tokens=4800, prompt_cache_write_tokens=0
    )
    assert fmt_prompt_cache_ratio(with_cache) == "4800r/0w (96%)"
