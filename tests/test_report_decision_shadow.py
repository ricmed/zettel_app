"""Tests for the shadow report (#206). Offline: rows in, numbers out."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from report_decision_shadow import agreement_by_band, band_of, build_report, load_rows
from zettel.state import StateDB


def _row(site, subject, baseline, jev, human=None, error=None, latency=10):
    return {
        "site": site,
        "subject_id": subject,
        "model": "typesafe/jev-test",
        "baseline": baseline,
        "jev": jev,
        "human": human,
        "error": error,
        "latency_ms": latency,
        "input_tokens": 50,
    }


def _choice(choice, confidence, spread=0.0):
    return {"choice": choice, "confidence": confidence, "spread": spread, "probabilities": {}}


def test_bands_cover_the_unit_interval():
    assert band_of(0.95) == "high"
    assert band_of(1.0) == "high"
    assert band_of(0.75) == "mid"
    assert band_of(0.2) == "low"


def test_agreement_by_band():
    out = agreement_by_band([(True, 0.95), (False, 0.92), (True, 0.3)])
    assert out["all"] == {"n": 3, "agreement": 0.6667}
    assert out["high"] == {"n": 2, "agreement": 0.5}
    assert out["mid"] == {"n": 0, "agreement": None}


def test_dedupe_report_compares_llm_and_jev_with_the_reviewer():
    rows = [
        _row(
            "dedupe",
            "a",
            {"decision": "ignore", "target": "01A"},
            {"decision": _choice("ignore", 0.95, 0.02), "target": _choice("01A", 0.9)},
            human={"verdict": "ignore"},
        ),
        _row(
            "dedupe",
            "b",
            {"decision": "ignore", "target": "01B"},
            {"decision": _choice("link", 0.7), "target": _choice("01B", 0.8)},
            human={"verdict": "not_ignore"},
        ),
        _row("dedupe", "c", {"decision": "create_new", "target": "none"}, None, error="429"),
    ]
    out = build_report(rows)["sites"]["dedupe"]
    assert out["n"] == 3 and out["answered"] == 2
    assert out["errors"] == {"api": 1}
    assert out["decision"]["all"]["agreement"] == 0.5
    assert out["target_when_llm_flags"]["all"]["agreement"] == 1.0
    assert out["human"] == {"n": 2, "llm_agreement": 0.5, "jev_agreement": 1.0}
    assert out["decision_confusion"] == {"ignore->ignore": 1, "ignore->link": 1}


def test_moc_report_separates_unassigned_baselines():
    rows = [
        _row("moc_category", "x", {"category": "Cat A"}, {"category": _choice("Cat A", 0.95)}),
        _row("moc_category", "y", {"category": "_unassigned"}, {"category": _choice("none", 0.5)}),
    ]
    out = build_report(rows)["sites"]["moc_category"]
    assert out["category"]["all"] == {"n": 1, "agreement": 1.0}
    assert out["unassigned_baseline"] == 1
    assert out["jev_none_rate"] == 0.5


def test_judge_report_puts_both_scores_on_the_0_10_scale():
    rows = [
        _row(
            "article_judge",
            "k:naturalness",
            {"score_0_10": 8.0},
            {"naturalness": {"score": 2.0, "confidence": 0.7}},
        ),
    ]
    dims = build_report(rows)["sites"]["article_judge"]["dimensions"]
    assert dims["naturalness"]["jev_mean_0_10"] == 5.0
    assert dims["naturalness"]["mean_abs_diff_0_10"] == 3.0
    assert dims["naturalness"]["pass_agreement"] == 0.0


def test_load_rows_reads_what_the_state_wrote(tmp_path):
    db_path = tmp_path / "state.db"
    db = StateDB(db_path)
    db.record_decision_shadow(
        site="dedupe",
        subject_id="a",
        state_checksum="s",
        model="m",
        state={"candidate": {}},
        baseline={"decision": "ignore"},
        jev={"decision": _choice("ignore", 0.9)},
        latency_ms=5,
        input_tokens=7,
    )
    db.close()
    (row,) = load_rows(db_path)
    assert row["baseline"] == {"decision": "ignore"}
    assert row["human"] is None


def test_load_rows_on_a_state_without_the_table(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    sqlite3.connect(path).close()
    assert load_rows(path) == []


def _corr_row(pair, order, similarity, edge, score, conf=0.8, converge=0.7):
    return _row(
        "corroborates",
        f"{pair}:{order}",
        {"similarity": similarity, "threshold": 0.85, "edge": edge, "new_note": "n"},
        {
            "same_idea": {"score": score, "confidence": conf},
            "converge": {"noul": converge},
        },
    )


def test_similarity_bands():
    from report_decision_shadow import similarity_band

    assert similarity_band(0.9, 0.85) == "above_threshold"
    assert similarity_band(0.82, 0.85) == "near_threshold"
    assert similarity_band(0.76, 0.85) == "low_band"


def test_corroborates_report_crosses_edge_with_level_and_measures_order_bias():
    rows = [
        _corr_row("a|b", "ab", 0.9, True, 2.0, conf=0.95),
        _corr_row("a|b", "ba", 0.9, True, 1.8, conf=0.95),
        _corr_row("a|c", "ab", 0.78, False, 1.9),
        _corr_row("a|c", "ba", 0.78, False, 1.3),
    ]
    out = build_report(rows)["sites"]["corroborates"]
    assert out["pairs"] == 2
    # a|c: mean 1.6 -> "same idea" with no edge, a possible missing link.
    assert out["edge_x_level"] == {"edge=no->level=2": 1, "edge=yes->level=2": 1}
    assert out["order_divergence_mean"] == pytest.approx(0.4)
    assert out["by_similarity_band"]["low_band"]["levels"] == {"2": 1}
    assert out["edge_vs_same_idea"]["all"] == {"n": 2, "agreement": 0.5}
