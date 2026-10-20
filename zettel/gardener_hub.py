"""Hub-anchored MOC pipeline — graph degree ranking and radial neighborhood expansion."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ulid import ULID

from zettel.config import DEFAULT_RELATION_WEIGHTS, AppConfig, HubMocsConfig
from zettel.garden_report import ClusterAction, GardenReport, entry_note_ids, fill_reachability
from zettel.gardener_assign import garden_edge_policy
from zettel.graph import expand_notes
from zettel.hashing import sha256_hex
from zettel.index import VectorIndex
from zettel.llm import fill_template, get_llm, load_prompt_parts, parse_llm_json
from zettel.schemas import MOCHubGenerationOutput
from zettel.state.connections import EdgePolicy
from zettel.taxonomy import resolve_allowed_topics
from zettel.time import now_vault_iso
from zettel.vault import note_filename

if TYPE_CHECKING:
    from zettel.state import StateDB

logger = logging.getLogger(__name__)

_HUB_ORIGIN = "hub_pipeline"


@dataclass
class _HubGardenStats:
    incremental: int = 0
    created: int = 0
    skipped_signature: int = 0
    skipped_small: int = 0
    skipped_dedup: int = 0
    actions: list[ClusterAction] = field(default_factory=list)
    omitted: int = 0


# ── Graph ranking (pure) ──────────────────────────────────────────────


def rank_note_hubs(
    db: StateDB,
    cfg: HubMocsConfig,
    policy: EdgePolicy | None = None,
) -> list[tuple[str, float]]:
    """Rank permanent notes by weighted graph degree under the garden's ``policy`` (#299)."""
    policy = policy or EdgePolicy(dict(DEFAULT_RELATION_WEIGHTS))
    degrees = db.get_weighted_note_degrees(policy.weights, proposed_factor=policy.proposed_factor)
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
    policy: EdgePolicy | None = None,
) -> list[str]:
    """Return hub_id plus neighbor note IDs ordered by BFS weight."""
    max_neighbor_slots = max(0, cfg.max_neighbors - 1)
    neighbors = _hub_bfs(db, hub_id, cfg, policy, max_neighbor_slots * 2)

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
    return [hub_id, *neighbor_ids]


def _hub_bfs(
    db: StateDB, hub_id: str, cfg: HubMocsConfig, policy: EdgePolicy | None, limit: int
) -> dict:
    policy = policy or EdgePolicy(dict(DEFAULT_RELATION_WEIGHTS))
    return expand_notes(
        db,
        [hub_id],
        max_hops=cfg.max_hops,
        decay=cfg.decay,
        relation_weights=dict(policy.weights),
        max_neighbors=limit,
        seed_weights={hub_id: 1.0},
        proposed_factor=policy.proposed_factor,
    )


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
    policy: EdgePolicy | None = None,
) -> dict[str, Any]:
    """Build per-neighbor hop/weight metadata for prompts."""
    expanded = _hub_bfs(db, hub_id, cfg, policy, cfg.max_neighbors)
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
    cfg: AppConfig,
    db: StateDB,
    idx: VectorIndex,
    *,
    recreate: bool = False,
    observer=None,
) -> list[str]:
    """Generate/update hub-anchored MOCs. Returns moc_ids.

    Same fail-fast contract as ``run_garden`` (ADR-059): any escaping
    exception records the run ``failed``; the LLM client is built before
    ``--recreate`` purges anything.
    """
    from zettel.usage import begin_run, finish_pipeline_run

    run_id = db.start_run("garden_hubs")
    begin_run(run_id)
    status = "completed"
    try:
        moc_ids, stats = _garden_hubs(cfg, db, idx, recreate=recreate, observer=observer)
        report = fill_reachability(
            GardenReport(
                pipeline="garden_hubs",
                actions=list(stats.actions),
                orphan_hubs=_orphan_hub_topics(db),
            ),
            db,
            entry_note_ids(db, origins=(_HUB_ORIGIN,)),
            gcfg=cfg.gardener,
        )
        for warning in report.warnings:
            logger.warning("Garden hubs: %s", warning)
        db.save_garden_report(run_id, "garden_hubs", report.to_json())
        return moc_ids
    except Exception:
        status = "failed"
        raise
    finally:
        finish_pipeline_run(db, run_id, status)


def _garden_hubs(
    cfg: AppConfig, db: StateDB, idx: VectorIndex, *, recreate: bool, observer
) -> tuple[list[str], _HubGardenStats]:
    llm = get_llm(cfg, "garden")
    if recreate:
        removed = purge_hub_pipeline_mocs(cfg, db, idx)
        logger.info("Recriacao hub: %d MOC(s) removidos", removed)

    stats = _HubGardenStats()
    ranked, cluster_pairs = _hub_neighbourhoods(cfg, db, stats)
    from zettel.progress import report

    report(
        observer,
        "garden_hubs",
        f"{len(ranked)} hub(s) candidato(s).",
        total_items=len(ranked),
    )
    if not cluster_pairs:
        logger.info("Nenhuma vizinhanca hub (sem hubs ou todas pequenas/duplicadas)")
        return [], stats

    moc_ids: list[str] = []
    degree_by_hub = dict(ranked)

    for hub_index, (hub_id, note_ids) in enumerate(cluster_pairs, 1):
        report(
            observer,
            "garden_hubs",
            f"Processando hub {hub_index}/{len(cluster_pairs)}.",
            current_item=hub_id,
            current_index=hub_index,
            total_items=len(cluster_pairs),
        )
        moc_id = _process_hub_cluster(
            cfg,
            db,
            idx,
            llm,
            hub_id,
            note_ids,
            degree_by_hub.get(hub_id, 0.0),
            stats,
            hub_entry_notes(db, hub_id, note_ids, cfg),
        )
        if moc_id:
            moc_ids.append(moc_id)

    logger.info(
        "Garden hubs: %d MOCs, %d incrementais, %d novos, %d skip assinatura, "
        "%d vizinhanca pequena, %d dedup",
        len(moc_ids),
        stats.incremental,
        stats.created,
        stats.skipped_signature,
        stats.skipped_small,
        stats.skipped_dedup,
    )
    return moc_ids, stats


def _hub_neighbourhoods(
    cfg: AppConfig, db: StateDB, stats: _HubGardenStats
) -> tuple[list[tuple[str, float]], list[tuple[str, list[str]]]]:
    """Ranked hubs and their deduplicated neighbourhoods (no LLM, no write)."""
    hcfg = cfg.hub_mocs
    policy = garden_edge_policy(cfg)
    ranked = rank_note_hubs(db, hcfg, policy)
    if not ranked:
        logger.info("Nenhum hub encontrado no grafo")
        return [], []
    logger.info("Hubs candidatos: %d", len(ranked))

    hubs_with_notes: list[tuple[str, float, list[str]]] = []
    for hub_id, degree in ranked:
        note_ids = build_hub_neighborhood(db, hub_id, hcfg, policy)
        if len(note_ids) < hcfg.min_neighbors:
            stats.skipped_small += 1
            continue
        hubs_with_notes.append((hub_id, degree, note_ids))

    cluster_pairs = dedup_hub_neighborhoods(hubs_with_notes, hcfg.dedup_subset_threshold)
    stats.skipped_dedup = len(hubs_with_notes) - len(cluster_pairs)
    logger.info("Vizinhancas hub: %d (dedup descartou %d)", len(cluster_pairs), stats.skipped_dedup)
    return ranked, cluster_pairs


def hub_entry_notes(db: StateDB, hub_id: str, note_ids: list[str], cfg: AppConfig) -> list[str]:
    """The hub plus its best neighbours by the entry signal, within the MOC cap (ADR-060).

    No embeddings are loaded on the hub path, so ``medoid`` falls back to the
    internal-degree tie-break.
    """
    from zettel.garden_entries import select_entry_notes

    neighbours = [nid for nid in note_ids if nid != hub_id]
    limit = max(0, cfg.gardener.max_entries_per_moc - 1)
    return [
        hub_id,
        *select_entry_notes(
            db, neighbours, cfg.gardener, limit=limit, policy=garden_edge_policy(cfg)
        ),
    ]


def _orphan_hub_topics(db: StateDB) -> list[str]:
    """Hub MOCs whose hub note was deleted (``delete-source --delete-permanent``, #300)."""
    return [moc["topic"] for moc in db.list_orphan_hub_mocs()]


def plan_garden_hubs(cfg: AppConfig, db: StateDB) -> GardenReport:
    """What ``run_garden_hubs`` would do, with no LLM call and no write (#298)."""
    stats = _HubGardenStats()
    _ranked, cluster_pairs = _hub_neighbourhoods(cfg, db, stats)
    planned_entries = entry_note_ids(db, origins=(_HUB_ORIGIN,))
    actions: list[ClusterAction] = []
    for hub_id, note_ids in cluster_pairs:
        existing = db.find_moc_by_hub_note_id(hub_id)
        signature = sha256_hex(f"hub:{hub_id}|" + "|".join(sorted(note_ids)))
        if existing:
            action, moc_id = "incremental", existing["moc_id"]
        elif (by_sig := db.get_moc_by_signature(signature)) is not None:
            action, moc_id = "signature_skip", by_sig["moc_id"]
        else:
            action, moc_id = "created", None
        actions.append(ClusterAction(hub_id, len(note_ids), action, moc_id))
        if action in ("created", "incremental"):
            planned_entries.update(hub_entry_notes(db, hub_id, note_ids, cfg))
    report = GardenReport(
        pipeline="garden_hubs", planned=True, actions=actions, orphan_hubs=_orphan_hub_topics(db)
    )
    return fill_reachability(report, db, planned_entries, gcfg=cfg.gardener)


def purge_hub_pipeline_mocs(cfg: AppConfig, db: StateDB, idx: VectorIndex) -> int:
    """Delete hub_pipeline MOCs from SQLite, ChromaDB and the vault."""
    from zettel.gardener import purge_mocs_by_origin

    return purge_mocs_by_origin(cfg, db, idx, _HUB_ORIGIN)


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
    entries: list[str] | None = None,
) -> str | None:
    """Route a hub neighbourhood; the MOC lists only ``entries`` (ADR-060)."""
    action, moc_id = _route_hub_cluster(
        cfg, db, idx, llm, hub_id, note_ids, weighted_degree, stats, entries
    )
    stats.actions.append(
        ClusterAction(hub_id, len(note_ids), action if moc_id else "failed", moc_id)
    )
    return moc_id


def _route_hub_cluster(
    cfg: AppConfig,
    db: StateDB,
    idx: VectorIndex,
    llm: Any,
    hub_id: str,
    note_ids: list[str],
    weighted_degree: float,
    stats: _HubGardenStats,
    entries: list[str] | None = None,
) -> tuple[str, str | None]:
    entries = list(note_ids) if entries is None else entries
    sorted_ids = sorted(note_ids)
    cluster_signature = sha256_hex(f"hub:{hub_id}|" + "|".join(sorted_ids))

    existing = db.find_moc_by_hub_note_id(hub_id)
    if existing:
        stats.incremental += 1
        return "incremental", _update_hub_moc(
            cfg,
            db,
            idx,
            llm,
            existing,
            hub_id,
            entries,
            cluster_signature,
        )

    existing_sig = db.get_moc_by_signature(cluster_signature)
    if existing_sig:
        logger.debug("MOC hub ja existe para assinatura: %s", existing_sig["moc_id"])
        stats.skipped_signature += 1
        return "signature_skip", existing_sig["moc_id"]

    moc_id = _create_new_hub_moc(
        cfg,
        db,
        idx,
        llm,
        hub_id,
        entries,
        cluster_signature,
        weighted_degree,
        stats,
    )
    if moc_id:
        stats.created += 1
    return "created", moc_id


def _create_new_hub_moc(
    cfg: AppConfig,
    db: StateDB,
    idx: VectorIndex,
    llm: Any,
    hub_id: str,
    note_ids: list[str],
    cluster_signature: str,
    weighted_degree: float,
    stats: _HubGardenStats | None = None,
) -> str | None:
    """New hub MOC from the hub plus its selected entry neighbours (ADR-060)."""
    from zettel.gardener import (
        _allowed_note_ids,
        _build_note_alias_map,
        _GardenStats,
        _keep_discards,
        _moc_embeddable,
        _store_moc_row,
        _subsections_from_output,
        _write_moc,
        call_garden_llm,
    )

    alias_to_id = _build_note_alias_map(note_ids)
    allowed = _allowed_note_ids(db, note_ids)
    graph_ctx = get_neighbor_graph_context(
        db, hub_id, note_ids, cfg.hub_mocs, garden_edge_policy(cfg)
    )

    prompt_parts = load_prompt_parts(cfg.prompts_path / "moc_hub_generation.md")
    try:
        _, taxonomy_detail = resolve_allowed_topics(
            cfg.gardener.topics_path,
            cfg.gardener.allowed_topics,
            strict=False,
        )
    except Exception:
        taxonomy_detail = "_(Taxonomia indisponivel.)_"

    mapping = {
        "domain": cfg.domain.name,
        "taxonomy_detail": taxonomy_detail,
        "hub_note_section": _format_hub_note_section(db, hub_id, weighted_degree),
        "neighbors_list": _format_neighbors_list(db, note_ids, hub_id, alias_to_id, graph_ctx),
        "graph_context": _format_graph_context(graph_ctx),
    }
    system = fill_template(prompt_parts.system, mapping) if prompt_parts.system else ""
    user = fill_template(prompt_parts.user_template, mapping)

    moc_output = call_garden_llm(
        cfg, db, llm, system, user, _parse_hub_moc_output, subject_id=cluster_signature
    )
    if moc_output is None:
        return None

    placed = {hub_id}
    subsections = [
        {"title": "Porta de entrada", "description": moc_output.hub_role, "entries": [(hub_id, "")]}
    ]
    subsections += _subsections_from_output(moc_output.subsections, allowed, alias_to_id, placed)
    garden_stats = _GardenStats()
    garden_stats.omitted = len(allowed - placed)

    moc_id = str(ULID())
    _keep_discards(cfg, db, moc_id, allowed - placed, "nao posicionada na criacao")
    topic = moc_output.topic
    now = now_vault_iso(cfg.vault_timezone)
    meta = {
        "type": "hub_moc",
        "moc_id": moc_id,
        "topic": topic,
        "origin": _HUB_ORIGIN,
        "hub_note_id": hub_id,
        "hub_weighted_degree": weighted_degree,
        "summary_entries": len(placed),
        "created_at": now,
        "updated_at": now,
    }
    moc_path = cfg.vault_path / "40_MOCs" / note_filename("HUB", moc_id, topic)
    _write_moc(
        cfg,
        db,
        moc_path,
        meta,
        topic,
        moc_output.summary,
        subsections,
        previous_body=None,
        stats=garden_stats,
    )
    _store_moc_row(db, moc_id, topic, moc_path, cluster_signature, _HUB_ORIGIN)
    idx.upsert_moc(
        moc_id,
        _moc_embeddable(topic, moc_output.summary),
        {"topic": topic, "note_count": len(placed), "hub_note_id": hub_id},
    )
    if stats is not None:
        stats.omitted += garden_stats.omitted
    logger.info("MOC hub criado: %s — %s (hub %s, %d entradas)", moc_id, topic, hub_id, len(placed))
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
    """Offer new entry neighbours to an existing hub MOC (shared incremental path)."""
    from zettel.gardener import _update_existing_moc

    return _update_existing_moc(
        cfg,
        db,
        idx,
        llm,
        existing_moc,
        [nid for nid in note_ids if nid != hub_id],
        cluster_signature,
        origin=_HUB_ORIGIN,
        prompt_name="moc_hub_incremental.md",
    )


# ── Prompt / body helpers ─────────────────────────────────────────────


def _parse_hub_moc_output(text: str) -> MOCHubGenerationOutput:
    data = parse_llm_json(text)
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
            f"- **{label}**: {note.get('title', '')} (hop={hop}, rel={rel}, peso={weight})",
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
