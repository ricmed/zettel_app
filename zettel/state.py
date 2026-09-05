"""SQLite state management for incremental processing.

Tracks files, sources, chapters, chunks, concepts, notes, MOCs and pipeline runs.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import unicodedata
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


def _escape_like(text: str) -> str:
    """Neutralize LIKE metacharacters so a search for ``%`` does not match everything."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _path_under_permanent(path: str | None) -> bool:
    """True when ``path`` points at a note under ``30_Permanent/`` (any slash style)."""
    normalized = (path or "").replace("\\", "/")
    return "30_Permanent" in normalized


def _fold(text: str | None) -> str:
    """Accent- and case-insensitive key, same shape as ``topic_index.fold``.

    Registered on the connection as ``zfold`` so ``LIKE`` can run inside SQLite
    (ASCII-only case-insensitivity would miss ``função`` vs ``funcao``).
    """
    if not text:
        return ""
    folded = unicodedata.normalize("NFKD", text.lower())
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return re.sub(r"[\s_-]+", " ", folded).strip()


_SOURCE_PICKER_COLS = "source_id, citekey, title, authors, year"
_LIT_PICKER_COLS = (
    "chunk_id, source_id, section_path, locator, page_in_book, "
    "chunk_index, literature_note_path"
)

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
    total_pages_file     INTEGER,
    total_pages_book     INTEGER,
    page_offset          INTEGER,
    page_offset_confidence TEXT,
    content_start_file_page INTEGER,
    content_start_book_page INTEGER,
    processing_status    TEXT NOT NULL DEFAULT 'completed',
    last_chunk_processed INTEGER,
    total_chunks         INTEGER,
    docling_config_hash  TEXT,
    cost_usd_total       REAL NOT NULL DEFAULT 0,
    cost_usd_llm         REAL NOT NULL DEFAULT 0,
    cost_usd_embedding   REAL NOT NULL DEFAULT 0,
    tokens_prompt        INTEGER NOT NULL DEFAULT 0,
    tokens_completion    INTEGER NOT NULL DEFAULT 0,
    tokens_embedding     INTEGER NOT NULL DEFAULT 0,
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
    chunk_index               INTEGER,
    page_in_file              INTEGER,
    page_in_book              INTEGER,
    page_confidence           TEXT NOT NULL DEFAULT 'unknown',
    literature_note_path      TEXT,
    literature_id             TEXT,
    review_confidence         REAL,
    summary_json              TEXT,
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
    page_in_file    INTEGER,
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

-- Cheap term -> note routing index, mirrored from the `auto-topic-index` blocks
-- in the vault so `ask` can look a term up without reading Markdown files.
-- `note_id` is set only when the target is a permanent note; a literature target
-- routes a human/agent but is not something the Retriever can score.
CREATE TABLE IF NOT EXISTS topic_index_terms (
    scope_kind  TEXT NOT NULL,
    scope_id    TEXT NOT NULL,
    term        TEXT NOT NULL,
    term_folded TEXT NOT NULL,
    target      TEXT NOT NULL,
    note_id     TEXT,
    PRIMARY KEY (scope_kind, scope_id, term_folded, target)
);

CREATE TABLE IF NOT EXISTS runs (
    run_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    pipeline_signature  TEXT NOT NULL,
    started_at          TEXT NOT NULL,
    finished_at         TEXT,
    status              TEXT NOT NULL DEFAULT 'running',
    duplicate_file_count     INTEGER NOT NULL DEFAULT 0,
    duplicate_content_count  INTEGER NOT NULL DEFAULT 0,
    duplicate_semantic_count INTEGER NOT NULL DEFAULT 0,
    cost_usd_total      REAL NOT NULL DEFAULT 0,
    cost_usd_llm        REAL NOT NULL DEFAULT 0,
    cost_usd_embedding  REAL NOT NULL DEFAULT 0,
    tokens_prompt       INTEGER NOT NULL DEFAULT 0,
    tokens_completion   INTEGER NOT NULL DEFAULT 0,
    tokens_embedding    INTEGER NOT NULL DEFAULT 0,
    llm_calls           INTEGER NOT NULL DEFAULT 0,
    cache_hits          INTEGER NOT NULL DEFAULT 0,
    prompt_cache_read_tokens   INTEGER NOT NULL DEFAULT 0,
    prompt_cache_write_tokens  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS web_jobs (
    job_id          TEXT PRIMARY KEY,
    operation       TEXT NOT NULL,
    payload_json    TEXT NOT NULL DEFAULT '{}',
    state           TEXT NOT NULL DEFAULT 'queued',
    phase           TEXT NOT NULL DEFAULT 'queued',
    current_item    TEXT,
    current_index   INTEGER,
    total_items     INTEGER,
    message         TEXT NOT NULL DEFAULT '',
    result_json     TEXT,
    error_message   TEXT,
    run_id          INTEGER,
    created_at      TEXT NOT NULL,
    started_at      TEXT,
    finished_at     TEXT,
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

CREATE TABLE IF NOT EXISTS web_job_events (
    event_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          TEXT NOT NULL,
    phase           TEXT NOT NULL,
    current_item    TEXT,
    current_index   INTEGER,
    total_items     INTEGER,
    message         TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    FOREIGN KEY (job_id) REFERENCES web_jobs(job_id) ON DELETE CASCADE
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
CREATE INDEX IF NOT EXISTS idx_topic_terms_folded ON topic_index_terms(term_folded);
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
        self.conn.create_function("zfold", 1, _fold, deterministic=True)
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
            # LIT granular — paginas, offset, checkpoint de processamento
            ("sources", "total_pages_file", "INTEGER"),
            ("sources", "total_pages_book", "INTEGER"),
            ("sources", "page_offset", "INTEGER"),
            ("sources", "page_offset_confidence", "TEXT"),
            ("sources", "content_start_file_page", "INTEGER"),
            ("sources", "content_start_book_page", "INTEGER"),
            ("sources", "processing_status", "TEXT NOT NULL DEFAULT 'completed'"),
            ("sources", "last_chunk_processed", "INTEGER"),
            ("sources", "total_chunks", "INTEGER"),
            ("sources", "docling_config_hash", "TEXT"),
            ("chunks", "chunk_index", "INTEGER"),
            ("chunks", "page_in_file", "INTEGER"),
            ("chunks", "page_in_book", "INTEGER"),
            # Custos LLM / embeddings
            ("runs", "cost_usd_total", "REAL NOT NULL DEFAULT 0"),
            ("runs", "cost_usd_llm", "REAL NOT NULL DEFAULT 0"),
            ("runs", "cost_usd_embedding", "REAL NOT NULL DEFAULT 0"),
            ("runs", "tokens_prompt", "INTEGER NOT NULL DEFAULT 0"),
            ("runs", "tokens_completion", "INTEGER NOT NULL DEFAULT 0"),
            ("runs", "tokens_embedding", "INTEGER NOT NULL DEFAULT 0"),
            ("runs", "llm_calls", "INTEGER NOT NULL DEFAULT 0"),
            ("runs", "cache_hits", "INTEGER NOT NULL DEFAULT 0"),
            ("runs", "prompt_cache_read_tokens", "INTEGER NOT NULL DEFAULT 0"),
            ("runs", "prompt_cache_write_tokens", "INTEGER NOT NULL DEFAULT 0"),
            ("sources", "cost_usd_total", "REAL NOT NULL DEFAULT 0"),
            ("sources", "cost_usd_llm", "REAL NOT NULL DEFAULT 0"),
            ("sources", "cost_usd_embedding", "REAL NOT NULL DEFAULT 0"),
            ("sources", "tokens_prompt", "INTEGER NOT NULL DEFAULT 0"),
            ("sources", "tokens_completion", "INTEGER NOT NULL DEFAULT 0"),
            ("sources", "tokens_embedding", "INTEGER NOT NULL DEFAULT 0"),
            ("chunks", "page_confidence", "TEXT NOT NULL DEFAULT 'unknown'"),
            ("chunks", "literature_note_path", "TEXT"),
            ("chunks", "literature_id", "TEXT"),
            ("chunks", "review_confidence", "REAL"),
            ("chunks", "summary_json", "TEXT"),
            ("assets", "page_in_file", "INTEGER"),
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

    def vacuum(self) -> None:
        """Reclaim free pages after bulk deletes (does not change logical data).

        Runs WAL checkpoint then VACUUM. Needs exclusive access; may use
        temporary disk space roughly the size of the DB file.
        """
        self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.conn.execute("VACUUM")
        # VACUUM recreates the DB file; ensure subsequent writes still commit.
        self.conn.commit()

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
        total_pages_file: int | None = None,
        total_pages_book: int | None = None,
        page_offset: int | None = None,
        page_offset_confidence: str | None = None,
        content_start_file_page: int | None = None,
        content_start_book_page: int | None = None,
        processing_status: str | None = None,
        last_chunk_processed: int | None = None,
        total_chunks: int | None = None,
        docling_config_hash: str | None = None,
    ) -> None:
        now = self._now()
        if processing_status is None:
            processing_status = "completed"
        self.conn.execute(
            """INSERT INTO sources (source_id, citekey, title, authors, year, file_checksum,
                                    extraction_checksum, origin_path, origin_type, origin,
                                    document_type, bibliography_json, abnt_reference,
                                    total_pages_file, total_pages_book, page_offset,
                                    page_offset_confidence, content_start_file_page,
                                    content_start_book_page, processing_status,
                                    last_chunk_processed, total_chunks, docling_config_hash,
                                    created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(source_id) DO UPDATE SET
                 title=excluded.title, authors=excluded.authors, year=excluded.year,
                 file_checksum=excluded.file_checksum,
                 extraction_checksum=excluded.extraction_checksum,
                 origin=excluded.origin,
                 document_type=COALESCE(excluded.document_type, sources.document_type),
                 bibliography_json=COALESCE(excluded.bibliography_json, sources.bibliography_json),
                 abnt_reference=COALESCE(excluded.abnt_reference, sources.abnt_reference),
                 total_pages_file=COALESCE(excluded.total_pages_file, sources.total_pages_file),
                 total_pages_book=COALESCE(excluded.total_pages_book, sources.total_pages_book),
                 page_offset=COALESCE(excluded.page_offset, sources.page_offset),
                 page_offset_confidence=COALESCE(excluded.page_offset_confidence, sources.page_offset_confidence),
                 content_start_file_page=COALESCE(excluded.content_start_file_page, sources.content_start_file_page),
                 content_start_book_page=COALESCE(excluded.content_start_book_page, sources.content_start_book_page),
                 processing_status=COALESCE(excluded.processing_status, sources.processing_status),
                 last_chunk_processed=COALESCE(excluded.last_chunk_processed, sources.last_chunk_processed),
                 total_chunks=COALESCE(excluded.total_chunks, sources.total_chunks),
                 docling_config_hash=COALESCE(excluded.docling_config_hash, sources.docling_config_hash),
                 updated_at=excluded.updated_at""",
            (
                source_id, citekey, title, json.dumps(authors), year, file_checksum,
                extraction_checksum, origin_path, origin_type, origin,
                document_type, bibliography_json, abnt_reference,
                total_pages_file, total_pages_book, page_offset,
                page_offset_confidence, content_start_file_page, content_start_book_page,
                processing_status, last_chunk_processed, total_chunks, docling_config_hash,
                now, now,
            ),
        )
        self.conn.commit()

    def update_source_texts(
        self,
        source_id: str,
        extracted_text: str | None = None,
        lit_body: str | None = None,
    ) -> None:
        """Persist the full extracted text and/or the LIT index snapshot for a source.

        Only overwrites columns whose argument is not None, so callers can update
        one field without clobbering the other. This is the durable retention layer
        that lets `rechunk` and `rebuild` run without reprocessing the source file.
        ``lit_body`` now stores the literature *index* note, not a monolithic LIT.
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

    def update_source_paging(
        self,
        source_id: str,
        *,
        total_pages_file: int | None = None,
        total_pages_book: int | None = None,
        page_offset: int | None = None,
        page_offset_confidence: str | None = None,
        content_start_file_page: int | None = None,
        content_start_book_page: int | None = None,
        processing_status: str | None = None,
        last_chunk_processed: int | None = None,
        total_chunks: int | None = None,
        docling_config_hash: str | None = None,
    ) -> None:
        """Update paging / processing checkpoint fields for a source."""
        self.conn.execute(
            """UPDATE sources SET
                 total_pages_file=COALESCE(?, total_pages_file),
                 total_pages_book=COALESCE(?, total_pages_book),
                 page_offset=COALESCE(?, page_offset),
                 page_offset_confidence=COALESCE(?, page_offset_confidence),
                 content_start_file_page=COALESCE(?, content_start_file_page),
                 content_start_book_page=COALESCE(?, content_start_book_page),
                 processing_status=COALESCE(?, processing_status),
                 last_chunk_processed=COALESCE(?, last_chunk_processed),
                 total_chunks=COALESCE(?, total_chunks),
                 docling_config_hash=COALESCE(?, docling_config_hash),
                 updated_at=?
               WHERE source_id=?""",
            (
                total_pages_file, total_pages_book, page_offset, page_offset_confidence,
                content_start_file_page, content_start_book_page,
                processing_status, last_chunk_processed, total_chunks, docling_config_hash,
                self._now(), source_id,
            ),
        )
        self.conn.commit()

    def delete_chunks(self, chunk_ids: list[str]) -> int:
        """Delete chunks by id (SQLite + FTS + concepts). Returns how many were removed."""
        if not chunk_ids:
            return 0
        removed = 0
        for cid in chunk_ids:
            self.conn.execute("DELETE FROM concepts WHERE chunk_id=?", (cid,))
            cur = self.conn.execute("DELETE FROM chunks WHERE chunk_id=?", (cid,))
            if cur.rowcount:
                removed += 1
                self._fts_delete_chunk(cid)
        if removed:
            self.conn.commit()
        return removed

    def update_chunk_pages(
        self,
        chunk_id: str,
        *,
        page_in_file: int | None = None,
        page_in_book: int | None = None,
        page_confidence: str | None = None,
    ) -> None:
        """Overwrite page fields (allows setting page_in_book explicitly)."""
        self.conn.execute(
            """UPDATE chunks SET
                 page_in_file=COALESCE(?, page_in_file),
                 page_in_book=?,
                 page_confidence=COALESCE(?, page_confidence)
               WHERE chunk_id=?""",
            (page_in_file, page_in_book, page_confidence, chunk_id),
        )
        self.conn.commit()

    def get_notes_for_source(self, source_id: str) -> list[dict]:
        return self._fetchall(
            "SELECT * FROM notes WHERE source_id=? ORDER BY created_at ASC",
            (source_id,),
        )

    def get_note_ids_for_source(self, source_id: str) -> list[str]:
        """Permanent note ids linked via notes.source_id or concepts.note_id."""
        ids: list[str] = []
        for row in self.get_notes_for_source(source_id):
            ids.append(row["note_id"])
        for row in self._fetchall(
            "SELECT DISTINCT note_id FROM concepts WHERE source_id=? AND note_id IS NOT NULL",
            (source_id,),
        ):
            ids.append(row["note_id"])
        return list(dict.fromkeys(ids))

    def delete_note(self, note_id: str) -> bool:
        """Delete a permanent note row (+ FTS + graph edges). Returns True if removed."""
        cur = self.conn.execute("DELETE FROM notes WHERE note_id=?", (note_id,))
        if not cur.rowcount:
            return False
        if self.fts_enabled:
            self.conn.execute("DELETE FROM fts_notes WHERE note_id=?", (note_id,))
        self.conn.execute(
            "DELETE FROM note_connections WHERE source_note_id=? OR target_note_id=?",
            (note_id, note_id),
        )
        self.conn.commit()
        return True

    def clear_source_id_on_notes(self, source_id: str) -> int:
        """Detach surviving permanent notes from a deleted source."""
        cur = self.conn.execute(
            "UPDATE notes SET source_id=NULL, updated_at=? WHERE source_id=?",
            (self._now(), source_id),
        )
        self.conn.commit()
        return cur.rowcount

    def delete_source_cascade(self, source_id: str) -> dict[str, int]:
        """Remove a source and all dependent rows (not permanent notes).

        Deletes chunks (+ concepts per chunk + FTS), chapters, orphan concepts,
        assets, files rows, and the sources row.
        """
        chunks = self.get_chunks_for_source(source_id)
        chunk_ids = [c["chunk_id"] for c in chunks]
        removed_chunks = self.delete_chunks(chunk_ids) if chunk_ids else 0

        chapters = self.get_chapters_for_source(source_id)
        for ch in chapters:
            self.conn.execute("DELETE FROM chapters WHERE chapter_id=?", (ch["chapter_id"],))

        cur_concepts = self.conn.execute(
            "DELETE FROM concepts WHERE source_id=?", (source_id,)
        )
        cur_assets = self.conn.execute(
            "DELETE FROM assets WHERE source_id=?", (source_id,)
        )
        cur_files = self.conn.execute(
            "DELETE FROM files WHERE source_id=?", (source_id,)
        )
        cur_source = self.conn.execute(
            "DELETE FROM sources WHERE source_id=?", (source_id,)
        )
        self.conn.execute(
            "DELETE FROM topic_index_terms WHERE scope_kind='source' AND scope_id=?",
            (source_id,),
        )
        self.conn.commit()
        return {
            "chunks": removed_chunks,
            "chapters": len(chapters),
            "concepts": cur_concepts.rowcount,
            "assets": cur_assets.rowcount,
            "files": cur_files.rowcount,
            "sources": cur_source.rowcount,
        }

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

    def search_sources(self, query: str = "", limit: int = 20) -> list[dict]:
        """Picker lookup: citekey/title/authors, never ``extracted_text`` / ``lit_body``.

        ``authors`` is stored as JSON text, so ``kahneman`` and ``daniel kahneman``
        match but ``kahneman, daniel`` (reordered) does not. Empty ``query``
        returns the most recently created sources.
        """
        limit = max(1, min(int(limit), 50))
        query = (query or "")[:200]
        cols = _SOURCE_PICKER_COLS
        if not query.strip():
            return self._fetchall(
                f"SELECT {cols} FROM sources ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (limit,),
            )
        folded = _fold(query)
        if not folded:
            return []
        pattern = f"%{_escape_like(folded)}%"
        return self._fetchall(
            f"SELECT {cols} FROM sources "
            f"WHERE zfold(citekey) LIKE ? ESCAPE '\\' "
            f"OR zfold(title) LIKE ? ESCAPE '\\' "
            f"OR zfold(authors) LIKE ? ESCAPE '\\' "
            f"ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (pattern, pattern, pattern, limit),
        )

    def search_literature_chunks(
        self, query: str = "", source_id: str | None = None, limit: int = 20,
    ) -> list[dict]:
        """Picker lookup over chunks that already have a literature note on disk.

        Matches folded ``section_path`` / ``locator`` / ``chunk_id``. Does not
        select ``chunks.text``. Empty ``query`` returns the lowest ``chunk_index``.
        """
        limit = max(1, min(int(limit), 50))
        query = (query or "")[:200]
        cols = _LIT_PICKER_COLS
        where = [
            "literature_note_path IS NOT NULL",
            "literature_note_path <> ''",
        ]
        params: list[Any] = []
        if source_id:
            where.append("source_id=?")
            params.append(source_id)
        if query.strip():
            folded = _fold(query)
            if not folded:
                return []
            pattern = f"%{_escape_like(folded)}%"
            where.append(
                "(zfold(section_path) LIKE ? ESCAPE '\\' "
                "OR zfold(locator) LIKE ? ESCAPE '\\' "
                "OR zfold(chunk_id) LIKE ? ESCAPE '\\')"
            )
            params.extend((pattern, pattern, pattern))
        params.append(limit)
        sql = (
            f"SELECT {cols} FROM chunks WHERE "
            + " AND ".join(where)
            + " ORDER BY chunk_index, chunk_id LIMIT ?"
        )
        return self._fetchall(sql, tuple(params))

    def search_literature_chunks_fts(
        self, query: str, source_id: str | None = None, limit: int = 20,
    ) -> list[dict]:
        """Second layer: match the chunk body via FTS5 without selecting ``text``."""
        if not self.fts_enabled:
            return []
        limit = max(1, min(int(limit), 50))
        query = (query or "")[:200]
        match = _fts_match_expr(query)
        if not match:
            return []
        where = [
            "fts_chunks MATCH ?",
            "c.literature_note_path IS NOT NULL",
            "c.literature_note_path <> ''",
        ]
        params: list[Any] = [match]
        if source_id:
            where.append("c.source_id=?")
            params.append(source_id)
        params.append(limit)
        cols = ", ".join(f"c.{name.strip()}" for name in _LIT_PICKER_COLS.split(","))
        try:
            return self._fetchall(
                f"SELECT {cols} FROM fts_chunks "
                f"JOIN chunks c ON c.chunk_id = fts_chunks.chunk_id "
                f"WHERE {' AND '.join(where)} "
                f"ORDER BY rank LIMIT ?",
                tuple(params),
            )
        except sqlite3.OperationalError as e:
            logger.warning("Busca FTS de literatura falhou: %s", e)
            return []

    def next_manual_chunk_index(self, source_id: str) -> int:
        """Next ``chunk_index`` for a hand-written granular LIT of this source."""
        row = self.conn.execute(
            "SELECT MAX(chunk_index) AS m FROM chunks "
            "WHERE source_id=? AND chunk_id LIKE '%::manual::%'",
            (source_id,),
        ).fetchone()
        current = row["m"] if row is not None and row["m"] is not None else 0
        return int(current) + 1

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
        chunk_index: int | None = None,
        page_in_file: int | None = None,
        page_in_book: int | None = None,
        page_confidence: str = "unknown",
        literature_note_path: str | None = None,
        literature_id: str | None = None,
        review_confidence: float | None = None,
        summary_json: str | None = None,
    ) -> None:
        self.conn.execute(
            """INSERT INTO chunks (chunk_id, source_id, chapter_id, text, chunk_checksum,
                                   locator, section_path, status, chunk_index,
                                   page_in_file, page_in_book, page_confidence,
                                   literature_note_path, literature_id,
                                   review_confidence, summary_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(chunk_id) DO UPDATE SET
                 text=excluded.text, chunk_checksum=excluded.chunk_checksum,
                 locator=excluded.locator, section_path=excluded.section_path,
                 status=excluded.status,
                 chunk_index=COALESCE(excluded.chunk_index, chunks.chunk_index),
                 page_in_file=COALESCE(excluded.page_in_file, chunks.page_in_file),
                 page_in_book=COALESCE(excluded.page_in_book, chunks.page_in_book),
                 page_confidence=excluded.page_confidence,
                 literature_note_path=COALESCE(excluded.literature_note_path, chunks.literature_note_path),
                 literature_id=COALESCE(excluded.literature_id, chunks.literature_id),
                 review_confidence=COALESCE(excluded.review_confidence, chunks.review_confidence),
                 summary_json=COALESCE(excluded.summary_json, chunks.summary_json)""",
            (
                chunk_id, source_id, chapter_id, text, chunk_checksum, locator, section_path,
                status, chunk_index, page_in_file, page_in_book, page_confidence,
                literature_note_path, literature_id, review_confidence, summary_json,
            ),
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
        if removed:
            self.delete_chunks(removed)
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
        if removed:
            self.delete_chunks(removed)
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

    def update_chunk_review(
        self,
        chunk_id: str,
        *,
        status: str | None = None,
        literature_note_path: str | None = None,
        literature_id: str | None = None,
        review_confidence: float | None = None,
        summary_json: str | None = None,
        page_in_book: int | None = None,
        page_confidence: str | None = None,
        llm_prompt1_hash: str | None = None,
        llm_call_checksum: str | None = None,
    ) -> None:
        """Update review / literature fields for a chunk (checkpoint after extract/review)."""
        self.conn.execute(
            """UPDATE chunks SET
                 status=COALESCE(?, status),
                 literature_note_path=COALESCE(?, literature_note_path),
                 literature_id=COALESCE(?, literature_id),
                 review_confidence=COALESCE(?, review_confidence),
                 summary_json=COALESCE(?, summary_json),
                 page_in_book=COALESCE(?, page_in_book),
                 page_confidence=COALESCE(?, page_confidence),
                 llm_prompt1_hash=COALESCE(?, llm_prompt1_hash),
                 llm_call_checksum_prompt1=COALESCE(?, llm_call_checksum_prompt1)
               WHERE chunk_id=?""",
            (
                status, literature_note_path, literature_id, review_confidence,
                summary_json, page_in_book, page_confidence,
                llm_prompt1_hash, llm_call_checksum, chunk_id,
            ),
        )
        self.conn.commit()

    def get_chunks_by_status(
        self, status: str, source_id: str | None = None
    ) -> list[dict]:
        if source_id:
            return self._fetchall(
                "SELECT * FROM chunks WHERE status=? AND source_id=? ORDER BY chunk_index ASC",
                (status, source_id),
            )
        return self._fetchall(
            "SELECT * FROM chunks WHERE status=? ORDER BY source_id, chunk_index ASC",
            (status,),
        )

    # ── Topic index (term -> note routing) ─────────────────────────────

    def replace_topic_index_terms(
        self, scope_kind: str, scope_id: str, rows: list[dict],
    ) -> int:
        """Replace every term row for one scope. Returns how many were written.

        Replace rather than merge: the vault block is regenerated wholesale on
        each refresh, and a term that disappeared from the notes must disappear
        from the lookup too.
        """
        self.conn.execute(
            "DELETE FROM topic_index_terms WHERE scope_kind=? AND scope_id=?",
            (scope_kind, scope_id),
        )
        if rows:
            self.conn.executemany(
                """INSERT OR REPLACE INTO topic_index_terms
                   (scope_kind, scope_id, term, term_folded, target, note_id)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                [
                    (
                        scope_kind, scope_id, r["term"], r["term_folded"],
                        r["target"], r.get("note_id"),
                    )
                    for r in rows
                ],
            )
        self.conn.commit()
        return len(rows)

    def delete_topic_index_scope(self, scope_kind: str, scope_id: str) -> None:
        self.conn.execute(
            "DELETE FROM topic_index_terms WHERE scope_kind=? AND scope_id=?",
            (scope_kind, scope_id),
        )
        self.conn.commit()

    def match_topic_index_scope(self, scope_kind: str, scope_id: str) -> list[dict]:
        """Every term row for one scope, ordered for stable rendering/reporting."""
        return self._fetchall(
            """SELECT * FROM topic_index_terms
               WHERE scope_kind=? AND scope_id=? ORDER BY term_folded, target""",
            (scope_kind, scope_id),
        )

    def match_topic_index(self, folded_query: str, limit: int = 20) -> list[dict]:
        """Permanent notes whose indexed term appears in ``folded_query``.

        The containment test runs in SQLite (``instr``) so a large index never
        has to cross into Python. Only rows with a ``note_id`` are returned:
        a literature target routes a reader but is not something the Retriever
        can score.
        """
        if not folded_query:
            return []
        return self._fetchall(
            """SELECT DISTINCT note_id, term, scope_kind, scope_id
               FROM topic_index_terms
               WHERE note_id IS NOT NULL AND instr(?, term_folded) > 0
               ORDER BY length(term_folded) DESC, term ASC
               LIMIT ?""",
            (folded_query, limit),
        )

    def get_concepts_for_notes(self, note_ids: list[str]) -> dict[str, dict]:
        """Batch-fetch the concept row behind each note, keyed by ``note_id``.

        One query instead of N, for consumers that need the original candidate
        (relevance score, author judgement) alongside a set of notes.
        """
        if not note_ids:
            return {}
        placeholders = ",".join("?" * len(note_ids))
        rows = self._fetchall(
            f"SELECT * FROM concepts WHERE note_id IN ({placeholders})",
            tuple(note_ids),
        )
        return {row["note_id"]: row for row in rows if row.get("note_id")}

    def get_concepts_for_chunk(self, chunk_id: str) -> list[dict]:
        return self._fetchall(
            "SELECT * FROM concepts WHERE chunk_id=?", (chunk_id,)
        )

    def get_concepts_for_source(
        self, source_id: str, *, without_notes: bool = False,
    ) -> list[dict]:
        """Concepts belonging to ``source_id``, optionally only those still without a note."""
        if without_notes:
            return self._fetchall(
                "SELECT * FROM concepts WHERE source_id=? AND note_id IS NULL",
                (source_id,),
            )
        return self._fetchall(
            "SELECT * FROM concepts WHERE source_id=?", (source_id,)
        )

    def update_concepts_status_for_chunk(self, chunk_id: str, status: str) -> None:
        self.conn.execute(
            "UPDATE concepts SET status=? WHERE chunk_id=?", (status, chunk_id)
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
                 anchor_hash=COALESCE(NULLIF(excluded.anchor_hash, ''), concepts.anchor_hash),
                 thesis_hash=COALESCE(NULLIF(excluded.thesis_hash, ''), concepts.thesis_hash),
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

    def get_concepts_by_status(self, status: str, without_notes: bool = False) -> list[dict]:
        """Return concepts in a given status. If without_notes, only unnoted ones.

        The `approved` + `without_notes` combination is how `connect` loads
        pending candidates from the DB (source of truth after review).
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

    def delete_pipeline_mocs(self) -> list[dict]:
        """Remove pipeline MOC rows and return the deleted records."""
        rows = self._fetchall("SELECT * FROM mocs WHERE origin='pipeline'")
        if rows:
            self.conn.execute("DELETE FROM mocs WHERE origin='pipeline'")
            self.conn.commit()
        return rows

    def delete_hub_pipeline_mocs(self) -> list[dict]:
        """Remove hub_pipeline MOC rows and return the deleted records."""
        rows = self._fetchall("SELECT * FROM mocs WHERE origin='hub_pipeline'")
        if rows:
            self.conn.execute("DELETE FROM mocs WHERE origin='hub_pipeline'")
            self.conn.commit()
        return rows

    def get_weighted_note_degrees(
        self, relation_weights: dict[str, float],
    ) -> dict[str, float]:
        """Undirected weighted degree per note from note_connections."""
        from collections import defaultdict

        degrees: dict[str, float] = defaultdict(float)
        rows = self._fetchall(
            "SELECT source_note_id, target_note_id, relation_type FROM note_connections",
        )
        for row in rows:
            rel = row.get("relation_type") or "related"
            weight = relation_weights.get(rel, relation_weights.get("related", 0.5))
            degrees[row["source_note_id"]] += weight
            degrees[row["target_note_id"]] += weight
        return dict(degrees)

    def list_permanent_note_ids(self) -> set[str]:
        """Note IDs whose vault path is under 30_Permanent/."""
        rows = self._fetchall("SELECT note_id, path FROM notes WHERE path IS NOT NULL")
        permanent: set[str] = set()
        for row in rows:
            if _path_under_permanent(row.get("path")):
                permanent.add(row["note_id"])
        return permanent

    def count_permanent_notes(self) -> int:
        """Count notes under ``30_Permanent/`` (path slash style agnostic)."""
        return len(self.list_permanent_note_ids())

    def find_moc_by_hub_note_id(self, hub_note_id: str) -> Optional[dict]:
        """Find hub_pipeline MOC anchored on hub_note_id (from frontmatter_json)."""
        import json

        for moc in self.list_mocs():
            if moc.get("origin") != "hub_pipeline":
                continue
            raw = moc.get("frontmatter_json")
            if not raw:
                continue
            try:
                meta = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if meta.get("hub_note_id") == hub_note_id:
                return moc
        return None

    def list_hub_anchor_note_ids(self) -> set[str]:
        """hub_note_id values from existing hub_pipeline MOCs."""
        import json

        anchors: set[str] = set()
        for moc in self.list_mocs():
            if moc.get("origin") != "hub_pipeline":
                continue
            raw = moc.get("frontmatter_json")
            if not raw:
                continue
            try:
                meta = json.loads(raw)
            except json.JSONDecodeError:
                continue
            hub_id = meta.get("hub_note_id")
            if hub_id:
                anchors.add(hub_id)
        return anchors

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
        page_in_file: int | None = None,
    ) -> None:
        self.conn.execute(
            """INSERT INTO assets (asset_id, source_id, chapter_id, path, image_checksum,
                                   context_snippet, status, page_in_file, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(asset_id) DO UPDATE SET
                 chapter_id=COALESCE(excluded.chapter_id, assets.chapter_id),
                 path=excluded.path,
                 context_snippet=excluded.context_snippet,
                 page_in_file=COALESCE(excluded.page_in_file, assets.page_in_file)""",
            (asset_id, source_id, chapter_id, path, image_checksum,
             context_snippet, status, page_in_file, self._now()),
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

    def finish_run(
        self,
        run_id: int,
        status: str = "completed",
        usage: dict | None = None,
    ) -> None:
        if usage:
            self.conn.execute(
                """UPDATE runs SET
                     finished_at=?, status=?,
                     cost_usd_total=?, cost_usd_llm=?, cost_usd_embedding=?,
                     tokens_prompt=?, tokens_completion=?, tokens_embedding=?,
                     llm_calls=?, cache_hits=?,
                     prompt_cache_read_tokens=?, prompt_cache_write_tokens=?
                   WHERE run_id=?""",
                (
                    self._now(),
                    status,
                    float(usage.get("cost_usd_total", 0) or 0),
                    float(usage.get("cost_usd_llm", 0) or 0),
                    float(usage.get("cost_usd_embedding", 0) or 0),
                    int(usage.get("tokens_prompt", 0) or 0),
                    int(usage.get("tokens_completion", 0) or 0),
                    int(usage.get("tokens_embedding", 0) or 0),
                    int(usage.get("llm_calls", 0) or 0),
                    int(usage.get("cache_hits", 0) or 0),
                    int(usage.get("prompt_cache_read_tokens", 0) or 0),
                    int(usage.get("prompt_cache_write_tokens", 0) or 0),
                    run_id,
                ),
            )
        else:
            self.conn.execute(
                "UPDATE runs SET finished_at=?, status=? WHERE run_id=?",
                (self._now(), status, run_id),
            )
        self.conn.commit()

    def add_source_usage(self, source_id: str, usage: dict) -> None:
        """Accumulate cost/token deltas onto a source row."""
        self.conn.execute(
            """UPDATE sources SET
                 cost_usd_total = COALESCE(cost_usd_total, 0) + ?,
                 cost_usd_llm = COALESCE(cost_usd_llm, 0) + ?,
                 cost_usd_embedding = COALESCE(cost_usd_embedding, 0) + ?,
                 tokens_prompt = COALESCE(tokens_prompt, 0) + ?,
                 tokens_completion = COALESCE(tokens_completion, 0) + ?,
                 tokens_embedding = COALESCE(tokens_embedding, 0) + ?,
                 updated_at=?
               WHERE source_id=?""",
            (
                float(usage.get("cost_usd_total", 0) or 0),
                float(usage.get("cost_usd_llm", 0) or 0),
                float(usage.get("cost_usd_embedding", 0) or 0),
                int(usage.get("tokens_prompt", 0) or 0),
                int(usage.get("tokens_completion", 0) or 0),
                int(usage.get("tokens_embedding", 0) or 0),
                self._now(),
                source_id,
            ),
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

    def get_recent_runs(self, limit: int = 30) -> list[dict]:
        """Newest-first run rows, including cost/token columns."""
        return self._fetchall(
            "SELECT * FROM runs ORDER BY run_id DESC LIMIT ?",
            (max(1, int(limit)),),
        )

    # ── Web job queue ──────────────────────────────────────────────────

    def recover_web_jobs(self) -> int:
        """Mark jobs left running by a process restart as interrupted.

        Queued work remains queued and is picked up by the new worker.
        """
        cur = self.conn.execute(
            "UPDATE web_jobs SET state='interrupted', phase='interrupted', "
            "message='Interrompido pela reinicializacao da aplicacao', finished_at=? "
            "WHERE state='running'",
            (self._now(),),
        )
        self.conn.commit()
        return cur.rowcount

    def create_web_job(self, job_id: str, operation: str, payload: dict) -> bool:
        """Atomically enqueue a job, allowing one mutating operation at a time."""
        now = self._now()
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            active = self.conn.execute(
                "SELECT job_id FROM web_jobs WHERE state IN ('queued','running') LIMIT 1"
            ).fetchone()
            if active:
                self.conn.rollback()
                return False
            self.conn.execute(
                "INSERT INTO web_jobs "
                "(job_id, operation, payload_json, state, phase, created_at) "
                "VALUES (?, ?, ?, 'queued', 'queued', ?)",
                (job_id, operation, json.dumps(payload), now),
            )
            self.conn.commit()
            return True
        except Exception:
            self.conn.rollback()
            raise

    def claim_web_job(self, job_id: str) -> bool:
        cur = self.conn.execute(
            "UPDATE web_jobs SET state='running', phase='starting', started_at=? "
            "WHERE job_id=? AND state='queued'",
            (self._now(), job_id),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def get_web_job(self, job_id: str) -> Optional[dict]:
        row = self._fetchone("SELECT * FROM web_jobs WHERE job_id=?", (job_id,))
        if row:
            row["payload"] = json.loads(row.pop("payload_json") or "{}")
            for key in ("result_json",):
                raw = row.get(key)
                row[key[:-5]] = json.loads(raw) if raw else None
        return row

    def list_web_jobs(self, limit: int = 50) -> list[dict]:
        rows = self._fetchall(
            "SELECT * FROM web_jobs ORDER BY created_at DESC LIMIT ?", (max(1, min(limit, 200)),)
        )
        for row in rows:
            raw = row.pop("payload_json", "{}")
            row["payload"] = json.loads(raw or "{}")
            result = row.pop("result_json", None)
            row["result"] = json.loads(result) if result else None
        return rows

    def update_web_job(
        self,
        job_id: str,
        *,
        state: str | None = None,
        phase: str | None = None,
        current_item: str | None = None,
        current_index: int | None = None,
        total_items: int | None = None,
        message: str | None = None,
        result: dict | None = None,
        error_message: str | None = None,
        run_id: int | None = None,
        finished: bool = False,
    ) -> None:
        row = self._fetchone("SELECT * FROM web_jobs WHERE job_id=?", (job_id,))
        if not row:
            return
        self.conn.execute(
            "UPDATE web_jobs SET state=COALESCE(?,state), phase=COALESCE(?,phase), "
            "current_item=COALESCE(?,current_item), current_index=COALESCE(?,current_index), "
            "total_items=COALESCE(?,total_items), message=COALESCE(?,message), "
            "result_json=COALESCE(?,result_json), error_message=COALESCE(?,error_message), "
            "run_id=COALESCE(?,run_id), finished_at=CASE WHEN ? THEN ? ELSE finished_at END "
            "WHERE job_id=?",
            (
                state, phase, current_item, current_index, total_items, message,
                json.dumps(result) if result is not None else None,
                error_message, run_id, finished, self._now() if finished else None, job_id,
            ),
        )
        self.conn.commit()

    def add_web_job_event(
        self, job_id: str, phase: str, *, current_item: str | None = None,
        current_index: int | None = None, total_items: int | None = None,
        message: str = "",
    ) -> None:
        self.conn.execute(
            "INSERT INTO web_job_events "
            "(job_id,phase,current_item,current_index,total_items,message,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (job_id, phase, current_item, current_index, total_items, message, self._now()),
        )
        self.conn.commit()

    def list_web_job_events(self, job_id: str, after_id: int = 0) -> list[dict]:
        return self._fetchall(
            "SELECT * FROM web_job_events WHERE job_id=? AND event_id>? ORDER BY event_id",
            (job_id, after_id),
        )

    def get_web_dashboard(self) -> dict[str, Any]:
        """Return aggregate operational metrics without loading note bodies."""
        stats = self.get_stats()
        count = lambda sql, params=(): int(
            self.conn.execute(sql, params).fetchone()["c"]  # type: ignore[index]
        )
        stats.update({
            "lit_index": count("SELECT COUNT(*) c FROM sources"),
            "lit_drafts": count("SELECT COUNT(*) c FROM chunks WHERE status='awaiting_review'"),
            "lit_approved": count("SELECT COUNT(*) c FROM chunks WHERE status IN ('approved','persisted')"),
            "permanent_notes": self.count_permanent_notes(),
            "manual_notes": count("SELECT COUNT(*) c FROM notes WHERE origin='manual'"),
            "isolated_notes": count(
                "SELECT COUNT(*) c FROM notes n WHERE NOT EXISTS "
                "(SELECT 1 FROM note_connections c WHERE c.source_note_id=n.note_id OR c.target_note_id=n.note_id)"
            ),
            "incomplete_sources": count(
                "SELECT COUNT(*) c FROM sources WHERE COALESCE(document_type,'')='' "
                "OR COALESCE(abnt_reference,'')=''"
            ),
        })
        confidence = self._fetchall(
            "SELECT CASE WHEN review_confidence IS NULL THEN 'sem avaliacao' "
            "WHEN review_confidence < 0.4 THEN 'baixa' "
            "WHEN review_confidence < ? THEN 'media' ELSE 'alta' END band, COUNT(*) c "
            "FROM chunks WHERE review_confidence IS NOT NULL GROUP BY band",
            (0.85,),
        )
        relations = self._fetchall(
            "SELECT relation_type, COUNT(*) c FROM note_connections "
            "GROUP BY relation_type ORDER BY c DESC"
        )
        origins = self._fetchall(
            "SELECT origin, COUNT(*) c FROM notes GROUP BY origin ORDER BY c DESC"
        )
        documents = self._fetchall(
            "SELECT COALESCE(document_type,'incompleto') document_type, COUNT(*) c "
            "FROM sources GROUP BY document_type ORDER BY c DESC"
        )
        sources_cost = self._fetchall(
            "SELECT source_id, title, cost_usd_total, tokens_prompt, tokens_completion "
            "FROM sources ORDER BY cost_usd_total DESC LIMIT 20"
        )
        from zettel.config import DEFAULT_RELATION_WEIGHTS
        note_titles = {row["note_id"]: row["title"] for row in self._fetchall(
            "SELECT note_id,title FROM notes"
        )}
        hubs = [
            {"note_id": note_id, "title": note_titles.get(note_id, note_id), "degree": degree}
            for note_id, degree in sorted(
                self.get_weighted_note_degrees(DEFAULT_RELATION_WEIGHTS).items(),
                key=lambda item: item[1], reverse=True,
            )[:10]
        ]
        return {
            "counts": stats, "confidence": confidence, "relations": relations,
            "origins": origins, "documents": documents, "sources_cost": sources_cost,
            "hubs": hubs,
            "runs": self._fetchall(
                "SELECT run_id,pipeline_signature,started_at,finished_at,status,"
                "cost_usd_total,tokens_prompt,tokens_completion,cache_hits,"
                "duplicate_file_count,duplicate_content_count,duplicate_semantic_count "
                "FROM runs ORDER BY run_id DESC LIMIT 10"
            ),
        }

    # ── Stats ──────────────────────────────────────────────────────────

    def get_stats(self) -> dict[str, int]:
        tables = ["files", "sources", "chapters", "chunks", "concepts", "notes", "mocs", "assets"]
        stats: dict[str, int] = {}
        for t in tables:
            row = self.conn.execute(f"SELECT COUNT(*) as cnt FROM {t}").fetchone()
            stats[t] = row["cnt"] if row else 0
        for status_key, status_val in (
            ("chunks_pending", "pending"),
            ("chunks_awaiting_review", "awaiting_review"),
            ("chunks_approved", "approved"),
            ("chunks_rejected", "rejected"),
            ("chunks_failed", "failed"),
        ):
            row = self.conn.execute(
                "SELECT COUNT(*) as cnt FROM chunks WHERE status=?", (status_val,)
            ).fetchone()
            stats[status_key] = row["cnt"] if row else 0
        # persisted counts as approved for status display
        persisted = self.conn.execute(
            "SELECT COUNT(*) as cnt FROM chunks WHERE status='persisted'"
        ).fetchone()
        stats["chunks_approved"] = stats.get("chunks_approved", 0) + (
            persisted["cnt"] if persisted else 0
        )
        return stats
