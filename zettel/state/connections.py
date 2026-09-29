"""Typed edges between permanent notes (``note_connections``)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping

from zettel.state.base import StateBase, placeholders


def edge_weight(edge: Mapping, weights: Mapping[str, float]) -> float:
    """Traversal weight of one ``note_connections`` row.

    A hand-written link (``origin='manual'``) weighs ``manual`` whatever its
    relation type (ADR-009); any other edge weighs its relation, falling back
    to ``related``. Shared by graph expansion and the weighted degree below.
    """
    if (edge.get("origin") or "llm") == "manual":
        return weights.get("manual", 0.95)
    relation = edge.get("relation_type") or "related"
    return weights.get(relation, weights.get("related", 0.5))


class ConnectionsMixin(StateBase):
    def upsert_note_connection(
        self,
        source_note_id: str,
        target_note_id: str,
        relation_type: str,
        description: str = "",
        origin: str = "llm",
    ) -> None:
        self.conn.execute(
            """INSERT INTO note_connections
               (source_note_id, target_note_id, relation_type, description, origin, created_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(source_note_id, target_note_id, relation_type) DO UPDATE SET
                 description=excluded.description, created_at=excluded.created_at""",
            (source_note_id, target_note_id, relation_type, description, origin, self._now()),
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
        marks = placeholders(note_ids)
        return self._fetchall(
            f"SELECT * FROM note_connections "
            f"WHERE source_note_id IN ({marks}) OR target_note_id IN ({marks})",
            (*note_ids, *note_ids),
        )

    def count_note_connections(self) -> int:
        return self._count("SELECT COUNT(*) FROM note_connections")

    def get_weighted_note_degrees(self, relation_weights: Mapping[str, float]) -> dict[str, float]:
        """Undirected weighted degree per note from note_connections."""
        degrees: dict[str, float] = defaultdict(float)
        rows = self._fetchall(
            "SELECT source_note_id, target_note_id, relation_type, origin FROM note_connections"
        )
        for row in rows:
            weight = edge_weight(row, relation_weights)
            degrees[row["source_note_id"]] += weight
            degrees[row["target_note_id"]] += weight
        return dict(degrees)
