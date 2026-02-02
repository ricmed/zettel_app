"""Tests for SQLite state management."""

import tempfile
from pathlib import Path

import pytest

from zettel.state import StateDB


@pytest.fixture
def db(tmp_path):
    db = StateDB(tmp_path / "test.db")
    yield db
    db.close()


def test_upsert_and_get_file(db):
    db.upsert_file("/test/file.pdf", "abc123", "pdf", "@TestSource")
    record = db.get_file("/test/file.pdf")
    assert record is not None
    assert record["file_checksum"] == "abc123"
    assert record["source_id"] == "@TestSource"


def test_upsert_and_get_source(db):
    db.upsert_source(
        source_id="@Test2024",
        citekey="Test2024",
        title="Test Document",
        authors=["Author One"],
        year=2024,
        file_checksum="hash123",
        origin_path="/path/to/file.pdf",
        origin_type="pdf",
    )
    source = db.get_source("@Test2024")
    assert source is not None
    assert source["title"] == "Test Document"
    assert source["citekey"] == "Test2024"


def test_upsert_chunk_and_get_pending(db):
    db.upsert_source("@S", "S", "Source", [], None, "h", "/p", "md")
    db.upsert_chapter("@S::ch000", "@S", "Ch1", "ch_hash")
    db.upsert_chunk("@S::ch000::abc", "@S", "@S::ch000", "text here", "ck_hash")

    pending = db.get_pending_chunks()
    assert len(pending) == 1
    assert pending[0]["chunk_id"] == "@S::ch000::abc"

    db.update_chunk_status("@S::ch000::abc", "extracted")
    pending = db.get_pending_chunks()
    assert len(pending) == 0


def test_upsert_note(db):
    db.upsert_note("note1", "@S", "/path/note.md", "Test Note", "sem_hash")
    note = db.get_note("note1")
    assert note is not None
    assert note["title"] == "Test Note"


def test_llm_cache(db):
    db.cache_llm_response("call1", '{"prompt": "test"}', '{"result": "ok"}')
    cached = db.get_cached_llm_response("call1")
    assert cached == '{"result": "ok"}'

    assert db.get_cached_llm_response("nonexistent") is None


def test_stats(db):
    stats = db.get_stats()
    assert "files" in stats
    assert "chunks_pending" in stats
    assert all(v == 0 for v in stats.values())
