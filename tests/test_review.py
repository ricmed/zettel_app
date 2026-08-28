"""Tests for granular literature review approve/reject."""

import json

import pytest

from zettel.config import AppConfig
from zettel.review import _literature_embed_text, approve_chunk, reject_chunk
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

    def upsert_literature_note(self, lit_id, text, meta):
        self.upserts.append((lit_id, text, meta))

    def delete_literature_notes(self, ids):
        self.deletes.extend(ids)


@pytest.fixture
def env(tmp_path):
    cfg = AppConfig(
        vault_path=tmp_path / "vault",
        cache_path=tmp_path / "cache",
        state_db_path=tmp_path / "state.db",
        chroma_path=tmp_path / "chroma",
    )
    for d in ("00_Inbox/Review", "10_Sources", "20_Literature", "30_Permanent", "90_Assets"):
        (cfg.vault_path / d).mkdir(parents=True, exist_ok=True)
    db = StateDB(cfg.state_db_path)
    db.upsert_source(
        "@Book2024", "Book2024", "Livro Teste", ["Autor"], 2024,
        "h", "/x.pdf", "pdf",
    )
    db.upsert_chapter("@Book2024::ch000", "@Book2024", "Ch1", "chh")
    db.upsert_chunk(
        "@Book2024::ch000::abc", "@Book2024", "@Book2024::ch000",
        "texto do chunk", "ck",
        chunk_index=3, page_in_file=20, page_in_book=10, page_confidence="inferred",
        status="awaiting_review",
        section_path="Ch1 > Intro",
        literature_id="lit123",
        summary_json=json.dumps({
            "summary": "Um resumo",
            "key_concepts": ["conceito"],
            "candidates": [{"thesis": "Tese X", "definition": "def " * 5,
                            "anchor_quote": "citacao ancora de dez palavras aqui sim",
                            "relevance_score": 4, "chunk_status": "ok",
                            "rejection_reason": "", "rejection_category": ""}],
        }),
        review_confidence=0.9,
    )
    chunk_row = db.get_chunk("@Book2024::ch000::abc")
    fname = literature_chunk_filename_for_row("Book2024", chunk_row)
    draft_dir = (
        cfg.vault_path / "00_Inbox" / "Review" / literature_source_dirname("Book2024")
    )
    draft_dir.mkdir(parents=True, exist_ok=True)
    meta, body = build_literature_chunk_note(
        source_id="@Book2024", citekey="Book2024", title="Livro Teste",
        chunk_id="@Book2024::ch000::abc", chunk_index=3, literature_id="lit123",
        summary="Um resumo", key_concepts=["conceito"], candidates=[],
        section_path="Ch1 > Intro",
        page_in_file=20, page_in_book=10, status="awaiting_review",
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
        "c1", "@Book2024", "@Book2024::ch000::abc",
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
    dest = (
        cfg.vault_path / "20_Literature" / literature_source_dirname("Book2024") / fname
    )
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
