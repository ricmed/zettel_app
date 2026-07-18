"""Tests for SQLite state management."""

import sqlite3
import tempfile
from pathlib import Path

import pytest

from zettel.state import StateDB


@pytest.fixture
def db(tmp_path):
    db = StateDB(tmp_path / "test.db")
    yield db
    db.close()


# Schema of a pre-Fase-0 database (before retention columns / assets table existed).
_OLD_SCHEMA_SQL = """
CREATE TABLE sources (source_id TEXT PRIMARY KEY, citekey TEXT NOT NULL UNIQUE, title TEXT,
    authors TEXT, year INTEGER, file_checksum TEXT NOT NULL, extraction_checksum TEXT,
    origin_path TEXT NOT NULL, origin_type TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE chunks (chunk_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, chapter_id TEXT NOT NULL,
    text TEXT NOT NULL, chunk_checksum TEXT NOT NULL, locator TEXT DEFAULT '', status TEXT DEFAULT 'pending',
    llm_prompt1_hash TEXT, llm_call_checksum_prompt1 TEXT);
CREATE TABLE concepts (concept_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, chunk_id TEXT NOT NULL,
    anchor_hash TEXT DEFAULT '', thesis_hash TEXT DEFAULT '', note_id TEXT);
CREATE TABLE notes (note_id TEXT PRIMARY KEY, source_id TEXT, path TEXT, title TEXT,
    note_semantic_checksum TEXT, auto_checksum TEXT, embedding_input_hash TEXT, embedding_model TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE mocs (moc_id TEXT PRIMARY KEY, topic TEXT, path TEXT, cluster_signature TEXT,
    embedding_input_hash TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE runs (run_id INTEGER PRIMARY KEY AUTOINCREMENT, pipeline_signature TEXT NOT NULL,
    started_at TEXT NOT NULL, finished_at TEXT, status TEXT DEFAULT 'running');
"""


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


def test_get_file_by_checksum_detects_renamed_copy(db):
    db.upsert_file("/inbox/original.pdf", "sharedhash", "pdf", "@Source2024")
    found = db.get_file_by_checksum("sharedhash", exclude_path="/inbox/copy.pdf")
    assert found is not None
    assert found["path"] == "/inbox/original.pdf"
    assert found["source_id"] == "@Source2024"

    assert db.get_file_by_checksum("nope") is None
    assert db.get_file_by_checksum("sharedhash", exclude_path="/inbox/original.pdf") is None


def test_get_source_by_extraction_checksum_cross_format(db):
    db.upsert_source(
        source_id="@Pdf2024",
        citekey="Pdf2024",
        title="Same Article",
        authors=["Author"],
        year=2024,
        file_checksum="pdfhash",
        origin_path="/inbox/article.pdf",
        origin_type="pdf",
        extraction_checksum="sharedtexthash",
    )
    found = db.get_source_by_extraction_checksum("sharedtexthash")
    assert found is not None
    assert found["source_id"] == "@Pdf2024"

    assert db.get_source_by_extraction_checksum("sharedtexthash", exclude_source_id="@Pdf2024") is None
    assert db.get_source_by_extraction_checksum("") is None


def test_run_duplicate_tracking(db):
    run_id = db.start_run("sig123")
    db.record_duplicate(run_id, "file")
    db.record_duplicate(run_id, "content")
    db.record_duplicate(run_id, "semantic")
    db.record_duplicate(run_id, "semantic")
    db.finish_run(run_id, "completed")

    last = db.get_last_run()
    assert last["run_id"] == run_id
    assert last["duplicate_file_count"] == 1
    assert last["duplicate_content_count"] == 1
    assert last["duplicate_semantic_count"] == 2
    assert last["status"] == "completed"

    with pytest.raises(ValueError):
        db.record_duplicate(run_id, "unknown-kind")


# ── Fase 0 — retenção máxima no SQLite ─────────────────────────────────


def test_migration_adds_new_columns_to_old_db(tmp_path):
    """Opening a pre-Fase-0 DB must add all new columns without losing data."""
    old_path = tmp_path / "old.db"
    conn = sqlite3.connect(str(old_path))
    conn.executescript(_OLD_SCHEMA_SQL)
    conn.execute(
        "INSERT INTO sources VALUES ('@S','S','t','[]',2020,'fc','ec','/p','md','now','now')"
    )
    conn.commit()
    conn.close()

    db = StateDB(old_path)
    try:
        def cols(table):
            return {r["name"] for r in db.conn.execute(f"PRAGMA table_info({table})").fetchall()}

        assert {"extracted_text", "lit_body", "origin"} <= cols("sources")
        assert "section_path" in cols("chunks")
        assert {"candidate_json", "status"} <= cols("concepts")
        assert {"body", "frontmatter_json", "origin"} <= cols("notes")
        assert {"body", "frontmatter_json", "origin"} <= cols("mocs")
        assert cols("assets")  # assets table created

        # Existing row preserved; new column defaulted.
        src = db.get_source("@S")
        assert src["title"] == "t"
        assert src["origin"] == "pipeline"
    finally:
        db.close()


def test_update_source_texts_selective(db):
    db.upsert_source("@S", "S", "T", [], None, "h", "/p", "md")
    db.update_source_texts("@S", extracted_text="full text")
    assert db.get_source("@S")["extracted_text"] == "full text"
    # Updating lit_body must not clobber extracted_text.
    db.update_source_texts("@S", lit_body="lit content")
    src = db.get_source("@S")
    assert src["extracted_text"] == "full text"
    assert src["lit_body"] == "lit content"


def test_concept_candidate_and_status(db):
    db.upsert_source("@S", "S", "T", [], None, "h", "/p", "md")
    db.upsert_chapter("@S::ch000", "@S", "Ch", "chk")
    db.upsert_chunk("@S::ch000::a", "@S", "@S::ch000", "txt", "ck")
    db.upsert_concept(
        "@S::concept::a", "@S", "@S::ch000::a", "ah", "th",
        candidate_json='{"thesis": "x"}', status="extracted",
    )
    concept = db.get_concept("@S::concept::a")
    assert concept["candidate_json"] == '{"thesis": "x"}'
    assert concept["status"] == "extracted"

    db.update_concept_status("@S::concept::a", "approved")
    approved = db.get_concepts_by_status("approved", without_notes=True)
    assert len(approved) == 1
    assert approved[0]["concept_id"] == "@S::concept::a"

    # Once noted, it drops out of the without_notes filter.
    db.upsert_concept("@S::concept::a", "@S", "@S::ch000::a", note_id="note1", status="noted")
    assert db.get_concepts_by_status("approved", without_notes=True) == []


def test_note_body_and_embedding_hash(db):
    db.upsert_note(
        "n1", "@S", "/p/n1.md", "Title",
        body="corpo completo", frontmatter_json='{"type": "permanent"}', origin="pipeline",
    )
    note = db.get_note("n1")
    assert note["body"] == "corpo completo"
    assert note["frontmatter_json"] == '{"type": "permanent"}'
    assert note["origin"] == "pipeline"

    db.update_note_embedding("n1", "emb_hash_123", "text-embedding-3-small")
    note = db.get_note("n1")
    assert note["embedding_input_hash"] == "emb_hash_123"


def test_moc_body_and_get_moc(db):
    db.upsert_moc("m1", "Topic", "/p/m1.md", "sig1", body="moc body", frontmatter_json='{}')
    moc = db.get_moc("m1")
    assert moc is not None
    assert moc["body"] == "moc body"
    assert moc["origin"] == "pipeline"


def test_assets_crud(db):
    db.upsert_source("@S", "S", "T", [], None, "h", "/p", "md")
    db.upsert_asset(
        "@S::img::x", "@S", "90_Assets/S/a.png", "imgck",
        chapter_id="@S::ch000", context_snippet="around the image",
    )
    assets = db.get_assets_for_source("@S")
    assert len(assets) == 1
    assert assets[0]["status"] == "pending"
    assert len(db.get_pending_assets()) == 1

    db.update_asset_description("@S::img::x", "Um grafico de barras.", "callck")
    assets = db.get_assets_for_source("@S")
    assert assets[0]["description"] == "Um grafico de barras."
    assert assets[0]["status"] == "described"
    assert db.get_pending_assets() == []


def test_delete_chunks_for_chapter(db):
    db.upsert_source("@S", "S", "T", [], None, "h", "/p", "md")
    db.upsert_chapter("@S::ch000", "@S", "Ch", "chk")
    db.upsert_chunk("@S::ch000::a", "@S", "@S::ch000", "a", "cka")
    db.upsert_chunk("@S::ch000::b", "@S", "@S::ch000", "b", "ckb")
    db.upsert_chunk("@S::ch000::c", "@S", "@S::ch000", "c", "ckc")

    removed = db.delete_chunks_for_chapter("@S::ch000", keep_ids={"@S::ch000::a"})
    assert set(removed) == {"@S::ch000::b", "@S::ch000::c"}
    remaining = {c["chunk_id"] for c in db.get_chunks_for_source("@S")}
    assert remaining == {"@S::ch000::a"}
