"""SQLite state management for incremental processing.

Tracks files, sources, chapters, chunks, concepts, notes, MOCs and pipeline runs.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_FTS_TOKEN_RE = re.compile(r"\w+", re.UNICODE)

# High-frequency PT-BR closed-class words (articles, prepositions, conjunctions,
# pronouns, common verb forms). Without this filter, a token like "que" appears
# in nearly every note's body, so the OR-joined MATCH expression matches almost
# the entire corpus regardless of topic — which in turn defeats any bm25-based
# relevance signal (a "hit" stops meaning anything). Comparison is case-insensitive;
# entries are stored lowercase.
_PT_STOPWORDS = frozenset({
    "a", "o", "as", "os", "um", "uma", "uns", "umas",
    "de", "da", "do", "das", "dos", "em", "na", "no", "nas", "nos",
    "por", "para", "com", "sem", "sobre", "entre", "ate", "apos",
    "e", "ou", "mas", "que", "se", "como", "quando", "onde", "porque", "pois",
    "eu", "tu", "ele", "ela", "voces", "eles", "elas",
    "seu", "sua", "seus", "suas", "este", "esta", "esse", "essa", "isso",
    "aquele", "aquela", "aquilo",
    "sao", "foi", "foram", "ser", "estar", "estao", "tem", "teve", "ha",
    "nao", "sim", "mais", "muito", "muitos", "muitas",
    "ja", "ainda", "tambem", "qual", "quais",
})


def _fts_match_expr(text: str, min_len: int = 2, max_tokens: int = 32) -> str | None:
    """Turn arbitrary user text into a safe FTS5 MATCH expression.

    Each word token is wrapped in double quotes, which neutralizes every FTS5
    operator (``-``, ``*``, ``NEAR``, ``:``, ``AND``/``OR``/``NOT``), so raw user
    text can never inject query syntax. Tokens are joined with ``OR`` because a
    natural-language question rarely has *all* its terms in a single note — bm25
    ranks whoever matches more terms, and RRF fuses with the vector side.
    High-frequency stopwords (see ``_PT_STOPWORDS``) are dropped so they can't
    turn "matches almost every note" into a false relevance signal.

    Returns ``None`` when there is no usable token (caller should treat as empty).
    """
    tokens = [
        t for t in _FTS_TOKEN_RE.findall(text)
        if len(t) >= min_len and t.lower() not in _PT_STOPWORDS
    ]
    if not tokens:
        return None
    return " OR ".join(f'"{t}"' for t in tokens[:max_tokens])

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
    extracted_text       TEXT,
    lit_body             TEXT,
    origin               TEXT NOT NULL DEFAULT 'pipeline',
    document_type        TEXT,
    bibliography_json    TEXT,
    abnt_reference       TEXT,
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
    section_path              TEXT NOT NULL DEFAULT '',
    status                    TEXT NOT NULL DEFAULT 'pending',
    llm_prompt1_hash          TEXT,
    llm_call_checksum_prompt1 TEXT,
    FOREIGN KEY (source_id) REFERENCES sources(source_id),
    FOREIGN KEY (chapter_id) REFERENCES chapters(chapter_id)
);

CREATE TABLE IF NOT EXISTS concepts (
    concept_id     TEXT PRIMARY KEY,
    source_id      TEXT NOT NULL,
    chunk_id       TEXT NOT NULL,
    anchor_hash    TEXT NOT NULL DEFAULT '',
    thesis_hash    TEXT NOT NULL DEFAULT '',
    note_id        TEXT,
    candidate_json TEXT,
    status         TEXT NOT NULL DEFAULT 'pending',
    FOREIGN KEY (source_id) REFERENCES sources(source_id),
    FOREIGN KEY (chunk_id) REFERENCES chunks(chunk_id)
);

CREATE TABLE IF NOT EXISTS notes (
    note_id                TEXT PRIMARY KEY,
    source_id              TEXT,
    path                   TEXT,
    title                  TEXT NOT NULL DEFAULT '',
    body                   TEXT,
    frontmatter_json       TEXT,
    origin                 TEXT NOT NULL DEFAULT 'pipeline',
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
    body                TEXT,
    frontmatter_json    TEXT,
    origin              TEXT NOT NULL DEFAULT 'pipeline',
    cluster_signature   TEXT,
    embedding_input_hash TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assets (
    asset_id        TEXT PRIMARY KEY,
    source_id       TEXT NOT NULL,
    chapter_id      TEXT,
    path            TEXT NOT NULL,
    image_checksum  TEXT NOT NULL,
    context_snippet TEXT NOT NULL DEFAULT '',
    description     TEXT,
    description_call_checksum TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',
    created_at      TEXT NOT NULL,
    FOREIGN KEY (source_id) REFERENCES sources(source_id)
);

CREATE TABLE IF NOT EXISTS llm_cache (
    call_checksum TEXT PRIMARY KEY,
    request_json  TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS note_connections (
    source_note_id TEXT NOT NULL,
    target_note_id TEXT NOT NULL,
    relation_type  TEXT NOT NULL,
    description    TEXT DEFAULT '',
    created_at     TEXT NOT NULL,
    PRIMARY KEY (source_note_id, target_note_id, relation_type)
);

CREATE TABLE IF NOT EXISTS runs (
    run_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    pipeline_signature  TEXT NOT NULL,
    started_at          TEXT NOT NULL,
    finished_at         TEXT,
    status              TEXT NOT NULL DEFAULT 'running',
    duplicate_file_count     INTEGER NOT NULL DEFAULT 0,
    duplicate_content_count  INTEGER NOT NULL DEFAULT 0,
    duplicate_semantic_count INTEGER NOT NULL DEFAULT 0
);
"""

# Indexes are created after schema migration, since some reference columns added by
# `_migrate_schema` (e.g. concepts.status) that don't exist on pre-migration databases.
_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_chunks_status    ON chunks(status);
CREATE INDEX IF NOT EXISTS idx_chunks_source_id ON chunks(source_id);
CREATE INDEX IF NOT EXISTS idx_concepts_note_id ON concepts(note_id);
CREATE INDEX IF NOT EXISTS idx_concepts_status  ON concepts(status);
CREATE INDEX IF NOT EXISTS idx_nc_source        ON note_connections(source_note_id);
CREATE INDEX IF NOT EXISTS idx_nc_target        ON note_connections(target_note_id);
CREATE INDEX IF NOT EXISTS idx_assets_source    ON assets(source_id);
CREATE INDEX IF NOT EXISTS idx_assets_status    ON assets(status);
"""

# FTS5 virtual tables for BM25 lexical search, kept in sync with notes/chunks.
# `remove_diacritics 2` matters for PT-BR: "conexao" matches "conexão".
# Executed separately from _SCHEMA_SQL so an fts5-less SQLite build degrades
# gracefully (see StateDB._init_fts) instead of aborting all schema creation.
_FTS_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS fts_notes USING fts5(
    note_id UNINDEXED, title, body,
    tokenize='unicode61 remove_diacritics 2'
);
CREATE VIRTUAL TABLE IF NOT EXISTS fts_chunks USING fts5(
    chunk_id UNINDEXED, text,
    tokenize='unicode61 remove_diacritics 2'
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
        # True if the SQLite build supports FTS5 (set by _init_fts). When False,
        # the hybrid retriever falls back to vector-only search.
        self.fts_enabled = False
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript(_SCHEMA_SQL)
        self.conn.commit()
        self._migrate_schema()
        self.conn.executescript(_INDEX_SQL)
        self.conn.commit()
        self._init_fts()

    def _init_fts(self) -> None:
        """Create the FTS5 tables and backfill them for pre-existing databases.

        Some SQLite builds ship without the fts5 module; in that case we set
        ``fts_enabled = False`` and the pipeline keeps working with vector search
        only (the Retriever degrades to ``mode="vector"``).
        """
        try:
            self.conn.executescript(_FTS_SQL)
            self.conn.commit()
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            if "fts5" in msg or "no such module" in msg:
                self.fts_enabled = False
                logger.warning(
                    "SQLite sem suporte a FTS5 — busca hibrida (BM25) desabilitada"
                )
                return
            raise
        self.fts_enabled = True
        self._backfill_fts()

    def _backfill_fts(self) -> None:
        """One-time population of the FTS tables from existing rows.

        Only runs when an FTS table is empty but its source table is not — i.e.
        a database created before FTS existed. Idempotent and cheap thereafter.
        """
        n_notes = self.conn.execute("SELECT COUNT(*) AS c FROM fts_notes").fetchone()["c"]
        if n_notes == 0:
            self.conn.execute(
                "INSERT INTO fts_notes (note_id, title, body) "
                "SELECT note_id, COALESCE(title,''), COALESCE(body,'') FROM notes"
            )
        n_chunks = self.conn.execute("SELECT COUNT(*) AS c FROM fts_chunks").fetchone()["c"]
        if n_chunks == 0:
            self.conn.execute(
                "INSERT INTO fts_chunks (chunk_id, text) "
                "SELECT chunk_id, COALESCE(text,'') FROM chunks"
            )
        self.conn.commit()

    def _fts_index_note(self, note_id: str) -> None:
        """Refresh the FTS row for a note from its (already-written) notes row.

        Called inside upsert_note *before* commit, so the resolved post-COALESCE
        title/body are visible on the same connection.
        """
        if not self.fts_enabled:
            return
        row = self.conn.execute(
            "SELECT title, body FROM notes WHERE note_id=?", (note_id,)
        ).fetchone()
        self.conn.execute("DELETE FROM fts_notes WHERE note_id=?", (note_id,))
        if row is not None:
            self.conn.execute(
                "INSERT INTO fts_notes (note_id, title, body) VALUES (?, ?, ?)",
                (note_id, row["title"] or "", row["body"] or ""),
            )

    def _fts_index_chunk(self, chunk_id: str, text: str) -> None:
        if not self.fts_enabled:
            return
        self.conn.execute("DELETE FROM fts_chunks WHERE chunk_id=?", (chunk_id,))
        self.conn.execute(
            "INSERT INTO fts_chunks (chunk_id, text) VALUES (?, ?)",
            (chunk_id, text or ""),
        )

    def _fts_delete_chunk(self, chunk_id: str) -> None:
        if not self.fts_enabled:
            return
        self.conn.execute("DELETE FROM fts_chunks WHERE chunk_id=?", (chunk_id,))

    def _migrate_schema(self) -> None:
        """Add columns to pre-existing tables that predate this migration.

        SQLite lacks `ADD COLUMN IF NOT EXISTS`, so we probe for the column
        and swallow the "duplicate column" error if it already exists.
        """
        migrations = [
            ("runs", "duplicate_file_count", "INTEGER NOT NULL DEFAULT 0"),
            ("runs", "duplicate_content_count", "INTEGER NOT NULL DEFAULT 0"),
            ("runs", "duplicate_semantic_count", "INTEGER NOT NULL DEFAULT 0"),
            # Fase 0 — retencao maxima no SQLite
            ("sources", "extracted_text", "TEXT"),
            ("sources", "lit_body", "TEXT"),
            ("sources", "origin", "TEXT NOT NULL DEFAULT 'pipeline'"),
            ("chunks", "section_path", "TEXT NOT NULL DEFAULT ''"),
            ("concepts", "candidate_json", "TEXT"),
            ("concepts", "status", "TEXT NOT NULL DEFAULT 'pending'"),
            ("notes", "body", "TEXT"),
            ("notes", "frontmatter_json", "TEXT"),
            ("notes", "origin", "TEXT NOT NULL DEFAULT 'pipeline'"),
            ("mocs", "body", "TEXT"),
            ("mocs", "frontmatter_json", "TEXT"),
            ("mocs", "origin", "TEXT NOT NULL DEFAULT 'pipeline'"),
            # Metadados bibliograficos ABNT
            ("sources", "document_type", "TEXT"),
            ("sources", "bibliography_json", "TEXT"),
            ("sources", "abnt_reference", "TEXT"),
        ]
        for table, column, coltype in migrations:
            try:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
                self.conn.commit()
            except sqlite3.OperationalError as e:
                if "duplicate column name" not in str(e).lower():
                    raise

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

    def upsert_file(
        self, path: str, file_checksum: str, origin_type: str, source_id: str | None = None
    ) -> None:
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

    def get_file_by_checksum(self, file_checksum: str, exclude_path: str | None = None) -> Optional[dict]:
        """Find any known file (regardless of path/name) with the same raw-byte checksum.

        Used to detect a renamed/copied duplicate dropped into the inbox under a
        different filename.
        """
        if exclude_path:
            return self._fetchone(
                "SELECT * FROM files WHERE file_checksum=? AND path<>? ORDER BY last_seen_at ASC LIMIT 1",
                (file_checksum, exclude_path),
            )
        return self._fetchone(
            "SELECT * FROM files WHERE file_checksum=? ORDER BY last_seen_at ASC LIMIT 1",
            (file_checksum,),
        )

    # ── Sources ────────────────────────────────────────────────────────

    def upsert_source(
        self,
        source_id: str,
        citekey: str,
        title: str,
        authors: list[str],
        year: int | None,
        file_checksum: str,
        origin_path: str,
        origin_type: str,
        extraction_checksum: str | None = None,
        origin: str = "pipeline",
        document_type: str | None = None,
        bibliography_json: str | None = None,
        abnt_reference: str | None = None,
    ) -> None:
        now = self._now()
        self.conn.execute(
            """INSERT INTO sources (source_id, citekey, title, authors, year, file_checksum,
                                    extraction_checksum, origin_path, origin_type, origin,
                                    document_type, bibliography_json, abnt_reference,
                                    created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(source_id) DO UPDATE SET
                 title=excluded.title, authors=excluded.authors, year=excluded.year,
                 file_checksum=excluded.file_checksum,
                 extraction_checksum=excluded.extraction_checksum,
                 origin=excluded.origin,
                 document_type=COALESCE(excluded.document_type, sources.document_type),
                 bibliography_json=COALESCE(excluded.bibliography_json, sources.bibliography_json),
                 abnt_reference=COALESCE(excluded.abnt_reference, sources.abnt_reference),
                 updated_at=excluded.updated_at""",
            (
                source_id, citekey, title, json.dumps(authors), year, file_checksum,
                extraction_checksum, origin_path, origin_type, origin,
                document_type, bibliography_json, abnt_reference, now, now,
            ),
        )
        self.conn.commit()

    def update_source_texts(
        self,
        source_id: str,
        extracted_text: str | None = None,
        lit_body: str | None = None,
    ) -> None:
        """Persist the full extracted text and/or the LIT note snapshot for a source.

        Only overwrites columns whose argument is not None, so callers can update
        one field without clobbering the other. This is the durable retention layer
        that lets `rechunk` and `rebuild` run without reprocessing the source file.
        """
        self.conn.execute(
            """UPDATE sources SET
                 extracted_text=COALESCE(?, extracted_text),
                 lit_body=COALESCE(?, lit_body),
                 updated_at=?
               WHERE source_id=?""",
            (extracted_text, lit_body, self._now(), source_id),
        )
        self.conn.commit()

    def get_source(self, source_id: str) -> Optional[dict]:
        return self._fetchone("SELECT * FROM sources WHERE source_id=?", (source_id,))

    def get_source_by_citekey(self, citekey: str) -> Optional[dict]:
        return self._fetchone("SELECT * FROM sources WHERE citekey=?", (citekey,))

    def get_source_by_extraction_checksum(
        self, extraction_checksum: str, exclude_source_id: str | None = None
    ) -> Optional[dict]:
        """Find any existing source with the same normalized extracted-text checksum.

        Used to detect the same article saved in a different format (e.g. PDF and
        Markdown) that extracts to textually identical content.
        """
        if not extraction_checksum:
            return None
        if exclude_source_id:
            return self._fetchone(
                "SELECT * FROM sources WHERE extraction_checksum=? AND source_id<>? "
                "ORDER BY created_at ASC LIMIT 1",
                (extraction_checksum, exclude_source_id),
            )
        return self._fetchone(
            "SELECT * FROM sources WHERE extraction_checksum=? ORDER BY created_at ASC LIMIT 1",
            (extraction_checksum,),
        )

    def list_sources(self) -> list[dict]:
        return self._fetchall("SELECT * FROM sources ORDER BY created_at DESC")

    # ── Chapters ───────────────────────────────────────────────────────

    def upsert_chapter(
        self, chapter_id: str, source_id: str, title: str, chapter_checksum: str, locator: str = ""
    ) -> None:
        self.conn.execute(
            """INSERT INTO chapters (chapter_id, source_id, title, chapter_checksum, locator)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(chapter_id) DO UPDATE SET
                 chapter_checksum=excluded.chapter_checksum,
                 title=excluded.title,
                 locator=excluded.locator""",
            (chapter_id, source_id, title, chapter_checksum, locator),
        )
        self.conn.commit()

    def get_chapters_for_source(self, source_id: str) -> list[dict]:
        return self._fetchall("SELECT * FROM chapters WHERE source_id=?", (source_id,))

    # ── Chunks ─────────────────────────────────────────────────────────

    def upsert_chunk(
        self,
        chunk_id: str,
        source_id: str,
        chapter_id: str,
        text: str,
        chunk_checksum: str,
        locator: str = "",
        status: str = "pending",
        section_path: str = "",
    ) -> None:
        self.conn.execute(
            """INSERT INTO chunks (chunk_id, source_id, chapter_id, text, chunk_checksum,
                                   locator, section_path, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(chunk_id) DO UPDATE SET
                 text=excluded.text, chunk_checksum=excluded.chunk_checksum,
                 locator=excluded.locator, section_path=excluded.section_path,
                 status=excluded.status""",
            (chunk_id, source_id, chapter_id, text, chunk_checksum, locator, section_path, status),
        )
        self._fts_index_chunk(chunk_id, text)
        self.conn.commit()

    def delete_chunks_for_chapter(self, chapter_id: str, keep_ids: set[str]) -> list[str]:
        """Delete chunks of a chapter whose id is not in keep_ids. Returns removed ids.

        Used after re-chunking a chapter so stale chunks (from an earlier chunking
        config or edited text) don't linger in SQLite and ChromaDB.
        """
        rows = self._fetchall(
            "SELECT chunk_id FROM chunks WHERE chapter_id=?", (chapter_id,)
        )
        removed = [r["chunk_id"] for r in rows if r["chunk_id"] not in keep_ids]
        for cid in removed:
            self.conn.execute("DELETE FROM chunks WHERE chunk_id=?", (cid,))
            self._fts_delete_chunk(cid)
        if removed:
            self.conn.commit()
        return removed

    def get_pending_chunks(self, source_id: str | None = None) -> list[dict]:
        if source_id:
            return self._fetchall(
                "SELECT * FROM chunks WHERE status='pending' AND source_id=?", (source_id,)
            )
        return self._fetchall("SELECT * FROM chunks WHERE status='pending'")

    def get_failed_chunks(self, source_id: str | None = None) -> list[dict]:
        """Return all chunks with status='failed', optionally filtered by source."""
        if source_id:
            return self._fetchall(
                "SELECT * FROM chunks WHERE status='failed' AND source_id=?", (source_id,)
            )
        return self._fetchall("SELECT * FROM chunks WHERE status='failed'")

    def get_chunks_for_source(self, source_id: str) -> list[dict]:
        return self._fetchall("SELECT * FROM chunks WHERE source_id=?", (source_id,))

    def get_chunk(self, chunk_id: str) -> Optional[dict]:
        return self._fetchone("SELECT * FROM chunks WHERE chunk_id=?", (chunk_id,))

    def delete_chapter(self, chapter_id: str) -> list[str]:
        """Delete a chapter and all its chunks. Returns removed chunk_ids."""
        rows = self._fetchall(
            "SELECT chunk_id FROM chunks WHERE chapter_id=?", (chapter_id,)
        )
        removed = [r["chunk_id"] for r in rows]
        for cid in removed:
            self.conn.execute("DELETE FROM chunks WHERE chunk_id=?", (cid,))
            self._fts_delete_chunk(cid)
        self.conn.execute("DELETE FROM chapters WHERE chapter_id=?", (chapter_id,))
        self.conn.commit()
        return removed

    def update_chunk_status(
        self,
        chunk_id: str,
        status: str,
        llm_prompt1_hash: str | None = None,
        llm_call_checksum: str | None = None,
    ) -> None:
        self.conn.execute(
            """UPDATE chunks SET status=?, llm_prompt1_hash=COALESCE(?, llm_prompt1_hash),
               llm_call_checksum_prompt1=COALESCE(?, llm_call_checksum_prompt1)
               WHERE chunk_id=?""",
            (status, llm_prompt1_hash, llm_call_checksum, chunk_id),
        )
        self.conn.commit()

    # ── Concepts ───────────────────────────────────────────────────────

    def upsert_concept(
        self,
        concept_id: str,
        source_id: str,
        chunk_id: str,
        anchor_hash: str = "",
        thesis_hash: str = "",
        note_id: str | None = None,
        candidate_json: str | None = None,
        status: str | None = None,
    ) -> None:
        self.conn.execute(
            """INSERT INTO concepts (concept_id, source_id, chunk_id, anchor_hash, thesis_hash,
                                     note_id, candidate_json, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, COALESCE(?, 'pending'))
               ON CONFLICT(concept_id) DO UPDATE SET
                 anchor_hash=excluded.anchor_hash, thesis_hash=excluded.thesis_hash,
                 note_id=COALESCE(excluded.note_id, concepts.note_id),
                 candidate_json=COALESCE(excluded.candidate_json, concepts.candidate_json),
                 status=COALESCE(?, concepts.status)""",
            (concept_id, source_id, chunk_id, anchor_hash, thesis_hash,
             note_id, candidate_json, status, status),
        )
        self.conn.commit()

    def update_concept_status(self, concept_id: str, status: str) -> None:
        self.conn.execute(
            "UPDATE concepts SET status=? WHERE concept_id=?", (status, concept_id)
        )
        self.conn.commit()

    def get_concept(self, concept_id: str) -> Optional[dict]:
        return self._fetchone("SELECT * FROM concepts WHERE concept_id=?", (concept_id,))

    def get_concepts_without_notes(self) -> list[dict]:
        return self._fetchall("SELECT * FROM concepts WHERE note_id IS NULL")

    def get_concepts_by_status(self, status: str, without_notes: bool = False) -> list[dict]:
        """Return concepts in a given status. If without_notes, only unnoted ones.

        The `approved` + `without_notes` combination is how `connect` reloads pending
        candidates straight from the DB when candidates.json is absent.
        """
        if without_notes:
            return self._fetchall(
                "SELECT * FROM concepts WHERE status=? AND note_id IS NULL", (status,)
            )
        return self._fetchall("SELECT * FROM concepts WHERE status=?", (status,))

    # ── Notes ──────────────────────────────────────────────────────────

    def upsert_note(
        self,
        note_id: str,
        source_id: str | None,
        path: str | None,
        title: str = "",
        note_semantic_checksum: str | None = None,
        auto_checksum: str | None = None,
        embedding_model: str | None = None,
        body: str | None = None,
        frontmatter_json: str | None = None,
        origin: str = "pipeline",
    ) -> None:
        now = self._now()
        self.conn.execute(
            """INSERT INTO notes (note_id, source_id, path, title, body, frontmatter_json,
                                  origin, note_semantic_checksum, auto_checksum,
                                  embedding_model, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(note_id) DO UPDATE SET
                 path=COALESCE(excluded.path, notes.path),
                 title=excluded.title,
                 body=COALESCE(excluded.body, notes.body),
                 frontmatter_json=COALESCE(excluded.frontmatter_json, notes.frontmatter_json),
                 origin=excluded.origin,
                 note_semantic_checksum=excluded.note_semantic_checksum,
                 auto_checksum=COALESCE(excluded.auto_checksum, notes.auto_checksum),
                 embedding_model=COALESCE(excluded.embedding_model, notes.embedding_model),
                 updated_at=excluded.updated_at""",
            (
                note_id, source_id, path, title, body, frontmatter_json, origin,
                note_semantic_checksum, auto_checksum, embedding_model, now, now,
            ),
        )
        self._fts_index_note(note_id)
        self.conn.commit()

    def update_note_embedding(
        self, note_id: str, embedding_input_hash: str, embedding_model: str | None = None
    ) -> None:
        """Record which embedding input the note's vector was last built from.

        Lets callers skip re-embedding a note whose semantic content and embedding
        model are unchanged.
        """
        self.conn.execute(
            """UPDATE notes SET
                 embedding_input_hash=?,
                 embedding_model=COALESCE(?, embedding_model)
               WHERE note_id=?""",
            (embedding_input_hash, embedding_model, note_id),
        )
        self.conn.commit()

    def get_note(self, note_id: str) -> Optional[dict]:
        return self._fetchone("SELECT * FROM notes WHERE note_id=?", (note_id,))

    def list_notes(self) -> list[dict]:
        return self._fetchall("SELECT * FROM notes ORDER BY created_at DESC")

    def count_notes(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) as cnt FROM notes").fetchone()
        return row["cnt"] if row else 0

    # ── Full-text (BM25) search ────────────────────────────────────────

    def search_notes_fts(self, query: str, limit: int = 20) -> list[dict]:
        """BM25 lexical search over notes. Returns ``[{note_id, rank}]``.

        FTS5's ``rank`` is already best-first (more negative = better match), so
        ``ORDER BY rank`` needs no sign flip. Returns empty when FTS is disabled
        or the query has no usable token.
        """
        if not self.fts_enabled:
            return []
        match = _fts_match_expr(query)
        if not match:
            return []
        try:
            rows = self.conn.execute(
                "SELECT note_id, rank FROM fts_notes WHERE fts_notes MATCH ? "
                "ORDER BY rank LIMIT ?",
                (match, limit),
            ).fetchall()
        except sqlite3.OperationalError as e:
            logger.warning("Busca FTS de notas falhou: %s", e)
            return []
        return [{"note_id": r["note_id"], "rank": r["rank"]} for r in rows]

    def search_chunks_fts(self, query: str, limit: int = 20) -> list[dict]:
        """BM25 lexical search over chunks. Returns ``[{chunk_id, rank}]``."""
        if not self.fts_enabled:
            return []
        match = _fts_match_expr(query)
        if not match:
            return []
        try:
            rows = self.conn.execute(
                "SELECT chunk_id, rank FROM fts_chunks WHERE fts_chunks MATCH ? "
                "ORDER BY rank LIMIT ?",
                (match, limit),
            ).fetchall()
        except sqlite3.OperationalError as e:
            logger.warning("Busca FTS de chunks falhou: %s", e)
            return []
        return [{"chunk_id": r["chunk_id"], "rank": r["rank"]} for r in rows]

    def rebuild_fts(self) -> dict[str, int]:
        """Rebuild both FTS tables from scratch from notes/chunks. Returns counts.

        Wired into ``zettel reindex`` so the lexical index is a disposable cache
        reconstructible from the SQLite source of truth, like the vector index.
        """
        if not self.fts_enabled:
            return {"fts_notes": 0, "fts_chunks": 0}
        self.conn.execute("DELETE FROM fts_notes")
        self.conn.execute("DELETE FROM fts_chunks")
        self.conn.execute(
            "INSERT INTO fts_notes (note_id, title, body) "
            "SELECT note_id, COALESCE(title,''), COALESCE(body,'') FROM notes"
        )
        self.conn.execute(
            "INSERT INTO fts_chunks (chunk_id, text) "
            "SELECT chunk_id, COALESCE(text,'') FROM chunks"
        )
        self.conn.commit()
        n = self.conn.execute("SELECT COUNT(*) AS c FROM fts_notes").fetchone()["c"]
        m = self.conn.execute("SELECT COUNT(*) AS c FROM fts_chunks").fetchone()["c"]
        return {"fts_notes": n, "fts_chunks": m}

    # ── Note Connections ──────────────────────────────────────────

    def upsert_note_connection(
        self, source_note_id: str, target_note_id: str, relation_type: str, description: str = ""
    ) -> None:
        self.conn.execute(
            """INSERT INTO note_connections
               (source_note_id, target_note_id, relation_type, description, created_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(source_note_id, target_note_id, relation_type) DO UPDATE SET
                 description=excluded.description, created_at=excluded.created_at""",
            (source_note_id, target_note_id, relation_type, description, self._now()),
        )
        self.conn.commit()

    def get_note_connections(self, note_id: str) -> list[dict]:
        """Get all connections where note_id is source or target."""
        return self._fetchall(
            "SELECT * FROM note_connections WHERE source_note_id=? OR target_note_id=?",
            (note_id, note_id),
        )

    def get_connections_for_notes(self, note_ids: list[str]) -> list[dict]:
        """Batch-fetch every edge touching any of ``note_ids`` (as source or target).

        One query per BFS frontier during graph expansion, instead of N per-note
        queries. Returns an empty list for an empty input.
        """
        if not note_ids:
            return []
        placeholders = ",".join("?" * len(note_ids))
        params = tuple(note_ids) + tuple(note_ids)
        return self._fetchall(
            f"SELECT * FROM note_connections "
            f"WHERE source_note_id IN ({placeholders}) "
            f"OR target_note_id IN ({placeholders})",
            params,
        )

    def count_note_connections(self) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS c FROM note_connections"
        ).fetchone()
        return row["c"] if row else 0

    # ── MOCs ───────────────────────────────────────────────────────────

    def upsert_moc(
        self,
        moc_id: str,
        topic: str,
        path: str | None = None,
        cluster_signature: str | None = None,
        body: str | None = None,
        frontmatter_json: str | None = None,
        origin: str = "pipeline",
    ) -> None:
        now = self._now()
        self.conn.execute(
            """INSERT INTO mocs (moc_id, topic, path, body, frontmatter_json, origin,
                                 cluster_signature, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(moc_id) DO UPDATE SET
                 topic=excluded.topic,
                 path=COALESCE(excluded.path, mocs.path),
                 body=COALESCE(excluded.body, mocs.body),
                 frontmatter_json=COALESCE(excluded.frontmatter_json, mocs.frontmatter_json),
                 origin=excluded.origin,
                 cluster_signature=excluded.cluster_signature,
                 updated_at=excluded.updated_at""",
            (moc_id, topic, path, body, frontmatter_json, origin, cluster_signature, now, now),
        )
        self.conn.commit()

    def get_moc(self, moc_id: str) -> Optional[dict]:
        return self._fetchone("SELECT * FROM mocs WHERE moc_id=?", (moc_id,))

    def get_moc_by_signature(self, signature: str) -> Optional[dict]:
        return self._fetchone("SELECT * FROM mocs WHERE cluster_signature=?", (signature,))

    def list_mocs(self) -> list[dict]:
        return self._fetchall("SELECT * FROM mocs ORDER BY created_at DESC")

    def find_moc_by_topic(self, topic: str) -> Optional[dict]:
        """Find existing MOC whose topic has a bidirectional substring match."""
        all_mocs = self.list_mocs()
        topic_lower = topic.lower()
        for moc in all_mocs:
            existing_lower = moc["topic"].lower()
            if existing_lower in topic_lower or topic_lower in existing_lower:
                return moc
        return None

    # ── Assets (images) ────────────────────────────────────────────────

    def upsert_asset(
        self,
        asset_id: str,
        source_id: str,
        path: str,
        image_checksum: str,
        chapter_id: str | None = None,
        context_snippet: str = "",
        status: str = "pending",
    ) -> None:
        self.conn.execute(
            """INSERT INTO assets (asset_id, source_id, chapter_id, path, image_checksum,
                                   context_snippet, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(asset_id) DO UPDATE SET
                 chapter_id=COALESCE(excluded.chapter_id, assets.chapter_id),
                 path=excluded.path,
                 context_snippet=excluded.context_snippet""",
            (asset_id, source_id, chapter_id, path, image_checksum,
             context_snippet, status, self._now()),
        )
        self.conn.commit()

    def get_asset(self, asset_id: str) -> Optional[dict]:
        return self._fetchone("SELECT * FROM assets WHERE asset_id=?", (asset_id,))

    def get_assets_for_source(self, source_id: str) -> list[dict]:
        return self._fetchall("SELECT * FROM assets WHERE source_id=?", (source_id,))

    def update_asset_chapter(self, asset_id: str, chapter_id: str | None) -> None:
        """Set chapter_id explicitly (including NULL) after rechunk re-resolution."""
        self.conn.execute(
            "UPDATE assets SET chapter_id=? WHERE asset_id=?",
            (chapter_id, asset_id),
        )
        self.conn.commit()

    def get_pending_assets(self) -> list[dict]:
        return self._fetchall("SELECT * FROM assets WHERE status='pending'")

    def reset_failed_assets(self) -> int:
        """Reset failed image descriptions back to pending. Returns count reset."""
        cur = self.conn.execute(
            "UPDATE assets SET status='pending' WHERE status='failed'"
        )
        self.conn.commit()
        return cur.rowcount

    def update_asset_description(
        self, asset_id: str, description: str, call_checksum: str, status: str = "described"
    ) -> None:
        self.conn.execute(
            """UPDATE assets SET description=?, description_call_checksum=?, status=?
               WHERE asset_id=?""",
            (description, call_checksum, status, asset_id),
        )
        self.conn.commit()

    # ── LLM Cache ──────────────────────────────────────────────────────

    def get_cached_llm_response(self, call_checksum: str) -> Optional[str]:
        row = self._fetchone(
            "SELECT response_json FROM llm_cache WHERE call_checksum=?", (call_checksum,)
        )
        return row["response_json"] if row else None

    def cache_llm_response(
        self, call_checksum: str, request_json: str, response_json: str
    ) -> None:
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

    def record_duplicate(self, run_id: int, kind: str) -> None:
        """Increment a duplicate counter on the run row.

        kind: one of "file", "content", "semantic".
        """
        column = {
            "file": "duplicate_file_count",
            "content": "duplicate_content_count",
            "semantic": "duplicate_semantic_count",
        }.get(kind)
        if not column:
            raise ValueError(f"Tipo de duplicidade desconhecido: {kind}")
        self.conn.execute(
            f"UPDATE runs SET {column} = {column} + 1 WHERE run_id=?", (run_id,)
        )
        self.conn.commit()

    def get_run(self, run_id: int) -> Optional[dict]:
        return self._fetchone("SELECT * FROM runs WHERE run_id=?", (run_id,))

    def get_last_run(self) -> Optional[dict]:
        return self._fetchone("SELECT * FROM runs ORDER BY run_id DESC LIMIT 1")

    # ── Stats ──────────────────────────────────────────────────────────

    def get_stats(self) -> dict[str, int]:
        tables = ["files", "sources", "chapters", "chunks", "concepts", "notes", "mocs", "assets"]
        stats: dict[str, int] = {}
        for t in tables:
            row = self.conn.execute(f"SELECT COUNT(*) as cnt FROM {t}").fetchone()
            stats[t] = row["cnt"] if row else 0
        pending = self.conn.execute(
            "SELECT COUNT(*) as cnt FROM chunks WHERE status='pending'"
        ).fetchone()
        stats["chunks_pending"] = pending["cnt"] if pending else 0
        return stats
