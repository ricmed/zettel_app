"""Prompt 2 split: the write prompt sees the source; the live path stays combined."""

from __future__ import annotations

import json

from zettel.config import AppConfig
from zettel.connector.note import _thesis_review
from zettel.connector.prompt import (
    Prompt2Payload,
    clip_source_excerpt,
    generate_split_note,
    note_write_messages,
    thesis_similarity,
)
from zettel.llm import load_prompt_parts
from zettel.schemas import PermanentNoteCandidate


def test_write_prompt_receives_excerpt_and_anchor():
    cfg = AppConfig()
    parts = load_prompt_parts(cfg.prompts_path / "note_write.md")
    cand = PermanentNoteCandidate(thesis="A tese aprovada.", definition="A definicao.")
    _system, user = note_write_messages(
        cfg,
        parts,
        cand,
        anchor_quote="ANCORA UNICA DO TRECHO",
        source_excerpt="TRECHO UNICO DA FONTE",
    )
    assert "ANCORA UNICA DO TRECHO" in user
    assert "TRECHO UNICO DA FONTE" in user
    assert "A tese aprovada." in user


def test_default_mode_stays_combined():
    assert AppConfig().linking.prompt2_mode == "combined"


def test_thesis_similarity_is_one_for_the_same_claim():
    assert thesis_similarity("A tese aprovada.", "A tese aprovada.") == 1.0
    assert thesis_similarity("A tese aprovada.", "Outra afirmacao inteira.") < 0.5


def test_thesis_review_stays_off_until_a_floor_is_set():
    cfg = AppConfig()
    assert _thesis_review(cfg, 0.2) == {}
    cfg.linking.thesis_drift_min_similarity = 0.8
    assert _thesis_review(cfg, 0.9) == {"thesis_similarity": 0.9}
    flagged = _thesis_review(cfg, 0.2)
    assert flagged["needs_thesis_review"] is True


def test_source_excerpt_respects_the_budget():
    assert clip_source_excerpt("abcdef", 3) == "abc..."


def test_split_writes_then_links_the_written_thesis(monkeypatch):
    seen: list[str] = []

    def fake_cached(*_args, **kwargs):
        seen.append(kwargs["label"])
        if kwargs["label"].startswith("connect-write"):
            body = {
                "title": "Titulo",
                "thesis": "A tese aprovada.",
                "definition": "A definicao.",
                "intuition": "",
                "example": "",
                "limits": "",
            }
        else:
            body = {"connections": []}
        return json.dumps(body), False

    monkeypatch.setattr("zettel.connector.prompt.cached_call_llm", fake_cached)
    monkeypatch.setattr("zettel.connector.prompt.needs_ptbr_fix", lambda _text: False)
    cfg = AppConfig()
    cand = PermanentNoteCandidate(thesis="A tese aprovada.", definition="A definicao.")
    output, hit, similarity = generate_split_note(
        cfg,
        db=None,
        llm=object(),
        cand=cand,
        payload=Prompt2Payload(
            source_id="@Fonte",
            literature_ref="",
            rag_context="notas",
            images_context="",
            examples={},
        ),
        source_text="o paragrafo de onde a ancora saiu",
        anchor_quote="paragrafo de onde",
        subject_id="conceito-1",
    )
    assert seen == ["connect-write:conceito-1", "connect-link:conceito-1"]
    assert output.thesis == "A tese aprovada."
    assert output.connections == []
    assert hit is False
    assert similarity == 1.0
