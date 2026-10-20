"""Tests for scripts/export_category_gold.py and scripts/score_category_gold.py (#295)."""

import json
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import export_category_gold as export_mod
import score_category_gold as score_mod
from score_decision_gold import read_sheet

CATEGORIES = [
    {"n": 1, "pilar": "IA", "categoria": "Aplicacoes de LLMs", "folhas": ["Prompt"]},
    {"n": 2, "pilar": "IA", "categoria": "Linguagem", "folhas": []},
]


def _label_vectors():
    a = {"Aplicacoes de LLMs": np.array([[1.0, 0.0]]), "Linguagem": np.array([[0.0, 1.0]])}
    c = {"Aplicacoes de LLMs": np.array([[0.6, 0.8]]), "Linguagem": np.array([[0.0, 1.0]])}
    return {"A": a, "B": a, "C": c}


def test_export_is_blind_stratified_and_refuses_to_overwrite(tmp_path):
    vectors = {"n1": np.array([1.0, 0.0]), "n2": np.array([0.5, 0.9]), "n3": np.array([0.0, 1.0])}
    answers = export_mod.condition_answers(vectors, _label_vectors())
    notes = {nid: {"title": f"Nota {nid}", "body": "Texto."} for nid in vectors}
    out = tmp_path / "gold"

    n = export_mod.export(
        out,
        notes,
        answers,
        {"Nota n1": "Linguagem"},
        CATEGORIES,
        per_stratum=10,
        seed=1,
        force=False,
        meta={},
    )

    key = json.loads((out / "categoria-notas-GABARITO-NAO-ABRIR.json").read_text(encoding="utf-8"))
    sheet = (out / "categoria-notas-planilha.csv").read_text(encoding="utf-8-sig")
    reading = (out / "categoria-notas-leitura.md").read_text(encoding="utf-8")
    assert n == 3 == len(key["items"])
    assert key["population"] == {"agree": 2, "disagree": 1}
    by_note = {i["note_id"]: i for i in key["items"]}
    assert by_note["n2"]["stratum"] == "disagree"
    assert by_note["n1"]["conditions"]["D"] == {"choice": "Linguagem"}
    for text in (sheet, reading):
        assert "label" not in text and "score" not in text and "agree" not in text
    assert "1. **Aplicacoes de LLMs**" in reading
    with pytest.raises(SystemExit):
        export_mod.export(
            out, notes, answers, {}, CATEGORIES, per_stratum=10, seed=1, force=False, meta={}
        )


def _key(n_items: int) -> dict:
    """A wins nothing; C answers the human label on every item."""
    items = []
    for i in range(n_items):
        low = i % 4 == 0  # a quarter of the notes fit no category
        items.append(
            {
                "item_id": f"N{i:03d}",
                "note_id": f"z{i}",
                "stratum": "disagree",
                "conditions": {
                    "A": {"choice": "Linguagem", "score": 0.2 if low else 0.6},
                    "B": {"choice": "Linguagem", "score": 0.2 if low else 0.6},
                    "C": {"choice": "Aplicacoes de LLMs", "score": 0.2 if low else 0.7},
                    "D": {"choice": None},
                },
            }
        )
    return {
        "categories": CATEGORIES,
        "answers": {"none": "nenhuma", "unjudgeable": "?"},
        "population": {"disagree": n_items},
        "items": items,
    }


def _sheet(n_items: int) -> dict:
    rows = {}
    for i in range(n_items):
        rows[f"N{i:03d}"] = {"categoria": "nenhuma" if i % 4 == 0 else "1"}
    rows["N000"] = {"categoria": "?"}
    return rows


def test_score_applies_the_preregistered_rule():
    result = score_mod.score(_key(120), _sheet(120))

    assert result["labelled"] == 119
    assert result["accuracy"]["A"] == 0.0
    decision = result["decision"]
    assert decision["category_label_mode"] == "max_leaf"
    assert decision["assign_min_similarity"] == 0.7
    assert decision["moc_category_shadow"].startswith("manter")


def test_score_is_inconclusive_below_the_minimum():
    assert score_mod.score(_key(40), _sheet(40))["decision"]["status"] == "inconclusivo"


def test_labels_accept_numbers_names_and_none():
    key = _key(1)
    sheet = read_sheet(b"item_id;categoria\nA;2\nB;aplicacoes de llms\nC;Nenhuma\nD;?\nE;xyz\n")

    assert score_mod.labels(sheet, key) == {
        "A": "Linguagem",
        "B": "Aplicacoes de LLMs",
        "C": "nenhuma",
    }
