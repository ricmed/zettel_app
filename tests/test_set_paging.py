"""Tests for run_set_paging repair command."""

from __future__ import annotations

from pathlib import Path

from zettel.config import AppConfig, LiteratureReviewConfig
from zettel.harvester import run_set_paging
from zettel.state import StateDB
from zettel.vault import (
    draft_chunk_filename,
    literature_chunk_dirname,
    safe_write_note,
)


class _FakeIdx:
    def delete_chunks(self, ids):
        self.deleted = list(ids)

    def __init__(self):
        self.deleted = []


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
        cfg.vault_path / "00_Inbox" / "Review" / "@TestBook",
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
    draft = (
        cfg.vault_path
        / "00_Inbox"
        / "Review"
        / literature_chunk_dirname("TestBook")
        / draft_chunk_filename(1)
    )
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
        status="awaiting_review",
        literature_note_path=str(draft),
    )

    idx = _FakeIdx()
    stats = run_set_paging(
        cfg, db, idx, "@TestBook",
        content_start_file=35,
        content_start_book=10,
    )
    assert stats["dropped_pending"] == 1
    assert stats["updated"] == 1
    assert db.get_chunk("@TestBook::ch000::aaaa") is None
    kept = db.get_chunk("@TestBook::ch001::bbbb")
    assert kept["page_in_book"] == 15  # 40 - 35 + 10
    src = db.get_source("@TestBook")
    assert src["content_start_file_page"] == 35
    assert src["content_start_book_page"] == 10
    assert src["page_offset"] == 25
    db.close()
