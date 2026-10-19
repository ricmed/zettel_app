"""The #281 floor rule, decided before the Gemini probe runs."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from probe_relevance_floor import (
    IN_DOMAIN_QUERIES,
    OFF_DOMAIN_QUERIES,
    next_hundredth_above,
    percentile_low,
    preregistered_rule_281,
    preregistered_rule_281_analogy,
)


def _bands(**overrides):
    base = {
        "embedding": {
            "provider": "gemini",
            "model": "gemini-embedding-2",
            "dimensions": 1536,
            "query_side": "embed_query",
        },
        "normalisation_ok": True,
        "distance_space": "cosine",
        "corpus_n": 781,
        "off_n": len(OFF_DOMAIN_QUERIES),
        "in_n": len(IN_DOMAIN_QUERIES),
        "self_n": 20,
        "current": 0.40,
        "off_hi": 0.693,
        "in_lo": 0.80,
        "self_lo": 0.745,
    }
    base.update(overrides)
    return base


def test_p10_is_the_pre_registered_rank():
    assert percentile_low([0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0], 0.10) == 0.1


def test_next_hundredth_matches_the_2026_09_07_placement():
    assert next_hundredth_above(0.693) == 0.70
    assert next_hundredth_above(0.70) == 0.71


def test_positive_margin_sets_the_floor_just_above_off_domain():
    decision = preregistered_rule_281(_bands())
    assert decision["outcome"] == "set_floor"
    assert decision["floor"] == 0.70
    assert decision["margin"] == 0.80 - 0.693


def test_negative_margin_keeps_the_current_floor():
    decision = preregistered_rule_281(_bands(off_hi=0.82, in_lo=0.80, self_lo=0.90))
    assert decision["outcome"] == "keep_floor_negative_margin"
    assert decision["floor"] == 0.40


def test_gap_thinner_than_one_hundredth_does_not_invent_a_floor():
    decision = preregistered_rule_281(_bands(off_hi=0.801, in_lo=0.805, self_lo=0.90))
    assert decision["outcome"] == "keep_floor_gap_below_resolution"
    assert decision["floor"] == 0.40


def test_already_placed_floor_is_kept():
    decision = preregistered_rule_281(_bands(current=0.70))
    assert decision == {"outcome": "keep_floor", "floor": 0.70, "margin": 0.80 - 0.693}


def test_wrong_embedding_or_short_corpus_is_invalid():
    assert preregistered_rule_281(_bands(corpus_n=10))["outcome"] == "invalid_setup"
    wrong = _bands()
    wrong["embedding"] = {**wrong["embedding"], "model": "qwen3-embedding"}
    assert preregistered_rule_281(wrong)["outcome"] == "invalid_setup"
    assert preregistered_rule_281(_bands(normalisation_ok=False))["floor"] == 0.40


def test_analogy_floor_rises_only_when_outside_the_bucket_is_weaker():
    raised = preregistered_rule_281_analogy(
        {"current": -0.20, "notes": 40, "out_p10": 0.659, "out_p90": 0.70, "in_p10": 0.80}
    )
    assert raised == {"outcome": "raise_analogy_floor", "floor": 0.66}
    overlap = preregistered_rule_281_analogy(
        {"current": -0.20, "notes": 40, "out_p10": 0.70, "out_p90": 0.81, "in_p10": 0.75}
    )
    assert overlap == {"outcome": "rank_only", "floor": -0.70}


def test_analogy_floor_stays_when_it_still_cuts_or_the_sample_is_small():
    cutting = preregistered_rule_281_analogy(
        {"current": 0.70, "notes": 40, "out_p10": 0.50, "out_p90": 0.60, "in_p10": 0.80}
    )
    assert cutting["outcome"] == "keep_analogy_floor"
    small = preregistered_rule_281_analogy(
        {"current": -0.20, "notes": 10, "out_p10": 0.9, "out_p90": 0.95, "in_p10": 0.99}
    )
    assert small == {"outcome": "keep_analogy_floor", "floor": -0.20}
