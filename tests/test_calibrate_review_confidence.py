"""Tests for the review-confidence calibration harness (issue #152).

Covers the pure functions — `reconstruct_output`, `distribution`, `pass_rate`,
`reachability`, `legacy_confidence`. `load_scores` needs a real state.db and is
meant to be run manually against the corpus, mirroring
`tests/test_calibrate_pre_llm_gate.py`.

The guardrail this file exists for is `test_every_relevance_level_is_reachable`:
the pre-#152 bug was structural, not a tuning miss, and only a test that walks
every valid `relevance_score` catches it coming back.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from calibrate_review_confidence import (
    distribution,
    legacy_confidence,
    pass_rate,
    reachability,
    reconstruct_output,
)
from zettel.config import AppConfig

# `verify_anchor_quote` is on by default, so the scorer only keeps a candidate
# whose `anchor_quote` is grounded in this text.
_ANCHOR = "adaptive learning rates converge faster in practice than fixed schedules"
_CHUNK_TEXT = f"Trecho da fonte para ancoragem: {_ANCHOR}, segundo os autores."


def _raw_output(candidates: list[dict], rejected: list[dict] | None = None) -> dict:
    return {
        "chunk_status": "accepted",
        "rejection_reason": "",
        "rejection_category": "",
        "summary": "Resumo do chunk para fins de teste.",
        "key_concepts": ["conceito_a"],
        "candidates": candidates,
        "rejected_candidates": rejected or [],
    }


def _candidate(**overrides) -> dict:
    base = {
        "thesis": "Gradient descent converge mais rapido com learning rate adaptativo",
        "definition": (
            "O algoritmo ajusta os pesos iterativamente na direcao oposta ao gradiente "
            "da funcao de perda, e taxas adaptativas aceleram a convergencia"
        ),
        "intuition": "Como descer uma montanha ajustando o passo",
        "limits": "Pode divergir com learning rates altos",
        "anchor_quote": _ANCHOR,
        "source_locator": "p.42",
        "tags": ["otimizacao"],
        "relevance_score": 4,
    }
    base.update(overrides)
    return base


# ── reconstruct_output ────────────────────────────────────────────────


def test_reconstruct_output_restores_dropped_candidates_for_the_ratio():
    """`summary_json` is post-filter; without this the integrity term reads 1.0."""
    raw = _raw_output(
        [_candidate()],
        rejected=[{"thesis": "tese descartada", "reason": "anchor_quote_words=27 fora de [10,25]"}],
    )
    output = reconstruct_output(raw)
    assert len(output.candidates) == 2


def test_reconstruct_output_stand_ins_are_dropped_again_by_the_filter():
    from zettel.extractor import _filter_candidates

    raw = _raw_output([_candidate()], rejected=[{"thesis": "tese descartada", "reason": "x"}])
    approved, rejected = _filter_candidates(
        reconstruct_output(raw).candidates, AppConfig(), _CHUNK_TEXT
    )
    assert len(approved) == 1
    assert len(rejected) == 1


def test_reconstruct_output_without_rejected_key_is_unchanged():
    raw = _raw_output([_candidate()])
    del raw["rejected_candidates"]
    assert len(reconstruct_output(raw).candidates) == 1


def test_reconstruct_output_does_not_mutate_the_input():
    raw = _raw_output([_candidate()], rejected=[{"thesis": "t", "reason": "r"}])
    reconstruct_output(raw)
    assert "rejected_candidates" in raw
    assert len(raw["candidates"]) == 1


# ── reachability (the #152 guardrail) ─────────────────────────────────


def test_every_relevance_level_is_reachable_at_the_configured_threshold():
    cfg = AppConfig()
    rows = reachability(cfg, cfg.literature_review.auto_approve_min_confidence)
    assert rows, "no relevance levels evaluated"
    unreachable = [r["relevance_score"] for r in rows if not r["reachable"]]
    assert not unreachable, f"relevance_score {unreachable} cannot ever auto-approve"


def test_reachability_reports_the_floor_level_first():
    cfg = AppConfig()
    rows = reachability(cfg, 0.75)
    assert rows[0]["relevance_score"] == cfg.extraction.min_relevance_score
    assert rows[-1]["relevance_score"] == 5
    assert rows[0]["ceiling"] < rows[-1]["ceiling"]  # ceiling rises with relevance


def test_reachability_flags_an_impossible_threshold():
    rows = reachability(AppConfig(), threshold=0.99)
    assert any(not r["reachable"] for r in rows)


# ── legacy_confidence (before/after comparison) ───────────────────────


def test_legacy_confidence_reproduces_the_word_count_gate():
    """The old formula's defect: two identical candidates, different verbosity."""
    cfg = AppConfig()
    short = reconstruct_output(_raw_output([_candidate(definition="palavra " * 30)]))
    long = reconstruct_output(_raw_output([_candidate(definition="palavra " * 60)]))
    assert legacy_confidence(long, cfg, _CHUNK_TEXT) > legacy_confidence(short, cfg, _CHUNK_TEXT)


def test_legacy_confidence_floor_relevance_never_reaches_075():
    """Documents the structural ceiling that motivated the issue."""
    cfg = AppConfig()
    best_possible = reconstruct_output(
        _raw_output([_candidate(relevance_score=3, definition="palavra " * 400)])
    )
    assert legacy_confidence(best_possible, cfg, _CHUNK_TEXT) <= 0.70


def test_legacy_confidence_short_circuits_match_the_current_scorer():
    cfg = AppConfig()
    rejected = reconstruct_output({**_raw_output([]), "chunk_status": "rejected"})
    assert legacy_confidence(rejected, cfg, _CHUNK_TEXT) == 0.1
    assert legacy_confidence(reconstruct_output(_raw_output([])), cfg, _CHUNK_TEXT) == 0.2


# ── distribution / pass_rate ──────────────────────────────────────────


def test_distribution_reports_the_five_number_summary():
    d = distribution([0.5, 0.7, 0.9])
    assert d == {"n": 3, "min": 0.5, "median": 0.7, "max": 0.9, "stdev": round(d["stdev"], 3)}


def test_distribution_of_empty_input_is_not_an_error():
    assert distribution([]) == {"n": 0}


def test_pass_rate_counts_values_at_the_threshold():
    assert pass_rate([0.74, 0.75, 0.76], 0.75) == round(2 / 3, 3)


def test_pass_rate_of_empty_input_is_zero():
    assert pass_rate([], 0.75) == 0.0
