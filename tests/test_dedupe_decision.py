"""Tests for DedupeDecision enum and parsing after MERGE removal."""

import json

from zettel.schemas import DedupeDecision, DedupeResult


def test_dedupe_decision_enum_no_merge():
    """MERGE should not exist in DedupeDecision."""
    members = [m.value for m in DedupeDecision]
    assert "merge" not in members
    assert "create_new" in members
    assert "ignore" in members
    assert "refine_existing" in members


def test_dedupe_decision_has_three_members():
    """Enum should have exactly 3 members after removing MERGE."""
    assert len(DedupeDecision) == 3


def test_parse_dedupe_result_refine():
    """DedupeResult can be parsed with refine_existing decision."""
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
    """DedupeResult with create_new works normally."""
    data = {
        "decision": "create_new",
        "target_note_id": None,
        "reason": "Conceito suficientemente distinto",
    }
    result = DedupeResult(**data)
    assert result.decision == DedupeDecision.CREATE_NEW
    assert result.target_note_id is None


def test_parse_dedupe_result_from_json():
    """Full round-trip: JSON string to DedupeResult."""
    raw = '{"decision": "ignore", "target_note_id": null, "reason": "Duplicata exata"}'
    data = json.loads(raw)
    result = DedupeResult(**data)
    assert result.decision == DedupeDecision.IGNORE
