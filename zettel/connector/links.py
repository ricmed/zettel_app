"""Typed connections of a permanent note: assembly, resolution, persistence, backlinks.

Relations are typed (supports, contradicts, extends, ...). Each edge is written
once, from the new note to its target; the target's ``auto-backlinks`` block
renders the inverse relation in PT-BR.
"""

from __future__ import annotations

import logging
from enum import Enum
from pathlib import Path
from typing import Any

from zettel.config import AppConfig
from zettel.retrieval import RetrievedNote
from zettel.schemas import RelationshipResult, RelationType
from zettel.state import StateDB
from zettel.vault import (
    normalize_note_id,
    permanent_wikilink,
    read_managed_block,
    safe_update_managed_blocks,
)

logger = logging.getLogger(__name__)

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

    Corroboration is a fact about authorship (two different ``source_id``), so
    only :func:`corroborating_note_ids` may assert it. ``permanent_note.md``
    deliberately omits it from the relation menu, but a model that emits it
    anyway is claiming conceptual agreement — which is what ``supports`` means.
    Applied to the LLM's own output only, never to the edges this module injects.
    """
    for conn in connections:
        if relation_type_value(conn.relation_type) == RelationType.CORROBORATES.value:
            logger.debug("Relacao corroborates emitida pelo LLM rebaixada para supports")
            conn.relation_type = RelationType.SUPPORTS
    return connections


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
    threshold = cfg.linking.corroborates_min_similarity
    scored: list[tuple[float, str]] = []
    for hit in similar:
        if hit.hop != 0 or hit.vector_distance is None or hit.note_id == note_id:
            continue
        similarity = 1.0 - hit.vector_distance / 2.0
        if similarity < threshold:
            continue
        other = (db.get_note(hit.note_id) or {}).get("source_id") or ""
        if not other or other == source_id:
            continue
        scored.append((similarity, hit.note_id))
    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    return [nid for _, nid in scored[: cfg.linking.corroborates_max_edges]]


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
                description="Outra fonte sustenta a mesma ideia",
            )
        )
    return connections


def note_on_disk(record: dict | None) -> bool:
    """True when the note row points at a file that still exists."""
    if not record or not record.get("path"):
        return False
    return Path(record["path"]).is_file()


def resolve_connections(db: StateDB, connections: list[RelationshipResult]) -> list[dict]:
    """Resolve LLM note_ids into wiki-links for vault rendering.

    Drops connections whose target is missing from SQLite or whose file is gone.
    Canonicalizes ``related_note_id`` (strips ``ZTL -`` / wikilink wrappers).
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


def persist_and_backlink(
    cfg: AppConfig,
    db: StateDB,
    new_note_id: str,
    connections: list[dict],
) -> None:
    """Persist resolved connections to DB and rebuild auto-backlinks from the graph.

    ``origin`` records who asserted the edge, so an audit can tell them apart:
    ``corroborates`` is derived from ``source_id`` by :func:`corroborating_note_ids`
    and was never proposed by a model, which is the whole basis for writing it as a
    real edge rather than an ``auto-connections`` suggestion (ADR-045 / ADR-043).
    Graph weighting is unaffected — only ``manual`` overrides the relation weight.
    """
    for conn in connections:
        target_id = conn["related_note_id"]
        relation = conn.get("relation_type") or "related"
        db.upsert_note_connection(
            source_note_id=new_note_id,
            target_note_id=target_id,
            relation_type=relation,
            description=conn.get("description") or "",
            origin="derived" if relation == RelationType.CORROBORATES.value else "llm",
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
