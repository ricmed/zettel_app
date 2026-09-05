"""Author-judgement fields on the extraction candidate (ADR-034).

`decision_rules`, `anti_patterns` and `named_frameworks` capture how the author
would *decide*, not only what the concept *is*. They are always optional: a chunk
that states none of them is a perfectly good candidate.
"""

from __future__ import annotations

import json

import pytest
from zettel.connector import _format_judgement
from zettel.schemas import (
    JUDGEMENT_FIELDS,
    LiteratureChunkOutput,
    PermanentNoteCandidate,
)
from zettel.vault import (
    build_literature_chunk_note,
    judgement_frontmatter,
    read_managed_block,
    render_decision_block,
)


def _candidate(**overrides) -> PermanentNoteCandidate:
    defaults = {
        "thesis": "Dropout funciona como ensemble implicito ao treinar subconjuntos de pesos",
        "definition": (
            "Cada passo de treino desliga aleatoriamente parte dos neuronios, de modo "
            "que a rede aprende varias sub-redes ao mesmo tempo e a predicao final "
            "media essas sub-redes, reduzindo a variancia sem treinar modelos separados."
        ),
        "anchor_quote": "dropout can be seen as training an implicit ensemble of subnetworks",
        "relevance_score": 4,
    }
    defaults.update(overrides)
    return PermanentNoteCandidate(**defaults)


# ── Schema contract ────────────────────────────────────────────────────


def test_fields_default_to_empty_lists():
    cand = _candidate()
    assert (cand.decision_rules, cand.anti_patterns, cand.named_frameworks) == (
        [],
        [],
        [],
    )


def test_legacy_json_without_the_keys_still_parses():
    """A cached Prompt 1 response predating these fields must keep working."""
    legacy = {
        "chunk_status": "accepted",
        "rejection_reason": "",
        "rejection_category": "",
        "summary": "Resumo antigo.",
        "key_concepts": ["dropout"],
        "candidates": [{"thesis": "T", "definition": "D", "relevance_score": 4}],
    }
    output = LiteratureChunkOutput(**json.loads(json.dumps(legacy)))
    cand = output.candidates[0]
    assert all(getattr(cand, field) == [] for field in JUDGEMENT_FIELDS)


def test_blank_items_are_dropped():
    cand = _candidate(decision_rules=["  Quando A, faca B, porque C  ", "", "   "])
    assert cand.decision_rules == ["Quando A, faca B, porque C"]


def test_lists_are_capped_at_three_without_rejecting_the_candidate(caplog):
    with caplog.at_level("WARNING"):
        cand = _candidate(anti_patterns=[f"evitar {i}" for i in range(7)])
    assert len(cand.anti_patterns) == 3
    assert "truncado" in caplog.text
    # The candidate itself survives: the fields are a bonus, not a gate.
    assert cand.thesis


def test_framework_names_are_not_normalized():
    """Quality Rule: the author's exact wording of a framework name is preserved."""
    cand = _candidate(named_frameworks=["The 5 Whys", "OODA Loop"])
    assert cand.named_frameworks == ["The 5 Whys", "OODA Loop"]


def test_roundtrips_through_candidate_json():
    """`concepts.candidate_json` is a JSON document — no column migration needed."""
    cand = _candidate(decision_rules=["Quando A, faca B, porque C"])
    restored = PermanentNoteCandidate(**json.loads(cand.model_dump_json()))
    assert restored.decision_rules == cand.decision_rules


# ── Quality filter ─────────────────────────────────────────────────────


def test_candidate_without_judgement_is_not_rejected():
    from zettel.config import AppConfig
    from zettel.extractor import _filter_candidates

    cfg = AppConfig()
    approved, rejected = _filter_candidates(
        [_candidate()],
        cfg,
        "dropout can be seen as training an implicit ensemble of subnetworks",
    )
    assert len(approved) == 1
    assert rejected == []


# ── LIT rendering ──────────────────────────────────────────────────────


def test_no_block_when_no_candidate_states_anything():
    assert render_decision_block([{"thesis": "T"}, {"thesis": "U"}]) == ""


def test_literature_note_omits_the_block_when_empty():
    _, body = build_literature_chunk_note(
        source_id="@A2020",
        citekey="A2020Titulo",
        title="Titulo",
        chunk_id="c1",
        chunk_index=0,
        literature_id="L1",
        summary="Resumo.",
        key_concepts=[],
        candidates=[_candidate().model_dump()],
        source_text="trecho",
    )
    assert "auto-decision" not in body
    assert "Julgamento do autor" not in body


def test_literature_note_renders_the_block_and_deduplicates():
    candidates = [
        _candidate(
            decision_rules=["Quando A, faca B, porque C"],
            named_frameworks=["The 5 Whys"],
        ).model_dump(),
        _candidate(
            anti_patterns=["O que evitar: X - por que falha: Y"],
            named_frameworks=["The 5 Whys"],
        ).model_dump(),
    ]
    _, body = build_literature_chunk_note(
        source_id="@A2020",
        citekey="A2020Titulo",
        title="Titulo",
        chunk_id="c1",
        chunk_index=0,
        literature_id="L1",
        summary="Resumo.",
        key_concepts=[],
        candidates=candidates,
        source_text="trecho",
    )
    block = read_managed_block(body, "auto-decision")
    assert block is not None
    assert "Quando A, faca B, porque C" in block
    assert "O que evitar: X - por que falha: Y" in block
    assert block.count("The 5 Whys") == 1
    # Section order: judgement sits between the candidates and the source excerpt.
    assert body.index("Julgamento do autor") < body.index("## Trecho da fonte")


def test_block_is_managed_so_manual_edits_survive(tmp_path):
    from zettel.vault import safe_update_managed_blocks, safe_write_note

    path = tmp_path / "LIT - A2020 - p001 - t-0001.md"
    _, body = build_literature_chunk_note(
        source_id="@A2020",
        citekey="A2020Titulo",
        title="Titulo",
        chunk_id="c1",
        chunk_index=0,
        literature_id="L1",
        summary="Resumo.",
        key_concepts=[],
        candidates=[_candidate(decision_rules=["Regra original"]).model_dump()],
        source_text="trecho",
    )
    safe_write_note(path, {"type": "literature"}, body + "\n## Minhas notas\n\nAnotacao a mao.\n")

    safe_update_managed_blocks(path, {"auto-decision": "**Regras de decisão**\n\n- Regra nova"})
    content = path.read_text(encoding="utf-8")
    assert "Regra nova" in content
    assert "Regra original" not in content
    assert "Anotacao a mao." in content


# ── ZTL frontmatter ────────────────────────────────────────────────────


def test_frontmatter_omits_empty_fields():
    assert judgement_frontmatter(_candidate()) == {}


def test_frontmatter_carries_stated_fields():
    cand = _candidate(
        decision_rules=["Quando A, faca B, porque C"],
        named_frameworks=["The 5 Whys"],
    )
    assert judgement_frontmatter(cand) == {
        "decision_rules": ["Quando A, faca B, porque C"],
        "named_frameworks": ["The 5 Whys"],
    }


# ── Prompt 2 payload ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "items,expected",
    [
        ([], "(nenhuma)"),
        (["uma"], "uma"),
        (["uma", "outra"], "uma; outra"),
    ],
)
def test_prompt_payload_rendering(items, expected):
    assert _format_judgement(items) == expected


def test_prompt_files_declare_the_new_keys():
    from pathlib import Path

    lit = Path("prompts/literature_note.md").read_text(encoding="utf-8")
    perm = Path("prompts/permanent_note.md").read_text(encoding="utf-8")
    for field in JUDGEMENT_FIELDS:
        assert field in lit, f"{field} ausente em literature_note.md"
    assert "{decision_rules}" in perm
    assert "{anti_patterns}" in perm
    assert "{named_frameworks}" in perm
    # The prompts must keep saying not to invent what the source never stated.
    assert "Nunca invente" in lit or "não invente" in lit.lower()
    assert "Nunca invente" in perm
