"""Which notes of a cluster become its MOC's entry points (#297, ADR-060).

A MOC is a door into a subject, not a list of every note in it: it shows a
few notes the reader should start from and the graph leads to the rest. This
module ranks a cluster's notes by one configurable signal and keeps the top
``gardener.max_entries_per_moc``. Every signal is computed without an LLM call.

Signals (``gardener.entry_selection``):

* ``internal_degree`` — weighted degree inside the cluster under the garden's
  ``EdgePolicy`` (traversable edges, plus proposed LLM edges at
  ``gardener.proposed_edge_factor``, #299): the notes the rest of the subject
  hangs from (provisional default; on the measured vault, 10 per MOC reach
  92.4% of the notes within two hops).
* ``medoid`` — cosine to the cluster centroid: the most representative note.
* ``first_in_source`` — earliest ``created_at`` (connect writes in source and
  chunk order), often the foundational definition of the subject.
* ``relevance`` — the concept's ``relevance_score`` from extract.
* ``corroboration`` — how many other-source notes corroborate it.

Ties fall back to internal degree, then ``note_id``, so the choice is stable.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from typing import TYPE_CHECKING

import numpy as np

from zettel.config import DEFAULT_RELATION_WEIGHTS, GardenerConfig
from zettel.state.connections import EdgePolicy, connections_for

if TYPE_CHECKING:
    from zettel.state import StateDB

ENTRY_SIGNALS = ("internal_degree", "medoid", "first_in_source", "relevance", "corroboration")


def internal_degrees(
    db: StateDB, note_ids: list[str], policy: EdgePolicy | None = None
) -> dict[str, float]:
    """Weighted degree of each note counting only edges inside ``note_ids``."""
    members = set(note_ids)
    policy = policy or EdgePolicy(dict(DEFAULT_RELATION_WEIGHTS))
    degree: dict[str, float] = defaultdict(float)
    seen: set[tuple[str, str, str]] = set()
    for edge in connections_for(db, list(members), policy):
        src, tgt = edge["source_note_id"], edge["target_note_id"]
        key = (src, tgt, edge.get("relation_type") or "")
        if src in members and tgt in members and key not in seen:
            seen.add(key)
            w = policy.weight(edge)
            degree[src] += w
            degree[tgt] += w
    return {nid: degree.get(nid, 0.0) for nid in note_ids}


def medoid_scores(note_ids: list[str], vectors: Mapping[str, np.ndarray]) -> dict[str, float]:
    """Cosine of each note to the cluster centroid (0 for a note with no vector)."""
    known = [nid for nid in note_ids if nid in vectors]
    if not known:
        return dict.fromkeys(note_ids, 0.0)
    matrix = np.stack([np.asarray(vectors[nid], dtype=float) for nid in known])
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    matrix = matrix / np.where(norms == 0, 1.0, norms)
    centroid = matrix.mean(axis=0)
    centroid /= np.linalg.norm(centroid) or 1.0
    sims = dict(zip(known, (matrix @ centroid).tolist(), strict=True))
    return {nid: float(sims.get(nid, 0.0)) for nid in note_ids}


def _first_in_source_scores(db: StateDB, note_ids: list[str]) -> dict[str, float]:
    created = {nid: (db.get_note(nid) or {}).get("created_at") or "~" for nid in note_ids}
    order = sorted(note_ids, key=lambda nid: (created[nid], nid))
    return {nid: float(len(order) - rank) for rank, nid in enumerate(order)}


def _relevance_scores(db: StateDB, note_ids: list[str]) -> dict[str, float]:
    import json

    concepts = db.get_concepts_for_notes(note_ids)
    scores: dict[str, float] = {}
    for nid in note_ids:
        raw = (concepts.get(nid) or {}).get("candidate_json") or "{}"
        try:
            scores[nid] = float(json.loads(raw).get("relevance_score") or 0)
        except (TypeError, ValueError):
            scores[nid] = 0.0
    return scores


def _corroboration_scores(db: StateDB, note_ids: list[str]) -> dict[str, float]:
    count: dict[str, float] = defaultdict(float)
    for edge in db.get_connections_for_notes(note_ids):
        if edge.get("relation_type") == "corroborates":
            count[edge["source_note_id"]] += 1
            count[edge["target_note_id"]] += 1
    return {nid: count.get(nid, 0.0) for nid in note_ids}


def select_entry_notes(
    db: StateDB,
    note_ids: list[str],
    cfg: GardenerConfig,
    vectors: Mapping[str, np.ndarray] | None = None,
    *,
    limit: int | None = None,
    policy: EdgePolicy | None = None,
) -> list[str]:
    """The top ``limit`` (default ``max_entries_per_moc``) notes by the configured signal.

    ``policy`` weighs the edges of the internal degree (default: the walkable
    graph with the built-in relation weights).
    """
    if not note_ids:
        return []
    cap = cfg.max_entries_per_moc if limit is None else limit
    degree = internal_degrees(db, note_ids, policy)
    signal = cfg.entry_selection
    if signal == "internal_degree":
        scores = degree
    elif signal == "medoid":
        scores = medoid_scores(note_ids, vectors or {})
    elif signal == "first_in_source":
        scores = _first_in_source_scores(db, note_ids)
    elif signal == "relevance":
        scores = _relevance_scores(db, note_ids)
    else:
        scores = _corroboration_scores(db, note_ids)
    ranked = sorted(note_ids, key=lambda nid: (-scores[nid], -degree[nid], nid))
    return ranked[: max(0, cap)]
