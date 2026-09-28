"""Permanent notes and the web catalog over notes + MOCs."""

from __future__ import annotations

import json

from zettel.search_terms import fold
from zettel.state.base import StateBase, placeholders
from zettel.state.sources import escape_like

# A note is permanent when its vault path sits under 30_Permanent/. The folder
# name has no slash in it, so the test is independent of the path's slash style.
_PERMANENT_PATH = "instr(path, '30_Permanent') > 0"


class NotesMixin(StateBase):
    def upsert_note(
        self,
        note_id: str,
        source_id: str | None,
        path: str | None,
        title: str = "",
        note_semantic_checksum: str | None = None,
        embedding_model: str | None = None,
        body: str | None = None,
        frontmatter_json: str | None = None,
        origin: str = "pipeline",
        provenance_json: str | None = None,
    ) -> None:
        """Insert or update a note row.

        ``frontmatter_json`` mirrors the file (what `rebuild` writes back);
        ``provenance_json`` holds what `connect` knows but keeps out of the
        file (literature ref, locator, citation, anchor quote, per-note LLM
        cost). It is COALESCEd, so `sync-manual` re-reading a file never wipes it.
        """
        now = self._now()
        self.conn.execute(
            """INSERT INTO notes (note_id, source_id, path, title, body, frontmatter_json,
                                  provenance_json, origin, note_semantic_checksum,
                                  embedding_model, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(note_id) DO UPDATE SET
                 path=COALESCE(excluded.path, notes.path),
                 title=excluded.title,
                 body=COALESCE(excluded.body, notes.body),
                 frontmatter_json=COALESCE(excluded.frontmatter_json, notes.frontmatter_json),
                 provenance_json=COALESCE(excluded.provenance_json, notes.provenance_json),
                 origin=excluded.origin,
                 note_semantic_checksum=excluded.note_semantic_checksum,
                 embedding_model=COALESCE(excluded.embedding_model, notes.embedding_model),
                 updated_at=excluded.updated_at""",
            (
                note_id,
                source_id,
                path,
                title,
                body,
                frontmatter_json,
                provenance_json,
                origin,
                note_semantic_checksum,
                embedding_model,
                now,
                now,
            ),
        )
        self._fts_index_note(note_id)
        self.conn.commit()

    def update_note_embedding(
        self,
        note_id: str,
        embedding_input_hash: str,
        embedding_model: str | None = None,
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

    def delete_note(self, note_id: str) -> bool:
        """Delete a permanent note row (+ FTS + graph edges). Returns True if removed."""
        if not self.conn.execute("DELETE FROM notes WHERE note_id=?", (note_id,)).rowcount:
            return False
        self._fts_delete_note(note_id)
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

    # ── Lookups ────────────────────────────────────────────────────────

    def get_note(self, note_id: str) -> dict | None:
        return self._fetchone("SELECT * FROM notes WHERE note_id=?", (note_id,))

    def list_notes(self) -> list[dict]:
        return self._fetchall("SELECT * FROM notes ORDER BY created_at DESC")

    def get_notes_for_source(self, source_id: str) -> list[dict]:
        return self._fetchall(
            "SELECT * FROM notes WHERE source_id=? ORDER BY created_at ASC", (source_id,)
        )

    def get_note_ids_for_source(self, source_id: str) -> list[str]:
        """Permanent note ids linked via notes.source_id or concepts.note_id."""
        rows = self._fetchall(
            "SELECT note_id FROM notes WHERE source_id=? "
            "UNION SELECT note_id FROM concepts WHERE source_id=? AND note_id IS NOT NULL "
            "ORDER BY note_id",
            (source_id, source_id),
        )
        return [r["note_id"] for r in rows]

    def get_notes_for_chunk(self, chunk_id: str) -> list[dict]:
        """Permanent notes written from this chunk's concepts, oldest first."""
        return self._fetchall(
            """SELECT DISTINCT n.*
                 FROM concepts c
                 JOIN notes n ON n.note_id = c.note_id
                WHERE c.chunk_id=? AND c.note_id IS NOT NULL
                ORDER BY n.created_at, n.note_id""",
            (chunk_id,),
        )

    def get_note_texts(self, note_ids: list[str]) -> dict[str, str]:
        """`{note_id: "title body"}` for many notes in one query.

        Mirrors what FTS5 indexed for a note (see ``schema.FTS_SQL``), so a
        lexical coverage check reads the same surface BM25 searched.
        """
        if not note_ids:
            return {}
        unique = list(dict.fromkeys(note_ids))
        rows = self._fetchall(
            f"SELECT note_id, title, body FROM notes WHERE note_id IN ({placeholders(unique)})",
            tuple(unique),
        )
        return {r["note_id"]: f"{r['title'] or ''} {r['body'] or ''}" for r in rows}

    def list_permanent_note_ids(self) -> set[str]:
        """Note IDs whose vault path is under 30_Permanent/."""
        rows = self._fetchall(f"SELECT note_id FROM notes WHERE {_PERMANENT_PATH}")
        return {r["note_id"] for r in rows}

    def count_permanent_notes(self) -> int:
        """Count notes under ``30_Permanent/`` (path slash style agnostic)."""
        return self._count(f"SELECT COUNT(*) FROM notes WHERE {_PERMANENT_PATH}")

    # ── Web catalog (notes + MOCs) ─────────────────────────────────────

    def catalog_facets(self) -> dict:
        """Filter options backed by actual indexed notes, not unrelated sources."""
        source_rows = self._fetchall(
            """SELECT DISTINCT n.source_id, COALESCE(s.citekey, n.source_id) AS citekey,
                      s.title, s.authors
               FROM notes n LEFT JOIN sources s ON s.source_id=n.source_id
               WHERE n.source_id IS NOT NULL
               ORDER BY s.title COLLATE NOCASE, n.source_id"""
        )
        authors: set[str] = set()
        for row in source_rows:
            try:
                names = json.loads(row["authors"] or "[]")
            except (TypeError, ValueError):
                names = []
            if isinstance(names, list):
                authors.update(
                    name.strip() for name in names if isinstance(name, str) and name.strip()
                )
        origins = self._fetchall(
            "SELECT origin FROM notes UNION SELECT origin FROM mocs ORDER BY origin"
        )
        return {
            "sources": source_rows,
            "authors": sorted(authors, key=fold),
            "origins": [row["origin"] for row in origins],
        }

    def search_catalog(
        self,
        *,
        query: str = "",
        kind: str = "",
        source_id: str = "",
        author: str = "",
        origin: str = "",
        sort: str = "recent",
        page: int = 1,
        per_page: int = 12,
    ) -> tuple[list[dict], int]:
        """Search both indexed collections with one stable, paginated result set."""
        catalog = """
            WITH catalog AS (
              SELECT 'ZTL' AS kind, n.note_id AS item_id, n.title, n.body, n.path,
                     n.origin, n.updated_at, n.source_id, s.title AS source_title,
                     s.authors
                FROM notes n LEFT JOIN sources s ON s.source_id=n.source_id
              UNION ALL
              SELECT 'MOC', m.moc_id, m.topic, m.body, m.path, m.origin,
                     m.updated_at, NULL, NULL, NULL
                FROM mocs m
            )
        """
        clauses: list[str] = []
        params: list[str] = []
        if kind in {"ZTL", "MOC"}:
            clauses.append("kind=?")
            params.append(kind)
        if query.strip():
            pattern = f"%{escape_like(fold(query[:200].strip()))}%"
            clauses.append("(zfold(title) LIKE ? ESCAPE '\\' OR zfold(body) LIKE ? ESCAPE '\\')")
            params.extend((pattern, pattern))
        if source_id:
            clauses.append("source_id=?")
            params.append(source_id)
        if author:
            clauses.append(
                "EXISTS (SELECT 1 FROM json_each("
                "CASE WHEN json_valid(authors) THEN authors ELSE '[]' END"
                ") WHERE value=?)"
            )
            params.append(author)
        if origin:
            clauses.append("origin=?")
            params.append(origin)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        total = self._count(catalog + "SELECT COUNT(*) FROM catalog" + where, tuple(params))
        orders = {
            "recent": "updated_at DESC, kind, item_id",
            "oldest": "updated_at ASC, kind, item_id",
            "title": "title COLLATE NOCASE ASC, kind, item_id",
        }
        per_page = max(1, min(per_page, 50))
        page = max(1, page)
        rows = self._fetchall(
            catalog + "SELECT kind, item_id, title, body, path, origin, updated_at, "
            "source_id, source_title, authors FROM catalog"
            + where
            + f" ORDER BY {orders.get(sort, orders['recent'])} LIMIT ? OFFSET ?",
            (*params, per_page, (page - 1) * per_page),
        )
        return rows, total
