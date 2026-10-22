"""Tests for granular literature review approve/reject."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from zettel.config import AppConfig
from zettel.review import (
    BAND_HIGH,
    BAND_MEDIUM,
    BAND_VERY_LOW,
    approve_chunk,
    ask_review_decision,
    chunk_confidence_band,
    confidence_band_counts,
    filter_chunks_by_band,
    format_confidence_report,
    format_review_item,
    normalize_reject_scope,
    normalize_review_decision,
    reject_chunk,
    run_review,
)
from zettel.state import StateDB
from zettel.vault import (
    build_literature_chunk_note,
    literature_chunk_filename_for_row,
    literature_source_dirname,
    safe_write_note,
)

from tests.fakes import FakeVectorIndex, deleted


@pytest.fixture
def env(tmp_path):
    cfg = AppConfig(
        vault_path=tmp_path / "vault",
        cache_path=tmp_path / "cache",
        state_db_path=tmp_path / "state.db",
        chroma_path=tmp_path / "chroma",
    )
    for d in (
        "00_Inbox/Review",
        "10_Sources",
        "20_Literature",
        "30_Permanent",
        "90_Assets",
    ):
        (cfg.vault_path / d).mkdir(parents=True, exist_ok=True)
    db = StateDB(cfg.state_db_path)
    db.upsert_source(
        "@Book2024",
        "Book2024",
        "Livro Teste",
        ["Autor"],
        2024,
        "h",
        "/x.pdf",
        "pdf",
    )
    db.upsert_chapter("@Book2024::ch000", "@Book2024", "Ch1", "chh")
    db.upsert_chunk(
        "@Book2024::ch000::abc",
        "@Book2024",
        "@Book2024::ch000",
        "texto do chunk",
        "ck",
        chunk_index=3,
        page_in_file=20,
        page_in_book=10,
        page_confidence="inferred",
        status="awaiting_review",
        section_path="Ch1 > Intro",
        literature_id="lit123",
        summary_json=json.dumps(
            {
                "summary": "Um resumo",
                "key_concepts": ["conceito"],
                "candidates": [
                    {
                        "thesis": "Tese X",
                        "definition": "def " * 5,
                        "anchor_quote": "citacao ancora de dez palavras aqui sim",
                        "relevance_score": 4,
                        "chunk_status": "ok",
                        "rejection_reason": "",
                        "rejection_category": "",
                    }
                ],
            }
        ),
        review_confidence=0.9,
    )
    chunk_row = db.get_chunk("@Book2024::ch000::abc")
    fname = literature_chunk_filename_for_row("Book2024", chunk_row)
    draft_dir = cfg.vault_path / "00_Inbox" / "Review" / literature_source_dirname("Book2024")
    draft_dir.mkdir(parents=True, exist_ok=True)
    meta, body = build_literature_chunk_note(
        source_id="@Book2024",
        citekey="Book2024",
        title="Livro Teste",
        chunk_id="@Book2024::ch000::abc",
        chunk_index=3,
        literature_id="lit123",
        summary="Um resumo",
        key_concepts=["conceito"],
        candidates=[],
        section_path="Ch1 > Intro",
        page_in_file=20,
        page_in_book=10,
        status="awaiting_review",
        review_confidence=0.9,
    )
    draft_path = draft_dir / fname
    safe_write_note(draft_path, meta, body)
    db.update_chunk_review(
        "@Book2024::ch000::abc",
        literature_note_path=str(draft_path),
        status="awaiting_review",
    )
    db.upsert_concept(
        "c1",
        "@Book2024",
        "@Book2024::ch000::abc",
        candidate_json='{"thesis":"t","definition":"d","chunk_status":"ok",'
        '"rejection_reason":"","rejection_category":"","anchor_quote":"x",'
        '"relevance_score":4}',
        status="awaiting_review",
    )
    idx = FakeVectorIndex()
    yield cfg, db, idx
    db.close()


def test_approve_moves_draft_without_embedding(env):
    """Approval persists to the vault and SQLite for audit, and embeds nothing."""
    cfg, db, idx = env
    ok = approve_chunk(cfg, db, idx, "@Book2024::ch000::abc")
    assert ok
    chunk = db.get_chunk("@Book2024::ch000::abc")
    assert chunk["status"] == "persisted"
    fname = literature_chunk_filename_for_row("Book2024", db.get_chunk("@Book2024::ch000::abc"))
    dest = cfg.vault_path / "20_Literature" / literature_source_dirname("Book2024") / fname
    assert dest.exists()
    dest_text = dest.read_text(encoding="utf-8")
    assert "texto do chunk" in dest_text
    assert "zettel:auto-source-excerpt:start" in dest_text
    assert "Um resumo" in dest_text
    assert not (
        cfg.vault_path / "00_Inbox" / "Review" / literature_source_dirname("Book2024") / fname
    ).exists()
    # The audit trail lives in the vault and in SQLite, never in the vector store.
    assert chunk["literature_note_path"] == str(dest)
    concepts = db.get_concepts_for_chunk("@Book2024::ch000::abc")
    assert concepts[0]["status"] == "extracted"


def test_approve_preserves_candidate_quotes_block_without_duplicating(tmp_path):
    """#58: the anchor-quote managed block survives promotion exactly once."""
    cfg = AppConfig(
        vault_path=tmp_path / "vault",
        cache_path=tmp_path / "cache",
        state_db_path=tmp_path / "state.db",
        chroma_path=tmp_path / "chroma",
    )
    for d in (
        "00_Inbox/Review",
        "10_Sources",
        "20_Literature",
        "30_Permanent",
        "90_Assets",
    ):
        (cfg.vault_path / d).mkdir(parents=True, exist_ok=True)
    db = StateDB(cfg.state_db_path)
    db.upsert_source("@Book2024", "Book2024", "Livro Teste", ["Autor"], 2024, "h", "/x.pdf", "pdf")
    db.upsert_chapter("@Book2024::ch000", "@Book2024", "Ch1", "chh")
    candidates = [
        {
            "thesis": "L1 induz esparsidade nos pesos do modelo",
            "anchor_quote": "a penalidade L1 empurra pesos irrelevantes para exatamente zero",
            "relevance_score": 4,
        }
    ]
    db.upsert_chunk(
        "@Book2024::ch000::abc",
        "@Book2024",
        "@Book2024::ch000",
        "texto do chunk",
        "ck",
        chunk_index=0,
        status="awaiting_review",
        literature_id="lit123",
        summary_json=json.dumps(
            {"summary": "Um resumo", "key_concepts": [], "candidates": candidates}
        ),
        review_confidence=0.9,
    )
    chunk_row = db.get_chunk("@Book2024::ch000::abc")
    fname = literature_chunk_filename_for_row("Book2024", chunk_row)
    draft_dir = cfg.vault_path / "00_Inbox" / "Review" / literature_source_dirname("Book2024")
    draft_dir.mkdir(parents=True, exist_ok=True)
    meta, body = build_literature_chunk_note(
        source_id="@Book2024",
        citekey="Book2024",
        title="Livro Teste",
        chunk_id="@Book2024::ch000::abc",
        chunk_index=0,
        literature_id="lit123",
        summary="Um resumo",
        key_concepts=[],
        candidates=candidates,
        page_in_book=10,
        status="awaiting_review",
        review_confidence=0.9,
    )
    draft_path = draft_dir / fname
    safe_write_note(draft_path, meta, body)
    db.update_chunk_review(
        "@Book2024::ch000::abc",
        literature_note_path=str(draft_path),
        status="awaiting_review",
    )
    idx = FakeVectorIndex()

    ok = approve_chunk(cfg, db, idx, "@Book2024::ch000::abc")
    assert ok
    dest = cfg.vault_path / "20_Literature" / literature_source_dirname("Book2024") / fname
    dest_text = dest.read_text(encoding="utf-8")
    assert dest_text.count("zettel:auto-candidate-quotes:start") == 1
    assert "a penalidade L1 empurra pesos irrelevantes" in dest_text
    db.close()


def test_reject_deletes_draft(env):
    cfg, db, idx = env
    ok = reject_chunk(cfg, db, idx, "@Book2024::ch000::abc")
    assert ok
    chunk = db.get_chunk("@Book2024::ch000::abc")
    assert chunk["status"] == "rejected"
    chunk = db.get_chunk("@Book2024::ch000::abc")
    fname = literature_chunk_filename_for_row("Book2024", chunk)
    assert not (
        cfg.vault_path / "00_Inbox" / "Review" / literature_source_dirname("Book2024") / fname
    ).exists()
    concepts = db.get_concepts_for_chunk("@Book2024::ch000::abc")
    assert concepts[0]["status"] == "rejected"


def test_reject_refuses_chunk_outside_review(env):
    cfg, db, idx = env
    assert approve_chunk(cfg, db, idx, "@Book2024::ch000::abc")
    assert not reject_chunk(cfg, db, idx, "@Book2024::ch000::abc")
    assert db.get_chunk("@Book2024::ch000::abc")["status"] == "persisted"


# ── #54: no draft file was ever written (rejected / content-less chunk) ──


@pytest.fixture
def env_no_draft(tmp_path):
    """A chunk with literature_note_path=None, as #54 now leaves rejected/empty chunks."""
    cfg = AppConfig(
        vault_path=tmp_path / "vault",
        cache_path=tmp_path / "cache",
        state_db_path=tmp_path / "state.db",
        chroma_path=tmp_path / "chroma",
    )
    for d in (
        "00_Inbox/Review",
        "10_Sources",
        "20_Literature",
        "30_Permanent",
        "90_Assets",
    ):
        (cfg.vault_path / d).mkdir(parents=True, exist_ok=True)
    db = StateDB(cfg.state_db_path)
    db.upsert_source(
        "@Book2024",
        "Book2024",
        "Livro Teste",
        ["Autor"],
        2024,
        "h",
        "/x.pdf",
        "pdf",
    )
    db.upsert_chapter("@Book2024::ch000", "@Book2024", "Ch1", "chh")
    db.upsert_chunk(
        "@Book2024::ch000::abc",
        "@Book2024",
        "@Book2024::ch000",
        "texto do chunk",
        "ck",
        chunk_index=0,
        status="awaiting_review",
        literature_id="lit123",
        summary_json=json.dumps(
            {
                "summary": "Um resumo",
                "key_concepts": ["conceito"],
                "candidates": [],
                "rejection_reason": "so uma referencia bibliografica",
                "rejection_category": "structural",
            }
        ),
        review_confidence=0.1,
    )
    idx = FakeVectorIndex()
    yield cfg, db, idx
    db.close()


def test_reject_tolerates_null_literature_note_path(env_no_draft):
    """#54: chunk_status=rejected never got a draft file; reject still works cleanly."""
    cfg, db, idx = env_no_draft
    assert db.get_chunk("@Book2024::ch000::abc")["literature_note_path"] is None
    ok = reject_chunk(cfg, db, idx, "@Book2024::ch000::abc")
    assert ok
    assert db.get_chunk("@Book2024::ch000::abc")["status"] == "rejected"


def test_approve_tolerates_null_literature_note_path_by_rebuilding(env_no_draft):
    """#54: no draft file to move, so approve_chunk rebuilds the note from summary_json."""
    cfg, db, idx = env_no_draft
    ok = approve_chunk(cfg, db, idx, "@Book2024::ch000::abc")
    assert ok
    chunk = db.get_chunk("@Book2024::ch000::abc")
    assert chunk["status"] == "persisted"
    fname = literature_chunk_filename_for_row("Book2024", chunk)
    dest = cfg.vault_path / "20_Literature" / literature_source_dirname("Book2024") / fname
    assert dest.exists()


def test_confidence_band_counts():
    limiar = 0.85
    chunks = [
        {"review_confidence": 0.1},
        {"review_confidence": 0.4},
        {"review_confidence": 0.5},
        {"review_confidence": 0.84},
        {"review_confidence": 0.85},
        {"review_confidence": 0.9},
        {"review_confidence": None},
    ]
    bands = confidence_band_counts(chunks, limiar)
    assert bands == {
        "very_low": 3,  # 0.1, 0.4, None→0
        "medium": 2,  # 0.5, 0.84
        "high": 2,  # 0.85, 0.9
        "total": 7,
    }
    report = format_confidence_report(bands, limiar)
    assert "Baixissima" in report
    assert "3" in report
    assert "0.85" in report

    assert chunk_confidence_band(0.1, limiar) == BAND_VERY_LOW
    assert chunk_confidence_band(0.5, limiar) == BAND_MEDIUM
    assert chunk_confidence_band(0.9, limiar) == BAND_HIGH
    assert len(filter_chunks_by_band(chunks, BAND_VERY_LOW, limiar)) == 3
    assert len(filter_chunks_by_band(chunks, BAND_MEDIUM, limiar)) == 2
    assert len(filter_chunks_by_band(chunks, BAND_HIGH, limiar)) == 2
    assert len(filter_chunks_by_band(chunks, "all", limiar)) == 7


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("t", "all"),
        ("todos", "all"),
        ("b", BAND_VERY_LOW),
        ("baixissima", BAND_VERY_LOW),
        ("m", BAND_MEDIUM),
        ("media", BAND_MEDIUM),
        ("h", BAND_HIGH),
        ("alta", BAND_HIGH),
        ("c", "cancel"),
        ("cancelar", "cancel"),
        ("x", None),
    ],
)
def test_normalize_reject_scope(raw, expected):
    assert normalize_reject_scope(raw) == expected


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("a", "aprovar"),
        ("aprovar", "aprovar"),
        ("r", "rejeitar"),
        ("rejeitar", "rejeitar"),
        ("p", "pular"),
        ("pular", "pular"),
        ("q", "sair"),
        ("sair", "sair"),
        ("A", "aprovar"),
        ("  r  ", "rejeitar"),
        ("x", None),
        ("", None),
    ],
)
def test_normalize_review_decision(raw, expected):
    assert normalize_review_decision(raw) == expected


def test_ask_review_decision_accepts_shortcut():
    console = MagicMock()
    with patch("rich.prompt.Prompt.ask", return_value="r") as ask:
        choice = ask_review_decision(console, conf=0.1, limiar=0.85)
    assert choice == "rejeitar"
    assert ask.call_args.kwargs["default"] == "p"
    assert ask.call_args.kwargs["show_choices"] is False


def test_ask_review_decision_enter_skips_even_when_confidence_is_high():
    console = MagicMock()
    with patch("rich.prompt.Prompt.ask", return_value="p") as ask:
        choice = ask_review_decision(console, conf=0.9, limiar=0.85)
    assert choice == "pular"
    assert ask.call_args.kwargs["default"] == "p"


def test_invalid_candidate_json_stays_extracted(env, caplog):
    import logging

    from zettel.review import _dedupe_approved_concepts

    cfg, db, idx = env
    db.upsert_concept(
        "c-bad",
        "@Book2024",
        "@Book2024::ch000::abc",
        candidate_json="{",
        status="extracted",
    )
    with caplog.at_level(logging.WARNING):
        _dedupe_approved_concepts(cfg, db, idx, "@Book2024")
    assert db.get_concept("c-bad")["status"] == "extracted"
    assert "c-bad" in caplog.text


def test_review_queue_puts_defects_before_low_confidence():
    from zettel.review import review_queue_key

    chunks = [
        {"chunk_id": "high", "review_confidence": 0.9, "summary_json": "{}"},
        {"chunk_id": "low", "review_confidence": 0.2, "summary_json": "{}"},
        {
            "chunk_id": "defect",
            "review_confidence": 0.95,
            "summary_json": json.dumps({"rejected_candidates": [{"thesis": "x"}]}),
        },
    ]
    ordered = sorted(chunks, key=review_queue_key)
    assert [c["chunk_id"] for c in ordered] == ["defect", "low", "high"]


def test_run_review_batch_approve_requires_confirmation(env):
    cfg, db, idx = env
    with (
        patch("zettel.usage.begin_run"),
        patch("zettel.usage.finish_pipeline_run"),
        patch("zettel.review._dedupe_approved_concepts"),
        patch("rich.prompt.Prompt.ask", side_effect=["a", "n", "q"]),
    ):
        stats = run_review(cfg, db, idx, interactive=True)
    assert stats["approved"] == 0
    assert stats["approved_threshold"] == 0
    assert db.get_chunk("@Book2024::ch000::abc")["status"] == "awaiting_review"


def test_format_review_item_shows_the_candidate_and_marks_the_anchor():
    chunk = {
        "chunk_id": "@Duan2026::ch002::7c9b6704",
        "review_confidence": 0.8,
        "page_in_book": 3,
        "text": "O modelo usa atencao para ligar tokens distantes no mesmo paragrafo.",
        "summary_json": json.dumps(
            {
                "summary": "Resumo do trecho.",
                "rejected_candidates": [
                    {"thesis": "Definicao de periferia", "reason": "so define o termo"}
                ],
            }
        ),
    }
    concept = {
        "concept_id": "c-atencao",
        "candidate_json": json.dumps(
            {
                "thesis": "Atencao liga tokens distantes",
                "definition": "Um mecanismo que pesa outras posicoes da sequencia.",
                "intuition": "Como consultar o resto da frase.",
                "limits": "Nao cria informacao nova.",
                "relevance_score": 4,
                "anchor_quote": "atencao para ligar tokens distantes",
            }
        ),
    }
    card = format_review_item(chunk, [concept])
    assert "Atencao liga tokens distantes" in card
    assert "Um mecanismo que pesa outras posicoes" in card
    assert "Como consultar o resto da frase." in card
    assert "Nao cria informacao nova." in card
    assert "relevancia 4/5" in card
    assert ">>>atencao para ligar tokens distantes<<<" in card
    assert "so define o termo" in card


def test_one_of_two_candidates_can_be_approved(env):
    cfg, db, idx = env
    from zettel.review import decide_concepts

    db.upsert_concept(
        "c2",
        "@Book2024",
        "@Book2024::ch000::abc",
        candidate_json=json.dumps(
            {
                "thesis": "Segunda tese que nao deve ir para o connect",
                "definition": "Definicao da segunda ideia com palavras suficientes.",
                "anchor_quote": "texto do chunk",
                "relevance_score": 3,
            }
        ),
        status="awaiting_review",
    )
    outcome = decide_concepts(cfg, db, idx, {"c1": "approve", "c2": "reject"})
    assert outcome["persisted"] == ["@Book2024::ch000::abc"]
    assert db.get_concept("c1")["status"] == "extracted"
    assert db.get_concept("c2")["status"] == "rejected"
    text = Path(db.get_chunk("@Book2024::ch000::abc")["literature_note_path"]).read_text(
        encoding="utf-8"
    )
    assert "**t**" in text
    assert "Segunda tese que nao deve ir para o connect" not in text


def test_edited_draft_thesis_reaches_candidate_json_and_bad_anchor_is_refused(env):
    from zettel.review import decide_concepts
    from zettel.vault import build_literature_chunk_note, safe_write_note

    cfg, db, idx = env
    chunk_id = "@Book2024::ch000::abc"
    thesis = "Tese original sobre o trecho fonte"
    quote = "texto do chunk"
    candidate = {
        "thesis": thesis,
        "definition": "Definicao autonoma com palavras suficientes para o schema.",
        "anchor_quote": quote,
        "relevance_score": 4,
    }
    db.upsert_concept(
        "c1",
        "@Book2024",
        chunk_id,
        candidate_json=json.dumps(candidate),
        status="awaiting_review",
    )
    chunk = db.get_chunk(chunk_id)
    meta, body = build_literature_chunk_note(
        source_id="@Book2024",
        citekey="Book2024",
        title="Livro Teste",
        chunk_id=chunk_id,
        chunk_index=3,
        literature_id="lit123",
        summary="Um resumo",
        key_concepts=["conceito"],
        candidates=[candidate],
        section_path="Ch1 > Intro",
        source_text=chunk["text"],
        page_in_file=20,
        page_in_book=10,
        status="awaiting_review",
        review_confidence=0.9,
    )
    body = body.replace(thesis, "Tese reescrita pelo revisor no rascunho")
    body = body.replace(quote, "uma frase que o chunk nao contem de modo algum")
    safe_write_note(Path(chunk["literature_note_path"]), meta, body)

    outcome = decide_concepts(cfg, db, idx, {"c1": "approve"})
    assert outcome["refused"] == ["c1"]
    assert db.get_concept("c1")["status"] == "awaiting_review"
    stored = json.loads(db.get_concept("c1")["candidate_json"])
    assert stored["thesis"] == "Tese reescrita pelo revisor no rascunho"
    assert stored["anchor_quote"] == quote

    body = body.replace(
        "uma frase que o chunk nao contem de modo algum",
        quote,
    )
    safe_write_note(Path(chunk["literature_note_path"]), meta, body)
    outcome = decide_concepts(cfg, db, idx, {"c1": "approve"})
    assert outcome["persisted"] == [chunk_id]
    assert json.loads(db.get_concept("c1")["candidate_json"])["thesis"].startswith("Tese reescrita")


def test_partial_approval_keeps_the_edited_candidate_line(env):
    from zettel.review import decide_concepts
    from zettel.vault import build_literature_chunk_note, safe_write_note

    cfg, db, idx = env
    chunk_id = "@Book2024::ch000::abc"
    first = {
        "thesis": "Primeira tese permanece no rascunho",
        "definition": "Definicao autonoma com palavras suficientes para o schema.",
        "anchor_quote": "texto",
        "relevance_score": 4,
    }
    second = {
        "thesis": "Segunda tese original do candidato",
        "definition": "Outra definicao autonoma com palavras suficientes para o schema.",
        "anchor_quote": "texto do chunk",
        "relevance_score": 3,
    }
    db.update_chunk_review(
        chunk_id,
        summary_json=json.dumps({"summary": "Um resumo", "candidates": [first, second]}),
    )
    for concept_id, candidate in (("c1", first), ("c2", second)):
        db.upsert_concept(
            concept_id,
            "@Book2024",
            chunk_id,
            candidate_json=json.dumps(candidate),
            status="awaiting_review",
        )
    chunk = db.get_chunk(chunk_id)
    meta, body = build_literature_chunk_note(
        source_id="@Book2024",
        citekey="Book2024",
        title="Livro Teste",
        chunk_id=chunk_id,
        chunk_index=3,
        literature_id="lit123",
        summary="Um resumo",
        key_concepts=["conceito"],
        candidates=[first, second],
        section_path="Ch1 > Intro",
        source_text=chunk["text"],
        page_in_file=20,
        page_in_book=10,
        status="awaiting_review",
        review_confidence=0.9,
    )
    body = body.replace(second["thesis"], "Tese reescrita do segundo candidato")
    body = body.replace(second["anchor_quote"], "uma frase que o chunk nao contem")
    safe_write_note(Path(chunk["literature_note_path"]), meta, body)

    outcome = decide_concepts(cfg, db, idx, {"c1": "reject", "c2": "approve"})
    assert outcome["refused"] == ["c2"]
    assert db.get_concept("c1")["status"] == "rejected"
    assert db.get_concept("c2")["status"] == "awaiting_review"
    stored = json.loads(db.get_concept("c2")["candidate_json"])
    assert stored["thesis"] == "Tese reescrita do segundo candidato"
    assert stored["anchor_quote"] == second["anchor_quote"]

    body = body.replace("uma frase que o chunk nao contem", second["anchor_quote"])
    safe_write_note(Path(chunk["literature_note_path"]), meta, body)
    outcome = decide_concepts(cfg, db, idx, {"c2": "approve"})
    assert outcome["persisted"] == [chunk_id]
    assert json.loads(db.get_concept("c2")["candidate_json"])["thesis"].startswith("Tese reescrita")
    approved = Path(db.get_chunk(chunk_id)["literature_note_path"]).read_text(encoding="utf-8")
    assert "Tese reescrita do segundo candidato" in approved


def test_format_review_item_includes_summary_and_chunk_text():
    chunk = {
        "chunk_id": "@Duan2026::ch002::7c9b6704",
        "review_confidence": 0.10,
        "page_in_book": 3,
        "page_in_file": 3,
        "section_path": "1 Introduction",
        "summary_json": json.dumps(
            {
                "summary": "O trecho apresenta o sumario do relatorio tecnico.",
            }
        ),
        "text": "This report is organized as follows. Section 1 introduces GLM-OCR.",
    }
    card = format_review_item(chunk)
    assert "@Duan2026::ch002::7c9b6704 conf=0.10  p.3  1 Introduction" in card
    assert "Resumo\nO trecho apresenta o sumario do relatorio tecnico." in card
    assert "Trecho\nThis report is organized as follows. Section 1 introduces GLM-OCR." in card


def test_format_review_item_fallbacks_when_empty():
    chunk = {
        "chunk_id": "@X::ch::1",
        "review_confidence": None,
        "text": "   ",
        "summary_json": "not-json",
    }
    card = format_review_item(chunk)
    assert "conf=0.00  p.?" in card
    assert "_Sem resumo._" in card
    assert "_Trecho nao disponivel._" in card


def _seed_awaiting_chunk(cfg, db, chunk_id, chunk_index, confidence, lit_id):
    text = f"texto {chunk_id}"
    db.upsert_chunk(
        chunk_id,
        "@Book2024",
        "@Book2024::ch000",
        text,
        f"ck{chunk_index}",
        chunk_index=chunk_index,
        page_in_file=20,
        page_in_book=10,
        page_confidence="inferred",
        status="awaiting_review",
        section_path="Ch1",
        literature_id=lit_id,
        summary_json=json.dumps(
            {"summary": f"resumo {chunk_index}", "key_concepts": [], "candidates": []}
        ),
        review_confidence=confidence,
    )
    chunk_row = db.get_chunk(chunk_id)
    fname = literature_chunk_filename_for_row("Book2024", chunk_row)
    draft_dir = cfg.vault_path / "00_Inbox" / "Review" / literature_source_dirname("Book2024")
    draft_dir.mkdir(parents=True, exist_ok=True)
    meta, body = build_literature_chunk_note(
        source_id="@Book2024",
        citekey="Book2024",
        title="Livro Teste",
        chunk_id=chunk_id,
        chunk_index=chunk_index,
        literature_id=lit_id,
        summary=f"resumo {chunk_index}",
        key_concepts=[],
        candidates=[],
        section_path="Ch1",
        page_in_file=20,
        page_in_book=10,
        status="awaiting_review",
        review_confidence=confidence,
    )
    draft_path = draft_dir / fname
    safe_write_note(draft_path, meta, body)
    db.update_chunk_review(
        chunk_id,
        literature_note_path=str(draft_path),
        status="awaiting_review",
        review_confidence=confidence,
    )


def test_run_review_mode_a_respects_threshold(env):
    cfg, db, idx = env
    # env already has one chunk at 0.9; add two below limiar
    _seed_awaiting_chunk(cfg, db, "@Book2024::ch000::low1", 4, 0.1, "lit-low1")
    _seed_awaiting_chunk(cfg, db, "@Book2024::ch000::med1", 5, 0.5, "lit-med1")

    with (
        patch("zettel.usage.begin_run"),
        patch("zettel.usage.finish_pipeline_run"),
        patch("zettel.review._dedupe_approved_concepts"),
        patch("rich.prompt.Prompt.ask", side_effect=["a", "s", "q"]) as ask,
    ):
        stats = run_review(cfg, db, idx, interactive=True)

    # Menu, then the explicit confirm, then quit. Enter on the menu is `r`.
    assert ask.call_count == 3
    assert ask.call_args_list[0].kwargs["default"] == "r"
    assert "sem ler" in ask.call_args_list[1].args[0]
    assert ask.call_args_list[1].kwargs["default"] == "n"
    assert stats["approved"] == 1
    assert stats["skipped"] == 0
    assert stats["rejected"] == 0
    assert db.get_chunk("@Book2024::ch000::abc")["status"] == "persisted"
    assert db.get_chunk("@Book2024::ch000::low1")["status"] == "awaiting_review"
    assert db.get_chunk("@Book2024::ch000::med1")["status"] == "awaiting_review"


def test_run_review_mode_d_confirm_rejects_all(env):
    cfg, db, idx = env
    _seed_awaiting_chunk(cfg, db, "@Book2024::ch000::low1", 4, 0.1, "lit-low1")

    with (
        patch("zettel.usage.begin_run"),
        patch("zettel.usage.finish_pipeline_run"),
        patch("rich.prompt.Prompt.ask", side_effect=["d", "t", "s", "q"]),
    ):
        stats = run_review(cfg, db, idx, interactive=True)

    assert stats["rejected"] == 2
    assert stats["approved"] == 0
    assert db.get_chunk("@Book2024::ch000::abc")["status"] == "rejected"
    assert db.get_chunk("@Book2024::ch000::low1")["status"] == "rejected"


def test_run_review_mode_d_reject_band_keeps_others(env):
    cfg, db, idx = env
    # fixture chunk is 0.9 (high); add very_low + medium
    _seed_awaiting_chunk(cfg, db, "@Book2024::ch000::low1", 4, 0.1, "lit-low1")
    _seed_awaiting_chunk(cfg, db, "@Book2024::ch000::med1", 5, 0.5, "lit-med1")

    with (
        patch("zettel.usage.begin_run"),
        patch("zettel.usage.finish_pipeline_run"),
        patch("rich.prompt.Prompt.ask", side_effect=["d", "b", "s", "q"]),
    ):
        stats = run_review(cfg, db, idx, interactive=True)

    assert stats["rejected"] == 1
    assert db.get_chunk("@Book2024::ch000::low1")["status"] == "rejected"
    assert db.get_chunk("@Book2024::ch000::med1")["status"] == "awaiting_review"
    assert db.get_chunk("@Book2024::ch000::abc")["status"] == "awaiting_review"


def test_run_review_mode_r_prints_chunk_text(env):
    cfg, db, idx = env
    printed: list[str] = []

    def _capture(msg="", *args, **kwargs):
        printed.append(str(msg))

    fake_console = MagicMock()
    fake_console.print.side_effect = _capture

    with (
        patch("zettel.usage.begin_run"),
        patch("zettel.usage.finish_pipeline_run"),
        patch("zettel.review._dedupe_approved_concepts"),
        patch("rich.console.Console", return_value=fake_console),
        # r -> first card -> q leaves the one-by-one loop -> q leaves the menu
        patch("rich.prompt.Prompt.ask", side_effect=["r", "q", "q"]),
    ):
        stats = run_review(cfg, db, idx, interactive=True)

    card = "\n".join(printed)
    assert "Resumo" in card
    assert "Um resumo" in card
    assert "Trecho" in card
    assert "texto do chunk" in card
    assert stats["skipped"] == 0
    assert db.get_chunk("@Book2024::ch000::abc")["status"] == "awaiting_review"


def test_run_review_mode_d_cancel_returns_to_menu_then_quit(env):
    cfg, db, idx = env

    with (
        patch("zettel.usage.begin_run"),
        patch("zettel.usage.finish_pipeline_run"),
        patch("rich.prompt.Prompt.ask", side_effect=["d", "c", "q"]),
    ):
        stats = run_review(cfg, db, idx, interactive=True)

    assert (stats["approved"], stats["rejected"], stats["skipped"]) == (0, 0, 0)
    assert db.get_chunk("@Book2024::ch000::abc")["status"] == "awaiting_review"


def test_run_review_auto_approve_respects_threshold(env):
    cfg, db, idx = env
    _seed_awaiting_chunk(cfg, db, "@Book2024::ch000::low1", 4, 0.1, "lit-low1")

    with (
        patch("zettel.usage.begin_run"),
        patch("zettel.usage.finish_pipeline_run"),
        patch("zettel.review._dedupe_approved_concepts"),
    ):
        stats = run_review(cfg, db, idx, auto_approve=True, interactive=False)

    assert stats["approved"] == 1
    assert stats["skipped"] == 1
    assert stats["pending_review"] == 1
    assert db.get_chunk("@Book2024::ch000::low1")["status"] == "awaiting_review"


def test_run_review_without_auto_approve_leaves_drafts(env):
    cfg, db, idx = env

    with (
        patch("zettel.usage.begin_run"),
        patch("zettel.usage.finish_pipeline_run"),
    ):
        stats = run_review(cfg, db, idx, auto_approve=False, interactive=False)

    assert stats["approved"] == 0
    assert stats["pending_review"] >= 1
    assert db.get_chunk("@Book2024::ch000::abc")["status"] == "awaiting_review"


def test_purge_rejected_removes_sqlite_and_chroma(env):
    from zettel.review import purge_rejected

    cfg, db, idx = env
    reject_chunk(cfg, db, idx, "@Book2024::ch000::abc")
    _seed_awaiting_chunk(cfg, db, "@Book2024::ch000::keep", 4, 0.9, "lit-keep")

    result = purge_rejected(cfg, db, idx, compact=False)
    assert result["chunks"] == 1
    assert result["compacted"] is False
    assert db.get_chunk("@Book2024::ch000::abc") is None
    assert db.get_concepts_for_chunk("@Book2024::ch000::abc") == []
    assert db.get_chunk("@Book2024::ch000::keep") is not None
    assert deleted(idx, "chunks") == ["@Book2024::ch000::abc"]


def test_purge_keeps_the_review_event_and_its_snapshot(env):
    from zettel.review import purge_rejected

    cfg, db, idx = env
    reject_chunk(cfg, db, idx, "@Book2024::ch000::abc", mode="manual")
    purge_rejected(cfg, db, idx, compact=False)
    events = db.list_review_events()
    assert len(events) == 1
    assert events[0]["decision"] == "reject"
    assert events[0]["snapshot"]["text"] == "texto do chunk"
    assert db.get_chunk("@Book2024::ch000::abc") is None


def test_auto_approve_records_a_threshold_event_per_concept(env):
    cfg, db, idx = env
    with (
        patch("zettel.usage.begin_run"),
        patch("zettel.usage.finish_pipeline_run"),
        patch("zettel.review._dedupe_approved_concepts"),
    ):
        run_review(cfg, db, idx, auto_approve=True, interactive=False)
    events = db.list_review_events()
    assert [(event["decision"], event["mode"], event["concept_id"]) for event in events] == [
        ("approve", "threshold", "c1")
    ]


def test_manual_candidate_decision_records_one_event(env):
    from zettel.review import decide_concepts

    cfg, db, idx = env
    decide_concepts(cfg, db, idx, {"c1": "approve"}, mode="manual", surface="cli", run_id=7)
    events = db.list_review_events()
    assert len(events) == 1
    assert events[0]["mode"] == "manual"
    assert events[0]["surface"] == "cli"
    assert events[0]["concept_id"] == "c1"
    assert events[0]["run_id"] == 7


def test_band_reject_and_keep_duplicate_record_their_modes(env):
    cfg, db, idx = env
    reject_chunk(cfg, db, idx, "@Book2024::ch000::abc", mode="band")
    db.upsert_concept(
        "dup-kept",
        "@Book2024",
        "@Book2024::ch000::abc",
        candidate_json=json.dumps({"thesis": "repetida", "definition": "d"}),
        status="dedupe_pending",
    )
    from zettel.review import keep_duplicate

    keep_duplicate(db, "dup-kept")
    events = db.list_review_events()
    assert [event["decision"] for event in events] == ["reject", "keep_duplicate"]
    assert events[0]["mode"] == "band"
    assert events[1]["concept_id"] == "dup-kept"
    summary = db.review_label_summary(0.75)
    assert summary[0]["source_id"] == "@Book2024"
    assert summary[0]["reject_above"] == 1


def test_retry_rejected_requeues_only_extract_rejections(env):
    from zettel.review import requeue_extract_rejected, requeueable_extract_ids

    cfg, db, idx = env
    reject_chunk(cfg, db, idx, "@Book2024::ch000::abc")
    extract_id = "@Book2024::ch000::scan"
    db.upsert_chunk(
        extract_id,
        "@Book2024",
        "@Book2024::ch000",
        "sumario",
        "ck2",
        status="rejected",
        chunk_index=9,
        summary_json=json.dumps({"candidates": [], "rejection_reason": "sumario"}),
    )
    rows = db.get_chunks_by_status("rejected", source_id="@Book2024")
    eligible = requeueable_extract_ids(db, [row["chunk_id"] for row in rows])
    assert requeue_extract_rejected(db, eligible) == 1
    assert db.get_chunk("@Book2024::ch000::abc")["status"] == "rejected"
    assert db.get_chunk(extract_id)["status"] == "pending"
    assert [event["decision"] for event in db.list_review_events(extract_id)] == ["requeue"]


def test_delete_source_keeps_review_events(env):
    cfg, db, idx = env
    reject_chunk(cfg, db, idx, "@Book2024::ch000::abc")
    db.delete_source_cascade("@Book2024")
    events = db.list_review_events()
    assert len(events) == 1
    assert events[0]["snapshot"]["text"] == "texto do chunk"


def test_purge_rejected_empty(env):
    from zettel.review import purge_rejected

    cfg, db, idx = env
    # fixture chunk is awaiting_review, not rejected
    result = purge_rejected(cfg, db, idx)
    assert result["chunks"] == 0
    assert result["compacted"] is False
    assert db.get_chunk("@Book2024::ch000::abc") is not None


def test_state_vacuum_reclaims_freelist(tmp_path):
    db = StateDB(tmp_path / "state.db")
    # DELETE journal so page counts live in the main file (WAL hides size until checkpoint).
    db.conn.execute("PRAGMA journal_mode=DELETE")
    db.upsert_source("@S", "S", "T", ["A"], 2020, "h", "/x.pdf", "pdf")
    db.upsert_chapter("@S::ch", "@S", "Ch", "chh")
    for i in range(20):
        db.upsert_chunk(
            f"@S::ch::{i:03d}",
            "@S",
            "@S::ch",
            "texto " * 200,
            f"ck{i}",
            status="rejected",
        )
    ids = [f"@S::ch::{i:03d}" for i in range(20)]
    db.delete_chunks(ids)
    path = tmp_path / "state.db"
    before = path.stat().st_size
    freelist_before = db.conn.execute("PRAGMA freelist_count").fetchone()[0]
    assert freelist_before > 0
    db.vacuum()
    after = path.stat().st_size
    freelist_after = db.conn.execute("PRAGMA freelist_count").fetchone()[0]
    assert freelist_after == 0
    assert after < before
    db.close()


# ── Extract rejections and possible duplicates ─────────────────────────


def _seed_extract_rejected(db, chunk_id, *, candidates=()):
    db.upsert_chunk(
        chunk_id,
        "@Book2024",
        "@Book2024::ch000",
        "Referencias bibliograficas",
        f"ck-{chunk_id}",
        status="rejected",
        section_path="Referencias",
        summary_json=json.dumps(
            {
                "chunk_status": "rejected",
                "rejection_category": "structural",
                "rejection_reason": "Lista de referencias",
                "candidates": list(candidates),
            }
        ),
        review_confidence=0.1,
    )


def test_extract_rejected_chunks_excludes_reviewer_rejections(env):
    from zettel.review import extract_rejected_chunks

    cfg, db, idx = env
    _seed_extract_rejected(db, "@Book2024::ch000::refs")
    # A reviewer rejection keeps the candidates the draft had.
    reject_chunk(cfg, db, idx, "@Book2024::ch000::abc")

    assert [c["chunk_id"] for c in extract_rejected_chunks(db)] == ["@Book2024::ch000::refs"]


def test_parse_selection():
    from zettel.review import parse_selection

    assert parse_selection("t", 3) == [0, 1, 2]
    assert parse_selection("3, 1, 3, 9, x", 3) == [2, 0]
    assert parse_selection("c", 3) == []


def test_run_review_x_requeues_an_extract_rejection(env):
    cfg, db, idx = env
    _seed_extract_rejected(db, "@Book2024::ch000::refs")

    with (
        patch("zettel.usage.begin_run"),
        patch("zettel.usage.finish_pipeline_run"),
        patch("rich.prompt.Prompt.ask", side_effect=["x", "e", "q"]),
    ):
        stats = run_review(cfg, db, idx, interactive=True)

    assert stats["requeued"] == 1
    assert stats["forced"] == 0
    row = db.get_chunk("@Book2024::ch000::refs")
    assert row["status"] == "pending"
    assert "force_extract" not in json.loads(row["summary_json"])
    assert db.get_chunk("@Book2024::ch000::abc")["status"] == "awaiting_review"


def test_run_review_f_marks_force_extract(env):
    cfg, db, idx = env
    _seed_extract_rejected(db, "@Book2024::ch000::refs")

    with (
        patch("zettel.usage.begin_run"),
        patch("zettel.usage.finish_pipeline_run"),
        patch("rich.prompt.Prompt.ask", side_effect=["x", "f", "q"]),
    ):
        stats = run_review(cfg, db, idx, interactive=True)

    assert stats["forced"] == 1
    assert stats["requeued"] == 0
    row = db.get_chunk("@Book2024::ch000::refs")
    assert row["status"] == "pending"
    assert json.loads(row["summary_json"])["force_extract"] is True
    assert db.get_chunk("@Book2024::ch000::abc")["status"] == "awaiting_review"


def test_force_extract_rejected_skips_reviewer_rejections(env):
    from zettel.review import force_extract_rejected

    cfg, db, idx = env
    _seed_extract_rejected(db, "@Book2024::ch000::refs")
    reject_chunk(cfg, db, idx, "@Book2024::ch000::abc")

    assert force_extract_rejected(db, ["@Book2024::ch000::abc"]) == 0
    assert db.get_chunk("@Book2024::ch000::abc")["status"] == "rejected"
    assert force_extract_rejected(db, ["@Book2024::ch000::refs"]) == 1
    forced = db.get_chunk("@Book2024::ch000::refs")
    assert forced["status"] == "pending"
    assert json.loads(forced["summary_json"])["force_extract"] is True


def test_run_review_d_deletes_an_extract_rejection(env):
    cfg, db, idx = env
    _seed_extract_rejected(db, "@Book2024::ch000::refs")

    with (
        patch("zettel.usage.begin_run"),
        patch("zettel.usage.finish_pipeline_run"),
        patch("rich.prompt.Prompt.ask", side_effect=["x", "d", "s", "q"]),
    ):
        stats = run_review(cfg, db, idx, interactive=True)

    assert stats["discarded"] == 1
    assert stats["requeued"] == 0
    assert db.get_chunk("@Book2024::ch000::refs") is None
    assert deleted(idx, "chunks") == ["@Book2024::ch000::refs"]
    assert db.get_chunk("@Book2024::ch000::abc")["status"] == "awaiting_review"


def test_run_review_d_cancel_keeps_the_extract_rejection(env):
    cfg, db, idx = env
    _seed_extract_rejected(db, "@Book2024::ch000::refs")

    with (
        patch("zettel.usage.begin_run"),
        patch("zettel.usage.finish_pipeline_run"),
        patch("rich.prompt.Prompt.ask", side_effect=["x", "d", "n", "q"]),
    ):
        stats = run_review(cfg, db, idx, interactive=True)

    assert stats["discarded"] == 0
    assert db.get_chunk("@Book2024::ch000::refs")["status"] == "rejected"
    assert deleted(idx, "chunks") == []


def test_discard_extract_rejected_skips_reviewer_rejections(env):
    from zettel.review import discard_extract_rejected

    cfg, db, idx = env
    _seed_extract_rejected(db, "@Book2024::ch000::refs")
    reject_chunk(cfg, db, idx, "@Book2024::ch000::abc")

    removed = discard_extract_rejected(db, idx, ["@Book2024::ch000::abc", "@Book2024::ch000::refs"])

    assert removed == 1
    assert db.get_chunk("@Book2024::ch000::refs") is None
    assert db.get_chunk("@Book2024::ch000::abc")["status"] == "rejected"
    assert deleted(idx, "chunks") == ["@Book2024::ch000::refs"]


class _ProgressProbe:
    """At every progress event, how many of ``ids`` had already left ``rejected``."""

    def __init__(self, db, ids):
        self.db, self.ids, self.events = db, ids, []

    def update(self, phase, message, *, current_item=None, current_index=None, total_items=None):
        done = sum((self.db.get_chunk(cid) or {}).get("status") != "rejected" for cid in self.ids)
        self.events.append((current_index, total_items, done))


@pytest.mark.parametrize("name", ["requeue_extract_rejected", "force_extract_rejected"])
def test_batch_requeue_reports_item_k_only_after_it_moved(env, name):
    """No "k/N" before item k is done: the bar used to reach 100% first (#313)."""
    from zettel import review

    _, db, _ = env
    ids = [f"@Book2024::ch000::r{n}" for n in range(3)]
    for cid in ids:
        _seed_extract_rejected(db, cid)
    probe = _ProgressProbe(db, ids)

    assert getattr(review, name)(db, ids, observer=probe) == 3
    assert probe.events == [(1, 3, 1), (2, 3, 2), (3, 3, 3)]


def test_batch_discard_reports_no_item_count_before_the_delete(env):
    from zettel.review import discard_extract_rejected

    _, db, idx = env
    ids = [f"@Book2024::ch000::r{n}" for n in range(3)]
    for cid in ids:
        _seed_extract_rejected(db, cid)
    probe = _ProgressProbe(db, ids)

    assert discard_extract_rejected(db, idx, ids, observer=probe) == 3
    assert probe.events
    assert all(index is None for index, _, _ in probe.events)


def test_web_duplicate_batch_reports_each_decision_after_it(env):
    from zettel.web_app import JobContext, _op_review_duplicates

    _, db, _ = env
    ids = ["dup-0", "dup-1"]
    for concept_id in ids:
        db.upsert_concept(
            concept_id,
            "@Book2024",
            "@Book2024::ch000::abc",
            candidate_json=json.dumps({"thesis": concept_id, "definition": "d"}),
            status="dedupe_pending",
        )
    seen = []

    class Probe:
        def emit(self, event):
            decided = sum(db.get_concept(c)["status"] != "dedupe_pending" for c in ids)
            seen.append((event.current_index, decided))

    with patch("zettel.usage.begin_run"), patch("zettel.usage.finish_pipeline_run"):
        payload = {"action": "keep_duplicate", "concept_ids": ids}
        _op_review_duplicates(JobContext(None, db, Probe(), payload, {}, "job"))
    assert seen == [(1, 1), (2, 2)]


def test_format_extract_rejected_item_keeps_full_text():
    from zettel.review import format_extract_rejected_item

    reason = "motivo " * 40
    text = "trecho " * 120
    card = format_extract_rejected_item(
        {
            "chunk_id": "@Book2024::ch000::refs",
            "page_in_book": 12,
            "section_path": "Referencias",
            "summary_json": json.dumps(
                {"rejection_category": "structural", "rejection_reason": reason}
            ),
            "text": text,
        },
        index=2,
        total=9,
    )
    assert "[2/9] @Book2024::ch000::refs  p.12  Referencias" in card
    assert reason.strip() in card
    assert text.strip() in card
    assert "structural" in card
    assert "modelo" in card


def test_format_extract_rejected_item_names_the_filter():
    from zettel.review import format_extract_rejected_item

    card = format_extract_rejected_item(
        {
            "chunk_id": "@Book2024::ch000::long",
            "page_in_book": 8,
            "section_path": "Capitulo",
            "summary_json": json.dumps(
                {
                    "chunk_status": "accepted",
                    "rejection_category": "filtered",
                    "rejection_reason": "",
                    "candidates": [],
                    "rejected_candidates": [
                        {
                            "thesis": "TESE_FILTRO",
                            "anchor_quote": "ANCORA_FILTRO",
                            "reason": "anchor_quote_words=40 > 37 (teto tolerado)",
                        }
                    ],
                }
            ),
            "text": "trecho do filtro",
        },
        index=1,
        total=1,
    )
    assert "filtro" in card
    assert "TESE_FILTRO" in card
    assert "ANCORA_FILTRO" in card
    assert "teto tolerado" in card
    assert "modelo" not in card.split("Origem", 1)[-1].split("Tese", 1)[0]


def test_run_review_m_hides_extract_rejection_for_the_session(env):
    cfg, db, idx = env
    _seed_extract_rejected(db, "@Book2024::ch000::refs")

    with (
        patch("zettel.usage.begin_run"),
        patch("zettel.usage.finish_pipeline_run"),
        patch("rich.prompt.Prompt.ask", side_effect=["x", "m", "x", "q"]),
    ):
        stats = run_review(cfg, db, idx, interactive=True)

    assert stats["requeued"] == 0
    assert db.get_chunk("@Book2024::ch000::refs")["status"] == "rejected"


def test_run_review_p_leaves_extract_rejection_for_a_later_pass(env):
    cfg, db, idx = env
    _seed_extract_rejected(db, "@Book2024::ch000::refs")

    with (
        patch("zettel.usage.begin_run"),
        patch("zettel.usage.finish_pipeline_run"),
        patch("rich.prompt.Prompt.ask", side_effect=["x", "p", "x", "e", "q"]),
    ):
        stats = run_review(cfg, db, idx, interactive=True)

    assert stats["requeued"] == 1
    assert db.get_chunk("@Book2024::ch000::refs")["status"] == "pending"


def test_requeueable_extract_ids_skips_reviewer_rejections(env):
    from zettel.review import requeueable_extract_ids

    cfg, db, idx = env
    _seed_extract_rejected(db, "@Book2024::ch000::refs")
    reject_chunk(cfg, db, idx, "@Book2024::ch000::abc")

    assert requeueable_extract_ids(
        db, ["@Book2024::ch000::abc", "@Book2024::ch000::refs", "missing"]
    ) == ["@Book2024::ch000::refs"]


def test_non_interactive_review_leaves_extract_rejections(env):
    cfg, db, idx = env
    _seed_extract_rejected(db, "@Book2024::ch000::refs")

    with (
        patch("zettel.usage.begin_run"),
        patch("zettel.usage.finish_pipeline_run"),
        patch("zettel.review._dedupe_approved_concepts"),
    ):
        stats = run_review(cfg, db, idx, auto_approve=True, interactive=False)

    assert stats["requeued"] == 0
    assert db.get_chunk("@Book2024::ch000::refs")["status"] == "rejected"


def _seed_pending_duplicate(db):
    db.upsert_concept(
        "dup1",
        "@Book2024",
        "@Book2024::ch000::abc",
        candidate_json=json.dumps({"thesis": "Series temporais", "definition": "d"}),
        status="extracted",
    )
    db.set_concept_dedupe("dup1", "dedupe_pending", {"reason": "repetido", "duplicate_of": "c1"})


@pytest.mark.parametrize(
    ("answer", "status", "override"),
    [("m", "approved", True), ("d", "duplicate", None), ("p", "dedupe_pending", None)],
)
def test_run_review_asks_about_possible_duplicates(env, answer, status, override):
    cfg, db, idx = env
    _seed_pending_duplicate(db)

    with (
        patch("zettel.usage.begin_run"),
        patch("zettel.usage.finish_pipeline_run"),
        patch("rich.prompt.Prompt.ask", side_effect=[answer, "q"]),
    ):
        stats = run_review(cfg, db, idx, interactive=True)

    concept = db.get_concept("dup1")
    assert concept["status"] == status
    assert json.loads(concept["dedupe_json"]).get("override") is override
    assert stats["dedupe_pending"] == (1 if answer == "p" else 0)


def test_non_interactive_review_never_drops_a_possible_duplicate(env):
    cfg, db, idx = env
    _seed_pending_duplicate(db)

    with (
        patch("zettel.usage.begin_run"),
        patch("zettel.usage.finish_pipeline_run"),
        patch("zettel.review._dedupe_approved_concepts"),
    ):
        stats = run_review(cfg, db, idx, auto_approve=True, interactive=False)

    assert stats["dedupe_pending"] == 1
    assert db.get_concept("dup1")["status"] == "dedupe_pending"


def test_format_dedupe_item_names_the_competing_candidate(env):
    from zettel.review import format_dedupe_item

    _, db, _ = env
    _seed_pending_duplicate(db)
    card = format_dedupe_item(db, db.get_concept("dup1"))
    assert "Tese nova: Series temporais" in card
    assert "Candidato do mesmo lote: t" in card
    assert "Motivo: repetido" in card


def test_format_dedupe_item_names_source_and_target_note(env):
    from zettel.review import format_dedupe_item

    _, db, _ = env
    db.upsert_note("01NOTA", "@Book2024", None, title="Nota antiga")
    db.upsert_concept(
        "dup2",
        "@Book2024",
        "@Book2024::ch000::abc",
        candidate_json=json.dumps({"thesis": "T", "definition": "d"}),
        status="extracted",
    )
    db.set_concept_dedupe("dup2", "dedupe_pending", {"reason": "r", "target_note_id": "01NOTA"})
    card = format_dedupe_item(db, db.get_concept("dup2"))
    assert card.startswith("Fonte: @Book2024 - Livro Teste")
    assert "Nota existente: Nota antiga (01NOTA)" in card


def test_format_dedupe_item_lists_compared_notes_when_llm_named_no_target(env):
    from zettel.review import format_dedupe_item

    _, db, _ = env
    db.upsert_note("01A", "@Book2024", None, title="Nota A")
    db.upsert_note("01B", "@Book2024", None, title="Nota B")
    db.upsert_concept(
        "dup3",
        "@Book2024",
        "@Book2024::ch000::abc",
        candidate_json=json.dumps({"thesis": "T", "definition": "d"}),
        status="extracted",
    )
    db.set_concept_dedupe(
        "dup3",
        "dedupe_pending",
        {"reason": "r", "target_note_id": None, "compared_note_ids": ["01A", "01B"]},
    )
    card = format_dedupe_item(db, db.get_concept("dup3"))
    assert "nao indicada pelo LLM" in card
    assert "  - Nota A (01A)" in card and "  - Nota B (01B)" in card


def test_approve_lists_the_lit_once_in_the_chapter_map(env):
    from zettel.vault import literature_index_filename, read_managed_block

    cfg, db, idx = env
    assert approve_chunk(cfg, db, idx, "@Book2024::ch000::abc")

    index = cfg.vault_path / "20_Literature" / literature_index_filename("Book2024", "Livro Teste")
    text = index.read_text(encoding="utf-8")
    assert "Notas de Literatura aprovadas" not in text
    block = read_managed_block(text, "auto-chapter-map")
    assert "### Ch1" in block
    assert "**Notas de literatura**" in block
    assert block.count("- [[Book2024/") == 1


def test_approved_lit_lists_the_permanent_notes_it_produced(env):
    from zettel.review import LIT_PERMANENT_BLOCK, sync_lit_permanent_links
    from zettel.vault import read_managed_block

    cfg, db, idx = env
    chunk_id = "@Book2024::ch000::abc"
    assert approve_chunk(cfg, db, idx, chunk_id)
    lit = Path(db.get_chunk(chunk_id)["literature_note_path"])
    # Approval scaffolds the section with a placeholder.
    text = lit.read_text(encoding="utf-8")
    assert "## Notas permanentes geradas" in text
    assert "Nenhuma nota permanente" in read_managed_block(text, LIT_PERMANENT_BLOCK)

    ztl = cfg.vault_path / "30_Permanent" / "ZTL - 01HNOTE - serie-temporal.md"
    db.upsert_note("01HNOTE", "@Book2024", str(ztl), title="Serie temporal")
    db.upsert_concept("c1", "@Book2024", chunk_id, note_id="01HNOTE", status="noted")
    sync_lit_permanent_links(cfg, db, [chunk_id])
    sync_lit_permanent_links(cfg, db, [chunk_id])  # idempotent: one section, one link

    text = lit.read_text(encoding="utf-8")
    assert text.count("## Notas permanentes geradas") == 1
    assert read_managed_block(text, LIT_PERMANENT_BLOCK) == "- [[ZTL - 01HNOTE - serie-temporal]]"


def test_lit_with_a_stray_end_marker_is_left_alone(env):
    """A LIT whose block lost its start marker is refused, not scaffolded over (#321)."""
    from zettel.review import LIT_PERMANENT_BLOCK, sync_lit_permanent_links

    cfg, db, idx = env
    chunk_id = "@Book2024::ch000::abc"
    assert approve_chunk(cfg, db, idx, chunk_id)
    lit = Path(db.get_chunk(chunk_id)["literature_note_path"])
    broken = (
        lit.read_text(encoding="utf-8").replace(f"<!-- zettel:{LIT_PERMANENT_BLOCK}:start -->", "")
        + "\nTexto manual do autor.\n"
    )
    lit.write_text(broken, encoding="utf-8")

    sync_lit_permanent_links(cfg, db, [chunk_id])

    assert lit.read_text(encoding="utf-8") == broken


def test_lit_links_skip_drafts_still_in_review(env):
    from zettel.review import sync_lit_permanent_links

    cfg, db, _ = env
    chunk_id = "@Book2024::ch000::abc"
    draft = Path(db.get_chunk(chunk_id)["literature_note_path"])
    before = draft.read_text(encoding="utf-8")
    sync_lit_permanent_links(cfg, db, [chunk_id])
    assert draft.read_text(encoding="utf-8") == before


def test_run_all_leaves_drafts_unless_auto_approve(monkeypatch):
    from zettel.cli.pipeline import _run_all_phases

    seen: dict[str, bool] = {}

    def harvest(*_a, **_k):
        return type("H", (), {"source_ids": [], "skipped": []})()

    def review(*_a, **kwargs):
        seen.clear()
        seen.update(kwargs)
        return {"approved": 0, "rejected": 0, "skipped": 3, "pending_review": 3}

    class Summary:
        chapters_summarized = 0
        chapters_skipped = 0
        sources_summarized = 0

    monkeypatch.setattr("zettel.harvester.run_harvest", harvest)
    monkeypatch.setattr("zettel.extractor.run_extract", lambda *_a, **_k: [])
    monkeypatch.setattr("zettel.review.run_review", review)
    monkeypatch.setattr("zettel.summarize.generate_summaries", lambda *_a, **_k: Summary())
    monkeypatch.setattr(
        "zettel.connector.load_approved_candidates", lambda _db, source_ids=None: []
    )
    monkeypatch.setattr(
        "zettel.connector.run_connect",
        lambda *_a, **_k: type("O", (), {"note_ids": []})(),
    )
    monkeypatch.setattr("zettel.gardener.run_garden", lambda *_a, **_k: [])
    monkeypatch.setattr("zettel.cli.pipeline.print_cost_by_phase", lambda *_a, **_k: None)

    db = type("DB", (), {"get_last_run": lambda self: None})()
    _run_all_phases(
        object(),
        db,
        object(),
        interactive=False,
        duplicate_action="skip",
        skip_biblio=False,
        yes=True,
        dry_run=False,
        auto_approve=False,
    )
    assert seen["auto_approve"] is False
    assert seen["interactive"] is False
    assert seen["source_ids"] == []
    assert seen["surface"] == "cli"
    assert seen["event_mode"] is None

    _run_all_phases(
        object(),
        db,
        object(),
        interactive=False,
        duplicate_action="skip",
        skip_biblio=False,
        yes=True,
        dry_run=False,
        auto_approve=True,
    )
    assert seen["auto_approve"] is True
    assert seen["interactive"] is False
    assert seen["source_ids"] == []
    assert seen["event_mode"] == "run_all"


def test_run_review_source_ids_does_not_approve_another_source(env):
    cfg, db, idx = env
    db.upsert_source("@Old2020", "Old2020", "Antigo", ["Autor"], 2020, "h", "/y.pdf", "pdf")
    db.upsert_chapter("@Old2020::ch000", "@Old2020", "Ch", "ch")
    db.upsert_chunk(
        "@Old2020::ch000::abc",
        "@Old2020",
        "@Old2020::ch000",
        "texto antigo",
        "ck2",
        chunk_index=0,
        status="awaiting_review",
        review_confidence=0.95,
    )

    with (
        patch("zettel.usage.begin_run"),
        patch("zettel.usage.finish_pipeline_run"),
        patch("zettel.review._dedupe_approved_concepts") as dedupe,
    ):
        stats = run_review(
            cfg,
            db,
            idx,
            source_ids=["@Book2024"],
            auto_approve=True,
            interactive=False,
        )

    assert stats["approved_threshold"] == 1
    assert db.get_chunk("@Book2024::ch000::abc")["status"] == "persisted"
    assert db.get_chunk("@Old2020::ch000::abc")["status"] == "awaiting_review"
    assert [item.args[3] for item in dedupe.call_args_list] == ["@Book2024"]


def test_run_review_empty_source_ids_approves_nothing(env):
    cfg, db, idx = env

    with (
        patch("zettel.usage.begin_run"),
        patch("zettel.usage.finish_pipeline_run"),
        patch("zettel.review._dedupe_approved_concepts") as dedupe,
    ):
        stats = run_review(cfg, db, idx, source_ids=[], auto_approve=True, interactive=False)

    assert stats["approved"] == 0
    assert stats["approved_threshold"] == 0
    assert db.get_chunk("@Book2024::ch000::abc")["status"] == "awaiting_review"
    dedupe.assert_not_called()


def test_auto_approve_refreshes_the_literature_index_once_per_source(env, monkeypatch):
    cfg, db, idx = env
    db.upsert_chunk(
        "@Book2024::ch000::second",
        "@Book2024",
        "@Book2024::ch000",
        "outro trecho do mesmo livro",
        "ck2",
        chunk_index=4,
        status="awaiting_review",
        review_confidence=0.95,
        summary_json="{}",
    )
    db.upsert_source("@Old2020", "Old2020", "Antigo", ["Autor"], 2020, "h", "/y.pdf", "pdf")
    db.upsert_chapter("@Old2020::ch000", "@Old2020", "Ch", "ch")
    db.upsert_chunk(
        "@Old2020::ch000::abc",
        "@Old2020",
        "@Old2020::ch000",
        "texto antigo",
        "ck3",
        chunk_index=0,
        status="awaiting_review",
        review_confidence=0.95,
        summary_json="{}",
    )
    refreshed: list[str] = []
    monkeypatch.setattr(
        "zettel.review._refresh_literature_index",
        lambda _cfg, _db, source_id: refreshed.append(source_id),
    )
    monkeypatch.setattr("zettel.review.sync_lit_permanent_links", lambda *_a, **_k: None)

    with (
        patch("zettel.usage.begin_run"),
        patch("zettel.usage.finish_pipeline_run"),
        patch("zettel.review._dedupe_approved_concepts"),
    ):
        stats = run_review(cfg, db, idx, auto_approve=True, interactive=False)

    assert stats["approved"] == 3
    assert sorted(refreshed) == ["@Book2024", "@Old2020"]
    assert len(refreshed) == 2


def test_approve_chunk_keeps_the_draft_when_sqlite_update_fails(env, monkeypatch):
    cfg, db, idx = env
    chunk_id = "@Book2024::ch000::abc"
    draft = Path(db.get_chunk(chunk_id)["literature_note_path"])

    def fail(*_a, **_k):
        raise RuntimeError("sqlite")

    monkeypatch.setattr(db, "update_chunk_review", fail)
    with pytest.raises(RuntimeError, match="sqlite"):
        approve_chunk(cfg, db, idx, chunk_id, approved_candidates=[])

    assert draft.is_file()
    assert db.get_chunk(chunk_id)["status"] == "awaiting_review"


def test_approve_chunk_deletes_the_draft_after_sqlite(env, monkeypatch):
    cfg, db, idx = env
    chunk_id = "@Book2024::ch000::abc"
    draft = Path(db.get_chunk(chunk_id)["literature_note_path"])
    order: list[str] = []
    real_update = db.update_chunk_review

    def tracked_update(*args, **kwargs):
        order.append("sqlite")
        return real_update(*args, **kwargs)

    real_unlink = Path.unlink

    def tracked_unlink(self, *args, **kwargs):
        order.append("unlink")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(db, "update_chunk_review", tracked_update)
    monkeypatch.setattr(Path, "unlink", tracked_unlink)
    monkeypatch.setattr("zettel.review.refresh_approved_literature", lambda *_a, **_k: None)

    assert approve_chunk(cfg, db, idx, chunk_id, approved_candidates=[])
    assert order.index("sqlite") < order.index("unlink")
    assert not draft.is_file()
    assert db.get_chunk(chunk_id)["status"] == "persisted"


def test_reject_chunk_counts_a_draft_left_on_disk(env, monkeypatch):
    from zettel.review import rejected_drafts_on_disk, review_followups

    cfg, db, idx = env
    chunk_id = "@Book2024::ch000::abc"
    draft = Path(db.get_chunk(chunk_id)["literature_note_path"])

    def refuse(self, *args, **kwargs):
        raise OSError("locked")

    monkeypatch.setattr(Path, "unlink", refuse)
    stats: dict[str, int] = {}
    assert reject_chunk(cfg, db, idx, chunk_id, stats=stats)
    assert stats["draft_delete_failures"] == 1
    assert draft.is_file()
    assert db.get_chunk(chunk_id)["status"] == "rejected"
    assert [row["chunk_id"] for row in rejected_drafts_on_disk(db)] == [chunk_id]
    assert "continuam no disco" in review_followups(stats)[0]
