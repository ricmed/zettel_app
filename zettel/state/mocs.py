"""MOCs and the term -> note routing index built from them (ADR-036)."""

from __future__ import annotations

from zettel.state.base import StateBase

# `hub_note_id` lives in the frontmatter mirror; json_valid guards a hand-edited
# row with malformed JSON (json_extract would raise on it).
_HUB_NOTE_ID = (
    "CASE WHEN json_valid(frontmatter_json) "
    "THEN json_extract(frontmatter_json, '$.hub_note_id') END"
)


class MocsMixin(StateBase):
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

    def get_moc(self, moc_id: str) -> dict | None:
        return self._fetchone("SELECT * FROM mocs WHERE moc_id=?", (moc_id,))

    def get_moc_by_signature(self, signature: str) -> dict | None:
        return self._fetchone("SELECT * FROM mocs WHERE cluster_signature=?", (signature,))

    def list_mocs(self) -> list[dict]:
        return self._fetchall("SELECT * FROM mocs ORDER BY created_at DESC")

    def find_moc_by_topic(self, topic: str) -> dict | None:
        """Find existing MOC whose topic has a bidirectional substring match."""
        topic_lower = topic.lower()
        for moc in self.list_mocs():
            existing_lower = moc["topic"].lower()
            if existing_lower in topic_lower or topic_lower in existing_lower:
                return moc
        return None

    def find_moc_by_hub_note_id(self, hub_note_id: str) -> dict | None:
        """Newest hub_pipeline MOC anchored on ``hub_note_id``."""
        return self._fetchone(
            f"SELECT * FROM mocs WHERE origin='hub_pipeline' AND {_HUB_NOTE_ID}=? "
            "ORDER BY created_at DESC LIMIT 1",
            (hub_note_id,),
        )

    def list_hub_anchor_note_ids(self) -> set[str]:
        """hub_note_id values from existing hub_pipeline MOCs."""
        rows = self._fetchall(
            f"SELECT {_HUB_NOTE_ID} AS hub_note_id FROM mocs WHERE origin='hub_pipeline'"
        )
        return {r["hub_note_id"] for r in rows if r["hub_note_id"]}

    def delete_pipeline_mocs(self) -> list[dict]:
        """Remove pipeline MOC rows and return the deleted records."""
        return self._delete_mocs_by_origin("pipeline")

    def delete_hub_pipeline_mocs(self) -> list[dict]:
        """Remove hub_pipeline MOC rows and return the deleted records."""
        return self._delete_mocs_by_origin("hub_pipeline")

    def _delete_mocs_by_origin(self, origin: str) -> list[dict]:
        rows = self._fetchall("SELECT * FROM mocs WHERE origin=?", (origin,))
        if rows:
            self.conn.execute("DELETE FROM mocs WHERE origin=?", (origin,))
            self.conn.commit()
        return rows

    # ── Topic index (term -> note routing) ─────────────────────────────

    def replace_topic_index_terms(
        self,
        scope_kind: str,
        scope_id: str,
        rows: list[dict],
    ) -> int:
        """Replace every term row for one scope. Returns how many were written.

        Replace rather than merge: the index is regenerated wholesale on each
        refresh, and a term that disappeared from the notes must disappear
        from the lookup too.
        """
        self.conn.execute(
            "DELETE FROM topic_index_terms WHERE scope_kind=? AND scope_id=?",
            (scope_kind, scope_id),
        )
        self.conn.executemany(
            """INSERT OR REPLACE INTO topic_index_terms
               (scope_kind, scope_id, term, term_folded, target, note_id)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [
                (scope_kind, scope_id, r["term"], r["term_folded"], r["target"], r.get("note_id"))
                for r in rows
            ],
        )
        self.conn.commit()
        return len(rows)

    def delete_topic_index_scope(self, scope_kind: str, scope_id: str) -> None:
        self.replace_topic_index_terms(scope_kind, scope_id, [])

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
