"""Shared plumbing for the ``StateDB`` mixins: row helpers and FTS5 sync.

Every domain mixin inherits from :class:`StateBase`. ``StateDB`` composes all
of them, so a mixin may call another mixin's public method through ``self``
(e.g. deleting a source deletes its chunks); ``tests/test_state_package.py``
guarantees no two mixins define the same name. The FTS writers never commit:
they run inside the caller's write, before its commit.
"""

from __future__ import annotations

import sqlite3

from zettel.time import now_utc_iso


def placeholders(values: list | tuple | set) -> str:
    """``?,?,?`` for an ``IN (...)`` clause with one mark per value."""
    return ",".join("?" * len(values))


class StateBase:
    conn: sqlite3.Connection
    # True if the SQLite build supports FTS5 (set by ``StateDB._init_fts``).
    # When False, the hybrid retriever falls back to vector-only search.
    fts_enabled: bool

    # ── Row helpers ────────────────────────────────────────────────────

    def _now(self) -> str:
        return now_utc_iso()

    def _fetchone(self, sql: str, params: tuple = ()) -> dict | None:
        row = self.conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def _fetchall(self, sql: str, params: tuple = ()) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def _count(self, sql: str, params: tuple = ()) -> int:
        """First column of the first row — meant for ``SELECT COUNT(*) ...``."""
        return int(self.conn.execute(sql, params).fetchone()[0])

    # ── FTS5 sync (no commit) ──────────────────────────────────────────

    def _fts_index_note(self, note_id: str) -> None:
        """Refresh the FTS row for a note from its (already-written) notes row.

        Called inside ``upsert_note`` *before* commit, so the resolved
        post-COALESCE title/body are visible on the same connection.
        """
        if not self.fts_enabled:
            return
        self.conn.execute("DELETE FROM fts_notes WHERE note_id=?", (note_id,))
        self.conn.execute(
            "INSERT INTO fts_notes (note_id, title, body) "
            "SELECT note_id, COALESCE(title,''), COALESCE(body,'') FROM notes WHERE note_id=?",
            (note_id,),
        )

    def _fts_delete_note(self, note_id: str) -> None:
        if self.fts_enabled:
            self.conn.execute("DELETE FROM fts_notes WHERE note_id=?", (note_id,))

    def _fts_index_chunk(self, chunk_id: str, text: str) -> None:
        if not self.fts_enabled:
            return
        self.conn.execute("DELETE FROM fts_chunks WHERE chunk_id=?", (chunk_id,))
        self.conn.execute(
            "INSERT INTO fts_chunks (chunk_id, text) VALUES (?, ?)", (chunk_id, text or "")
        )

    def _fts_delete_chunks(self, chunk_ids: list[str]) -> None:
        if self.fts_enabled:
            self.conn.executemany(
                "DELETE FROM fts_chunks WHERE chunk_id=?", [(cid,) for cid in chunk_ids]
            )

    def _fts_index_chapter_summary(self, chapter_id: str, title: str, summary: str) -> None:
        if not self.fts_enabled:
            return
        self.conn.execute("DELETE FROM fts_chapter_summaries WHERE chapter_id=?", (chapter_id,))
        if summary:
            self.conn.execute(
                "INSERT INTO fts_chapter_summaries (chapter_id, title, summary) VALUES (?, ?, ?)",
                (chapter_id, title or "", summary),
            )

    def _fts_delete_chapter_summaries(self, where: str, params: tuple) -> None:
        """Drop the FTS rows of the chapters matching ``where`` (on ``chapters``)."""
        if self.fts_enabled:
            self.conn.execute(
                "DELETE FROM fts_chapter_summaries WHERE chapter_id IN "
                f"(SELECT chapter_id FROM chapters WHERE {where})",
                params,
            )
