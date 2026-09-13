"""Tests for the pairwise comparison of extractor runs on the gold set (#181)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from compare_gold_runs import (
    compare_pair,
    correctness,
    human_verdicts,
    inclusion_weights,
    paired_exact_p,
    parse_named,
    per_source,
)
from zettel.evals.extraction import KeyItem


def _item(item_id, verdict, source="@S"):
    stratum = "accepted" if verdict == "accepted" else "contested"
    return KeyItem(item_id, f"c-{item_id}", source, verdict, "", stratum)


# -- exact paired test ---------------------------------------------------


def test_paired_exact_matches_the_hand_computed_value():
    """17 items only one model gets right vs 6 the other does -- the #177 book split."""
    assert paired_exact_p(17, 6) == pytest.approx(0.0347, abs=5e-4)


def test_paired_exact_is_symmetric_and_bounded():
    assert paired_exact_p(6, 17) == paired_exact_p(17, 6)
    assert paired_exact_p(5, 5) == 1.0
    assert paired_exact_p(0, 0) == 1.0
    assert 0.0 < paired_exact_p(10, 0) < 0.01


# -- correctness ---------------------------------------------------------


def test_human_verdicts_drop_unjudgeable():
    payload = {
        "labels": [
            {"item_id": "G1", "human_verdict": "keep"},
            {"item_id": "G2", "human_verdict": "unjudgeable"},
            {"item_id": "G3", "human_verdict": "discard"},
        ]
    }
    assert human_verdicts(payload) == {"G1": "keep", "G3": "discard"}


def test_correctness_positive_class_is_keep():
    human = {"G1": "keep", "G2": "discard", "G3": "keep", "G4": "discard"}
    items = [
        _item("G1", "accepted"),
        _item("G2", "accepted"),
        _item("G3", "rejected"),
        _item("G4", "rejected"),
    ]
    assert correctness(items, human) == {"G1": True, "G2": False, "G3": False, "G4": True}


# -- pair ----------------------------------------------------------------


def test_compare_pair_counts_flips_by_direction_and_label():
    human = {"G1": "keep", "G2": "discard", "G3": "discard", "G4": "keep"}
    a = [
        _item("G1", "rejected"),
        _item("G2", "rejected"),
        _item("G3", "accepted"),
        _item("G4", "accepted"),
    ]
    b = [
        _item("G1", "accepted"),
        _item("G2", "accepted"),
        _item("G3", "accepted"),
        _item("G4", "accepted"),
    ]

    pair = compare_pair(a, b, human)

    assert pair["n_common"] == 4
    assert pair["verdict_agreement"] == 0.5
    assert pair["flips"] == {"rejected->accepted (discard)": 1, "rejected->accepted (keep)": 1}
    # G1: only b right; G2: only a right; G3: both wrong; G4: both right
    assert (pair["only_a_right"], pair["only_b_right"]) == (1, 1)
    assert pair["paired_exact_p"] == 1.0


def test_identical_runs_are_a_perfect_noise_floor():
    human = {"G1": "keep", "G2": "discard"}
    run = [_item("G1", "accepted"), _item("G2", "rejected")]
    pair = compare_pair(run, list(run), human)
    assert pair["verdict_agreement"] == 1.0
    assert pair["flips"] == {}
    assert pair["paired_exact_p"] == 1.0


def test_compare_pair_uses_only_items_in_both_runs():
    human = {"G1": "keep", "G2": "keep"}
    pair = compare_pair(
        [_item("G1", "accepted"), _item("G2", "accepted")], [_item("G1", "accepted")], human
    )
    assert pair["n_common"] == 1


# -- per source ----------------------------------------------------------


def test_per_source_keeps_documents_apart():
    human = {"G1": "keep", "G2": "discard", "G3": "keep"}
    items = [
        _item("G1", "accepted", "@Livro"),
        _item("G2", "accepted", "@Livro"),
        _item("G3", "rejected", "@Paper"),
    ]
    result = per_source(items, human)
    assert result["@Livro"] == {"tp": 1, "fp": 1, "fn": 0, "tn": 0, "precision": 0.5, "recall": 1.0}
    assert result["@Paper"]["precision"] is None  # accepted nothing
    assert result["@Paper"]["recall"] == 0.0


def test_parse_named_refuses_malformed_values():
    assert parse_named(["a=b/c.json"], "=") == [("a", "b/c.json")]
    with pytest.raises(ValueError):
        parse_named(["sem-separador"], "=")


# -- corpus weighting ----------------------------------------------------


def test_weighted_net_correct_can_disagree_with_the_raw_count():
    """Raw: B wins 2-1. Weighted: A's one win sits in a stratum 10x the size, so A wins.

    This is the case the #181 pre-registration guards against -- a decision taken on
    sample counts that points the other way for the corpus.
    """
    human = {"C1": "keep", "C2": "keep", "A1": "keep"}
    a = [_item("C1", "rejected"), _item("C2", "rejected"), _item("A1", "accepted")]
    b = [_item("C1", "accepted"), _item("C2", "accepted"), _item("A1", "rejected")]
    population = {"contested": 2, "accepted": 10}

    pair = compare_pair(a, b, human, inclusion_weights(a, population))

    assert (pair["only_a_right"], pair["only_b_right"]) == (1, 2)
    assert pair["weighted_net_correct_b_minus_a"] == -8.0  # +1 +1 (weight 1 each) -10


def test_inclusion_weights_follow_the_stratum_drawn_from():
    items = [_item("C1", "rejected"), _item("C2", "rejected"), _item("A1", "accepted")]
    weights = inclusion_weights(items, {"contested": 50, "accepted": 474})
    assert weights == {"C1": 25.0, "C2": 25.0, "A1": 474.0}
