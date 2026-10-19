"""Tests for DedupeDecision enum and parsing."""

import json

from zettel.schemas import DedupeDecision, DedupeResult


def test_dedupe_decision_enum_members():
    members = {m.value for m in DedupeDecision}
    assert "create_new" in members
    assert "ignore" in members
    assert "refine_existing" in members
    assert "merge" in members


def test_dedupe_decision_has_four_members():
    assert len(DedupeDecision) == 4


def test_parse_dedupe_result_refine():
    data = {
        "decision": "refine_existing",
        "target_note_id": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "reason": "O candidato amplia a nota existente com nuances novas",
    }
    result = DedupeResult(**data)
    assert result.decision == DedupeDecision.REFINE_EXISTING
    assert result.target_note_id == "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    assert "amplia" in result.reason


def test_parse_dedupe_result_create_new():
    data = {
        "decision": "create_new",
        "target_note_id": None,
        "reason": "Conceito suficientemente distinto",
    }
    result = DedupeResult(**data)
    assert result.decision == DedupeDecision.CREATE_NEW
    assert result.target_note_id is None


def test_checked_dedupe_result_repairs_a_wrapped_id_and_refuses_a_stranger():
    from zettel.schemas import checked_dedupe_result, resolve_dedupe_result

    compared = {"01NOTE"}
    kept = checked_dedupe_result(
        {
            "decision": "ignore",
            "target_note_id": "veja [[01NOTE]]",
            "reason": "repete",
        },
        compared,
    )
    assert kept.target_note_id == "01NOTE"
    replaced, fell_back = resolve_dedupe_result(
        {"decision": "ignore", "target_note_id": None, "reason": "repete"},
        compared,
    )
    assert fell_back is True
    assert replaced.decision.value == "create_new"
    assert replaced.target_note_id is None


def test_parse_dedupe_result_from_json():
    raw = '{"decision": "ignore", "target_note_id": null, "reason": "Duplicata exata"}'
    data = json.loads(raw)
    result = DedupeResult(**data)
    assert result.decision == DedupeDecision.IGNORE
