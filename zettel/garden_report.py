"""What one garden run did, and how far its entry points reach (#298).

MOCs and hubs are entry points, not complete indexes (epic #291): a note left
out of every MOC is expected. What signals a problem is a subject with no door
or a low **reachability** — the share of notes within ``k`` hops of an entry
note over the graph the system actually walks. This module computes both and
holds the run report the garden persists; the math is pure so ``garden --plan``
and the probes reuse it without a database.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING

from zettel.gardener_assign import NOTE_ID_RE

if TYPE_CHECKING:
    from zettel.config import GardenerConfig
    from zettel.state import StateDB

ENTRY_ORIGINS = ("pipeline", "hub_pipeline", "manual")
REACH_HOPS = (1, 2)


@dataclass
class ClusterAction:
    """The routing decision for one cluster (or hub neighbourhood)."""

    category: str
    size: int
    # signature_skip | incremental | created | submoc | moc_of_mocs | home (planned only)
    # | rejected_cohesion | failed
    action: str
    moc_id: str | None = None


@dataclass
class GardenReport:
    pipeline: str  # garden | garden_hubs
    planned: bool = False
    notes_total: int = 0
    backends: list[str] = field(default_factory=list)
    clusters_found: int = 0
    noise_notes: int = 0
    small_buckets: int = 0
    bucket_sizes: dict[str, int] = field(default_factory=dict)
    actions: list[ClusterAction] = field(default_factory=list)
    discarded: dict[str, int] = field(default_factory=dict)  # moc_id -> notes answered "ignorar"
    llm_failures: int = 0
    omitted: int = 0  # selected entries the LLM did not place
    cap_reached: int = 0  # entry candidates refused because the MOC was full
    known_discards: int = 0  # candidates not offered again: discarded before (#296)
    entries: int = 0
    reach: dict[str, float] = field(default_factory=dict)  # "r1", "r2"
    reach_by_source: dict[str, dict[str, float]] = field(default_factory=dict)
    traversable_edges: int = 0
    total_edges: int = 0
    proposed_edges: int = 0  # LLM edges waiting for review (#279)
    # Stable MOCs (#300): bucket models refitted / reused, notes placed by a
    # reused model, entries that left a MOC after changing cluster, pipeline
    # MOCs no cluster reached, and hub MOCs whose hub note is gone.
    refit_buckets: int = 0
    model_buckets: int = 0
    predicted_notes: int = 0
    departed: dict[str, int] = field(default_factory=dict)
    mocs_without_cluster: list[str] = field(default_factory=list)
    orphan_hubs: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)  # graph health (#299)
    subjects_without_door: list[str] = field(default_factory=list)

    def action_counts(self) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        for item in self.actions:
            counts[item.action] += 1
        return dict(counts)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_dict(cls, data: Mapping) -> GardenReport:
        payload = dict(data)
        payload["actions"] = [ClusterAction(**a) for a in payload.get("actions") or []]
        return cls(**payload)


# ── Pure graph math ───────────────────────────────────────────────────


def adjacency(edges: Iterable[Mapping]) -> dict[str, set[str]]:
    """Undirected adjacency from ``note_connections`` rows."""
    adj: dict[str, set[str]] = defaultdict(set)
    for edge in edges:
        src, tgt = edge["source_note_id"], edge["target_note_id"]
        if src and tgt:
            adj[src].add(tgt)
            adj[tgt].add(src)
    return adj


def reachable(entries: Iterable[str], adj: Mapping[str, set[str]], hops: int) -> set[str]:
    """Entries plus every note at most ``hops`` edges away."""
    seen = set(entries)
    frontier = set(seen)
    for _ in range(hops):
        frontier = {n for node in frontier for n in adj.get(node, ())} - seen
        if not frontier:
            break
        seen |= frontier
    return seen


def reach_shares(
    entries: Iterable[str],
    adj: Mapping[str, set[str]],
    note_ids: Iterable[str],
    hops: Iterable[int] = REACH_HOPS,
) -> dict[str, float]:
    """``R_k`` for each ``k``: share of ``note_ids`` within ``k`` hops of an entry."""
    universe = set(note_ids)
    if not universe:
        return {f"r{k}": 0.0 for k in hops}
    entry_set = set(entries)  # a door of another scope still reaches this one
    return {
        f"r{k}": round(len(reachable(entry_set, adj, k) & universe) / len(universe), 4)
        for k in hops
    }


def subjects_without_door(
    buckets: Mapping[str, list[str]], entries: Iterable[str], min_notes: int
) -> list[str]:
    """Subjects with enough notes for a MOC and no entry note among them."""
    entry_set = set(entries)
    return sorted(
        subject
        for subject, members in buckets.items()
        if subject != "_unassigned"
        and len(members) >= min_notes
        and not entry_set.intersection(members)
    )


# ── Reading the vault state ───────────────────────────────────────────


def entry_note_ids(db: StateDB, origins: Iterable[str] = ENTRY_ORIGINS) -> set[str]:
    """Notes listed in the bodies of MOCs of the given origins (the doors)."""
    ids: set[str] = set()
    for origin in origins:
        for moc in db.list_mocs(origin=origin):
            ids.update(NOTE_ID_RE.findall(moc.get("body") or ""))
    return ids


def graph_warnings(
    report: GardenReport, gcfg: GardenerConfig, *, with_proposed_note: bool = True
) -> list[str]:
    """What in the graph limits the doors of ``report`` (#299), as messages for the user.

    The doors lead the reader only over traversable edges; LLM edges stay
    ``proposed`` until a human accepts them, so an unreviewed queue shrinks
    both the walkable share and the reachability. ``with_proposed_note`` adds
    the reminder that proposed edges weigh in the garden's own choices.
    """
    review = "revise a fila com `zettel links` (ou /review?queue=links)"
    warnings: list[str] = []
    if not report.total_edges:
        warnings.append(
            "Nenhuma aresta no grafo: as portas nao levam a outras notas (rode `zettel connect`)."
        )
        return warnings
    share = report.traversable_edges / report.total_edges
    if share < gcfg.warn_min_traversable_share:
        warnings.append(
            f"So {report.traversable_edges}/{report.total_edges} arestas sao percorriveis "
            f"({share:.0%}, abaixo de {gcfg.warn_min_traversable_share:.0%}): {review}."
        )
    reach = report.reach.get("r2", 0.0)
    if report.entries and reach < gcfg.warn_min_reach:
        warnings.append(
            f"Alcancabilidade a 2 saltos das portas de {reach:.0%}, abaixo de "
            f"{gcfg.warn_min_reach:.0%}: {review}."
        )
    if report.orphan_hubs:
        warnings.append(
            f"{len(report.orphan_hubs)} MOC(s) hub sem a nota-hub "
            f"({', '.join(report.orphan_hubs)}): remova com `zettel garden --hubs --recreate` "
            "ou adote com `zettel adopt-moc`."
        )
    if with_proposed_note and report.proposed_edges and gcfg.proposed_edge_factor > 0:
        warnings.append(
            f"{report.proposed_edges} aresta(s) propostas contam com peso "
            f"{gcfg.proposed_edge_factor:g} nos hubs, na coesao e na escolha das portas, "
            "mas o leitor so percorre as aceitas."
        )
    return warnings


def graph_health(db: StateDB, gcfg: GardenerConfig) -> tuple[GardenReport, list[str]]:
    """The current edge counts and the last garden's reach, with their warnings (doctor)."""
    from zettel.state.connections import edge_is_traversable

    latest = db.latest_garden_report("garden")
    report = GardenReport.from_dict(latest) if latest else GardenReport(pipeline="garden")
    edges = db.list_note_connections()
    report.total_edges = len(edges)
    report.traversable_edges = sum(1 for e in edges if edge_is_traversable(e))
    report.proposed_edges = sum(1 for e in edges if e.get("status") == "proposed")
    return report, graph_warnings(report, gcfg, with_proposed_note=False)


def fill_reachability(
    report: GardenReport,
    db: StateDB,
    entries: set[str],
    *,
    buckets: Mapping[str, list[str]] | None = None,
    min_notes: int = 0,
    gcfg: GardenerConfig | None = None,
) -> GardenReport:
    """Reachability, per-source reach, subjects without door and warnings, on ``report``."""
    from zettel.state.connections import edge_is_traversable

    notes = db.list_note_sources()
    edges = db.list_note_connections()
    walkable = [e for e in edges if edge_is_traversable(e)]
    adj = adjacency(walkable)

    report.entries = len(entries & set(notes))
    report.total_edges = len(edges)
    report.traversable_edges = len(walkable)
    report.proposed_edges = sum(1 for e in edges if e.get("status") == "proposed")
    report.reach = reach_shares(entries, adj, notes)
    by_source: dict[str, list[str]] = defaultdict(list)
    for note_id, source_id in notes.items():
        by_source[source_id or "(sem fonte)"].append(note_id)
    report.reach_by_source = {
        source: {"notes": float(len(ids)), **reach_shares(entries, adj, ids)}
        for source, ids in sorted(by_source.items())
    }
    if buckets is not None:
        report.subjects_without_door = subjects_without_door(buckets, entries, min_notes)
    if gcfg is not None:
        report.warnings = graph_warnings(report, gcfg)
    return report
