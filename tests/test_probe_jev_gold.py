"""Tests for the Jev gold-set probe (#206). The model is injected; nothing calls out."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from probe_jev_gold import (
    build_decision,
    build_rows,
    collect,
    judged_items,
    notes_for_reader,
    recorded_notes,
    recording_key,
    regenerate_key,
    score,
    sheet_rows,
    verdict_from,
)

ROOT = Path(__file__).resolve().parents[1]


def _answer(noul: float, category: str = "accepted", spread: float = 0.0) -> dict:
    probs = dict.fromkeys(
        ("accepted", "structural", "narrative", "promotional", "trivial", "fragmented"), 0.02
    )
    probs[category] = 0.9
    return {
        "keep": {"type": "noul", "noul": noul},
        "category": {
            "choice": category,
            "probabilities": probs,
            "confidence": 0.8,
            "spread": spread,
        },
    }


def _items():
    return {
        "c1": {"item_id": "G1", "verdict": "keep", "category": ""},
        "c2": {"item_id": "G2", "verdict": "keep", "category": ""},
        "c3": {"item_id": "G3", "verdict": "discard", "category": "structural"},
        "c4": {"item_id": "G4", "verdict": "discard", "category": "narrative"},
    }


def test_judged_items_drop_unjudgeable():
    payload = {
        "labels": [
            {"chunk_id": "a", "item_id": "G1", "human_verdict": "keep", "human_category": ""},
            {
                "chunk_id": "b",
                "item_id": "G2",
                "human_verdict": "unjudgeable",
                "human_category": "",
            },
        ]
    }
    assert set(judged_items(payload)) == {"a"}


def test_real_gold_has_judged_items():
    payload = json.loads((ROOT / "evals/gold/extracao-rotulos.json").read_text(encoding="utf-8"))
    items = judged_items(payload)
    assert len(items) == 118
    assert {v["verdict"] for v in items.values()} == {"keep", "discard"}


def test_reader_state_keeps_only_filled_note_fields():
    assert notes_for_reader([{"thesis": "T", "definition": "", "limits": None}]) == [
        {"thesis": "T"}
    ]
    decision = build_decision(
        "reader", {"source_title": "S", "text": "P", "candidates": [{"thesis": "T"}]}, "en"
    )
    assert decision.state["extracted_notes"] == [{"thesis": "T"}]


def test_recording_key_changes_with_language_and_model():
    base = recording_key("extract", "en", "typesafe/jev-1.13.0", 3, "sheet")
    assert base == recording_key("extract", "en", "typesafe/jev-1.13.0", 3, "sheet")
    assert base != recording_key("extract", "pt", "typesafe/jev-1.13.0", 3, "sheet")
    assert base != recording_key("extract", "en", "typesafe/jev-2", 3, "sheet")
    assert base != recording_key("reader", "en", "typesafe/jev-1.13.0", 3, "sheet")


def test_collect_replays_recorded_answers_without_asking():
    def must_not_ask(_):
        raise AssertionError("a recorded run must not call the model")

    answers, failures = collect(["c1"], {"c1": _answer(0.9)}, must_not_ask, lambda *a: None)
    assert answers["c1"]["keep"]["noul"] == 0.9
    assert failures == {}


def test_collect_does_not_record_failures():
    recorded = []
    _, failures = collect(
        ["c1", "c2"],
        {},
        lambda c: {"error": "429"} if c == "c1" else _answer(0.8),
        lambda c, e: recorded.append(c),
    )
    assert failures == {"c1": "429"}
    assert recorded == ["c2"]


def test_score_extract_measures_auc_and_category_agreement():
    answers = {
        "c1": _answer(0.9),
        "c2": _answer(0.8),
        "c3": _answer(0.1, "structural", spread=0.02),
        "c4": _answer(0.2, "trivial", spread=0.04),
    }
    result = score("extract", answers, _items())
    assert result["keep_noul"]["auc"] == 1.0
    assert result["category_on_human_discards"]["agreement"] == 0.5
    assert result["category_on_human_discards"]["confusion"] == {
        "narrative->trivial": 1,
        "structural->structural": 1,
    }
    assert result["category_spread_mean"] == pytest.approx(0.015)


def test_score_reader_adds_the_quality_score():
    answers = {
        c: {"keep": {"noul": n}, "quality": {"score": s}}
        for c, n, s in (("c1", 0.9, 4.0), ("c2", 0.6, 3.0), ("c3", 0.7, 1.0), ("c4", 0.1, 2.0))
    }
    result = score("reader", answers, _items())
    assert result["quality_score"]["auc"] == 1.0
    assert result["keep_noul"]["auc"] == 0.75


def test_verdict_uses_the_preregistered_threshold_and_best_rejection_class():
    assert verdict_from(_answer(0.5)) == ("accepted", "")
    assert verdict_from(_answer(0.49, "fragmented")) == ("rejected", "fragmented")
    # When `accepted` leads the category choice but the noul says discard,
    # the category is still a rejection class.
    assert verdict_from(_answer(0.3, "accepted"))[0] == "rejected"
    assert verdict_from(_answer(0.3, "accepted"))[1] != "accepted"


def test_regenerated_key_keeps_strata_and_drops_unanswered_items():
    original = {
        "population": {"accepted": 10},
        "items": [
            {
                "chunk_id": "c1",
                "item_id": "G1",
                "sampling_stratum": "accepted",
                "llm_verdict": "accepted",
            },
            {
                "chunk_id": "c9",
                "item_id": "G9",
                "sampling_stratum": "accepted",
                "llm_verdict": "accepted",
            },
        ],
    }
    key = regenerate_key(original, {"c1": _answer(0.2, "trivial")}, {"label": "jev-en"})
    assert key["population"] == {"accepted": 10}
    assert key["n_items"] == 1
    assert key["items"][0]["llm_verdict"] == "rejected"
    assert key["items"][0]["llm_category"] == "trivial"
    assert key["regenerated"] == {"label": "jev-en"}


def test_recording_key_changes_with_the_notes_run():
    a = recording_key("reader", "en", "typesafe/jev-1.13.0", 3, "gemini-t01-a.json")
    b = recording_key("reader", "en", "typesafe/jev-1.13.0", 3, "gemini-t01-b.json")
    assert a != b


def test_sheet_rows_read_the_labelled_text(tmp_path):
    sheet = tmp_path / "planilha.csv"
    sheet.write_bytes(
        "item_id;veredito;categoria;nota;fonte;locator;densidade_tabela;razao_alfanumerica;texto\n"
        'G001;n;structural;;@S;p.1;0;70;"linha 1\nlinha 2; com ponto e virgula"\n'.encode("cp850")
    )
    assert sheet_rows(sheet) == {
        "G001": {"text": "linha 1\nlinha 2; com ponto e virgula", "source": "@S"}
    }


def test_recorded_notes_parse_each_response(tmp_path):
    run = tmp_path / "run.json"
    run.write_text(
        json.dumps(
            {
                "responses": {
                    "c1": {"response": '```json\n{"candidates": [{"thesis": "T"}]}\n```'},
                    "c2": {"response": '{"chunk_status": "rejected", "candidates": []}'},
                }
            }
        ),
        encoding="utf-8",
    )
    assert recorded_notes(run) == {"c1": [{"thesis": "T"}], "c2": []}


def test_build_rows_joins_by_item_and_skips_items_not_in_the_sheet():
    items = {"c1": {"item_id": "G1"}, "c2": {"item_id": "G2"}}
    rows = build_rows(items, {"G1": {"text": "P", "source": "@S"}}, {"c1": [{"thesis": "T"}]})
    assert rows == {"c1": {"text": "P", "source_title": "@S", "candidates": [{"thesis": "T"}]}}


def test_local_gold_sheet_covers_every_judged_item_when_present():
    sheet = ROOT / "evals/gold/extracao-planilha.csv"
    if not sheet.exists():
        pytest.skip("planilha rotulada e gitignored (direitos autorais)")
    payload = json.loads((ROOT / "evals/gold/extracao-rotulos.json").read_text(encoding="utf-8"))
    items = judged_items(payload)
    assert len(build_rows(items, sheet_rows(sheet), {})) == len(items)
