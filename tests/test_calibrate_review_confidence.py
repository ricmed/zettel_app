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

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from calibrate_review_confidence import (
    ChunkScore,
    auc,
    auc_ci,
    build_gold_report,
    distribution,
    hanley_mcneil_se,
    items_needed,
    legacy_confidence,
    load_gold,
    min_detectable_auc,
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


# ── gold set (#176) ───────────────────────────────────────────────────


def _score(chunk_id, *, current, relevance=4.0, n_approved=1, n_rejected=0, completeness=1.0):
    return ChunkScore(
        chunk_id=chunk_id,
        chunk_index=0,
        source_id="@S",
        section_path="",
        legacy=current,
        current=current,
        n_approved=n_approved,
        n_rejected=n_rejected,
        avg_relevance=relevance,
        avg_definition_words=20.0,
        completeness=completeness,
    )


def test_auc_perfect_inverted_and_constant():
    assert auc([0.9, 0.8], [0.2, 0.1]) == 1.0
    assert auc([0.1, 0.2], [0.8, 0.9]) == 0.0
    assert auc([0.9, 0.9], [0.9, 0.9]) == 0.5  # a constant signal is a coin by construction


def test_auc_counts_ties_as_half():
    assert auc([0.9], [0.9, 0.1]) == 0.75


def test_hanley_mcneil_matches_the_hand_computed_value():
    """At AUC 0.5 with 24 keep / 14 discard -- the #176 sample -- SE is ~0.098."""
    assert hanley_mcneil_se(0.5, 24, 14) == pytest.approx(0.0983, abs=5e-4)


def test_auc_ci_verdicts():
    assert auc_ci([0.9] * 30, [0.1] * 30)["verdict"] == "separa"
    assert auc_ci([0.1] * 30, [0.9] * 30)["verdict"] == "invertido"
    assert auc_ci([0.5, 0.6], [0.55, 0.5])["verdict"] == "indistinguivel de moeda"


def test_min_detectable_auc_shrinks_as_the_sample_grows():
    small = min_detectable_auc(24, 14)
    large = min_detectable_auc(240, 140)
    assert 0.5 < large < small < 1.0


def test_min_detectable_auc_uses_80_percent_power_not_50():
    """At the #176 sample, 50% power would claim ~0.675 is detectable; 80% needs ~0.73.

    The 50% criterion (`a - 1.96*SE > 0.5`) is the one this function started with.
    It overstates what the sample can see: a signal at exactly that AUC would be
    missed half the time.
    """
    assert 0.72 <= min_detectable_auc(24, 14) <= 0.75


def test_items_needed_is_lower_for_a_stronger_signal():
    weak = items_needed(0.70, keep_share=24 / 38)
    strong = items_needed(0.80, keep_share=24 / 38)
    assert strong is not None and weak is not None
    assert strong < weak


def test_load_gold_keeps_only_judged_accepted_chunks(tmp_path):
    import json

    key = tmp_path / "gabarito.json"
    key.write_text(
        json.dumps(
            {
                "items": [
                    {"item_id": "G1", "llm_verdict": "accepted", "llm_category": ""},
                    {"item_id": "G2", "llm_verdict": "accepted", "llm_category": ""},
                    {"item_id": "G3", "llm_verdict": "rejected", "llm_category": "narrative"},
                ]
            }
        ),
        encoding="utf-8",
    )
    labels = tmp_path / "rotulos.json"
    labels.write_text(
        json.dumps(
            {
                "labels": [
                    {"item_id": "G1", "chunk_id": "c1", "human_verdict": "keep"},
                    {"item_id": "G2", "chunk_id": "c2", "human_verdict": "unjudgeable"},
                    {"item_id": "G3", "chunk_id": "c3", "human_verdict": "keep"},
                ]
            }
        ),
        encoding="utf-8",
    )
    # G2 is `?`; G3 is a rejection the gate never sees.
    assert load_gold(labels, key) == {"c1": "keep"}


def test_gold_report_detects_a_separating_signal_and_saturation():
    keep = [_score(f"k{i}", current=0.9, n_rejected=0) for i in range(30)]
    discard = [_score(f"d{i}", current=0.3, n_rejected=1) for i in range(30)]
    gold = {s.chunk_id: "keep" for s in keep} | {s.chunk_id: "discard" for s in discard}

    report = build_gold_report(keep + discard, gold)

    assert report["signals"]["review_confidence"]["verdict"] == "separa"
    assert report["signals"]["relevance"]["verdict"] == "indistinguivel de moeda"  # constant
    assert report["saturation"]["completeness"] == 1.0  # every item at the maximum
    assert report["saturation"]["integrity"] == 0.5  # only the keep half is at 1.0


def test_gold_report_flags_labels_without_a_chunk():
    report = build_gold_report([_score("c1", current=0.9)], {"c1": "keep", "gone": "discard"})
    assert report["labels_matched"] == 1
    assert report["labels_unmatched"] == 1
