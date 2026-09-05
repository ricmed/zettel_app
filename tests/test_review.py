"""Tests for granular literature review approve/reject."""

import json
from unittest.mock import MagicMock, patch

import pytest
from zettel.config import AppConfig
from zettel.review import (
    BAND_HIGH,
    BAND_MEDIUM,
    BAND_VERY_LOW,
    _literature_embed_text,
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


class _FakeLitIndex:
    def __init__(self):
        self.upserts = []
        self.deletes = []
        self.chunk_deletes = []

    def upsert_literature_note(self, lit_id, text, meta):
        self.upserts.append((lit_id, text, meta))

    def delete_literature_notes(self, ids):
        self.deletes.extend(ids)

    def delete_chunks(self, chunk_ids):
        self.chunk_deletes.extend(chunk_ids)


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
    idx = _FakeLitIndex()
    yield cfg, db, idx
    db.close()


def test_approve_moves_draft_and_embeds(env):
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
    embed = _literature_embed_text(dest)
    assert "texto do chunk" not in embed
    assert "Um resumo" in embed
    assert not (
        cfg.vault_path / "00_Inbox" / "Review" / literature_source_dirname("Book2024") / fname
    ).exists()
    assert len(idx.upserts) == 1
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
    idx = _FakeLitIndex()

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
    idx = _FakeLitIndex()
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


def test_ask_review_decision_default_approve_when_high_conf():
    console = MagicMock()
    with patch("rich.prompt.Prompt.ask", return_value="a") as ask:
        choice = ask_review_decision(console, conf=0.9, limiar=0.85)
    assert choice == "aprovar"
    assert ask.call_args.kwargs["default"] == "a"


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
        patch("rich.prompt.Prompt.ask", return_value="a"),
    ):
        stats = run_review(cfg, db, idx, interactive=True)

    assert stats["approved"] == 1
    assert stats["skipped"] == 2
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
        patch("rich.prompt.Prompt.ask", side_effect=["d", "t", "s"]),
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
        patch("rich.prompt.Prompt.ask", side_effect=["r", "q"]),
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

    assert stats == {"approved": 0, "rejected": 0, "skipped": 0}
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
    assert db.get_chunk("@Book2024::ch000::low1")["status"] == "awaiting_review"


def test_purge_rejected_removes_sqlite_and_chroma(env):
    from zettel.review import purge_rejected

    cfg, db, idx = env
    reject_chunk(cfg, db, idx, "@Book2024::ch000::abc")
    _seed_awaiting_chunk(cfg, db, "@Book2024::ch000::keep", 4, 0.9, "lit-keep")

    result = purge_rejected(cfg, db, idx, compact=False)
    assert result["chunks"] == 1
    assert result["literature_notes"] == 1
    assert result["compacted"] is False
    assert db.get_chunk("@Book2024::ch000::abc") is None
    assert db.get_concepts_for_chunk("@Book2024::ch000::abc") == []
    assert db.get_chunk("@Book2024::ch000::keep") is not None
    assert "lit123" in idx.deletes
    assert idx.chunk_deletes == ["@Book2024::ch000::abc"]


def test_purge_rejected_empty(env):
    from zettel.review import purge_rejected

    cfg, db, idx = env
    # fixture chunk is awaiting_review, not rejected
    result = purge_rejected(cfg, db, idx)
    assert result["chunks"] == 0
    assert result["literature_notes"] == 0
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
