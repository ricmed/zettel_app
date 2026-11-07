"""Tests for run_set_paging repair command."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from zettel.config import AppConfig, LiteratureReviewConfig
from zettel.harvester import run_set_paging
from zettel.state import StateDB
from zettel.vault import (
    build_permanent_note_body,
    frontmatter_json,
    literature_chunk_filename,
    literature_chunk_filename_for_row,
    literature_source_dirname,
    parse_frontmatter,
    safe_write_note,
)

from tests.fakes import FakeVectorIndex


def test_run_set_paging_updates_book_and_drops_pending(tmp_path: Path):
    cfg = AppConfig(
        vault_path=tmp_path / "vault",
        inbox_path=tmp_path / "inbox",
        chroma_path=tmp_path / "chroma",
        state_db_path=tmp_path / "state.db",
        cache_path=tmp_path / "cache",
        prompts_path=tmp_path / "prompts",
        literature_review=LiteratureReviewConfig(drafts_subdir="00_Inbox/Review"),
    )
    for d in (
        cfg.vault_path / "10_Sources",
        cfg.vault_path / "20_Literature",
        cfg.vault_path / "00_Inbox" / "Review" / "TestBook",
    ):
        d.mkdir(parents=True)

    db = StateDB(cfg.state_db_path)
    db.upsert_source(
        source_id="@TestBook",
        citekey="TestBook",
        title="Test Book",
        authors=["Author"],
        year=2024,
        file_checksum="abc",
        origin_path="book.pdf",
        origin_type="pdf",
        total_pages_file=100,
    )
    db.upsert_chapter("@TestBook::ch000", "@TestBook", "Front", "h0", "Front")
    db.upsert_chapter("@TestBook::ch001", "@TestBook", "Ch1", "h1", "Ch1")
    # Front matter pending — should be dropped
    db.upsert_chunk(
        "@TestBook::ch000::aaaa",
        "@TestBook",
        "@TestBook::ch000",
        "front",
        "ck1",
        chunk_index=0,
        page_in_file=10,
        page_in_book=10,
        status="pending",
    )
    # Content awaiting_review — pages updated, kept
    fname = literature_chunk_filename(
        "TestBook",
        chunk_index=1,
        page_in_file=40,
        page_in_book=40,
    )
    draft = cfg.vault_path / "00_Inbox" / "Review" / literature_source_dirname("TestBook") / fname
    safe_write_note(
        draft,
        {
            "type": "literature",
            "page_in_file": 40,
            "page_in_book": 40,
            "chunk_index": 1,
        },
        "body",
    )
    db.upsert_chunk(
        "@TestBook::ch001::bbbb",
        "@TestBook",
        "@TestBook::ch001",
        "content",
        "ck2",
        chunk_index=1,
        page_in_file=40,
        page_in_book=40,
        page_confidence="fuzzy",
        status="awaiting_review",
        literature_note_path=str(draft),
    )

    idx = FakeVectorIndex()
    stats = run_set_paging(
        cfg,
        db,
        idx,
        "@TestBook",
        content_start_file=35,
        content_start_book=10,
    )
    assert stats["dropped_pending"] == 1
    assert stats["updated"] == 1
    assert db.get_chunk("@TestBook::ch000::aaaa") is None
    kept = db.get_chunk("@TestBook::ch001::bbbb")
    assert kept["page_in_book"] == 15  # 40 - 35 + 10
    assert kept["page_confidence"] == "fuzzy"
    renamed = literature_chunk_filename(
        "TestBook",
        chunk_index=1,
        page_in_file=40,
        page_in_book=15,
    )
    new_path = draft.parent / renamed
    assert new_path.exists()
    assert kept["literature_note_path"] == str(new_path)
    src = db.get_source("@TestBook")
    assert src["content_start_file_page"] == 35
    assert src["content_start_book_page"] == 10
    assert src["page_offset"] == 25
    db.close()


def test_run_set_paging_refuses_a_source_without_pages(tmp_path: Path):
    """Native Markdown has no pages (ADR-013): repairing its paging is refused, not faked (#330).

    It used to report the chunks as updated and store a meaningless page offset.
    """
    db = StateDB(tmp_path / "s.db")
    try:
        db.upsert_source("@Md2024", "Md2024", "T", ["A"], 2024, "h", "/x.md", "md")
        before = db.get_source("@Md2024")
        with pytest.raises(ValueError, match="paginas"):
            run_set_paging(AppConfig(), db, FakeVectorIndex(), "@Md2024", content_start_file=2)
        after = db.get_source("@Md2024")
        assert after["page_offset"] == before["page_offset"]
        assert after["content_start_file_page"] == before["content_start_file_page"]
    finally:
        db.close()


def test_set_paging_from_pdf_rewrites_citation_without_touching_hashes(tmp_path: Path):
    """Injected candidates stand in for the PDF text layer. No LLM, no re-embed."""
    cfg = AppConfig(
        vault_path=tmp_path / "vault",
        inbox_path=tmp_path / "inbox",
        chroma_path=tmp_path / "chroma",
        state_db_path=tmp_path / "state.db",
        cache_path=tmp_path / "cache",
        prompts_path=tmp_path / "prompts",
        literature_review=LiteratureReviewConfig(drafts_subdir="00_Inbox/Review"),
    )
    for folder in ("10_Sources", "20_Literature", "30_Permanent"):
        (cfg.vault_path / folder).mkdir(parents=True)
    db = StateDB(cfg.state_db_path)
    db.upsert_source(
        source_id="@TestBook",
        citekey="TestBook",
        title="Test Book",
        authors=["Author Name"],
        year=2024,
        file_checksum="abc",
        origin_path="book.pdf",
        origin_type="pdf",
        total_pages_file=30,
        content_start_file_page=25,
        content_start_book_page=3,
        page_offset=22,
        page_offset_confidence="confirmed",
    )
    db.upsert_chapter("@TestBook::ch001", "@TestBook", "Ch1", "h1", "Ch1")
    chunk_id = "@TestBook::ch001::bbbb"
    chunk = {
        "chunk_id": chunk_id,
        "chunk_index": 1,
        "page_in_file": 30,
        "page_in_book": 8,
        "section_path": "Capitulo",
    }
    old_name = literature_chunk_filename_for_row("TestBook", chunk)
    lit_dir = cfg.vault_path / "20_Literature" / literature_source_dirname("TestBook")
    lit_dir.mkdir(parents=True)
    lit_path = lit_dir / old_name
    safe_write_note(
        lit_path,
        {"type": "literature", "page_in_file": 30, "page_in_book": 8, "chunk_index": 1},
        "corpo da literatura",
    )
    db.upsert_chunk(
        chunk_id,
        "@TestBook",
        "@TestBook::ch001",
        "content",
        "ck2",
        chunk_index=1,
        page_in_file=30,
        page_in_book=8,
        section_path="Capitulo",
        status="approved",
        literature_note_path=str(lit_path),
    )
    stem = lit_path.stem
    body = build_permanent_note_body(
        thesis="Tese sobre o conceito.",
        definition="Definicao que o embedding continua a ler.",
        literature_ref=f"[[{stem}|p. 8]]",
        source_locator="p.8 - capitulo",
        page=8,
        citation="(NAME, 2024, p. 8)",
        anchor_quote="ancora que nao esta no texto extraido",
    )
    ztl = cfg.vault_path / "30_Permanent" / "ZTL - N1 - conceito.md"
    meta = {
        "type": "permanent",
        "page": 8,
        "citation_page_confidence": "chunk",
        "chunk_id": chunk_id,
        "origin": "pipeline",
    }
    safe_write_note(ztl, meta, body)
    db.upsert_note(
        "N1",
        "@TestBook",
        str(ztl),
        title="Conceito",
        note_semantic_checksum="keep-me",
        body=body,
        frontmatter_json=frontmatter_json(meta),
        origin="pipeline",
        provenance_json=json.dumps(
            {
                "citation": "(NAME, 2024, p. 8)",
                "anchor_quote": "ancora que nao esta no texto extraido",
            }
        ),
    )
    db.update_note_embedding("N1", "emb-hash", "fake")
    candidates: list[list[int]] = [[] for _ in range(30)]
    candidates[24] = [3]
    candidates[25] = [5]
    candidates[26] = [6]
    candidates[27] = [8]
    candidates[28] = [9]
    candidates[29] = [10]
    try:
        stats = run_set_paging(
            cfg,
            db,
            FakeVectorIndex(),
            "@TestBook",
            from_pdf=True,
            printed_candidates=candidates,
        )
        assert stats["permanent_patched"] == 1
        kept = db.get_chunk(chunk_id)
        assert kept["page_in_book"] == 10
        assert kept["page_in_file"] == 30
        new_name = literature_chunk_filename_for_row("TestBook", kept)
        assert new_name != old_name
        assert (lit_dir / new_name).exists()
        assert "p010" in new_name
        note = db.get_note("N1")
        assert note["note_semantic_checksum"] == "keep-me"
        assert note["embedding_input_hash"] == "emb-hash"
        assert "p. 10" in note["provenance_json"]
        written_meta, written_body = parse_frontmatter(ztl.read_text(encoding="utf-8"))
        assert written_meta["page"] == 10
        assert "- Página: 10" in written_body
        assert "p.10" in written_body
        assert new_name[:-3] in written_body
        stored = db.get_printed_pages("@TestBook")
        assert stored is not None
        assert stored[29] == 10
        src = db.get_source("@TestBook")
        assert src["content_start_file_page"] == 25
        assert src["content_start_book_page"] == 3
        assert src["total_pages_book"] == 10
        with pytest.raises(ValueError, match="serie"):
            run_set_paging(
                cfg,
                db,
                FakeVectorIndex(),
                "@TestBook",
                from_pdf=True,
                printed_candidates=[[8], [100], [3]],
            )
    finally:
        db.close()
