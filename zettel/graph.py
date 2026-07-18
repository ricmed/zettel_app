"""Graph expansion over the typed note-connection edges.

The pipeline already persists a typed, directed edge list in
``note_connections`` (built by the ``connect`` phase). This module turns that
write-only graph into a retrieval signal: given a set of seed notes, walk 1..N
hops of connections and return the reachable neighbours, weighted by relation
type and hop distance. This surfaces conceptually linked notes that dense
embedding similarity misses (e.g. a ``contradicts`` neighbour sits far apart in
vector space precisely because it argues the opposite).

Traversal is a plain Python BFS (not a recursive SQL CTE): the graph is small,
and we need per-relation weighting plus the winning path recorded for
provenance — both awkward in SQL. One batched query per BFS frontier
(``StateDB.get_connections_for_notes``) keeps it to O(hops) round-trips.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from .config import DEFAULT_RELATION_WEIGHTS

if TYPE_CHECKING:
    from .state import StateDB


@dataclass
class GraphNeighbor:
    """A note reachable from the seeds, with its best-path weight and provenance."""

    note_id: str
    weight: float                 # max over paths of: relation_weight * decay^(hop-1)
    hop: int                      # 1 = direct neighbour of a seed
    via: list[dict] = field(default_factory=list)
    # via entries: {"from": seed/intermediate note_id, "relation_type": str,
    #               "description": str}


def expand_notes(
    db: "StateDB",
    seed_ids: list[str],
    max_hops: int = 1,
    decay: float = 0.5,
    relation_weights: Optional[dict[str, float]] = None,
    max_neighbors: int = 10,
    seed_weights: Optional[dict[str, float]] = None,
) -> dict[str, GraphNeighbor]:
    """BFS over note_connections from ``seed_ids``.

    Edges are treated as **undirected**: a connection is relevant to both notes
    it touches, and the reverse direction reuses the same relation weight (the
    inverse label in the vault is presentation-only). Seeds are excluded from the
    result; the best (highest-weight) path to each neighbour wins. Returns at most
    ``max_neighbors`` neighbours, strongest first.

    ``seed_weights`` seeds the BFS with each seed's own relevance (e.g. its RRF
    score), so a neighbour's weight is ``seed_score * relation_weight * decay^hop``.
    Defaults to 1.0 per seed.
    """
    weights = relation_weights or DEFAULT_RELATION_WEIGHTS
    seeds = [s for s in seed_ids if s]
    if not seeds or max_hops < 1:
        return {}

    seed_w = seed_weights or {}
    visited: set[str] = set(seeds)
    best: dict[str, GraphNeighbor] = {}
    # frontier: note_id -> (accumulated_weight, via_path) of how we reached it.
    frontier: dict[str, tuple[float, list[dict]]] = {
        s: (seed_w.get(s, 1.0), []) for s in seeds
    }

    for hop in range(1, max_hops + 1):
        if not frontier:
            break
        edges = db.get_connections_for_notes(list(frontier.keys()))
        next_frontier: dict[str, tuple[float, list[dict]]] = {}
        for edge in edges:
            src = edge["source_note_id"]
            tgt = edge["target_note_id"]
            rel = edge["relation_type"]
            desc = edge.get("description") or ""
            rel_weight = weights.get(rel, weights.get("related", 0.5))
            # Consider the edge from whichever endpoint is on the current frontier.
            for anchor, other in ((src, tgt), (tgt, src)):
                if anchor not in frontier or not other:
                    continue
                anchor_weight, anchor_via = frontier[anchor]
                cand_weight = anchor_weight * rel_weight * (decay ** (hop - 1))
                step = {"from": anchor, "relation_type": rel, "description": desc}
                cand_via = anchor_via + [step]

                if other in best:
                    if cand_weight > best[other].weight:
                        best[other].weight = cand_weight
                        best[other].hop = hop
                        best[other].via = cand_via
                elif other not in visited:
                    best[other] = GraphNeighbor(
                        note_id=other, weight=cand_weight, hop=hop, via=cand_via
                    )
                # Track the strongest way to keep expanding through `other`.
                if other not in visited:
                    prev = next_frontier.get(other)
                    if prev is None or cand_weight > prev[0]:
                        next_frontier[other] = (cand_weight, cand_via)

        visited.update(next_frontier.keys())
        frontier = next_frontier

    ranked = sorted(best.values(), key=lambda n: n.weight, reverse=True)[:max_neighbors]
    return {n.note_id: n for n in ranked}
