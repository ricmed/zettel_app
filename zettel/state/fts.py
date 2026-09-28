"""BM25 lexical search over FTS5, and its rebuild from the source tables."""

from __future__ import annotations

import logging
import sqlite3

from zettel.search_terms import fts_match_expr
from zettel.state.base import StateBase

logger = logging.getLogger(__name__)

# FTS table -> the INSERT ... SELECT that repopulates it from its source table.
_FTS_POPULATE = {
    "fts_notes": (
        "INSERT INTO fts_notes (note_id, title, body) "
        "SELECT note_id, COALESCE(title,''), COALESCE(body,'') FROM notes"
    ),
    "fts_chunks": (
        "INSERT INTO fts_chunks (chunk_id, text) SELECT chunk_id, COALESCE(text,'') FROM chunks"
    ),
    "fts_chapter_summaries": (
        "INSERT INTO fts_chapter_summaries (chapter_id, title, summary) "
        "SELECT chapter_id, COALESCE(title,''), summary FROM chapters "
        "WHERE summary IS NOT NULL AND summary <> ''"
    ),
}


class FtsMixin(StateBase):
    def search_notes_fts(self, query: str, limit: int = 20) -> list[dict]:
        """BM25 lexical search over notes. Returns ``[{note_id, rank}]``."""
        return self._fts_search("fts_notes", "note_id", query, limit)

    def search_chapter_summaries_fts(self, query: str, limit: int = 20) -> list[dict]:
        """BM25 lexical search over chapter summaries. Returns ``[{chapter_id, rank}]``."""
        return self._fts_search("fts_chapter_summaries", "chapter_id", query, limit)

    def _fts_search(self, table: str, id_col: str, query: str, limit: int) -> list[dict]:
        """FTS5's ``rank`` is already best-first (more negative = better match), so
        ``ORDER BY rank`` needs no sign flip. Empty when FTS is disabled or the
        query has no usable term.
        """
        if not self.fts_enabled:
            return []
        match = fts_match_expr(query)
        if not match:
            return []
        try:
            return self._fetchall(
                f"SELECT {id_col}, rank FROM {table} WHERE {table} MATCH ? ORDER BY rank LIMIT ?",
                (match, limit),
            )
        except sqlite3.OperationalError as e:
            logger.warning("Busca FTS em %s falhou: %s", table, e)
            return []

    def rebuild_fts(self) -> dict[str, int]:
        """Rebuild every FTS table from scratch from its source table. Returns counts.

        Wired into ``zettel reindex`` so the lexical index is a disposable cache
        reconstructible from the SQLite source of truth, like the vector index.
        """
        if not self.fts_enabled:
            return dict.fromkeys(_FTS_POPULATE, 0)
        for table, populate in _FTS_POPULATE.items():
            self.conn.execute(f"DELETE FROM {table}")
            self.conn.execute(populate)
        self.conn.commit()
        return {table: self._count(f"SELECT COUNT(*) FROM {table}") for table in _FTS_POPULATE}
