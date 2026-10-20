"""MOCs and the term -> note routing index built from them (ADR-036)."""

from __future__ import annotations

from collections.abc import Iterable

from zettel.search_terms import fold
from zettel.state.base import StateBase, placeholders

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
        parent_moc_id: str | None = None,
    ) -> None:
        """Insert or update a MOC row. ``parent_moc_id`` is kept when not given."""
        now = self._now()
        self.conn.execute(
            """INSERT INTO mocs (moc_id, topic, path, body, frontmatter_json, origin,
                                 cluster_signature, parent_moc_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(moc_id) DO UPDATE SET
                 topic=excluded.topic,
                 path=COALESCE(excluded.path, mocs.path),
                 body=COALESCE(excluded.body, mocs.body),
                 frontmatter_json=COALESCE(excluded.frontmatter_json, mocs.frontmatter_json),
                 origin=excluded.origin,
                 cluster_signature=excluded.cluster_signature,
                 parent_moc_id=COALESCE(excluded.parent_moc_id, mocs.parent_moc_id),
                 updated_at=excluded.updated_at""",
            (
                moc_id,
                topic,
                path,
                body,
                frontmatter_json,
                origin,
                cluster_signature,
                parent_moc_id,
                now,
                now,
            ),
        )
        self.conn.commit()

    def get_moc(self, moc_id: str) -> dict | None:
        return self._fetchone("SELECT * FROM mocs WHERE moc_id=?", (moc_id,))

    def get_moc_by_signature(self, signature: str) -> dict | None:
        return self._fetchone("SELECT * FROM mocs WHERE cluster_signature=?", (signature,))

    def list_mocs(self, origin: str | None = None) -> list[dict]:
        if origin is None:
            return self._fetchall("SELECT * FROM mocs ORDER BY created_at DESC")
        return self._fetchall(
            "SELECT * FROM mocs WHERE origin=? ORDER BY created_at DESC", (origin,)
        )

    def find_moc_by_topic(self, topic: str, *, origins: Iterable[str] | None) -> dict | None:
        """Newest MOC whose topic equals ``topic`` (accent/case-folded).

        ``origins`` is required so no caller crosses pipelines by accident:
        the taxonomy pipeline passes ``("pipeline",)``; ``None`` means any
        origin and must be spelled out. Equality, not substring: "Etica" must
        not route into "Etica Normativa e Metaetica".
        """
        wanted = fold(topic)
        if not wanted:
            return None
        allowed = None if origins is None else set(origins)
        for moc in self.list_mocs():
            if allowed is not None and moc.get("origin") not in allowed:
                continue
            if fold(moc["topic"]) == wanted:
                return moc
        return None

    def list_child_mocs(self, parent_moc_id: str) -> list[dict]:
        """Sub-MOCs of a MOC of MOCs (#296), oldest first."""
        return self._fetchall(
            "SELECT * FROM mocs WHERE parent_moc_id=? ORDER BY created_at, moc_id",
            (parent_moc_id,),
        )

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

    def delete_mocs(self, moc_ids: list[str]) -> None:
        """Remove MOC rows by id (the caller decides which ones are safe)."""
        if not moc_ids:
            return
        marks = placeholders(moc_ids)
        self.conn.execute(f"DELETE FROM moc_discards WHERE moc_id IN ({marks})", tuple(moc_ids))
        self.conn.execute(f"DELETE FROM mocs WHERE moc_id IN ({marks})", tuple(moc_ids))
        self.conn.commit()

    # ── Discarded entry candidates (#296) ──────────────────────────────

    def record_moc_discards(
        self,
        moc_id: str,
        rows: list[dict],
        *,
        prompt_checksum: str,
        model: str,
    ) -> None:
        """Keep the notes the LLM answered "ignorar" for ``moc_id``.

        Each row carries ``note_id``, ``reason`` and ``note_checksum`` (the
        note's semantic checksum when it was judged).
        """
        now = self._now()
        self.conn.executemany(
            """INSERT OR REPLACE INTO moc_discards
               (moc_id, note_id, reason, note_checksum, prompt_checksum, model, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    moc_id,
                    r["note_id"],
                    r.get("reason") or "",
                    r.get("note_checksum") or "",
                    prompt_checksum,
                    model,
                    now,
                )
                for r in rows
            ],
        )
        self.conn.commit()

    def moc_discard_checksums(self, moc_id: str) -> dict[str, str]:
        """``note_id -> note_checksum`` of the notes discarded for ``moc_id``."""
        rows = self._fetchall(
            "SELECT note_id, note_checksum FROM moc_discards WHERE moc_id=?", (moc_id,)
        )
        return {r["note_id"]: r["note_checksum"] for r in rows}

    def clear_moc_discards(self) -> int:
        """Forget every discard so the next garden offers those notes again."""
        cur = self.conn.execute("DELETE FROM moc_discards")
        self.conn.commit()
        return cur.rowcount

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
