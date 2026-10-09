"""Tests for the decision-gold scorer (#206, #208, #209, #212, #226, #231). Offline."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from score_decision_gold import (
    corroborates_labels,
    dedupe_labels,
    key_conditions,
    preregistered_human_rule,
    preregistered_rule_226,
    read_sheet,
    relations_labels,
    reviewer_agreement,
    score_conditions,
    score_corroborates,
    score_relations,
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


def _pair(item_id, stratum, similarity, edge, ab, ba):
    return {
        "item_id": item_id,
        "subject_id": f"01A|01{item_id}:ab",
        "sampling_stratum": stratum,
        "baseline": {"edge": edge, "similarity": similarity, "threshold": 0.85},
        "jev": {"ab_score": ab, "ba_score": ba},
    }


def test_corroborates_scores_edge_jev_level_and_cosine_against_same_idea():
    key = {
        "site": "corroborates",
        "population": {"above_threshold": 20, "near_threshold": 10},
        "items": [
            _pair("P1", "above_threshold", 0.90, True, 1.9, 1.8),  # same idea, all right
            _pair("P2", "above_threshold", 0.88, True, 1.0, 1.2),  # same topic: edge wrong
            _pair("P3", "above_threshold", 0.87, False, 1.7, 1.6),  # same idea, no edge
            _pair("P4", "near_threshold", 0.82, False, 0.2, 0.6),
            _pair("P5", "near_threshold", 0.81, False, 0.0, 0.0),
        ],
    }
    sheet = read_sheet(
        "item_id;decisao;nota\nP1;mesma-ideia;\nP2;mesmo-tema;\nP3;Mesma-Ideia;\n"
        "P4;diferente;\nP5;?;\n".encode("utf-8-sig")
    )
    labels, problems = corroborates_labels(sheet, key)
    assert not problems
    assert [lab["human_level"] for lab in labels] == [2, 1, 2, 0, None]

    result = score_corroborates(labels, key)
    conditions = result["conditions"]
    assert conditions["jev"]["correct"] == 4 and conditions["jev"]["n"] == 4
    assert conditions["threshold"]["correct"] == 2
    assert conditions["cosine"]["correct"] == 3  # P3 above threshold, though no edge
    assert "target" not in conditions["jev"]
    assert result["pairs"]["threshold:jev"]["only_b_right"] == 2
    detail = result["detail"]
    assert detail["human_level_to_jev_level"] == {"0->0": 1, "1->1": 1, "2->2": 2}
    assert detail["order_divergence_mean"] == 0.2
    rule = result["preregistered_rule_208"]
    assert rule["labels_per_band"]["low_band"] == 0
    assert not rule["rule1_sample"] and not rule["open_gate_issue"]
    assert conditions["cos88"]["correct"] == 2  # P2 reaches 0.88, P3 does not
    assert result["preregistered_rule_226"]["outcome"] == "keep_accumulating"


def test_corroborates_flags_invalid_and_missing_answers():
    key = {
        "items": [
            _pair("P1", "low_band", 0.76, False, 0, 0),
            _pair("P2", "low_band", 0.77, False, 0, 0),
        ]
    }
    labels, problems = corroborates_labels(read_sheet(b"item_id;decisao;nota\nP1;igual;\n"), key)
    assert labels == [] and problems == {"decisao_invalida": ["P1"], "sem_resposta": ["P2"]}


def _gate_result(*, n=80, same_idea=10, jev=78, cos88=70, cosine=60, p_jev=0.01, p_cut=0.01):
    return {
        "conditions": {
            "jev": {"n": n, "correct": jev},
            "cos88": {"n": n, "correct": cos88},
            "cosine": {"n": n, "correct": cosine},
        },
        "pairs": {"cos88:jev": {"mcnemar_p": p_jev}, "cosine:cos88": {"mcnemar_p": p_cut}},
        "detail": {"auc_same_idea": {"cosine": {"n_same_idea": same_idea}}},
    }


def test_rule_226_outcomes_and_tie_goes_to_the_cut():
    assert preregistered_rule_226(_gate_result())["outcome"] == "jev_decides_edge"
    assert preregistered_rule_226(_gate_result(p_jev=0.2))["outcome"] == "raise_threshold_to_cut"
    tie = _gate_result(jev=70)
    assert preregistered_rule_226(tie)["outcome"] == "raise_threshold_to_cut"
    assert preregistered_rule_226(_gate_result(p_jev=0.2, p_cut=0.3))["outcome"] == "keep_threshold"
    assert preregistered_rule_226(_gate_result(n=59))["outcome"] == "keep_accumulating"
    assert preregistered_rule_226(_gate_result(same_idea=7))["outcome"] == "keep_accumulating"


def test_relations_key_with_compared_prompts_scores_rule_231_and_keeps_unjudgeable():
    runs = ("current:trunc-a", "new:trunc-a")

    def item(item_id, current, new):
        return {
            "item_id": item_id,
            "subject_id": f"c|n|{item_id}",
            "sampling_stratum": "both",
            "answers": {runs[0]: current, runs[1]: new},
        }

    key = {
        "compared": list(runs),
        "population": {"both": 3},
        "items": [
            item("R1", "supports", "extends"),
            item("R2", "nenhuma", "nenhuma"),
            item("R3", "related", "nenhuma"),
        ],
    }
    sheet = read_sheet(b"item_id;relacao;nota\nR1;extends;\nR2;nenhuma;\nR3;?;\n")
    labels, problems = relations_labels(sheet, key)
    assert not problems
    result = score_relations(labels, key)
    assert "preregistered_rule_212" not in result
    rule = result["preregistered_rule_231"]
    assert (rule["current_correct"], rule["new_correct"], rule["labels"]) == (1, 2, 2)
    assert not rule["enough_labels"] and not rule["new_beats_current"]
