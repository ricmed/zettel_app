"""Tests for the pre-LLM gate calibration script (issues #66/#173).

Covers the pure functions -- `heuristic_predict`, `evaluate_predictions`,
`attach_embeddings`, `category_breakdown` and the source guard in
`cross_val_proba`. `load_dataset` needs a real state.db / Chroma and is meant to
be run manually against the actual corpus.

Nothing here touches the network: the embedder is injected as a fake, the same
discipline the ask-eval harness enforces (ADR-038).
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from calibrate_pre_llm_gate import (
    InsufficientSourcesError,
    Record,
    attach_embeddings,
    category_breakdown,
    cross_val_proba,
    evaluate_predictions,
    heuristic_predict,
    operating_points,
)


def _rec(chunk_id, label, *, source="@Src", text="texto", category="", embedding=None):
    return Record(
        chunk_id=chunk_id,
        source_id=source,
        text=text,
        section_path="",
        label=label,
        rejection_category=category,
        embedding=embedding,
    )


# -- heuristic -----------------------------------------------------------


def test_heuristic_predict_rejects_short_chunk():
    assert heuristic_predict("muito curto") == 0


def test_heuristic_predict_rejects_horizontal_rule():
    assert heuristic_predict("---") == 0
    assert heuristic_predict("===================") == 0


def test_heuristic_predict_rejects_low_alnum_ratio():
    noisy = ".-.-.-.-.-.-.-.-.-.-.-.-.-.-.-.-.-.-.-.-.-.-.-.-.-.-.-.-.-.-.-.-.-.-.-.-.-.-.-.-" * 5
    assert len(noisy) >= 200
    assert heuristic_predict(noisy) == 0


def test_heuristic_predict_rejects_table_heavy_chunk():
    table = "\n".join(["| a | b | c |" for _ in range(20)])
    assert heuristic_predict(table) == 0


def test_heuristic_predict_accepts_normal_prose():
    prose = (
        "Este e um paragrafo de prosa normal, com bastante conteudo substantivo "
        "explicando um conceito tecnico em profundidade, com frases completas "
        "e vocabulario tecnico relevante para o dominio do documento inteiro."
    )
    assert heuristic_predict(prose) == 1


# -- metrics -------------------------------------------------------------


def test_evaluate_predictions_perfect_match():
    stats = evaluate_predictions([1, 1, 0, 0], [1, 1, 0, 0])
    assert stats["precision"] == 1.0
    assert stats["recall"] == 1.0
    assert stats["accepted_notes_lost_pct"] == 0.0
    assert stats["calls_avoided_pct"] == 50.0  # the two rejected chunks


def test_evaluate_predictions_false_negative_rate():
    labels = [1, 1, 1, 1, 0]
    preds = [1, 1, 1, 0, 0]  # one accepted chunk missed
    stats = evaluate_predictions(labels, preds)
    assert stats["fn"] == 1
    assert stats["accepted_notes_lost_pct"] == 25.0


def test_calls_avoided_excludes_false_positives():
    """A chunk the gate sent to the LLM is not a saving, even if the LLM rejects it.

    The original formula was `(fp + tn) / n`, which counted every rejected chunk in
    the corpus -- turning the metric into the base rate of rejection: constant
    across thresholds and blind to the predictions it claimed to measure.
    """
    labels = [0, 0, 0, 0, 1]
    preds = [1, 1, 1, 0, 1]  # three fp (calls made), one tn (call avoided)
    stats = evaluate_predictions(labels, preds)
    assert (stats["fp"], stats["tn"], stats["fn"]) == (3, 1, 0)
    assert stats["calls_avoided_pct"] == 20.0  # 1 of 5, not the 80% of (fp+tn)


def test_calls_avoided_counts_false_negatives():
    """A wrongly skipped call is still a call not paid for."""
    labels = [1, 1, 0, 0]
    preds = [0, 1, 0, 1]  # one fn, one tn -> two calls not made
    stats = evaluate_predictions(labels, preds)
    assert stats["calls_avoided_pct"] == 50.0
    assert stats["accepted_notes_lost_pct"] == 50.0


def test_evaluate_predictions_empty_is_safe():
    stats = evaluate_predictions([], [])
    assert stats["precision"] == 0.0
    assert stats["recall"] == 0.0
    assert stats["calls_avoided_pct"] == 0.0


def test_category_breakdown_counts_only_rejections():
    records = [
        _rec("a", 0, category="structural"),
        _rec("b", 0, category="structural"),
        _rec("c", 0, category="narrative"),
        _rec("d", 1),  # accepted -- must not appear
    ]
    breakdown = category_breakdown(records, [0, 1, 1, 0])
    assert breakdown == {"structural": "1/2", "narrative": "0/1"}


def test_operating_points_picks_strictest_threshold_within_budget():
    labels = [1, 1, 0, 0]
    proba = [0.9, 0.8, 0.2, 0.1]
    (point,) = operating_points(labels, proba, max_loss_pct=(0.0,))
    assert point["accepted_notes_lost_pct"] == 0.0
    assert point["calls_avoided_pct"] == 50.0


# -- embeddings ----------------------------------------------------------


def test_attach_embeddings_computes_missing_vectors():
    """A labeled chunk with no stored vector is embedded, not dropped.

    Dropping it was silent, and is how an entire source disappeared from the
    dataset without appearing in any count.
    """
    calls: list[list[str]] = []

    def fake_embedder(texts):
        calls.append(list(texts))
        return [[float(len(t)), 1.0] for t in texts]

    records = [
        _rec("stored", 1, text="ja tem vetor"),
        _rec("missing", 0, text="precisa embedar"),
    ]
    usable, report = attach_embeddings(records, {"stored": [0.5, 0.5]}, fake_embedder)

    assert [r.chunk_id for r in usable] == ["stored", "missing"]
    assert calls == [["precisa embedar"]]
    assert (report.reused, report.computed, report.unusable) == (1, 1, 0)
    assert report.dimensions == 2


def test_attach_embeddings_reports_records_with_no_text():
    def fake_embedder(texts):
        return [[1.0] for _ in texts]

    records = [_rec("empty", 0, text="   ")]
    usable, report = attach_embeddings(records, {}, fake_embedder)

    assert usable == []
    assert report.unusable == 1


def test_attach_embeddings_batches_requests():
    batches: list[int] = []

    def fake_embedder(texts):
        batches.append(len(texts))
        return [[1.0] for _ in texts]

    records = [_rec(f"c{i}", 1, text=f"texto {i}") for i in range(5)]
    attach_embeddings(records, {}, fake_embedder, batch_size=2)

    assert batches == [2, 2, 1]


# -- source guard --------------------------------------------------------


def test_cross_val_proba_requires_three_sources():
    records = [
        _rec("a", 1, source="@Um", embedding=[1.0, 0.0]),
        _rec("b", 0, source="@Um", embedding=[0.0, 1.0]),
        _rec("c", 1, source="@Dois", embedding=[1.0, 0.1]),
        _rec("d", 0, source="@Dois", embedding=[0.1, 1.0]),
    ]
    with pytest.raises(InsufficientSourcesError) as exc:
        cross_val_proba(records)
    assert "2 fonte" in str(exc.value)


def test_cross_val_proba_requires_both_classes():
    records = [_rec(f"c{i}", 1, source=f"@Fonte{i}", embedding=[float(i), 1.0]) for i in range(3)]
    with pytest.raises(InsufficientSourcesError):
        cross_val_proba(records)


def test_cross_val_proba_runs_with_three_sources():
    records = []
    for i in range(3):
        records.append(_rec(f"a{i}", 1, source=f"@Fonte{i}", embedding=[1.0, 0.0, float(i)]))
        records.append(_rec(f"b{i}", 0, source=f"@Fonte{i}", embedding=[0.0, 1.0, float(i)]))
    proba = cross_val_proba(records)
    assert len(proba) == len(records)
    assert all(0.0 <= p <= 1.0 for p in proba)
