"""Chunks: the extract queue, review checkpoints and the granular-LIT pickers."""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from zettel.search_terms import fold, fts_match_expr
from zettel.state.base import StateBase
from zettel.state.sources import escape_like

logger = logging.getLogger(__name__)

# Picker columns — never ``chunks.text``.
_LIT_PICKER_COLS = (
    "chunk_id",
    "source_id",
    "section_path",
    "locator",
    "page_in_book",
    "chunk_index",
    "literature_note_path",
)


class ChunksMixin(StateBase):
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
                 literature_note_path=COALESCE(
                     excluded.literature_note_path, chunks.literature_note_path
                 ),
                 literature_id=COALESCE(excluded.literature_id, chunks.literature_id),
                 review_confidence=COALESCE(excluded.review_confidence, chunks.review_confidence),
                 summary_json=COALESCE(excluded.summary_json, chunks.summary_json)""",
            (
                chunk_id,
                source_id,
                chapter_id,
                text,
                chunk_checksum,
                locator,
                section_path,
                status,
                chunk_index,
                page_in_file,
                page_in_book,
                page_confidence,
                literature_note_path,
                literature_id,
                review_confidence,
                summary_json,
            ),
        )
        self._fts_index_chunk(chunk_id, text)
        self.conn.commit()

    def get_chunk(self, chunk_id: str) -> dict | None:
        return self._fetchone("SELECT * FROM chunks WHERE chunk_id=?", (chunk_id,))

    def get_chunks_for_source(self, source_id: str) -> list[dict]:
        return self._fetchall("SELECT * FROM chunks WHERE source_id=?", (source_id,))

    def get_chunks_for_chapter(self, chapter_id: str) -> list[dict]:
        return self._fetchall(
            "SELECT * FROM chunks WHERE chapter_id=? ORDER BY chunk_index", (chapter_id,)
        )

    def get_chunks_by_status(self, status: str, source_id: str | None = None) -> list[dict]:
        """Chunks in ``status``, in document order (by source, then ``chunk_index``)."""
        return self._fetchall(
            "SELECT * FROM chunks WHERE status=? AND (? IS NULL OR source_id=?) "
            "ORDER BY source_id, chunk_index ASC",
            (status, source_id or None, source_id),
        )

    # ── Status / review checkpoints ────────────────────────────────────

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
                status,
                literature_note_path,
                literature_id,
                review_confidence,
                summary_json,
                page_in_book,
                page_confidence,
                llm_prompt1_hash,
                llm_call_checksum,
                chunk_id,
            ),
        )
        self.conn.commit()

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

    def reset_chunks_to_pending(
        self,
        status: str,
        source_id: str | None = None,
        *,
        drop_llm_cache: bool = False,
    ) -> int:
        """Move chunks in ``status`` back to ``pending``. Returns how many moved.

        ``drop_llm_cache`` is for extract *verdicts* (``rejected``): the SQLite
        response cache would otherwise replay the same empty yield for free.
        """
        rows = self.get_chunks_by_status(status, source_id=source_id)
        return sum(
            self.reset_chunk_to_pending(row["chunk_id"], drop_llm_cache=drop_llm_cache)
            for row in rows
        )

    def reset_chunk_to_pending(self, chunk_id: str, *, drop_llm_cache: bool = False) -> bool:
        """Move one chunk back to ``pending``; see :meth:`reset_chunks_to_pending`."""
        row = self.get_chunk(chunk_id)
        if not row:
            return False
        if drop_llm_cache:
            self.delete_llm_cache([row.get("llm_call_checksum_prompt1") or ""])
        self.update_chunk_status(chunk_id, "pending")
        return True

    # ── Deletion ───────────────────────────────────────────────────────

    def delete_chunks(self, chunk_ids: list[str]) -> int:
        """Delete chunks by id (SQLite + FTS + concepts). Returns how many were removed."""
        if not chunk_ids:
            return 0
        params = [(cid,) for cid in chunk_ids]
        self.conn.executemany("DELETE FROM concepts WHERE chunk_id=?", params)
        removed = self.conn.executemany("DELETE FROM chunks WHERE chunk_id=?", params).rowcount
        self._fts_delete_chunks(chunk_ids)
        self.conn.commit()
        return removed

    def delete_chunks_for_chapter(self, chapter_id: str, keep_ids: set[str]) -> list[str]:
        """Delete chunks of a chapter whose id is not in keep_ids. Returns removed ids.

        Used after re-chunking a chapter so stale chunks (from an earlier chunking
        config or edited text) don't linger in SQLite and ChromaDB.
        """
        rows = self._fetchall("SELECT chunk_id FROM chunks WHERE chapter_id=?", (chapter_id,))
        removed = [r["chunk_id"] for r in rows if r["chunk_id"] not in keep_ids]
        self.delete_chunks(removed)
        return removed

    def delete_chapter(self, chapter_id: str) -> list[str]:
        """Delete a chapter and all its chunks. Returns removed chunk_ids."""
        removed = self.delete_chunks_for_chapter(chapter_id, keep_ids=set())
        self._fts_delete_chapter_summaries("chapter_id=?", (chapter_id,))
        self.conn.execute("DELETE FROM chapters WHERE chapter_id=?", (chapter_id,))
        self.conn.commit()
        return removed

    # ── Granular LIT pickers (web /notes/new) ──────────────────────────

    def search_literature_chunks(
        self,
        query: str = "",
        source_id: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        """Picker lookup over chunks that already have a literature note on disk.

        Matches folded ``section_path`` / ``locator`` / ``chunk_id``. Does not
        select ``chunks.text``. Empty ``query`` returns the lowest ``chunk_index``.
        """
        limit = max(1, min(int(limit), 50))
        query = (query or "")[:200]
        where = ["literature_note_path IS NOT NULL", "literature_note_path <> ''"]
        params: list[Any] = []
        if source_id:
            where.append("source_id=?")
            params.append(source_id)
        if query.strip():
            folded = fold(query)
            if not folded:
                return []
            pattern = f"%{escape_like(folded)}%"
            where.append(
                "(zfold(section_path) LIKE ? ESCAPE '\\' "
                "OR zfold(locator) LIKE ? ESCAPE '\\' "
                "OR zfold(chunk_id) LIKE ? ESCAPE '\\')"
            )
            params.extend((pattern, pattern, pattern))
        params.append(limit)
        return self._fetchall(
            f"SELECT {', '.join(_LIT_PICKER_COLS)} FROM chunks WHERE {' AND '.join(where)} "
            "ORDER BY chunk_index, chunk_id LIMIT ?",
            tuple(params),
        )

    def search_literature_chunks_fts(
        self,
        query: str,
        source_id: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        """Second layer: match the chunk body via FTS5 without selecting ``text``."""
        if not self.fts_enabled:
            return []
        limit = max(1, min(int(limit), 50))
        match = fts_match_expr((query or "")[:200])
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
        cols = ", ".join(f"c.{name}" for name in _LIT_PICKER_COLS)
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
        current = self.conn.execute(
            "SELECT COALESCE(MAX(chunk_index), 0) FROM chunks "
            "WHERE source_id=? AND chunk_id LIKE '%::manual::%'",
            (source_id,),
        ).fetchone()[0]
        return int(current) + 1
