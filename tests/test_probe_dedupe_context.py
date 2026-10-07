"""Tests for the #209 dedupe-context probe. The LLM is injected; nothing calls out."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from probe_dedupe_context import collect, existing_notes_for, parse_answer, summarize_runs

STATE = {
    "candidate": {"thesis": "T", "definition": "D"},
    "existing_notes": [{"id": "01A", "title": "A", "text": "excerpt of A"}],
}


def test_trunc_shows_the_stored_excerpt_and_full_asks_the_pipeline_function():
    calls = []

    def full(hits):
        calls.append(hits)
        return [{"id": h["id"], "title": h["metadata"]["title"], "text": "FULL"} for h in hits]

    assert existing_notes_for("trunc", STATE, full) == [
        {"id": "01A", "title": "A", "text": "excerpt of A"}
    ]
    assert calls == []
    assert existing_notes_for("full", STATE, full)[0]["text"] == "FULL"
    assert calls[0][0]["id"] == "01A"


def test_parse_answer_collapses_refine_and_merge_and_flags_garbage():
    answer = parse_answer('{"decision": "merge", "target_note_id": "01A", "reason": "r"}')
    assert answer["decision"] == "link" and answer["target"] == "01A"
    assert parse_answer('{"decision": "create_new", "target_note_id": null}')["target"] is None
    assert "error" in parse_answer("sem json")


def test_collect_replays_and_never_records_errors():
    recorded = {"D1": {"decision": "link", "target": "01A"}}
    stored = []
    answers = collect(
        ["D1", "D2", "D3"],
        recorded,
        lambda i: {"error": "x"} if i == "D2" else {"decision": "create_new", "target": None},
        lambda i, e: stored.append(i),
    )
    assert answers["D1"] == recorded["D1"]
    assert "error" in answers["D2"]
    assert stored == ["D3"]


def _item(item_id, stratum):
    return {
        "item_id": item_id,
        "subject_id": item_id,
        "sampling_stratum": stratum,
        "letters": {"A": "01A"},
        "baseline": {"decision": "create_new", "llm_decision": "create_new", "target": "none"},
        "jev": {"decision": "create_new", "decision_confidence": 0.5, "target": "none"},
    }


def test_summarize_applies_the_preregistered_rule():
    key = {
        "population": {"create_new": 2},
        "items": [_item("D1", "create_new"), _item("D2", "create_new")],
    }
    labels = [
        {
            "item_id": i,
            "sampling_stratum": "create_new",
            "human_decision": "link",
            "human_target": "01A",
        }
        for i in ("D1", "D2")
    ]
    link = {"decision": "link", "target": "01A"}
    new = {"decision": "create_new", "target": None}
    runs = {
        "trunc-a": {"D1": new, "D2": new},
        "trunc-b": {"D1": new, "D2": link},
        "full-a": {"D1": link, "D2": link},
        "full-b": {"D1": link, "D2": {"error": "x"}},
    }
    result = summarize_runs(labels, key, runs)
    rule = result["preregistered_rule_209"]
    assert rule["full_a_correct"] == 2 and rule["trunc_a_correct"] == 0
    assert rule["non_inferior"] is True
    assert rule["valid"] is False  # full-b has 1 of 2 invalid, above 5%
    assert result["stability"]["trunc"] == {"n": 2, "same_decision": 0.5}
    assert result["decision_distribution"]["full-a"] == {"link": 2}


def test_prompt_args_and_run_ids():
    import pytest
    from probe_dedupe_context import parse_prompt_args, run_ids

    assert parse_prompt_args(["current=a.md", "new=b.md"]) == [
        ("current", Path("a.md")),
        ("new", Path("b.md")),
    ]
    for bad in (["semigual"], ["x:y=a.md"], ["a=1.md", "a=2.md"]):
        with pytest.raises(ValueError):
            parse_prompt_args(bad)
    assert run_ids(["current"]) == ["trunc-a", "trunc-b", "full-a", "full-b"]
    assert run_ids(["current", "new"])[:2] == ["current:trunc-a", "current:trunc-b"]


def test_summarize_two_prompts_applies_the_218_rule():
    key = {
        "population": {"create_new": 2},
        "items": [_item("D1", "create_new"), _item("D2", "create_new")],
    }
    labels = [
        {
            "item_id": i,
            "sampling_stratum": "create_new",
            "human_decision": "create_new",
            "human_target": None,
        }
        for i in ("D1", "D2")
    ]
    link = {"decision": "link", "target": "01A"}
    new = {"decision": "create_new", "target": None}
    runs = {
        "current:trunc-a": {"D1": new, "D2": link},
        "current:trunc-b": {"D1": new, "D2": link},
        "current:full-a": {"D1": link, "D2": link},
        "current:full-b": {"D1": link, "D2": link},
        "new:trunc-a": {"D1": new, "D2": new},
        "new:trunc-b": {"D1": new, "D2": new},
        "new:full-a": {"D1": new, "D2": new},
        "new:full-b": {"D1": new, "D2": link},
    }
    result = summarize_runs(labels, key, runs, ["current", "new"])
    rule = result["preregistered_rule_218"]
    assert rule["new_full_a_correct"] == 2
    assert rule["current_full_a_correct"] == 0 and rule["current_trunc_a_correct"] == 1
    assert rule["beats_current_full"] and rule["not_worse_than_current_trunc"]
    assert rule["new_link_answers"] == 0 and rule["human_link_answers"] == 0
    assert result["stability"]["new:full"] == {"n": 2, "same_decision": 0.5}
    assert "preregistered_rule_209" not in result
