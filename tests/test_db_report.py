"""Offline tests for the database-size report.

No LLM, no embedding model, no network. The collector reads SQLite and the
filesystem; Chroma's client is optional and a dummy directory is enough to
assert disk totals.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from zettel.config import AppConfig
from zettel.db_report import (
    HEAVY_COLUMNS,
    collect_db_report,
    companion_bytes,
    file_bytes,
    inspect_sqlite,
    walk_chroma_segments,
)
from zettel.state import StateDB


@pytest.fixture
def db(tmp_path: Path):
    database = StateDB(tmp_path / "state.db")
    yield database
    database.close()


def test_heavy_columns_cover_the_known_payloads():
    expected = {
        ("sources", "extracted_text"),
        ("sources", "lit_body"),
        ("chunks", "text"),
        ("llm_cache", "request_json"),
        ("llm_cache", "response_json"),
        ("notes", "body"),
    }
    assert expected.issubset(set(HEAVY_COLUMNS))


def test_inspect_sqlite_counts_and_payload_length(db: StateDB):
    extracted = "texto extraido conhecido"
    request = '{"prompt": "x"}'
    response = '{"result": "ok-payload"}'
    db.upsert_source("@S", "S", "Source", [], None, "h", "/p", "md")
    db.update_source_texts("@S", extracted_text=extracted)
    db.cache_llm_response("call1", request, response)

    report = inspect_sqlite(db.conn, Path(db.db_path), heavy_columns=HEAVY_COLUMNS)

    sources = next(row for row in report.tables if row.name == "sources")
    assert sources.rows == 1
    assert sources.payload_bytes is not None
    assert sources.payload_bytes >= len(extracted)

    cache = next(row for row in report.tables if row.name == "llm_cache")
    assert cache.rows == 1

    extracted_col = next(
        col for col in report.columns if col.table == "sources" and col.column == "extracted_text"
    )
    assert extracted_col.rows == 1
    assert extracted_col.rows_nonnull == 1
    assert extracted_col.bytes == len(extracted)
    assert extracted_col.max_bytes == len(extracted)

    request_col = next(
        col for col in report.columns if col.table == "llm_cache" and col.column == "request_json"
    )
    assert request_col.bytes == len(request)
    response_col = next(
        col for col in report.columns if col.table == "llm_cache" and col.column == "response_json"
    )
    assert response_col.bytes == len(response)

    assert report.exists
    assert report.file_bytes > 0
    assert report.pragmas is not None
    assert report.pragmas.page_size > 0
    assert report.pragmas.page_count > 0


def test_inspect_sqlite_survives_missing_dbstat(db: StateDB, monkeypatch: pytest.MonkeyPatch):
    db.upsert_source("@S", "S", "Source", [], None, "h", "/p", "md")
    db.update_source_texts("@S", extracted_text="abc")

    monkeypatch.setattr("zettel.db_report.try_dbstat", lambda _conn: None)

    report = inspect_sqlite(db.conn, Path(db.db_path), heavy_columns=HEAVY_COLUMNS)
    assert report.dbstat_available is False
    sources = next(row for row in report.tables if row.name == "sources")
    assert sources.rows == 1
    assert sources.disk_bytes is None
    extracted_col = next(
        col for col in report.columns if col.table == "sources" and col.column == "extracted_text"
    )
    assert extracted_col.bytes == 3


def test_walk_chroma_segments_sums_planted_files(tmp_path: Path):
    chroma = tmp_path / "chroma"
    chroma.mkdir()
    sqlite_path = chroma / "chroma.sqlite3"
    sqlite_path.write_bytes(b"x" * 64)
    segment = chroma / "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    segment.mkdir()
    payload = segment / "data_level0.bin"
    payload.write_bytes(b"y" * 128)

    dir_total, hnsw, segments = walk_chroma_segments(chroma)
    assert dir_total == 64 + 128
    assert hnsw == 128
    assert len(segments) == 1
    assert segments[0].bytes == 128
    assert segments[0].files == 1
    assert file_bytes(sqlite_path) == 64
    main, wal, shm = companion_bytes(sqlite_path)
    assert main == 64
    assert wal == 0
    assert shm == 0


def test_collect_db_report_combines_state_and_chroma_disk(tmp_path: Path):
    state_path = tmp_path / "state.db"
    chroma = tmp_path / "chroma"
    chroma.mkdir()
    conn = sqlite3.connect(chroma / "chroma.sqlite3")
    conn.execute("CREATE TABLE IF NOT EXISTS t (id INTEGER)")
    conn.close()
    sqlite_size = (chroma / "chroma.sqlite3").stat().st_size
    segment = chroma / "11111111-2222-3333-4444-555555555555"
    segment.mkdir()
    (segment / "data_level0.bin").write_bytes(b"w" * 48)

    database = StateDB(state_path)
    try:
        database.upsert_source("@S", "S", "Source", [], None, "h", "/p", "md")
        database.update_source_texts("@S", extracted_text="payload-state")
        database.cache_llm_response("c1", "{}", '{"ok": 1}')
        cfg = AppConfig(state_db_path=state_path, chroma_path=chroma)
        report = collect_db_report(cfg, database)
    finally:
        database.close()

    extracted = next(
        col
        for col in report.state.columns
        if col.table == "sources" and col.column == "extracted_text"
    )
    assert extracted.bytes == len("payload-state")
    assert report.chroma.exists
    assert report.chroma.dir_total_bytes >= sqlite_size + 48
    assert report.chroma.hnsw_bytes >= 48
    assert any(seg.bytes >= 48 for seg in report.chroma.segments)
