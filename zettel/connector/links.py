"""Typed connections of a permanent note: assembly, resolution, persistence, backlinks.

Relations are typed (supports, contradicts, extends, ...). Each edge is written
once, from the new note to its target; the target's ``auto-backlinks`` block
renders the inverse relation in PT-BR.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from zettel.config import AppConfig
from zettel.markdown_fences import headings_outside_fences
from zettel.note_content import note_sections
from zettel.retrieval import RetrievedNote
from zettel.schemas import RelationshipResult, RelationType
from zettel.state import StateDB
from zettel.state.connections import edge_is_traversable
from zettel.time import now_vault_iso
from zettel.vault import (
    compose_note,
    format_suggestion_line,
    normalize_note_id,
    parse_frontmatter,
    permanent_wikilink,
    read_managed_block,
    safe_update_managed_blocks,
    write_auto_connections,
)

logger = logging.getLogger(__name__)

# Same wording connect writes on the edge and on the source note's ## Conexões.
CORROBORATES_DESCRIPTION = "Outra fonte sustenta a mesma ideia"

_H2 = re.compile(r"^##[ \t]+(.+?)[ \t]*$", re.MULTILINE)
_CORROBORATES_BULLET = re.compile(r"^[ \t]*-[ \t]+\[\[[^\]]+\]\][ \t]+\(corroborates\)")

CorroborationSearch = Callable[[str, str], Sequence[RetrievedNote]]

INVERSE_RELATION: dict[str, str] = {
    "supports": "suportado por",
    "contradicts": "contradiz",
    "extends": "estendido por",
    "depends_on": "base para",
    "exemplifies": "exemplificado por",
    "related": "relacionado",
    # Simetrica: se A corrobora B, B corrobora A. Uma linha so e gravada; a
    # travessia ja e nao-direcionada e o backlink renderiza o outro lado.
    "corroborates": "corroborado por",
}


def inverse_relation(relation_type: str) -> str:
    """Return the inverse relation label in PT-BR."""
    return INVERSE_RELATION.get(relation_type, "relacionado")


def relation_type_value(relation_type: Any) -> str:
    """Normalize RelationType / str to the canonical string value.

    ``RelationType`` is a ``str, Enum`` hybrid, so ``isinstance(x, str)`` is True
    for members — but ``f"{RelationType.SUPPORTS}"`` renders as
    ``RelationType.SUPPORTS``, not ``supports``. Always prefer ``.value``.
    """
    if isinstance(relation_type, Enum):
        return str(relation_type.value)
    return str(relation_type or "related")


def demote_llm_corroborates(connections: list[RelationshipResult]) -> list[RelationshipResult]:
    """Downgrade a model-emitted ``corroborates`` to ``supports``.

    Two different ``source_id`` values are a fact about authorship. Whether the
    notes state the same idea is a similarity judgement, so only
    :func:`corroborating_note_ids` may assert the edge. ``permanent_note.md``
    deliberately omits it from the relation menu, but a model that emits it
    anyway is claiming conceptual agreement — which is what ``supports`` means.
    Applied to the LLM's own output only, never to the edges this module injects.
    """
    for conn in connections:
        if relation_type_value(conn.relation_type) == RelationType.CORROBORATES.value:
            logger.debug("Relacao corroborates emitida pelo LLM rebaixada para supports")
            conn.relation_type = RelationType.SUPPORTS
    return connections


@dataclass(frozen=True)
class CorroborationCandidate:
    """One other note, already scored, as the cut sees it."""

    note_id: str
    similarity: float
    source_id: str


def select_corroborating_ids(
    candidates: Sequence[CorroborationCandidate],
    *,
    source_id: str,
    note_id: str,
    min_similarity: float,
    max_edges: int,
) -> list[str]:
    """Other-source notes at or above the cut, closest first, capped.

    This is the rule. ``corroborating_note_ids`` (connect) and
    ``build_corroboration_plan`` (rebuild) both call it, so a hit clears the cut
    in one place only. A repeated ``note_id`` keeps its highest similarity.
    """
    best: dict[str, float] = {}
    for cand in candidates:
        if cand.note_id == note_id or cand.similarity < min_similarity:
            continue
        if not cand.source_id or cand.source_id == source_id:
            continue
        previous = best.get(cand.note_id)
        if previous is None or cand.similarity > previous:
            best[cand.note_id] = cand.similarity
    ranked = sorted(best.items(), key=lambda pair: (-pair[1], pair[0]))
    return [nid for nid, _ in ranked[:max_edges]]


def corroborating_note_ids(
    cfg: AppConfig,
    db: StateDB,
    similar: list[RetrievedNote],
    source_id: str,
    note_id: str,
) -> list[str]:
    """Permanent notes from *other* sources that state this same idea.

    Two authors converging on one idea is the product of research, not a
    duplicate to collapse — so the second note is written and the pair is linked
    by ``corroborates`` instead of one being dropped.

    Derived from the hits ``Retriever.search_notes`` already returned for the RAG
    context, so it costs no extra embedding and no LLM call. Only search seeds
    (``hop == 0``) carry a real distance; graph neighbours arrived by traversal
    and have no similarity to judge.

    Processing order closes itself: if A was connected before B existed, B links
    B->A on its own run, and traversal is undirected while the backlink block
    renders the other side.
    """
    candidates: list[CorroborationCandidate] = []
    for hit in similar:
        if hit.hop != 0 or hit.note_id == note_id:
            continue
        similarity = hit.similarity
        if similarity is None:
            continue
        other = (db.get_note(hit.note_id) or {}).get("source_id") or ""
        candidates.append(CorroborationCandidate(hit.note_id, similarity, other))
    return select_corroborating_ids(
        candidates,
        source_id=source_id,
        note_id=note_id,
        min_similarity=cfg.linking.corroborates_min_similarity,
        max_edges=cfg.linking.corroborates_max_edges,
    )


def assemble_connections(
    cfg: AppConfig,
    db: StateDB,
    llm_connections: list[RelationshipResult],
    *,
    similar: list[RetrievedNote],
    source_id: str,
    note_id: str,
    refines_note_id: str | None = None,
    refine_reason: str = "",
) -> list[RelationshipResult]:
    """Every connection the new note asserts, in precedence order.

    1. The model's own edges, with ``corroborates`` demoted (not the model's call).
    2. ``extends`` towards the note dedupe judged this candidate to refine
       (``refine_existing`` / ``merge``), unless the model already linked it.
    3. ``corroborates`` towards same-idea notes from other sources, derived from
       ``source_id`` — injected after the demotion so it survives.
    """
    connections = demote_llm_corroborates(list(llm_connections))
    linked = {c.related_note_id for c in connections}

    if refines_note_id and refines_note_id not in linked:
        connections.append(
            RelationshipResult(
                related_note_id=refines_note_id,
                relation_type=RelationType.EXTENDS,
                description=refine_reason or "Refina nota existente",
            )
        )
        linked.add(refines_note_id)

    for target in corroborating_note_ids(cfg, db, similar, source_id, note_id):
        if target in linked:
            continue
        connections.append(
            RelationshipResult(
                related_note_id=target,
                relation_type=RelationType.CORROBORATES,
                description=CORROBORATES_DESCRIPTION,
            )
        )
    return connections


def note_on_disk(record: dict | None) -> bool:
    """True when the note row points at a file that still exists."""
    if not record or not record.get("path"):
        return False
    return Path(record["path"]).is_file()


def resolve_connections(
    db: StateDB,
    connections: list[RelationshipResult],
    *,
    allowed_ids: set[str] | None = None,
) -> list[dict]:
    """Resolve LLM note_ids into wiki-links for vault rendering.

    Drops connections whose target is missing from SQLite or whose file is gone.
    Canonicalizes ``related_note_id`` (strips ``ZTL -`` / wikilink wrappers).
    When ``allowed_ids`` is set, a target that was not in the RAG block is
    dropped even if the note exists.
    """
    resolved: list[dict] = []
    seen: set[str] = set()
    for conn in connections:
        note_id = normalize_note_id(conn.related_note_id)
        if not note_id:
            logger.warning(
                "Conexao descartada: related_note_id=%r nao e um id utilizavel",
                conn.related_note_id,
            )
            continue
        if note_id in seen:
            continue
        if allowed_ids is not None and note_id not in allowed_ids:
            logger.warning(
                "Conexao descartada: related_note_id=%s nao estava no contexto RAG",
                note_id,
            )
            continue
        note_record = db.get_note(note_id)
        if not note_on_disk(note_record):
            logger.warning(
                "Conexao descartada: related_note_id=%r (canonico=%s) nao existe no vault",
                conn.related_note_id,
                note_id,
            )
            continue
        seen.add(note_id)
        resolved.append(
            {
                "related_note_id": note_id,
                "wiki_link": permanent_wikilink(
                    note_id, note_record.get("title", ""), path=note_record.get("path")
                ),
                "relation_type": relation_type_value(conn.relation_type),
                "description": conn.description,
            }
        )
    return resolved


def split_for_review(
    cfg: AppConfig, edges: list[dict], *, refines_note_id: str | None = None
) -> tuple[list[dict], list[dict]]:
    """Split edges that enter the graph now from LLM edges that wait for a human.

    ``corroborates`` and the dedupe ``extends`` are derived by code and stay in
    the first list. Other edges follow ``linking.llm_edges``.
    """
    hard: list[dict] = []
    proposed: list[dict] = []
    for edge in edges:
        relation = edge.get("relation_type") or "related"
        derived = relation == RelationType.CORROBORATES.value or (
            relation == "extends"
            and refines_note_id
            and edge.get("related_note_id") == refines_note_id
        )
        if derived:
            edge["origin"] = "derived"
            edge["status"] = "accepted"
            hard.append(edge)
        elif cfg.linking.llm_edges == "propose":
            edge["origin"] = "llm"
            edge["status"] = "proposed"
            proposed.append(edge)
        else:
            edge["origin"] = "llm"
            edge["status"] = "accepted"
            hard.append(edge)
    return hard, proposed


def persist_and_backlink(
    cfg: AppConfig,
    db: StateDB,
    new_note_id: str,
    connections: list[dict],
    *,
    provenance: dict | None = None,
) -> None:
    """Persist resolved connections to DB and rebuild auto-backlinks from the graph.

    ``origin`` records who asserted the edge, so an audit can tell them apart:
    ``corroborates`` is derived from ``source_id`` by :func:`corroborating_note_ids`
    and was never proposed by a model, which is the whole basis for writing it as a
    real edge rather than an ``auto-connections`` suggestion (ADR-045 / ADR-043).
    Graph weighting is unaffected — only ``manual`` overrides the relation weight.
    """
    prov = provenance or {}
    for conn in connections:
        target_id = conn["related_note_id"]
        relation = conn.get("relation_type") or "related"
        origin = conn.get("origin") or (
            "derived" if relation == RelationType.CORROBORATES.value else "llm"
        )
        if origin == "llm" and cfg.linking.llm_edges == "propose":
            status = "proposed"
        else:
            status = conn.get("status") or "accepted"
        db.upsert_note_connection(
            source_note_id=new_note_id,
            target_note_id=target_id,
            relation_type=relation,
            description=conn.get("description") or "",
            origin=origin,
            prompt_checksum="" if origin == "derived" else prov.get("prompt_checksum") or "",
            model="" if origin == "derived" else prov.get("model") or "",
            run_id=prov.get("run_id"),
            status=status,
        )
        rebuild_auto_backlinks(db, target_id, vault_timezone=cfg.vault_timezone)
    rebuild_auto_backlinks(db, new_note_id, vault_timezone=cfg.vault_timezone)


def rebuild_auto_backlinks(
    db: StateDB, note_id: str, *, vault_timezone: str = "America/Sao_Paulo"
) -> bool:
    """Replace ``auto-backlinks`` with incoming graph edges whose source file exists.

    Returns True when the vault file was written (including clearing a stale block).
    """
    record = db.get_note(note_id)
    if not note_on_disk(record):
        return False
    path = Path(record["path"])

    lines: list[str] = []
    for edge in db.get_note_connections(note_id):
        if edge["target_note_id"] != note_id or edge["source_note_id"] == note_id:
            continue
        if not edge_is_traversable(edge):
            continue
        source = db.get_note(edge["source_note_id"])
        if not note_on_disk(source):
            continue
        wiki = permanent_wikilink(
            edge["source_note_id"], source.get("title") or "", path=source.get("path")
        )
        line = f"- {wiki} ({inverse_relation(edge.get('relation_type') or 'related')})"
        if edge.get("description"):
            line += f" -- {edge['description']}"
        lines.append(line)

    existing = read_managed_block(path.read_text(encoding="utf-8"), "auto-backlinks")
    inner = "\n".join(lines)
    if not lines and not existing:
        return False
    if existing is not None and existing.strip() == inner.strip():
        return False
    safe_update_managed_blocks(path, {"auto-backlinks": inner}, vault_timezone=vault_timezone)
    return True


def corroboration_query(title: str, body: str) -> str:
    """Thesis and definition, the text connect embeds on the query side.

    Connect joins the approved candidate's fields (``f"{thesis} {definition}"``).
    A rebuild has the written note, so it reads those two sections from the body.
    Empty means the note cannot be re-scored; its edges stay where they are.
    """
    sections = note_sections(title or "", body or "")
    return f"{sections['thesis']} {sections['definition']}".strip()


@dataclass(frozen=True)
class CorroborationChange:
    """One derived ``corroborates`` edge the rebuild would write or drop."""

    source_note_id: str
    target_note_id: str
    source_source_id: str
    target_source_id: str
    similarity: float | None


@dataclass(frozen=True)
class CorroborationPlan:
    """Diff between the graph and a fresh pass of :func:`corroborating_note_ids`."""

    create: tuple[CorroborationChange, ...]
    remove: tuple[CorroborationChange, ...]
    keep: tuple[CorroborationChange, ...]
    unresolved_note_ids: tuple[str, ...] = ()
    untouched_edges: int = 0

    def source_pair_counts(self) -> list[tuple[str, str, int, int, int]]:
        """``(source_a, source_b, created, removed, kept)``, pairs sorted."""
        counts: dict[tuple[str, str], list[int]] = {}
        for kind, changes in enumerate((self.create, self.remove, self.keep)):
            for change in changes:
                left = change.source_source_id or "?"
                right = change.target_source_id or "?"
                bucket = counts.setdefault(tuple(sorted((left, right))), [0, 0, 0])
                bucket[kind] += 1
        return [
            (pair[0], pair[1], created, removed, kept)
            for pair, (created, removed, kept) in sorted(counts.items())
        ]


def _hit_similarity(hits: Sequence[RetrievedNote], note_id: str) -> float | None:
    best: float | None = None
    for hit in hits:
        if hit.note_id != note_id or hit.hop != 0 or hit.similarity is None:
            continue
        if best is None or hit.similarity > best:
            best = hit.similarity
    return best


def _change(
    db: StateDB,
    source_note_id: str,
    target_note_id: str,
    *,
    similarity: float | None,
    sources: dict[str, str],
) -> CorroborationChange:
    def source_of(note_id: str) -> str:
        if note_id not in sources:
            sources[note_id] = (db.get_note(note_id) or {}).get("source_id") or ""
        return sources[note_id]

    return CorroborationChange(
        source_note_id=source_note_id,
        target_note_id=target_note_id,
        source_source_id=source_of(source_note_id),
        target_source_id=source_of(target_note_id),
        similarity=similarity,
    )


def build_corroboration_plan(
    cfg: AppConfig,
    db: StateDB,
    search: CorroborationSearch,
) -> CorroborationPlan:
    """Recompute every permanent note's outgoing ``corroborates``.

    ``search(note_id, query)`` returns the hits connect would have passed to
    :func:`corroborating_note_ids` (seeds only; graph neighbours are ignored
    there). A note with no thesis and no definition is unresolved: its edges
    are not touched. Targets whose file is gone are dropped, the same way
    :func:`resolve_connections` drops them before persist.
    """
    permanent = db.list_permanent_note_ids()
    notes = [
        row
        for row in db.list_notes()
        if row["note_id"] in permanent and (row.get("source_id") or "")
    ]
    notes.sort(key=lambda row: row["note_id"])

    existing: dict[str, set[str]] = {}
    for edge in db.list_connections(relation_type="corroborates", origin="derived"):
        existing.setdefault(edge["source_note_id"], set()).add(edge["target_note_id"])

    create: list[CorroborationChange] = []
    remove: list[CorroborationChange] = []
    keep: list[CorroborationChange] = []
    unresolved: list[str] = []
    recomputed: set[str] = set()
    sources: dict[str, str] = {}

    for row in notes:
        note_id = row["note_id"]
        query = corroboration_query(row.get("title") or "", row.get("body") or "")
        if not query:
            unresolved.append(note_id)
            continue
        hits = list(search(note_id, query))
        desired = corroborating_note_ids(cfg, db, hits, row["source_id"], note_id)
        persisted = [nid for nid in desired if note_on_disk(db.get_note(nid))]
        recomputed.add(note_id)
        current = existing.get(note_id, set())
        for target_id in sorted(set(persisted) - current):
            create.append(
                _change(
                    db,
                    note_id,
                    target_id,
                    similarity=_hit_similarity(hits, target_id),
                    sources=sources,
                )
            )
        for target_id in sorted(current - set(persisted)):
            remove.append(
                _change(
                    db,
                    note_id,
                    target_id,
                    similarity=_hit_similarity(hits, target_id),
                    sources=sources,
                )
            )
        for target_id in sorted(current & set(persisted)):
            keep.append(
                _change(
                    db,
                    note_id,
                    target_id,
                    similarity=_hit_similarity(hits, target_id),
                    sources=sources,
                )
            )

    untouched = sum(
        len(targets) for source, targets in existing.items() if source not in recomputed
    )
    return CorroborationPlan(
        create=tuple(create),
        remove=tuple(remove),
        keep=tuple(keep),
        unresolved_note_ids=tuple(unresolved),
        untouched_edges=untouched,
    )


def corroborates_bullets(db: StateDB, note_id: str) -> list[str]:
    """Outgoing derived ``corroborates`` as ``## Conexões`` lines."""
    lines: list[str] = []
    edges = [
        edge
        for edge in db.get_note_connections(note_id)
        if edge["source_note_id"] == note_id
        and edge.get("relation_type") == RelationType.CORROBORATES.value
        and (edge.get("origin") or "") == "derived"
    ]
    edges.sort(key=lambda edge: edge["target_note_id"])
    for edge in edges:
        target = db.get_note(edge["target_note_id"]) or {}
        if not note_on_disk(target):
            continue
        wiki = permanent_wikilink(
            edge["target_note_id"], target.get("title") or "", path=target.get("path")
        )
        lines.append(f"- {wiki} (corroborates) -- {CORROBORATES_DESCRIPTION}")
    return lines


def rewrite_corroborates_section(text: str, bullets: list[str]) -> str:
    """Replace ``(corroborates)`` lines inside ``## Conexões``; keep every other line.

    A heading that would be left empty is removed. Other relations in the same
    section stay, in their original order.
    """
    headings = headings_outside_fences(_H2, text)
    index = next((i for i, match in enumerate(headings) if match.group(1) == "Conexões"), None)
    if index is None:
        if not bullets:
            return text
        block = "## Conexões\n\n" + "\n".join(bullets) + "\n"
        sep = "" if text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
        return text + sep + block

    start = headings[index].start()
    end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
    preserved = [
        line
        for line in text[start:end].splitlines()[1:]
        if line.strip() and _CORROBORATES_BULLET.match(line) is None
    ]
    if not preserved and not bullets:
        merged = text[:start] + text[end:]
        return re.sub(r"\n{3,}", "\n\n", merged)
    out = ["## Conexões", "", *preserved]
    if bullets:
        if preserved:
            out.append("")
        out.extend(bullets)
    block = "\n".join(out).rstrip() + "\n\n"
    return text[:start] + block + text[end:].lstrip("\n")


def replace_connections_section(text: str, bullets: list[str]) -> str:
    """Replace every bullet under ``## Conexões`` with ``bullets``.

    Non-bullet lines in that section stay. An empty section is removed.
    """
    headings = headings_outside_fences(_H2, text)
    index = next((i for i, match in enumerate(headings) if match.group(1) == "Conexões"), None)
    if index is None:
        if not bullets:
            return text
        block = "## Conexões\n\n" + "\n".join(bullets) + "\n"
        sep = "" if text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
        return text + sep + block

    start = headings[index].start()
    end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
    preserved = [
        line
        for line in text[start:end].splitlines()[1:]
        if line.strip() and not line.lstrip().startswith("-")
    ]
    if not preserved and not bullets:
        merged = text[:start] + text[end:]
        return re.sub(r"\n{3,}", "\n\n", merged)
    out = ["## Conexões", "", *preserved]
    if bullets:
        if preserved:
            out.append("")
        out.extend(bullets)
    block = "\n".join(out).rstrip() + "\n\n"
    return text[:start] + block + text[end:].lstrip("\n")


def _edge_shows_in_body(edge: dict) -> bool:
    """Proposed LLM edges are suggestions, not ``## Conexões`` lines."""
    return edge_is_traversable(edge)


def outgoing_connection_bullets(db: StateDB, note_id: str) -> list[str]:
    """Every traversable outgoing edge as a ``## Conexões`` line, graph order."""
    edges = [
        edge
        for edge in db.get_note_connections(note_id)
        if edge["source_note_id"] == note_id and _edge_shows_in_body(edge)
    ]
    edges.sort(key=lambda edge: (edge["target_note_id"], edge.get("relation_type") or ""))
    lines: list[str] = []
    for edge in edges:
        target = db.get_note(edge["target_note_id"]) or {}
        if not note_on_disk(target):
            continue
        wiki = permanent_wikilink(
            edge["target_note_id"], target.get("title") or "", path=target.get("path")
        )
        relation = edge.get("relation_type") or "related"
        line = f"- {wiki} ({relation})"
        if edge.get("description"):
            line += f" -- {edge['description']}"
        lines.append(line)
    return lines


def sync_connections_section(
    path: Path, bullets: list[str], *, vault_timezone: str = "America/Sao_Paulo"
) -> bool:
    """Rewrite ``## Conexões`` from ``bullets`` and bump ``updated_at`` when it changes."""
    if not path.is_file():
        return False
    original = path.read_text(encoding="utf-8")
    rewritten = replace_connections_section(original, bullets)
    if rewritten == original:
        return False
    meta, body = parse_frontmatter(rewritten)
    if meta:
        meta["updated_at"] = now_vault_iso(vault_timezone)
        rewritten = compose_note(meta, body)
    path.write_text(rewritten, encoding="utf-8")
    return True


def sync_corroborates_section(
    path: Path, bullets: list[str], *, vault_timezone: str = "America/Sao_Paulo"
) -> bool:
    """Write ``bullets`` into ``## Conexões`` and bump ``updated_at`` when it changes."""
    if not path.is_file():
        return False
    original = path.read_text(encoding="utf-8")
    rewritten = rewrite_corroborates_section(original, bullets)
    if rewritten == original:
        return False
    meta, body = parse_frontmatter(rewritten)
    if meta:
        meta["updated_at"] = now_vault_iso(vault_timezone)
        rewritten = compose_note(meta, body)
    path.write_text(rewritten, encoding="utf-8")
    return True


def apply_corroboration_plan(cfg: AppConfig, db: StateDB, plan: CorroborationPlan) -> None:
    """Write ``plan`` to SQLite, then refresh backlinks and ``## Conexões``.

    Only ``origin='derived'`` rows are deleted. A second call with a plan built
    afterwards is a no-op when the search result did not change.
    """
    for change in plan.remove:
        db.delete_note_connection(
            change.source_note_id,
            change.target_note_id,
            RelationType.CORROBORATES.value,
            origin="derived",
        )
    for change in plan.create:
        db.upsert_note_connection(
            source_note_id=change.source_note_id,
            target_note_id=change.target_note_id,
            relation_type=RelationType.CORROBORATES.value,
            description=CORROBORATES_DESCRIPTION,
            origin="derived",
        )
    touched = {change.source_note_id for change in (*plan.remove, *plan.create)}
    touched.update(change.target_note_id for change in (*plan.remove, *plan.create))
    for note_id in sorted(touched):
        rebuild_auto_backlinks(db, note_id, vault_timezone=cfg.vault_timezone)
    for note_id in sorted({change.source_note_id for change in (*plan.remove, *plan.create)}):
        record = db.get_note(note_id)
        if not note_on_disk(record):
            continue
        sync_corroborates_section(
            Path(record["path"]),
            corroborates_bullets(db, note_id),
            vault_timezone=cfg.vault_timezone,
        )


def _refresh_after_link_decision(
    cfg: AppConfig, db: StateDB, source_note_id: str, target_note_id: str
) -> None:
    """Backlinks follow the traversable graph. The suggestion block drops the decided target."""
    for note_id in (source_note_id, target_note_id):
        rebuild_auto_backlinks(db, note_id, vault_timezone=cfg.vault_timezone)
    record = db.get_note(source_note_id)
    if not note_on_disk(record):
        return
    path = Path(record["path"])
    sync_connections_section(
        path,
        outgoing_connection_bullets(db, source_note_id),
        vault_timezone=cfg.vault_timezone,
    )
    text = path.read_text(encoding="utf-8")
    block = read_managed_block(text, "auto-connections") or ""
    target = db.get_note(target_note_id) or {}
    stem = Path(target["path"]).stem if target.get("path") else target_note_id
    kept = [
        line
        for line in block.splitlines()
        if line.strip() and target_note_id not in line and stem not in line
    ]
    proposed = [
        edge
        for edge in db.list_outgoing_connections(source_note_id, origin="llm")
        if edge.get("status") == "proposed"
    ]
    for edge in proposed:
        other = db.get_note(edge["target_note_id"]) or {}
        if not note_on_disk(other):
            continue
        wiki = permanent_wikilink(
            edge["target_note_id"], other.get("title") or "", path=other.get("path")
        )
        line = format_suggestion_line(
            wiki,
            relation_type=edge.get("relation_type") or "",
            description=edge.get("description") or "",
        )
        if line not in kept:
            kept.append(line)
    write_auto_connections(path, kept, vault_timezone=cfg.vault_timezone)


def decide_link(
    cfg: AppConfig,
    db: StateDB,
    source_note_id: str,
    target_note_id: str,
    relation_type: str,
    action: str,
    *,
    new_type: str | None = None,
    surface: str = "cli",
) -> bool:
    """Accept, reject, or retype one proposed LLM edge. Records a review event."""
    from zettel.note_content import note_sections

    matches = [
        edge
        for edge in db.get_note_connections(source_note_id)
        if edge["source_note_id"] == source_note_id
        and edge["target_note_id"] == target_note_id
        and edge.get("relation_type") == relation_type
    ]
    if not matches:
        return False
    row = matches[0]
    source = db.get_note(source_note_id) or {}
    target = db.get_note(target_note_id) or {}
    provenance = {
        "prompt_checksum": row.get("prompt_checksum") or "",
        "model": row.get("model") or "",
        "run_id": row.get("run_id"),
        "description": row.get("description") or "",
    }
    if action == "accept":
        db.upsert_note_connection(
            source_note_id,
            target_note_id,
            relation_type,
            provenance["description"],
            origin=row.get("origin") or "llm",
            prompt_checksum=provenance["prompt_checksum"],
            model=provenance["model"],
            run_id=provenance["run_id"],
            status="accepted",
        )
        decision = "accept_link"
        stored_type = relation_type
    elif action == "reject":
        db.upsert_note_connection(
            source_note_id,
            target_note_id,
            relation_type,
            provenance["description"],
            origin=row.get("origin") or "llm",
            prompt_checksum=provenance["prompt_checksum"],
            model=provenance["model"],
            run_id=provenance["run_id"],
            status="rejected",
        )
        decision = "reject_link"
        stored_type = relation_type
    elif action == "retype":
        if not new_type or new_type == relation_type:
            return False
        db.delete_note_connection(source_note_id, target_note_id, relation_type)
        db.upsert_note_connection(
            source_note_id,
            target_note_id,
            new_type,
            provenance["description"],
            origin="review",
            prompt_checksum=provenance["prompt_checksum"],
            model=provenance["model"],
            run_id=provenance["run_id"],
            status="accepted",
        )
        decision = "retype_link"
        stored_type = new_type
    else:
        return False
    db.record_review_event(
        source_id=source.get("source_id") or "",
        chunk_id=source_note_id,
        decision=decision,
        mode="links",
        surface=surface,
        snapshot={
            "source_note_id": source_note_id,
            "target_note_id": target_note_id,
            "relation_type": relation_type,
            "stored_type": stored_type,
            "description": provenance["description"],
            "source_thesis": note_sections(source.get("title") or "", source.get("body") or "")[
                "thesis"
            ],
            "target_thesis": note_sections(target.get("title") or "", target.get("body") or "")[
                "thesis"
            ],
        },
    )
    _refresh_after_link_decision(cfg, db, source_note_id, target_note_id)
    return True
