"""Tests for the pre-LLM gate calibration spike (issue #66, scripts/calibrate_pre_llm_gate.py).

Only the pure, DB/Chroma-independent functions are covered here (`heuristic_predict`,
`evaluate_predictions`) -- `load_dataset`/`evaluate_classifier` need a real state.db /
Chroma index and are meant to be run manually against the actual corpus.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from calibrate_pre_llm_gate import evaluate_predictions, heuristic_predict


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


def test_evaluate_predictions_perfect_match():
    labels = [1, 1, 0, 0]
    preds = [1, 1, 0, 0]
    stats = evaluate_predictions(labels, preds)
    assert stats["precision"] == 1.0
    assert stats["recall"] == 1.0
    assert stats["accepted_notes_lost_pct"] == 0.0


def test_evaluate_predictions_false_negative_rate():
    labels = [1, 1, 1, 1, 0]
    preds = [1, 1, 1, 0, 0]  # one accepted chunk missed
    stats = evaluate_predictions(labels, preds)
    assert stats["fn"] == 1
    assert stats["accepted_notes_lost_pct"] == 25.0


def test_evaluate_predictions_empty_is_safe():
    stats = evaluate_predictions([], [])
    assert stats["precision"] == 0.0
    assert stats["recall"] == 0.0
