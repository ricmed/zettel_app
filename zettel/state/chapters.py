"""Chapters, their summaries (ADR-047) and the chapter <-> note aggregates."""

from __future__ import annotations

import json

from zettel.state.base import StateBase, placeholders


class ChaptersMixin(StateBase):
    def upsert_chapter(
        self,
        chapter_id: str,
        source_id: str,
        title: str,
        chapter_checksum: str,
        locator: str = "",
    ) -> None:
        # The summary_* columns are deliberately absent from DO UPDATE SET: a
        # re-harvest preserves the summary and the checksum divergence marks it stale.
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
        """Chapters in document order — `chapter_id` ends in `chNNN`, so it sorts."""
        return self._fetchall(
            "SELECT * FROM chapters WHERE source_id=? ORDER BY chapter_id", (source_id,)
        )

    def get_chapter(self, chapter_id: str) -> dict | None:
        return self._fetchone("SELECT * FROM chapters WHERE chapter_id=?", (chapter_id,))

    # ── Summaries (ADR-047) ────────────────────────────────────────────

    def update_chapter_summary(
        self,
        chapter_id: str,
        summary: str,
        summary_topics: list[str],
        summary_checksum: str,
        summary_model: str,
        updated_at: str,
    ) -> None:
        """Persist a chapter summary and mirror it into FTS5.

        `summary_checksum` is the `chapter_checksum` the summary was generated
        from; a later re-chunk changes the latter and the divergence is what
        marks the summary stale.
        """
        self.conn.execute(
            """UPDATE chapters
                  SET summary=?, summary_topics=?, summary_checksum=?,
                      summary_model=?, summary_updated_at=?
                WHERE chapter_id=?""",
            (
                summary,
                json.dumps(summary_topics, ensure_ascii=False),
                summary_checksum,
                summary_model,
                updated_at,
                chapter_id,
            ),
        )
        row = self.conn.execute(
            "SELECT title FROM chapters WHERE chapter_id=?", (chapter_id,)
        ).fetchone()
        self._fts_index_chapter_summary(chapter_id, row["title"] if row else "", summary)
        self.conn.commit()

    def get_chapters_with_summaries(self, source_id: str | None = None) -> list[dict]:
        """Chapters that already carry a summary, in document order."""
        return self._fetchall(
            "SELECT * FROM chapters WHERE summary IS NOT NULL AND summary <> '' "
            "AND (? IS NULL OR source_id=?) ORDER BY chapter_id",
            (source_id or None, source_id),
        )

    def get_chapters_needing_summary(self, source_id: str | None = None) -> list[dict]:
        """Chapters whose summary is missing or stale (checksum drifted)."""
        # The OR group must stay parenthesised: without it the source filter
        # binds only to the last branch and silently does nothing.
        return self._fetchall(
            "SELECT * FROM chapters "
            "WHERE (summary IS NULL OR summary = '' "
            "       OR summary_checksum IS NULL OR summary_checksum <> chapter_checksum) "
            "AND (? IS NULL OR source_id=?) ORDER BY chapter_id",
            (source_id or None, source_id),
        )

    def get_chapter_summary_texts(self, chapter_ids: list[str]) -> dict[str, str]:
        """`{chapter_id: "title summary"}` — what FTS5 indexed for each chapter."""
        if not chapter_ids:
            return {}
        unique = list(dict.fromkeys(chapter_ids))
        rows = self._fetchall(
            "SELECT chapter_id, title, summary FROM chapters "
            f"WHERE chapter_id IN ({placeholders(unique)})",
            tuple(unique),
        )
        return {r["chapter_id"]: f"{r['title'] or ''} {r['summary'] or ''}" for r in rows}

    # ── Chapter <-> note aggregates ────────────────────────────────────

    def get_chapter_note_counts(self, source_id: str | None = None) -> dict[str, int]:
        """Permanent notes produced by each chapter — a pure SQL aggregate.

        The path is chapters <- chunks.chapter_id <- concepts.chunk_id ->
        notes.note_id. Counting distinct notes matters because one chunk can
        yield several concepts that were merged into a single note.
        """
        rows = self._fetchall(
            """SELECT k.chapter_id AS chapter_id, COUNT(DISTINCT c.note_id) AS n
                 FROM concepts c
                 JOIN chunks k ON k.chunk_id = c.chunk_id
                 JOIN notes n ON n.note_id = c.note_id
                WHERE c.note_id IS NOT NULL AND (? IS NULL OR k.source_id=?)
                GROUP BY k.chapter_id""",
            (source_id or None, source_id),
        )
        return {r["chapter_id"]: r["n"] for r in rows}

    def get_notes_for_chapter(self, chapter_id: str) -> list[dict]:
        """Permanent notes derived from a chapter's chunks, in chunk order."""
        return self._fetchall(
            """SELECT DISTINCT n.*, k.chunk_index AS _chunk_index
                 FROM concepts c
                 JOIN chunks k ON k.chunk_id = c.chunk_id
                 JOIN notes n ON n.note_id = c.note_id
                WHERE k.chapter_id=? AND c.note_id IS NOT NULL
                ORDER BY k.chunk_index, n.note_id""",
            (chapter_id,),
        )

    def get_chapters_for_note(self, note_id: str) -> list[str]:
        """Chapters a permanent note came from — the reverse of the note count.

        Usually one, but a merged note can carry concepts from several chunks
        and therefore from more than one chapter.
        """
        rows = self._fetchall(
            """SELECT DISTINCT k.chapter_id AS chapter_id
                 FROM concepts c
                 JOIN chunks k ON k.chunk_id = c.chunk_id
                WHERE c.note_id=?""",
            (note_id,),
        )
        return [r["chapter_id"] for r in rows]

    def get_chapter_page_ranges(self, source_id: str) -> dict[str, tuple[int, int]]:
        """First/last `page_in_book` per chapter. Empty for page-less sources."""
        rows = self._fetchall(
            """SELECT chapter_id, MIN(page_in_book) AS lo, MAX(page_in_book) AS hi
                 FROM chunks
                WHERE source_id=? AND page_in_book IS NOT NULL
                GROUP BY chapter_id""",
            (source_id,),
        )
        return {r["chapter_id"]: (r["lo"], r["hi"]) for r in rows}
