"""SQLite state management for incremental processing.

Tracks files, sources, chapters, chunks, concepts, notes, MOCs and pipeline runs.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS files (
    path            TEXT PRIMARY KEY,
    file_checksum   TEXT NOT NULL,
    origin_type     TEXT NOT NULL,
    source_id       TEXT,
    last_seen_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
    source_id            TEXT PRIMARY KEY,
    citekey              TEXT NOT NULL UNIQUE,
    title                TEXT NOT NULL DEFAULT '',
    authors              TEXT NOT NULL DEFAULT '[]',
    year                 INTEGER,
    file_checksum        TEXT NOT NULL,
    extraction_checksum  TEXT,
    origin_path          TEXT NOT NULL,
    origin_type          TEXT NOT NULL,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chapters (
    chapter_id       TEXT PRIMARY KEY,
    source_id        TEXT NOT NULL,
    title            TEXT NOT NULL DEFAULT '',
    chapter_checksum TEXT NOT NULL,
    locator          TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (source_id) REFERENCES sources(source_id)
);

CREATE TABLE IF NOT EXISTS chunks (
    chunk_id                  TEXT PRIMARY KEY,
    source_id                 TEXT NOT NULL,
    chapter_id                TEXT NOT NULL,
    text                      TEXT NOT NULL,
    chunk_checksum            TEXT NOT NULL,
    locator                   TEXT NOT NULL DEFAULT '',
    status                    TEXT NOT NULL DEFAULT 'pending',
    llm_prompt1_hash          TEXT,
    llm_call_checksum_prompt1 TEXT,
    FOREIGN KEY (source_id) REFERENCES sources(source_id),
    FOREIGN KEY (chapter_id) REFERENCES chapters(chapter_id)
);

CREATE TABLE IF NOT EXISTS concepts (
    concept_id  TEXT PRIMARY KEY,
    source_id   TEXT NOT NULL,
    chunk_id    TEXT NOT NULL,
    anchor_hash TEXT NOT NULL DEFAULT '',
    thesis_hash TEXT NOT NULL DEFAULT '',
    note_id     TEXT,
    FOREIGN KEY (source_id) REFERENCES sources(source_id),
    FOREIGN KEY (chunk_id) REFERENCES chunks(chunk_id)
);

CREATE TABLE IF NOT EXISTS notes (
    note_id                TEXT PRIMARY KEY,
    source_id              TEXT,
    path                   TEXT,
    title                  TEXT NOT NULL DEFAULT '',
    note_semantic_checksum TEXT,
    auto_checksum          TEXT,
    embedding_input_hash   TEXT,
    embedding_model        TEXT,
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mocs (
    moc_id              TEXT PRIMARY KEY,
    topic               TEXT NOT NULL DEFAULT '',
    path                TEXT,
    cluster_signature   TEXT,
    embedding_input_hash TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS llm_cache (
    call_checksum TEXT PRIMARY KEY,
    request_json  TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    run_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    pipeline_signature  TEXT NOT NULL,
    started_at          TEXT NOT NULL,
    finished_at         TEXT,
    status              TEXT NOT NULL DEFAULT 'running'
);
"""


class StateDB:
    """Thin wrapper around SQLite for pipeline state."""

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA foreign_keys=ON;")
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript(_SCHEMA_SQL)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ── Generic helpers ────────────────────────────────────────────────

    def _now(self) -> str:
        return datetime.now().isoformat()

    def _fetchone(self, sql: str, params: tuple = ()) -> Optional[dict]:
        row = self.conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def _fetchall(self, sql: str, params: tuple = ()) -> list[dict]:
        rows = self.conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    # ── Files ──────────────────────────────────────────────────────────

    def upsert_file(self, path: str, file_checksum: str, origin_type: str, source_id: str | None = None) -> None:
        self.conn.execute(
            """INSERT INTO files (path, file_checksum, origin_type, source_id, last_seen_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(path) DO UPDATE SET
                 file_checksum=excluded.file_checksum,
                 origin_type=excluded.origin_type,
                 source_id=COALESCE(excluded.source_id, files.source_id),
                 last_seen_at=excluded.last_seen_at""",
            (path, file_checksum, origin_type, source_id, self._now()),
        )
        self.conn.commit()

    def get_file(self, path: str) -> Optional[dict]:
        return self._fetchone("SELECT * FROM files WHERE path=?", (path,))

    # ── Sources ────────────────────────────────────────────────────────

    def upsert_source(self, source_id: str, citekey: str, title: str, authors: list[str],
                      year: int | None, file_checksum: str, origin_path: str, origin_type: str,
                      extraction_checksum: str | None = None) -> None:
        now = self._now()
        self.conn.execute(
            """INSERT INTO sources (source_id, citekey, title, authors, year, file_checksum,
                                    extraction_checksum, origin_path, origin_type, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(source_id) DO UPDATE SET
                 title=excluded.title, authors=excluded.authors, year=excluded.year,
                 file_checksum=excluded.file_checksum, extraction_checksum=excluded.extraction_checksum,
                 updated_at=excluded.updated_at""",
            (source_id, citekey, title, json.dumps(authors), year, file_checksum,
             extraction_checksum, origin_path, origin_type, now, now),
        )
        self.conn.commit()

    def get_source(self, source_id: str) -> Optional[dict]:
        return self._fetchone("SELECT * FROM sources WHERE source_id=?", (source_id,))

    def get_source_by_citekey(self, citekey: str) -> Optional[dict]:
        return self._fetchone("SELECT * FROM sources WHERE citekey=?", (citekey,))

    def list_sources(self) -> list[dict]:
        return self._fetchall("SELECT * FROM sources ORDER BY created_at DESC")

    # ── Chapters ───────────────────────────────────────────────────────

    def upsert_chapter(self, chapter_id: str, source_id: str, title: str,
                       chapter_checksum: str, locator: str = "") -> None:
        self.conn.execute(
            """INSERT INTO chapters (chapter_id, source_id, title, chapter_checksum, locator)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(chapter_id) DO UPDATE SET
                 chapter_checksum=excluded.chapter_checksum, title=excluded.title, locator=excluded.locator""",
            (chapter_id, source_id, title, chapter_checksum, locator),
        )
        self.conn.commit()

    def get_chapters_for_source(self, source_id: str) -> list[dict]:
        return self._fetchall("SELECT * FROM chapters WHERE source_id=?", (source_id,))

    # ── Chunks ─────────────────────────────────────────────────────────

    def upsert_chunk(self, chunk_id: str, source_id: str, chapter_id: str,
                     text: str, chunk_checksum: str, locator: str = "",
                     status: str = "pending") -> None:
        self.conn.execute(
            """INSERT INTO chunks (chunk_id, source_id, chapter_id, text, chunk_checksum, locator, status)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(chunk_id) DO UPDATE SET
                 text=excluded.text, chunk_checksum=excluded.chunk_checksum,
                 locator=excluded.locator, status=excluded.status""",
            (chunk_id, source_id, chapter_id, text, chunk_checksum, locator, status),
        )
        self.conn.commit()

    def get_pending_chunks(self, source_id: str | None = None) -> list[dict]:
        if source_id:
            return self._fetchall(
                "SELECT * FROM chunks WHERE status='pending' AND source_id=?", (source_id,)
            )
        return self._fetchall("SELECT * FROM chunks WHERE status='pending'")

    def get_chunks_for_source(self, source_id: str) -> list[dict]:
        return self._fetchall("SELECT * FROM chunks WHERE source_id=?", (source_id,))

    def update_chunk_status(self, chunk_id: str, status: str,
                            llm_prompt1_hash: str | None = None,
                            llm_call_checksum: str | None = None) -> None:
        self.conn.execute(
            """UPDATE chunks SET status=?, llm_prompt1_hash=COALESCE(?, llm_prompt1_hash),
               llm_call_checksum_prompt1=COALESCE(?, llm_call_checksum_prompt1)
               WHERE chunk_id=?""",
            (status, llm_prompt1_hash, llm_call_checksum, chunk_id),
        )
        self.conn.commit()

    # ── Concepts ───────────────────────────────────────────────────────

    def upsert_concept(self, concept_id: str, source_id: str, chunk_id: str,
                       anchor_hash: str = "", thesis_hash: str = "",
                       note_id: str | None = None) -> None:
        self.conn.execute(
            """INSERT INTO concepts (concept_id, source_id, chunk_id, anchor_hash, thesis_hash, note_id)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(concept_id) DO UPDATE SET
                 anchor_hash=excluded.anchor_hash, thesis_hash=excluded.thesis_hash,
                 note_id=COALESCE(excluded.note_id, concepts.note_id)""",
            (concept_id, source_id, chunk_id, anchor_hash, thesis_hash, note_id),
        )
        self.conn.commit()

    def get_concept(self, concept_id: str) -> Optional[dict]:
        return self._fetchone("SELECT * FROM concepts WHERE concept_id=?", (concept_id,))

    def get_concepts_without_notes(self) -> list[dict]:
        return self._fetchall("SELECT * FROM concepts WHERE note_id IS NULL")

    # ── Notes ──────────────────────────────────────────────────────────

    def upsert_note(self, note_id: str, source_id: str | None, path: str | None,
                    title: str = "", note_semantic_checksum: str | None = None,
                    auto_checksum: str | None = None, embedding_model: str | None = None) -> None:
        now = self._now()
        self.conn.execute(
            """INSERT INTO notes (note_id, source_id, path, title, note_semantic_checksum,
                                  auto_checksum, embedding_model, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(note_id) DO UPDATE SET
                 path=COALESCE(excluded.path, notes.path),
                 title=excluded.title,
                 note_semantic_checksum=excluded.note_semantic_checksum,
                 auto_checksum=COALESCE(excluded.auto_checksum, notes.auto_checksum),
                 embedding_model=COALESCE(excluded.embedding_model, notes.embedding_model),
                 updated_at=excluded.updated_at""",
            (note_id, source_id, path, title, note_semantic_checksum, auto_checksum, embedding_model, now, now),
        )
        self.conn.commit()

    def get_note(self, note_id: str) -> Optional[dict]:
        return self._fetchone("SELECT * FROM notes WHERE note_id=?", (note_id,))

    def list_notes(self) -> list[dict]:
        return self._fetchall("SELECT * FROM notes ORDER BY created_at DESC")

    def count_notes(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) as cnt FROM notes").fetchone()
        return row["cnt"] if row else 0

    # ── MOCs ───────────────────────────────────────────────────────────

    def upsert_moc(self, moc_id: str, topic: str, path: str | None = None,
                   cluster_signature: str | None = None) -> None:
        now = self._now()
        self.conn.execute(
            """INSERT INTO mocs (moc_id, topic, path, cluster_signature, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(moc_id) DO UPDATE SET
                 topic=excluded.topic, path=COALESCE(excluded.path, mocs.path),
                 cluster_signature=excluded.cluster_signature, updated_at=excluded.updated_at""",
            (moc_id, topic, path, cluster_signature, now, now),
        )
        self.conn.commit()

    def get_moc_by_signature(self, signature: str) -> Optional[dict]:
        return self._fetchone("SELECT * FROM mocs WHERE cluster_signature=?", (signature,))

    def list_mocs(self) -> list[dict]:
        return self._fetchall("SELECT * FROM mocs ORDER BY created_at DESC")

    # ── LLM Cache ──────────────────────────────────────────────────────

    def get_cached_llm_response(self, call_checksum: str) -> Optional[str]:
        row = self._fetchone("SELECT response_json FROM llm_cache WHERE call_checksum=?", (call_checksum,))
        return row["response_json"] if row else None

    def cache_llm_response(self, call_checksum: str, request_json: str, response_json: str) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO llm_cache (call_checksum, request_json, response_json, created_at)
               VALUES (?, ?, ?, ?)""",
            (call_checksum, request_json, response_json, self._now()),
        )
        self.conn.commit()

    # ── Runs ───────────────────────────────────────────────────────────

    def start_run(self, pipeline_signature: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO runs (pipeline_signature, started_at, status) VALUES (?, ?, 'running')",
            (pipeline_signature, self._now()),
        )
        self.conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def finish_run(self, run_id: int, status: str = "completed") -> None:
        self.conn.execute(
            "UPDATE runs SET finished_at=?, status=? WHERE run_id=?",
            (self._now(), status, run_id),
        )
        self.conn.commit()

    # ── Stats ──────────────────────────────────────────────────────────

    def get_stats(self) -> dict[str, int]:
        tables = ["files", "sources", "chapters", "chunks", "concepts", "notes", "mocs"]
        stats: dict[str, int] = {}
        for t in tables:
            row = self.conn.execute(f"SELECT COUNT(*) as cnt FROM {t}").fetchone()
            stats[t] = row["cnt"] if row else 0
        pending = self.conn.execute("SELECT COUNT(*) as cnt FROM chunks WHERE status='pending'").fetchone()
        stats["chunks_pending"] = pending["cnt"] if pending else 0
        return stats
