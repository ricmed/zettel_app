"""Hub-anchored MOC pipeline — graph degree ranking and radial neighborhood expansion."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ulid import ULID

from zettel.config import DEFAULT_RELATION_WEIGHTS, AppConfig, HubMocsConfig
from zettel.graph import expand_notes
from zettel.hashing import sha256_hex
from zettel.index import VectorIndex
from zettel.llm import call_llm, extract_json, fill_template, get_llm, load_prompt_parts
from zettel.schemas import MOCHubGenerationOutput
from zettel.taxonomy import resolve_allowed_topics
from zettel.vault import note_filename, permanent_wikilink, safe_write_note

if TYPE_CHECKING:
    from zettel.state import StateDB

logger = logging.getLogger(__name__)

_HUB_ORIGIN = "hub_pipeline"
_MOC_FALLBACK_SUBSECTION = "Outras notas do cluster"


@dataclass
class _HubGardenStats:
    incremental: int = 0
    created: int = 0
    skipped_signature: int = 0
    skipped_small: int = 0
    skipped_dedup: int = 0


# ── Graph ranking (pure) ──────────────────────────────────────────────


def rank_note_hubs(
    db: StateDB,
    cfg: HubMocsConfig,
    relation_weights: dict[str, float] | None = None,
) -> list[tuple[str, float]]:
    """Rank permanent notes by weighted graph degree."""
    weights = relation_weights or DEFAULT_RELATION_WEIGHTS
    degrees = db.get_weighted_note_degrees(weights)
    permanent = db.list_permanent_note_ids()
    if permanent:
        degrees = {nid: deg for nid, deg in degrees.items() if nid in permanent}

    if not degrees:
        return []

    ranked = sorted(degrees.items(), key=lambda x: x[1], reverse=True)

    if cfg.selection_mode == "absolute":
        candidates = [(nid, deg) for nid, deg in ranked if deg >= cfg.min_weighted_degree]
    else:
        if len(ranked) == 1:
            threshold = ranked[0][1]
        else:
            idx = int((1.0 - cfg.hub_percentile) * (len(ranked) - 1))
            idx = max(0, min(idx, len(ranked) - 1))
            threshold = ranked[idx][1]
        candidates = [(nid, deg) for nid, deg in ranked if deg >= threshold]

    existing_anchors = db.list_hub_anchor_note_ids()
    seen: set[str] = set()
    result: list[tuple[str, float]] = []
    for nid, deg in candidates:
        if nid in seen:
            continue
        seen.add(nid)
        result.append((nid, deg))
    for nid in existing_anchors:
        if nid not in seen and nid in degrees:
            seen.add(nid)
            result.append((nid, degrees[nid]))

    result.sort(key=lambda x: x[1], reverse=True)
    return result[: cfg.top_n_hubs]


def build_hub_neighborhood(
    db: StateDB,
    hub_id: str,
    cfg: HubMocsConfig,
    relation_weights: dict[str, float] | None = None,
) -> list[str]:
    """Return hub_id plus neighbor note IDs ordered by BFS weight."""
    weights = relation_weights or DEFAULT_RELATION_WEIGHTS
    max_neighbor_slots = max(0, cfg.max_neighbors - 1)

    neighbors = expand_notes(
        db,
        [hub_id],
        max_hops=cfg.max_hops,
        decay=cfg.decay,
        relation_weights=weights,
        max_neighbors=max_neighbor_slots * 2,
        seed_weights={hub_id: 1.0},
    )

    ranked = sorted(
        (
            (nid, n.weight, n.hop)
            for nid, n in neighbors.items()
            if n.weight >= cfg.min_neighbor_weight
        ),
        key=lambda x: x[1],
        reverse=True,
    )
    neighbor_ids = [nid for nid, _, _ in ranked[:max_neighbor_slots]]
    return [hub_id] + neighbor_ids


def dedup_hub_neighborhoods(
    hubs_with_notes: list[tuple[str, float, list[str]]],
    threshold: float,
) -> list[tuple[str, list[str]]]:
    """Drop smaller hubs whose neighborhood is mostly contained in a larger one."""
    sorted_hubs = sorted(hubs_with_notes, key=lambda x: x[1], reverse=True)
    accepted: list[tuple[str, list[str]]] = []
    accepted_sets: list[set[str]] = []

    for hub_id, _degree, note_ids in sorted_hubs:
        current = set(note_ids)
        skip = False
        for prev in accepted_sets:
            if not current:
                skip = True
                break
            overlap = len(current & prev) / len(current)
            if overlap >= threshold:
                skip = True
                break
        if skip:
            continue
        accepted.append((hub_id, note_ids))
        accepted_sets.append(current)

    return accepted


def get_neighbor_graph_context(
    db: StateDB,
    hub_id: str,
    neighbor_ids: list[str],
    cfg: HubMocsConfig,
) -> dict[str, Any]:
    """Build per-neighbor hop/weight metadata for prompts."""
    weights = DEFAULT_RELATION_WEIGHTS
    expanded = expand_notes(
        db,
        [hub_id],
        max_hops=cfg.max_hops,
        decay=cfg.decay,
        relation_weights=weights,
        max_neighbors=cfg.max_neighbors,
        seed_weights={hub_id: 1.0},
    )
    context: dict[str, dict[str, Any]] = {}
    for nid in neighbor_ids:
        if nid == hub_id:
            continue
        if nid in expanded:
            n = expanded[nid]
            rel = n.via[-1].get("relation_type", "related") if n.via else "related"
            context[nid] = {"hop": n.hop, "weight": round(n.weight, 3), "relation": rel}
        else:
            context[nid] = {"hop": 0, "weight": 1.0, "relation": "hub"}
    return context


# ── Public API ─────────────────────────────────────────────────────────


def run_garden_hubs(
    cfg: AppConfig, db: StateDB, idx: VectorIndex, *, recreate: bool = False, observer=None,
) -> list[str]:
    """Generate/update hub-anchored MOCs. Returns moc_ids."""
    from zettel.usage import begin_run, finish_pipeline_run

    run_id = db.start_run("garden_hubs")
    begin_run(run_id)

    if recreate:
        removed = purge_hub_pipeline_mocs(cfg, db, idx)
        logger.info("Recriacao hub: %d MOC(s) removidos", removed)

    hcfg = cfg.hub_mocs
    stats = _HubGardenStats()

    ranked = rank_note_hubs(db, hcfg)
    from zettel.progress import report
    report(observer, "garden_hubs", f"{len(ranked)} hub(s) candidato(s).", total_items=len(ranked))
    if not ranked:
        logger.info("Nenhum hub encontrado no grafo")
        finish_pipeline_run(db, run_id)
        return []

    logger.info("Hubs candidatos: %d", len(ranked))

    hubs_with_notes: list[tuple[str, float, list[str]]] = []
    for hub_id, degree in ranked:
        note_ids = build_hub_neighborhood(db, hub_id, hcfg)
        if len(note_ids) < hcfg.min_neighbors:
            stats.skipped_small += 1
            continue
        hubs_with_notes.append((hub_id, degree, note_ids))

    before_dedup = len(hubs_with_notes)
    cluster_pairs = dedup_hub_neighborhoods(hubs_with_notes, hcfg.dedup_subset_threshold)
    stats.skipped_dedup = before_dedup - len(cluster_pairs)

    if not cluster_pairs:
        logger.info("Nenhuma vizinhanca hub apos deduplicacao")
        finish_pipeline_run(db, run_id)
        return []

    logger.info(
        "Vizinhancas hub: %d (dedup descartou %d)",
        len(cluster_pairs), stats.skipped_dedup,
    )

    llm = get_llm(cfg)
    moc_ids: list[str] = []
    degree_by_hub = dict(ranked)

    for hub_index, (hub_id, note_ids) in enumerate(cluster_pairs, 1):
        report(
            observer, "garden_hubs", f"Processando hub {hub_index}/{len(cluster_pairs)}.",
            current_item=hub_id, current_index=hub_index, total_items=len(cluster_pairs),
        )
        moc_id = _process_hub_cluster(
            cfg, db, idx, llm, hub_id, note_ids,
            degree_by_hub.get(hub_id, 0.0), stats,
        )
        if moc_id:
            moc_ids.append(moc_id)

    logger.info(
        "Garden hubs: %d MOCs, %d incrementais, %d novos, %d skip assinatura, "
        "%d vizinhanca pequena, %d dedup",
        len(moc_ids), stats.incremental, stats.created,
        stats.skipped_signature, stats.skipped_small, stats.skipped_dedup,
    )

    finish_pipeline_run(db, run_id)
    return moc_ids


def purge_hub_pipeline_mocs(cfg: AppConfig, db: StateDB, idx: VectorIndex) -> int:
    """Delete hub_pipeline MOCs from SQLite, ChromaDB and the vault."""
    from zettel.gardener import _moc_vault_path

    removed = db.delete_hub_pipeline_mocs()
    if not removed:
        return 0

    idx.delete_mocs([m["moc_id"] for m in removed])

    for moc in removed:
        path = _moc_vault_path(cfg, moc)
        if path.is_file():
            path.unlink()
            logger.debug("Arquivo MOC hub removido: %s", path)

    return len(removed)


# ── Hub cluster processing ────────────────────────────────────────────


def _process_hub_cluster(
    cfg: AppConfig,
    db: StateDB,
    idx: VectorIndex,
    llm: Any,
    hub_id: str,
    note_ids: list[str],
    weighted_degree: float,
    stats: _HubGardenStats,
) -> str | None:
    sorted_ids = sorted(note_ids)
    cluster_signature = sha256_hex(f"hub:{hub_id}|" + "|".join(sorted_ids))

    existing = db.find_moc_by_hub_note_id(hub_id)
    if existing:
        stats.incremental += 1
        return _update_hub_moc(
            cfg, db, idx, llm, existing, hub_id, note_ids, cluster_signature,
        )

    existing_sig = db.get_moc_by_signature(cluster_signature)
    if existing_sig:
        logger.debug("MOC hub ja existe para assinatura: %s", existing_sig["moc_id"])
        stats.skipped_signature += 1
        return existing_sig["moc_id"]

    moc_id = _create_new_hub_moc(
        cfg, db, idx, llm, hub_id, note_ids, cluster_signature, weighted_degree,
    )
    if moc_id:
        stats.created += 1
    return moc_id


def _create_new_hub_moc(
    cfg: AppConfig,
    db: StateDB,
    idx: VectorIndex,
    llm: Any,
    hub_id: str,
    note_ids: list[str],
    cluster_signature: str,
    weighted_degree: float,
) -> str | None:
    from zettel.gardener import (
        _allowed_note_ids,
        _build_note_alias_map,
        _moc_embeddable,
    )

    alias_to_id = _build_note_alias_map(note_ids)
    allowed = _allowed_note_ids(db, note_ids)
    graph_ctx = get_neighbor_graph_context(db, hub_id, note_ids, cfg.hub_mocs)

    prompt_parts = load_prompt_parts(cfg.prompts_path / "moc_hub_generation.md")
    domain = cfg.gardener.domain or "Geral"
    try:
        _, taxonomy_detail = resolve_allowed_topics(
            cfg.gardener.topics_path,
            cfg.gardener.allowed_topics,
            strict=False,
        )
    except Exception:
        taxonomy_detail = "_(Taxonomia indisponivel.)_"

    mapping = {
        "domain": domain,
        "taxonomy_detail": taxonomy_detail,
        "hub_note_section": _format_hub_note_section(db, hub_id, weighted_degree),
        "neighbors_list": _format_neighbors_list(db, note_ids, hub_id, alias_to_id, graph_ctx),
        "graph_context": _format_graph_context(graph_ctx),
    }
    system = fill_template(prompt_parts.system, mapping) if prompt_parts.system else ""
    user = fill_template(prompt_parts.user_template, mapping)

    try:
        response = call_llm(
            llm,
            user,
            system=system or None,
            provider=cfg.llm.provider,
            prompt_cache=cfg.llm.prompt_cache,
        )
        moc_output = _parse_hub_moc_output(response)
    except Exception as e:
        logger.error("Erro ao gerar MOC hub: %s", e)
        return None

    moc_id = str(ULID())
    topic = moc_output.topic
    now = datetime.now().isoformat()
    meta = {
        "type": "hub_moc",
        "moc_id": moc_id,
        "topic": topic,
        "cluster_signature": cluster_signature,
        "origin": _HUB_ORIGIN,
        "hub_note_id": hub_id,
        "hub_weighted_degree": weighted_degree,
        "created_at": now,
        "updated_at": now,
    }

    body = _build_hub_moc_body(
        db, topic, moc_output.summary, moc_output.hub_role, hub_id,
        moc_output.subsections, allowed, alias_to_id,
    )

    filename = note_filename("HUB", moc_id, topic)
    moc_path = cfg.vault_path / "40_MOCs" / filename
    safe_write_note(moc_path, meta, body)

    db.upsert_moc(
        moc_id, topic, str(moc_path), cluster_signature,
        body=body, frontmatter_json=json.dumps(meta, ensure_ascii=False),
        origin=_HUB_ORIGIN,
    )
    idx.upsert_moc(moc_id, _moc_embeddable(topic, moc_output.summary), {
        "topic": topic, "note_count": len(note_ids), "hub_note_id": hub_id,
    })

    logger.info("MOC hub criado: %s — %s (hub %s, %d notas)", moc_id, topic, hub_id, len(note_ids))
    return moc_id


def _update_hub_moc(
    cfg: AppConfig,
    db: StateDB,
    idx: VectorIndex,
    llm: Any,
    existing_moc: dict,
    hub_id: str,
    note_ids: list[str],
    cluster_signature: str,
) -> str | None:
    from zettel.gardener import (
        _allowed_note_ids,
        _apply_incremental_placements,
        _build_note_alias_map,
        _build_notes_list,
        _moc_embeddable,
        _parse_incremental_output,
        _parse_moc_structure,
        _snapshot_moc_file,
    )

    moc_id = existing_moc["moc_id"]
    moc_path = Path(existing_moc["path"]) if existing_moc.get("path") else None
    if not moc_path or not moc_path.exists():
        logger.warning("MOC hub file not found for %s", moc_id)
        return None

    structure = _parse_moc_structure(moc_path)
    if structure is None:
        return None

    existing_ids = structure["all_note_ids"]
    truly_new = [nid for nid in note_ids if nid not in existing_ids]

    if not truly_new:
        logger.info("MOC hub %s: nenhuma nota nova, atualizando assinatura", moc_id)
        body_snap, fm_snap = _snapshot_moc_file(moc_path)
        db.upsert_moc(
            moc_id, existing_moc["topic"], str(moc_path), cluster_signature,
            body=body_snap, frontmatter_json=fm_snap, origin=_HUB_ORIGIN,
        )
        return moc_id

    prompt_parts = load_prompt_parts(cfg.prompts_path / "moc_hub_incremental.md")
    alias_to_id = _build_note_alias_map(truly_new)

    sub_parts: list[str] = []
    for sub in structure["subsections"]:
        sub_parts.append(f"#### {sub['title']}")
        if sub["description"]:
            sub_parts.append(sub["description"])
        sub_parts.append("")

    hub_note = db.get_note(hub_id)
    hub_title = hub_note.get("title", hub_id) if hub_note else hub_id

    mapping = {
        "moc_topic": structure["topic"],
        "moc_summary": structure["summary"],
        "hub_note_title": hub_title,
        "existing_subsections": "\n".join(sub_parts) if sub_parts else "_(Nenhuma subsecao)_",
        "new_notes_list": _build_notes_list(db, truly_new, alias_to_id),
    }
    system = fill_template(prompt_parts.system, mapping) if prompt_parts.system else ""
    user = fill_template(prompt_parts.user_template, mapping)

    try:
        response = call_llm(
            llm,
            user,
            system=system or None,
            provider=cfg.llm.provider,
            prompt_cache=cfg.llm.prompt_cache,
        )
        incremental_output = _parse_incremental_output(response)
    except Exception as e:
        logger.error("Erro ao classificar notas hub incrementais: %s", e)
        return None

    _apply_incremental_placements(
        db, moc_path, structure, incremental_output,
        _allowed_note_ids(db, truly_new), alias_to_id,
    )

    body_snap, fm_snap = _snapshot_moc_file(moc_path)
    db.upsert_moc(
        moc_id, existing_moc["topic"], str(moc_path), cluster_signature,
        body=body_snap, frontmatter_json=fm_snap, origin=_HUB_ORIGIN,
    )
    idx.upsert_moc(moc_id, _moc_embeddable(structure["topic"], structure["summary"]), {
        "topic": existing_moc["topic"],
        "note_count": len(existing_ids) + len(truly_new),
        "hub_note_id": hub_id,
    })

    logger.info("MOC hub %s atualizado com %d notas novas", moc_id, len(truly_new))
    return moc_id


# ── Prompt / body helpers ─────────────────────────────────────────────


def _parse_hub_moc_output(text: str) -> MOCHubGenerationOutput:
    json_text = extract_json(text)
    data = json.loads(json_text)
    return MOCHubGenerationOutput(**data)


def _format_hub_note_section(db: StateDB, hub_id: str, weighted_degree: float) -> str:
    note = db.get_note(hub_id)
    if not note:
        return f"- **{hub_id}** (grau ponderado: {weighted_degree:.1f})"
    title = note.get("title", "Sem titulo")
    body = (note.get("body") or "")[:800]
    excerpt = body[:400] + "..." if len(body) > 400 else body
    return (
        f"- **N1 / {hub_id}**: {title}\n"
        f"  - Grau ponderado no grafo: {weighted_degree:.1f}\n"
        f"  - Trecho: {excerpt}"
    )


def _format_neighbors_list(
    db: StateDB,
    note_ids: list[str],
    hub_id: str,
    alias_to_id: dict[str, str],
    graph_ctx: dict[str, dict[str, Any]],
) -> str:
    id_to_alias = {nid: alias for alias, nid in alias_to_id.items()}
    parts: list[str] = []
    for nid in note_ids:
        if nid == hub_id:
            continue
        note = db.get_note(nid)
        if not note:
            continue
        label = id_to_alias.get(nid, nid)
        ctx = graph_ctx.get(nid, {})
        hop = ctx.get("hop", "?")
        rel = ctx.get("relation", "?")
        weight = ctx.get("weight", "?")
        parts.append(
            f"- **{label}**: {note.get('title', '')} "
            f"(hop={hop}, rel={rel}, peso={weight})",
        )
    return "\n".join(parts) if parts else "_(Sem vizinhos)_"


def _format_graph_context(graph_ctx: dict[str, dict[str, Any]]) -> str:
    if not graph_ctx:
        return "_(Sem metadados de grafo)_"
    lines = [
        f"- {nid}: hop={m['hop']}, rel={m['relation']}, peso={m['weight']}"
        for nid, m in graph_ctx.items()
    ]
    return "\n".join(lines)


def _build_hub_moc_body(
    db: StateDB,
    topic: str,
    summary: str,
    hub_role: str,
    hub_note_id: str,
    subsections: list,
    allowed_ids: set[str],
    alias_to_id: dict[str, str],
) -> str:
    from zettel.gardener import (
        _format_note_links,
        _resolve_note_ref,
    )

    placed: set[str] = set()
    body = f"# {topic}\n\n{summary}\n\n"

    body += "## Porta de entrada\n\n"
    if hub_role:
        body += f"{hub_role}\n\n"
    hub_note = db.get_note(hub_note_id)
    if hub_note:
        link = permanent_wikilink(
            hub_note_id, hub_note.get("title", ""), path=hub_note.get("path"),
        )
        body += f"- {link}\n\n"
        placed.add(hub_note_id)

    for sub in subsections:
        body += f"## {sub.title}\n\n"
        if sub.description:
            body += f"{sub.description}\n\n"
        subsection_ids: list[str] = []
        for ref in sub.note_ids:
            nid = _resolve_note_ref(ref, allowed_ids, alias_to_id)
            if not nid or nid in placed:
                continue
            subsection_ids.append(nid)
            placed.add(nid)
        body += _format_note_links(db, subsection_ids)
        body += "\n"

    missing = allowed_ids - placed
    if missing:
        logger.info(
            "MOC hub: %d nota(s) reconciliada(s) em '%s'",
            len(missing), _MOC_FALLBACK_SUBSECTION,
        )
        body += f"## {_MOC_FALLBACK_SUBSECTION}\n\n"
        body += _format_note_links(db, sorted(missing))
        body += "\n"

    return body
