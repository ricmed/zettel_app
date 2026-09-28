"""Concepts: permanent-note candidates between extract, review and connect."""

from __future__ import annotations

import json

from zettel.state.base import StateBase, placeholders


class ConceptsMixin(StateBase):
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
        # `status` binds twice: the insert defaults it to 'pending', the update
        # keeps the stored value when the caller passes None.
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
            (
                concept_id,
                source_id,
                chunk_id,
                anchor_hash,
                thesis_hash,
                note_id,
                candidate_json,
                status,
                status,
            ),
        )
        self.conn.commit()

    def get_concept(self, concept_id: str) -> dict | None:
        return self._fetchone("SELECT * FROM concepts WHERE concept_id=?", (concept_id,))

    def get_concepts_for_chunk(self, chunk_id: str) -> list[dict]:
        return self._fetchall("SELECT * FROM concepts WHERE chunk_id=?", (chunk_id,))

    def get_concepts_for_source(
        self,
        source_id: str,
        *,
        without_notes: bool = False,
    ) -> list[dict]:
        """Concepts belonging to ``source_id``, optionally only those still without a note."""
        sql = "SELECT * FROM concepts WHERE source_id=?"
        if without_notes:
            sql += " AND note_id IS NULL"
        return self._fetchall(sql, (source_id,))

    def get_concepts_by_status(self, status: str, without_notes: bool = False) -> list[dict]:
        """Return concepts in a given status. If without_notes, only unnoted ones.

        The `approved` + `without_notes` combination is how `connect` loads
        pending candidates from the DB (source of truth after review).
        """
        sql = "SELECT * FROM concepts WHERE status=?"
        if without_notes:
            sql += " AND note_id IS NULL"
        return self._fetchall(sql, (status,))

    def get_concepts_for_notes(self, note_ids: list[str]) -> dict[str, dict]:
        """Batch-fetch the concept row behind each note, keyed by ``note_id``.

        One query instead of N, for consumers that need the original candidate
        (relevance score, author judgement) alongside a set of notes.
        """
        if not note_ids:
            return {}
        rows = self._fetchall(
            f"SELECT * FROM concepts WHERE note_id IN ({placeholders(note_ids)})",
            tuple(note_ids),
        )
        return {row["note_id"]: row for row in rows}

    def update_concept_status(self, concept_id: str, status: str) -> None:
        self.conn.execute("UPDATE concepts SET status=? WHERE concept_id=?", (status, concept_id))
        self.conn.commit()

    def update_concepts_status_for_chunk(self, chunk_id: str, status: str) -> None:
        self.conn.execute("UPDATE concepts SET status=? WHERE chunk_id=?", (status, chunk_id))
        self.conn.commit()

    def set_concept_dedupe(self, concept_id: str, status: str, dedupe: dict | None = None) -> None:
        """Record a dedupe outcome. ``dedupe=None`` keeps the stored payload, so
        an ``override`` the reviewer set survives a later approval."""
        if dedupe is None:
            self.update_concept_status(concept_id, status)
            return
        self.conn.execute(
            "UPDATE concepts SET status=?, dedupe_json=? WHERE concept_id=?",
            (status, json.dumps(dedupe, ensure_ascii=False), concept_id),
        )
        self.conn.commit()
