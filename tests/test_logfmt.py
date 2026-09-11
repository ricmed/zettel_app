"""One-line pipeline log format."""

from __future__ import annotations

import logging

from zettel.logfmt import (
    clip_one_line,
    embed_step,
    fmt_money,
    fmt_tokens,
    format_step,
    llm_extra,
    log_step,
)


def test_fmt_money_local_is_short():
    assert fmt_money(0.0) == "$0"
    assert fmt_money(0.0000001) == "$0"
    assert fmt_money(0.003049) == "$0.003"
    assert fmt_money(0.01) == "$0.010"


def test_fmt_tokens_compact_prefix():
    assert fmt_tokens(126) == "126 tok"
    assert fmt_tokens(4800, compact=True) == "4.8k tok"
    assert fmt_tokens(12000, compact=True) == "12k tok"


def test_clip_one_line_collapses_whitespace():
    assert clip_one_line("foo\n  bar", 20) == "foo bar"
    clipped = clip_one_line("a" * 80, 20)
    assert clipped.endswith("...")
    assert len(clipped) == 20


def test_format_step_progress_at_end():
    line = format_step(
        "llm",
        "gemini-3.1-flash-lite",
        tokens="7671 -> 754",
        cost="$0.003",
        extra=llm_extra(cache_local_hit=False),
        progress="nota 36/36",
    )
    assert line.startswith("llm    gemini-3.1-flash-lite")
    assert line.endswith("[nota 36/36]")
    assert "cache local miss" in line
    assert "cache_hit" not in line


def test_llm_extra_keeps_caches_distinct():
    extra = llm_extra(
        cache_local_hit=False,
        cache_read_tokens=4800,
        cache_write_tokens=100,
    )
    assert extra == "cache local miss  prefixo 4.8k tok  grava 100 tok"
    assert llm_extra(cache_local_hit=True) == "cache local hit"


def test_embed_step_purpose_and_label():
    assert embed_step("densa", "query_notes", 20) == ("busca", "densa (20 notas)")
    assert embed_step("topic index", "query_notes_by_ids", 5) == (
        "busca",
        "topic index (5 notas)",
    )
    assert embed_step("analogias", "query_notes", 60) == ("busca", "analogias (60 notas)")
    assert embed_step("", "note:01M297985YAY09YW0GM30R9B6P") == (
        "index",
        "ZTL 01M297985YAY09YW0GM30R9B6P",
    )
    assert embed_step("", "source:@Citekey") == ("index", "fonte @Citekey")


def test_log_step_emits_stable_info(caplog):
    log = logging.getLogger("zettel.test_logfmt")
    with caplog.at_level(logging.INFO, logger="zettel.test_logfmt"):
        message = log_step(
            log,
            "busca",
            "densa (20 notas)",
            model="qwen3-embedding",
            tokens="126 tok",
            cost="$0",
        )
    assert message == "busca  densa (20 notas)  qwen3-embedding  126 tok  $0"
    assert any(r.message == message for r in caplog.records)


def test_setup_logging_quiets_gemini_vendor():
    from zettel.config import setup_logging

    setup_logging("INFO")
    assert logging.getLogger("google_genai").level == logging.WARNING
    assert logging.getLogger("langchain_google_genai").level == logging.WARNING
    assert logging.getLogger("grpc").level == logging.WARNING
