"""The Mesa's conversation (ADR-067): what the typed graph says about the desk, no LLM.

Luhmann's partner was the archive itself: follow a link and an unexpected note
agrees, disagrees or gives an example. Here the desk asks the same of the
edges ``connect`` and the author already wrote (only traversable ones:
accepted, derived or manual):

- tensions: ``contradicts`` between two cards, or from outside against one;
- the frontier: notes one edge away that are not on the desk yet, strongest first;
- corroborations: the same idea held by another source;
- isolated cards: a card with no edge to any other card of its section;
- coverage: a section that leans on a single source.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from zettel.project_file import ProjectBody

FRONTIER_LIMIT = 15


@dataclass
class Link:
    """One edge seen from the desk: ``desk_note`` relates to ``other_note``."""

    desk_note: str
    other_note: str
    relation_type: str
    label: str
    description: str
    weight: float


@dataclass
class Neighbour:
    note_id: str
    weight: float
    links: list[Link] = field(default_factory=list)

    @property
    def best(self) -> Link:
        return max(self.links, key=lambda link: link.weight)


@dataclass
class Conversation:
    tensions: list[Link]
    frontier: list[Neighbour]
    corroborations: list[Link]
    isolated: list[tuple[int, str]]  # (section index, note_id)
    single_source: list[tuple[int, str]]  # (section index, source_id)
    titles: dict[str, str]
    theses: dict[str, str]

    @property
    def empty(self) -> bool:
        return not (self.tensions or self.frontier or self.corroborations or self.isolated)


def _link(edge: dict, desk_note: str, weights: dict[str, float]) -> Link:
    from zettel.connector.links import inverse_relation, relation_label
    from zettel.state.connections import edge_weight

    relation = edge.get("relation_type") or "related"
    outgoing = edge["source_note_id"] == desk_note
    other = edge["target_note_id"] if outgoing else edge["source_note_id"]
    return Link(
        desk_note=desk_note,
        other_note=other,
        relation_type=relation,
        label=relation_label(relation) if outgoing else inverse_relation(relation),
        description=edge.get("description") or "",
        weight=edge_weight(edge, weights),
    )


def converse(cfg: Any, db: Any, body: ProjectBody) -> Conversation:
    from zettel.note_content import note_sections

    weights = dict(cfg.retrieval.graph_expansion.relation_weights)
    section_of = {
        card.note_id: index for index, section in enumerate(body.sections) for card in section.cards
    }
    desk = set(section_of)
    tensions: list[Link] = []
    corroborations: list[Link] = []
    neighbours: dict[str, Neighbour] = {}
    linked_in_section: set[str] = set()
    edges = db.get_connections_for_notes(sorted(desk)) if desk else []
    rows = db.get_notes_by_ids(
        sorted(desk | {e["source_note_id"] for e in edges} | {e["target_note_id"] for e in edges})
    )
    for edge in edges:
        source, target = edge["source_note_id"], edge["target_note_id"]
        relation = edge.get("relation_type") or "related"
        if source in desk and target in desk:
            if section_of[source] == section_of[target] and source != target:
                linked_in_section.update((source, target))
            if relation == "contradicts":
                tensions.append(_link(edge, source, weights))
            continue
        desk_note = source if source in desk else target
        link = _link(edge, desk_note, weights)
        if link.other_note not in rows:
            continue
        if relation == "contradicts":
            tensions.append(link)
        if relation == "corroborates":
            other_source = rows[link.other_note].get("source_id")
            if other_source and other_source != (rows.get(desk_note) or {}).get("source_id"):
                corroborations.append(link)
                continue
        neighbour = neighbours.setdefault(link.other_note, Neighbour(link.other_note, 0.0))
        neighbour.weight += link.weight
        neighbour.links.append(link)
    frontier = sorted(neighbours.values(), key=lambda n: (-n.weight, n.note_id))[:FRONTIER_LIMIT]

    isolated = []
    single_source = []
    for index, section in enumerate(body.sections):
        cards = [c.note_id for c in section.cards if c.note_id in rows]
        if len(cards) < 2:
            continue
        isolated.extend((index, nid) for nid in cards if nid not in linked_in_section)
        sources = Counter(rows[nid].get("source_id") for nid in cards if rows[nid].get("source_id"))
        if len(sources) == 1:
            single_source.append((index, next(iter(sources))))

    shown = {link.other_note for link in tensions + corroborations} | {n.note_id for n in frontier}
    return Conversation(
        tensions=tensions,
        frontier=frontier,
        corroborations=corroborations,
        isolated=isolated,
        single_source=single_source,
        titles={nid: row.get("title") or nid for nid, row in rows.items()},
        theses={
            nid: note_sections(rows[nid].get("title") or "", rows[nid].get("body") or "")["thesis"]
            for nid in shown
            if nid in rows
        },
    )


def section_query(
    body: ProjectBody, section: int, theses: dict[str, str], title: str, focus: str = ""
) -> str:
    """What "search for this section" asks.

    With the author's ``focus`` (#387), that text and the section title: the
    cards' theses are left out, since they would dilute the focus in the
    embedding and in the BM25 coverage. Without it, the title and the theses.
    """
    target = body.sections[section]
    heading = target.title or title
    wanted = " ".join(focus.split())
    if wanted:
        return f"{wanted} {heading}"[:1000]
    parts = [heading]
    parts.extend(theses.get(card.note_id, "") for card in target.cards)
    return " ".join(p for p in parts if p)[:1000]
