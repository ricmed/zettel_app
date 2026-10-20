"""Typed edges between permanent notes (``note_connections``)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field

from zettel.state.base import StateBase, placeholders


def edge_is_traversable(edge: Mapping) -> bool:
    """Whether graph walk and hub degree may follow this edge.

    LLM edges are traversable only once a human accepted them. Derived and
    manual edges are traversable until rejected.
    """
    origin = edge.get("origin") or "llm"
    status = edge.get("status") or "accepted"
    if status == "rejected":
        return False
    if origin in ("manual", "derived"):
        return True
    return status == "accepted"


_TRAVERSABLE_SQL = (
    "((origin IN ('manual', 'derived') AND status != 'rejected') "
    "OR (origin NOT IN ('manual', 'derived') AND status = 'accepted'))"
)


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


@dataclass(frozen=True)
class EdgePolicy:
    """How the garden weighs an edge (#299).

    A traversable edge weighs its relation (``edge_weight``). An LLM edge still
    waiting for review (``status='proposed'``) weighs ``proposed_factor`` times
    that: the model's judgement is a real signal of centrality, but not yet a
    path the reader walks. A rejected edge weighs nothing. ``proposed_factor=0``
    is the walkable graph only. Retrieval and reachability never use this.
    """

    weights: Mapping[str, float] = field(default_factory=dict)
    proposed_factor: float = 0.0

    @property
    def include_proposed(self) -> bool:
        return self.proposed_factor > 0

    def weight(self, edge: Mapping) -> float:
        if edge_is_traversable(edge):
            return edge_weight(edge, self.weights)
        if self.include_proposed and edge.get("status") == "proposed":
            return self.proposed_factor * edge_weight(edge, self.weights)
        return 0.0


def connections_for(db, note_ids: list[str], policy: EdgePolicy) -> list[dict]:
    """``db.get_connections_for_notes`` with the proposed edges ``policy`` counts.

    The keyword is passed only when needed, so read-only adapters that serve
    the walkable graph (probes, test doubles) keep working.
    """
    if policy.include_proposed:
        return db.get_connections_for_notes(note_ids, include_proposed=True)
    return db.get_connections_for_notes(note_ids)


class ConnectionsMixin(StateBase):
    def upsert_note_connection(
        self,
        source_note_id: str,
        target_note_id: str,
        relation_type: str,
        description: str = "",
        origin: str = "llm",
        *,
        prompt_checksum: str = "",
        model: str = "",
        run_id: int | None = None,
        status: str = "accepted",
    ) -> None:
        self.conn.execute(
            """INSERT INTO note_connections
               (source_note_id, target_note_id, relation_type, description, origin,
                prompt_checksum, model, run_id, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(source_note_id, target_note_id, relation_type) DO UPDATE SET
                 description=excluded.description,
                 prompt_checksum=excluded.prompt_checksum,
                 model=excluded.model,
                 run_id=excluded.run_id,
                 status=excluded.status,
                 created_at=excluded.created_at""",
            (
                source_note_id,
                target_note_id,
                relation_type,
                description,
                origin,
                prompt_checksum,
                model,
                run_id,
                status,
                self._now(),
            ),
        )
        self.conn.commit()

    def list_outgoing_connections(
        self, source_note_id: str, *, origin: str | None = None
    ) -> list[dict]:
        """Edges leaving ``source_note_id``, optionally of one origin, in a stable order."""
        sql = "SELECT * FROM note_connections WHERE source_note_id=?"
        params: tuple[str, ...] = (source_note_id,)
        if origin is not None:
            sql += " AND origin=?"
            params = (*params, origin)
        sql += " ORDER BY target_note_id, relation_type"
        return self._fetchall(sql, params)

    def delete_outgoing_connections(self, source_note_id: str, *, origin: str) -> int:
        """Delete edges leaving ``source_note_id`` with this origin. Returns the count."""
        cur = self.conn.execute(
            "DELETE FROM note_connections WHERE source_note_id=? AND origin=?",
            (source_note_id, origin),
        )
        self.conn.commit()
        return cur.rowcount

    def delete_note_connection(
        self,
        source_note_id: str,
        target_note_id: str,
        relation_type: str,
        *,
        origin: str | None = None,
    ) -> int:
        """Delete one edge. ``origin`` limits the delete to that author.

        Returns the number of rows removed. A missing edge deletes nothing.
        """
        sql = (
            "DELETE FROM note_connections "
            "WHERE source_note_id=? AND target_note_id=? AND relation_type=?"
        )
        params: tuple[str, ...] = (source_note_id, target_note_id, relation_type)
        if origin is not None:
            sql += " AND origin=?"
            params = (*params, origin)
        cur = self.conn.execute(sql, params)
        self.conn.commit()
        return cur.rowcount

    def list_connections(self, *, relation_type: str, origin: str) -> list[dict]:
        """Edges of one relation and origin, ordered for a stable diff."""
        return self._fetchall(
            "SELECT * FROM note_connections WHERE relation_type=? AND origin=? "
            "ORDER BY source_note_id, target_note_id",
            (relation_type, origin),
        )

    def list_note_connections(self) -> list[dict]:
        """Every edge, whatever its status (callers apply ``edge_is_traversable``)."""
        return self._fetchall(
            "SELECT * FROM note_connections ORDER BY source_note_id, target_note_id, relation_type"
        )

    def list_proposed_connections(self) -> list[dict]:
        """LLM edges waiting for a human, in a stable order."""
        return self._fetchall(
            "SELECT * FROM note_connections WHERE origin='llm' AND status='proposed' "
            "ORDER BY source_note_id, target_note_id, relation_type"
        )

    def mark_llm_edges_proposed(self) -> int:
        """Move accepted LLM edges back to the review queue. Derived and manual stay."""
        cur = self.conn.execute(
            "UPDATE note_connections SET status='proposed' WHERE origin='llm' AND status='accepted'"
        )
        self.conn.commit()
        return cur.rowcount

    def get_note_connections(self, note_id: str) -> list[dict]:
        """Get all connections where note_id is source or target."""
        return self._fetchall(
            "SELECT * FROM note_connections WHERE source_note_id=? OR target_note_id=?",
            (note_id, note_id),
        )

    def get_connections_for_notes(
        self, note_ids: list[str], *, include_proposed: bool = False
    ) -> list[dict]:
        """Batch-fetch every traversable edge touching any of ``note_ids``.

        One query per BFS frontier during graph expansion, instead of N per-note
        queries. ``include_proposed`` also returns LLM edges waiting for review,
        for the garden's ``EdgePolicy`` (#299). Empty input, empty list.
        """
        if not note_ids:
            return []
        marks = placeholders(note_ids)
        allowed = (
            f"({_TRAVERSABLE_SQL} OR status = 'proposed')" if include_proposed else _TRAVERSABLE_SQL
        )
        return self._fetchall(
            f"SELECT * FROM note_connections "
            f"WHERE (source_note_id IN ({marks}) OR target_note_id IN ({marks})) "
            f"AND {allowed}",
            (*note_ids, *note_ids),
        )

    def count_note_connections(self) -> int:
        return self._count("SELECT COUNT(*) FROM note_connections")

    def get_weighted_note_degrees(
        self, relation_weights: Mapping[str, float], *, proposed_factor: float = 0.0
    ) -> dict[str, float]:
        """Undirected weighted degree per note; proposed edges count at ``proposed_factor``."""
        policy = EdgePolicy(relation_weights, proposed_factor)
        degrees: dict[str, float] = defaultdict(float)
        rows = self._fetchall(
            "SELECT source_note_id, target_note_id, relation_type, origin, status "
            "FROM note_connections"
        )
        for row in rows:
            weight = policy.weight(row)
            if weight <= 0:
                continue
            degrees[row["source_note_id"]] += weight
            degrees[row["target_note_id"]] += weight
        return dict(degrees)
