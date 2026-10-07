"""Tests for the decision-gold scorer (#206, #209). Offline: sheet + key in, numbers out."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from score_decision_gold import (
    dedupe_labels,
    key_conditions,
    preregistered_human_rule,
    read_sheet,
    reviewer_agreement,
    score_conditions,
)


def _item(
    item_id, stratum, llm, jev, *, conf=0.5, llm_target="none", jev_target="none", reviewer=None
):
    return {
        "item_id": item_id,
        "subject_id": f"@S::concept::{item_id}",
        "sampling_stratum": stratum,
        "letters": {"A": "01A", "B": "01B"},
        "baseline": {"decision": llm, "llm_decision": llm, "target": llm_target},
        "jev": {"decision": jev, "decision_confidence": conf, "target": jev_target},
        "reviewer": reviewer,
    }


KEY = {
    "population": {"create_new": 40, "link": 10},
    "items": [
        _item("D1", "create_new", "create_new", "link", conf=0.95, jev_target="01A"),
        _item("D2", "create_new", "create_new", "create_new"),
        _item("D3", "link", "link", "link", llm_target="01A", jev_target="01B", conf=0.7),
        _item("D4", "link", "link", "create_new", llm_target="01B", reviewer="not_ignore"),
    ],
}


def _sheet(rows):
    lines = ["item_id;decisao;alvo;nota"] + [";".join(r) for r in rows]
    return read_sheet("\n".join(lines).encode("utf-8-sig"))


def test_read_sheet_accepts_comma_too():
    sheet = read_sheet("item_id,decisao,alvo,nota\nD1,nova,,ok\n".encode("cp1252"))
    assert sheet["D1"]["decisao"] == "nova"


def test_labels_map_answers_and_letters_and_flag_problems():
    sheet = _sheet(
        [("D1", "desenvolve", "a", "n"), ("D2", "nova", "", ""), ("D3", "repete", "Z", "")]
    )
    labels, problems = dedupe_labels(sheet, KEY)
    by_id = {lab["item_id"]: lab for lab in labels}
    assert by_id["D1"]["human_decision"] == "link" and by_id["D1"]["human_target"] == "01A"
    assert by_id["D2"]["human_target"] is None
    assert problems == {"alvo_invalido": ["D3"], "sem_resposta": ["D4"]}


def test_unjudgeable_is_kept_but_never_scored():
    labels, _ = dedupe_labels(_sheet([("D1", "?", "", ""), ("D2", "nova", "", "")]), KEY)
    result = score_conditions(labels, key_conditions(KEY), KEY["population"], [])
    assert result["conditions"]["llm"]["n"] == 1


def test_scores_accuracy_weights_targets_bands_and_mcnemar():
    sheet = _sheet(
        [
            ("D1", "desenvolve", "A", ""),
            ("D2", "nova", "", ""),
            ("D3", "desenvolve", "A", ""),
            ("D4", "desenvolve", "B", ""),
        ]
    )
    labels, _ = dedupe_labels(sheet, KEY)
    result = score_conditions(labels, key_conditions(KEY), KEY["population"], [("llm", "jev")])
    llm, jev = result["conditions"]["llm"], result["conditions"]["jev"]
    assert llm["correct"] == 3  # D2, D3, D4
    assert jev["correct"] == 3  # D1, D2, D3
    # create_new items weigh 40/2 = 20 each, link items 10/2 = 5 each.
    assert llm["accuracy_weighted"] == round((20 + 5 + 5) / 50, 4)
    assert jev["accuracy_weighted"] == round((20 + 20 + 5) / 50, 4)
    # Targets: only where human and model both link. LLM D3 right, D4 right; Jev D1 right, D3 wrong.
    assert llm["target"] == {"n": 2, "accuracy": 1.0}
    assert jev["target"] == {"n": 2, "accuracy": 0.5}
    assert jev["by_confidence_band"]["high"] == {"n": 1, "accuracy": 1.0}
    assert "by_confidence_band" not in llm
    assert result["pairs"]["llm:jev"] == {
        "n": 4,
        "only_a_right": 1,
        "only_b_right": 1,
        "mcnemar_p": 1.0,
    }


def test_reviewer_agreement_and_preregistered_rule():
    labels, _ = dedupe_labels(_sheet([("D4", "desenvolve", "B", "")]), KEY)
    assert reviewer_agreement(labels, KEY) == {"n": 1, "agreement": 1.0}
    result = score_conditions(labels, key_conditions(KEY), KEY["population"], [])
    rule = preregistered_human_rule(result)
    assert rule == {"labels": 1, "enough_labels": False, "jev_at_least_llm": False}
