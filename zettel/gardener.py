"""The Gardener — clustering, topic extraction, MOC generation.

Uses UMAP + HDBSCAN for clustering and TF-IDF for topic extraction.
Falls back gracefully if dependencies are missing.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from ulid import ULID

from zettel.config import DEFAULT_RELATION_WEIGHTS, AppConfig, llm_phase
from zettel.decision import shadow as decision_shadow
from zettel.garden_entries import select_entry_notes
from zettel.garden_report import ClusterAction, GardenReport, entry_note_ids, fill_reachability
from zettel.gardener_assign import (
    NOTE_ID_RE,
    ClusterReport,
    assign_notes_to_categories,
    build_embeddings_by_id,
    category_pairs,
    cluster_notes_global,
    cluster_notes_within_buckets,
    dominant_category_for_cluster,
    embed_category_labels,
    find_moc_by_note_overlap,
    graph_cohesion,
)
from zettel.hashing import sha256_hex
from zettel.index import VectorIndex
from zettel.llm import (
    _raise_unavailable,
    call_llm,
    fill_template,
    get_llm,
    is_llm_unavailable,
    load_prompt_parts,
    parse_llm_json,
)
from zettel.schemas import (
    MOCEntry,
    MOCGenerationOutput,
    MOCIncrementalOutput,
    MOCSubsection,
    MOCSummaryOutput,
)
from zettel.search_terms import fold
from zettel.state import StateDB
from zettel.taxonomy import TaxonomyLoadError, resolve_allowed_topics
from zettel.time import now_vault_iso
from zettel.vault import (
    note_filename,
    parse_frontmatter,
    permanent_wikilink,
    safe_write_note,
)

logger = logging.getLogger(__name__)

PIPELINE_ORIGIN = "pipeline"


@dataclass
class _GardenStats:
    incremental: int = 0
    created: int = 0
    skipped_signature: int = 0
    rejected_cohesion: int = 0
    actions: list[ClusterAction] = field(default_factory=list)
    discarded: dict[str, int] = field(default_factory=dict)
    omitted: int = 0  # selected entries the LLM did not place (or past a subsection cap)
    cap_reached: int = 0  # entry candidates refused because the MOC was full


# ── Public API ─────────────────────────────────────────────────────────


def run_garden(
    cfg: AppConfig,
    db: StateDB,
    idx: VectorIndex,
    *,
    recreate: bool = False,
    observer=None,
) -> list[str]:
    """Cluster permanent notes and generate/update MOCs. Returns moc_ids.

    Provider outages stop the phase (ADR-059): the run is recorded ``failed``
    for any exception that escapes, and ``--recreate`` purges only after the
    taxonomy, the LLM client and the label embeddings proved usable. A
    completed run persists its ``GardenReport`` (#298).
    """
    from zettel.usage import begin_run, finish_pipeline_run

    run_id = db.start_run("garden")
    begin_run(run_id)
    status = "completed"
    failures_before = _garden_failure_count(db)
    try:
        moc_ids, report = _garden(cfg, db, idx, recreate=recreate, observer=observer)
        report.llm_failures = _garden_failure_count(db) - failures_before
        db.save_garden_report(run_id, "garden", report.to_json())
        return moc_ids
    except Exception:
        status = "failed"
        raise
    finally:
        finish_pipeline_run(db, run_id, status)


def plan_garden(cfg: AppConfig, db: StateDB, idx: VectorIndex) -> GardenReport:
    """What ``run_garden`` would do, with no LLM call and no write (#298).

    Costs only the category-label embeddings. Predicted entry points are the
    current MOC notes plus the notes of every cluster that would create or
    update a MOC.
    """
    _check_taxonomy(cfg)
    cluster_pairs, cluster_report = _cluster_pairs(cfg, idx, None)
    stats = _GardenStats()
    planned_entries = entry_note_ids(db)
    for category, note_ids in cluster_pairs:
        route = route_cluster(cfg, db, category, note_ids)
        moc_id = route.moc["moc_id"] if route.moc else None
        stats.actions.append(ClusterAction(category, len(note_ids), route.action, moc_id))
        if route.action in ("created", "incremental"):
            planned_entries.update(
                select_entry_notes(db, note_ids, cfg.gardener, cluster_report.vectors)
            )
    report = _garden_report(cfg, cluster_report, stats, planned=True)
    return fill_reachability(
        report,
        db,
        planned_entries,
        buckets=cluster_report.buckets,
        min_notes=cfg.gardener.min_notes_for_moc,
    )


def _check_taxonomy(cfg: AppConfig) -> None:
    try:
        resolve_allowed_topics(
            cfg.gardener.topics_path,
            cfg.gardener.allowed_topics,
            strict=cfg.gardener.strict_topics,
        )
    except TaxonomyLoadError as e:
        logger.error("Taxonomia de MOCs indisponivel: %s", e)
        raise


def _garden(
    cfg: AppConfig, db: StateDB, idx: VectorIndex, *, recreate: bool, observer
) -> tuple[list[str], GardenReport]:
    from zettel.progress import report

    _check_taxonomy(cfg)
    llm = get_llm(cfg, "garden")
    cluster_pairs, cluster_report = _cluster_pairs(cfg, idx, observer)
    _log_cluster_report(cluster_report, len(cluster_pairs))

    if recreate:
        removed = purge_pipeline_mocs(cfg, db, idx)
        logger.info("Recriacao: %d MOC(s) do pipeline removidos", removed)

    stats = _GardenStats()
    moc_ids: list[str] = []
    if not cluster_pairs:
        logger.info("Nenhum cluster encontrado")
    else:
        logger.info("Clusters encontrados: %d", len(cluster_pairs))

    for cluster_index, (category, cluster_ids) in enumerate(cluster_pairs, 1):
        report(
            observer,
            "garden",
            f"Processando cluster {cluster_index}/{len(cluster_pairs)}.",
            current_item=category,
            current_index=cluster_index,
            total_items=len(cluster_pairs),
        )
        if decision_shadow.enabled(cfg, "moc_category"):
            decision_shadow.shadow_moc_category(
                cfg,
                db,
                category=category,
                note_ids=cluster_ids,
                terms=_extract_cluster_terms(db, cluster_ids),
            )
        entries = select_entry_notes(db, cluster_ids, cfg.gardener, cluster_report.vectors)
        moc_id = _process_cluster(cfg, db, idx, llm, category, cluster_ids, stats, entries)
        if moc_id:
            moc_ids.append(moc_id)

    logger.info(
        "Garden: %d MOCs, %d incrementais, %d novos, %d skip assinatura, %d rejeitados coesao",
        len(moc_ids),
        stats.incremental,
        stats.created,
        stats.skipped_signature,
        stats.rejected_cohesion,
    )
    garden_report = fill_reachability(
        _garden_report(cfg, cluster_report, stats),
        db,
        entry_note_ids(db),
        buckets=cluster_report.buckets,
        min_notes=cfg.gardener.min_notes_for_moc,
    )
    return moc_ids, garden_report


def _garden_report(
    cfg: AppConfig, cluster_report: ClusterReport, stats: _GardenStats, *, planned: bool = False
) -> GardenReport:
    return GardenReport(
        pipeline="garden",
        planned=planned,
        notes_total=cluster_report.notes_total,
        backends=sorted(cluster_report.backends),
        clusters_found=cluster_report.clusters,
        noise_notes=cluster_report.noise_notes,
        small_buckets=cluster_report.small_buckets,
        bucket_sizes={k: len(v) for k, v in sorted(cluster_report.buckets.items())},
        actions=list(stats.actions),
        discarded=dict(stats.discarded),
        omitted=stats.omitted,
        cap_reached=stats.cap_reached,
    )


def _garden_failure_count(db: StateDB) -> int:
    return sum(int(r["n"]) for r in db.llm_failure_counts() if r["phase"] == "garden")


def _cluster_pairs(
    cfg: AppConfig, idx: VectorIndex, observer
) -> tuple[list[tuple[str, list[str]]], ClusterReport]:
    """``(category, note_ids)`` per cluster that can become a MOC, and how they were made."""
    from zettel.progress import report

    gcfg = cfg.gardener
    cluster_report = ClusterReport()
    note_count = idx.count_permanent_notes()
    report(observer, "garden", f"Analisando {note_count} nota(s).", total_items=note_count)
    if note_count < gcfg.min_cluster_size:
        logger.info("Poucas notas para clusterização (%d < %d)", note_count, gcfg.min_cluster_size)
        return [], cluster_report

    ids, embeddings = idx.get_all_permanent_embeddings()
    if embeddings is None or len(embeddings) < gcfg.min_cluster_size:
        logger.info("Embeddings insuficientes para clusterização")
        return [], cluster_report
    embeddings_by_id = build_embeddings_by_id(ids, embeddings)
    cluster_report.notes_total = len(ids)
    cluster_report.vectors = embeddings_by_id

    categories = category_pairs(gcfg)
    cat_vectors = _category_vectors(cfg, idx, categories) if categories else None
    buckets = assign_notes_to_categories(ids, embeddings_by_id, cat_vectors) if cat_vectors else {}
    cluster_report.buckets = buckets
    if buckets and gcfg.cluster_within_category:
        pairs = cluster_notes_within_buckets(buckets, embeddings_by_id, gcfg, cluster_report)
        if pairs:
            return pairs, cluster_report

    pairs = [
        (dominant_category_for_cluster(cluster_ids, buckets), cluster_ids)
        for cluster_ids in cluster_notes_global(ids, np.array(embeddings), gcfg, cluster_report)
    ]
    return pairs, cluster_report


def _log_cluster_report(cluster_report: ClusterReport, kept: int) -> None:
    backends = ", ".join(sorted(cluster_report.backends)) or "nenhum"
    logger.info(
        "Clusterizacao: backend=%s, clusters=%d, aceitos=%d, notas em ruido=%d, "
        "buckets pequenos=%d",
        backends,
        cluster_report.clusters,
        kept,
        cluster_report.noise_notes,
        cluster_report.small_buckets,
    )
    if "kmeans" in cluster_report.backends:
        logger.warning("Clusterizacao caiu para KMeans: sem ruido, toda nota forcada num cluster")


def _category_vectors(
    cfg: AppConfig, idx: VectorIndex, categories: list[tuple[str, str]]
) -> dict[str, np.ndarray] | None:
    """Embedded category labels; a provider outage stops the phase (ADR-059).

    Any other failure falls back to global clustering, as before.
    """
    try:
        return embed_category_labels(
            idx, categories, cfg.domain.name, cfg.gardener.category_label_template
        )
    except Exception as e:
        if is_llm_unavailable(e):
            _raise_unavailable(e, phase="embedding")
        logger.warning("Atribuicao por categoria falhou (%s); cluster global.", e)
        return None


def purge_pipeline_mocs(cfg: AppConfig, db: StateDB, idx: VectorIndex) -> int:
    """Delete pipeline MOCs from SQLite, ChromaDB and the vault. Returns count removed."""
    return purge_mocs_by_origin(cfg, db, idx, PIPELINE_ORIGIN)


def purge_mocs_by_origin(cfg: AppConfig, db: StateDB, idx: VectorIndex, origin: str) -> int:
    """Delete every MOC of one pipeline ``origin``. Returns count removed.

    The vault file has the last word: a row whose file frontmatter names
    another origin (a manual or hub MOC the database mislabeled) is kept,
    file and row, so a recreate can never delete a note the pipeline does
    not own.
    """
    from zettel.moc_backrefs import clear_moc_backrefs

    removed: list[dict] = []
    for moc in db.list_mocs(origin=origin):
        file_origin = moc_file_origin(_moc_vault_path(cfg, moc))
        if file_origin is not None and file_origin != origin:
            logger.warning(
                "MOC %s mantido: origem '%s' no arquivo, '%s' no banco",
                moc["moc_id"],
                file_origin,
                origin,
            )
            continue
        removed.append(moc)
    if not removed:
        return 0

    for moc in removed:
        clear_moc_backrefs(db, moc, vault_timezone=cfg.vault_timezone)

    moc_ids = [m["moc_id"] for m in removed]
    db.delete_mocs(moc_ids)
    idx.delete_mocs(moc_ids)

    for moc in removed:
        path = _moc_vault_path(cfg, moc)
        if path.is_file():
            path.unlink()
            logger.debug("Arquivo MOC removido: %s", path)

    return len(removed)


def owns_moc(moc: dict, moc_path: Path, origin: str) -> bool:
    """True when both the row and the file say ``origin`` wrote this MOC.

    Defense in depth for the incremental rewrite: it replaces the whole file,
    so it must never touch a manual MOC or another pipeline's MOC.
    """
    file_origin = moc_file_origin(moc_path)
    if moc.get("origin") != origin or (file_origin is not None and file_origin != origin):
        logger.error(
            "MOC %s nao pertence ao pipeline '%s' (banco: %s, arquivo: %s); nao sera reescrito",
            moc.get("moc_id"),
            origin,
            moc.get("origin"),
            file_origin,
        )
        return False
    return True


def moc_file_origin(path: Path) -> str | None:
    """``origin`` from a MOC file's frontmatter (None when absent or unreadable)."""
    if not path.is_file():
        return None
    meta, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
    origin = meta.get("origin")
    return str(origin) if origin else None


def _moc_vault_path(cfg: AppConfig, moc: dict) -> Path:
    path_str = moc.get("path")
    if path_str:
        return Path(path_str)
    return cfg.vault_path / "40_MOCs" / note_filename("MOC", moc["moc_id"], moc["topic"])


# ── Topic Extraction ──────────────────────────────────────────────────


def _extract_cluster_terms(db: StateDB, note_ids: list[str], n_terms: int = 10) -> list[str]:
    """Extract representative terms for a cluster using TF-IDF."""
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
    except ImportError:
        return []

    texts: list[str] = []
    for nid in note_ids:
        note = db.get_note(nid)
        if note:
            texts.append(note.get("title", ""))

    if not texts:
        return []

    vectorizer = TfidfVectorizer(max_features=n_terms, stop_words=None)
    try:
        vectorizer.fit_transform(texts)
        terms = vectorizer.get_feature_names_out()
        return list(terms)
    except Exception:
        return []


# ── MOC Generation ────────────────────────────────────────────────────


@dataclass
class _Route:
    """Where a cluster goes, decided without calling the LLM or writing anything."""

    action: str  # signature_skip | incremental | created | rejected_cohesion
    signature: str
    moc: dict | None = None


def route_cluster(cfg: AppConfig, db: StateDB, category: str, note_ids: list[str]) -> _Route:
    """The routing tree of ADR-021, read-only (``garden --plan`` reuses it)."""
    signature = sha256_hex("|".join(sorted(note_ids)))

    existing_sig = db.get_moc_by_signature(signature)
    if existing_sig:
        return _Route("signature_skip", signature, existing_sig)

    overlap_moc = find_moc_by_note_overlap(db, note_ids, cfg.gardener.overlap_threshold)
    if overlap_moc:
        return _Route("incremental", signature, overlap_moc)

    if category and category != "_unassigned":
        topic_moc = db.find_moc_by_topic(category, origins=(PIPELINE_ORIGIN,))
        if topic_moc:
            return _Route("incremental", signature, topic_moc)

    if cfg.gardener.graph_cohesion_enabled:
        cohesion = graph_cohesion(db, note_ids, DEFAULT_RELATION_WEIGHTS)
        logger.debug("Coesao de grafo do cluster: %.3f", cohesion)
        min_ratio = cfg.gardener.graph_cohesion_min_ratio
        if min_ratio > 0 and cohesion < min_ratio:
            logger.warning("Cluster rejeitado: coesao de grafo %.3f < %.3f", cohesion, min_ratio)
            return _Route("rejected_cohesion", signature)

    return _Route("created", signature)


def _process_cluster(
    cfg: AppConfig,
    db: StateDB,
    idx: VectorIndex,
    llm: Any,
    category: str,
    note_ids: list[str],
    stats: _GardenStats,
    entries: list[str] | None = None,
) -> str | None:
    """Route a cluster to incremental update or new MOC creation (at most one LLM call).

    Routing uses the whole cluster; the MOC receives only ``entries``, the
    cluster's selected entry notes (ADR-060). ``None`` means the whole cluster.
    """
    entries = list(note_ids) if entries is None else entries
    route = route_cluster(cfg, db, category, note_ids)
    moc_id: str | None
    if route.action == "signature_skip":
        logger.debug("MOC ja existe para assinatura: %s", route.moc["moc_id"])
        stats.skipped_signature += 1
        moc_id = route.moc["moc_id"]
    elif route.action == "incremental":
        logger.info("Cluster roteado para MOC %s", route.moc["moc_id"])
        stats.incremental += 1
        moc_id = _update_existing_moc(
            cfg, db, idx, llm, route.moc, entries, route.signature, stats=stats
        )
    elif route.action == "rejected_cohesion":
        stats.rejected_cohesion += 1
        moc_id = None
    else:
        moc_id = _create_new_moc(cfg, db, idx, llm, category, entries, route.signature, stats=stats)
        if moc_id:
            stats.created += 1
    action = route.action if moc_id or route.action == "rejected_cohesion" else "failed"
    stats.actions.append(ClusterAction(category, len(note_ids), action, moc_id))
    return moc_id


def _create_new_moc(
    cfg: AppConfig,
    db: StateDB,
    idx: VectorIndex,
    llm: Any,
    category: str,
    note_ids: list[str],
    cluster_signature: str,
    *,
    stats: _GardenStats | None = None,
) -> str | None:
    """Generate a new MOC via LLM (single call) from the cluster's entry notes."""
    stats = stats if stats is not None else _GardenStats()
    notes_list_text = _build_notes_list(db, note_ids, _build_note_alias_map(note_ids))
    cluster_terms = _extract_cluster_terms(db, note_ids)

    prompt_parts = load_prompt_parts(cfg.prompts_path / "moc_generation.md")

    domain = cfg.domain.name
    try:
        allowed_topics, taxonomy_detail = resolve_allowed_topics(
            cfg.gardener.topics_path,
            cfg.gardener.allowed_topics,
            strict=cfg.gardener.strict_topics,
        )
    except TaxonomyLoadError as e:
        logger.error("Taxonomia de MOCs indisponivel: %s", e)
        return None

    if allowed_topics:
        topics_section = "\n".join(f"- {t}" for t in allowed_topics)
    else:
        topics_section = "_(Nenhuma lista de categorias definida — escolha livremente.)_"

    suggested = category if category and category != "_unassigned" else ""

    mapping = {
        "domain": domain,
        "allowed_topics_section": topics_section,
        "taxonomy_detail": taxonomy_detail,
        "notes_list": notes_list_text,
        "cluster_terms": ", ".join(cluster_terms) if cluster_terms else "N/A",
        "suggested_category": suggested or "_(Nenhuma — escolha da lista.)_",
    }
    system = fill_template(prompt_parts.system, mapping) if prompt_parts.system else ""
    user = fill_template(prompt_parts.user_template, mapping)

    moc_output = call_garden_llm(
        cfg, db, llm, system, user, _parse_moc_output, subject_id=cluster_signature
    )
    if moc_output is None:
        return None

    if suggested and _topic_matches_allowed(suggested, allowed_topics):
        moc_output.topic = suggested

    if not _validate_moc_topic(cfg, moc_output):
        return None

    allowed = _allowed_note_ids(db, note_ids)
    placed: set[str] = set()
    subsections = _subsections_from_output(
        moc_output.subsections, allowed, _build_note_alias_map(note_ids), placed
    )
    if not subsections:
        logger.error("MOC nao criado: o LLM nao posicionou nenhuma entrada (%s)", category)
        return None
    stats.omitted += len(allowed - placed)

    moc_id = str(ULID())
    topic = moc_output.topic
    now = now_vault_iso(cfg.vault_timezone)
    meta = {
        "type": "moc",
        "moc_id": moc_id,
        "topic": topic,
        "cluster_signature": cluster_signature,
        "origin": PIPELINE_ORIGIN,
        "summary_entries": len(placed),
        "created_at": now,
        "updated_at": now,
    }
    moc_path = cfg.vault_path / "40_MOCs" / note_filename("MOC", moc_id, topic)
    _write_moc(
        cfg,
        db,
        moc_path,
        meta,
        topic,
        moc_output.summary,
        subsections,
        previous_body=None,
        stats=stats,
    )
    _store_moc_row(db, moc_id, topic, moc_path, cluster_signature)
    idx.upsert_moc(
        moc_id,
        _moc_embeddable(topic, moc_output.summary),
        {"topic": topic, "note_count": len(placed)},
    )
    logger.info("MOC criado: %s — %s (%d entradas)", moc_id, topic, len(placed))
    return moc_id


def _topic_matches_allowed(topic: str, allowed: list[str]) -> bool:
    if not allowed:
        return True
    topic_lower = topic.lower()
    for allowed_topic in allowed:
        al = allowed_topic.lower()
        if al in topic_lower or topic_lower in al:
            return True
    return False


def _generate_moc(
    cfg: AppConfig,
    db: StateDB,
    idx: VectorIndex,
    llm: Any,
    note_ids: list[str],
) -> str | None:
    """Backward-compatible entry: process cluster with unknown category."""
    stats = _GardenStats()
    return _process_cluster(cfg, db, idx, llm, "_unassigned", note_ids, stats)


# ── MOC Topic Validation ─────────────────────────────────────────────


def _validate_moc_topic(cfg: AppConfig, moc_output: MOCGenerationOutput) -> bool:
    """Validate that the MOC topic matches an allowed category name."""
    try:
        allowed, _ = resolve_allowed_topics(
            cfg.gardener.topics_path,
            cfg.gardener.allowed_topics,
            strict=cfg.gardener.strict_topics,
        )
    except TaxonomyLoadError as e:
        logger.error("Taxonomia de MOCs indisponivel na validacao: %s", e)
        return False

    if not allowed:
        return True

    topic = moc_output.topic
    topic_lower = topic.lower()

    for allowed_topic in allowed:
        allowed_lower = allowed_topic.lower()
        if allowed_lower in topic_lower or topic_lower in allowed_lower:
            logger.debug("Topico '%s' corresponde a '%s'", topic, allowed_topic)
            return True

    justification = moc_output.topic_justification
    if cfg.gardener.strict_topics:
        logger.warning(
            "MOC rejeitado: topico '%s' fora da lista permitida. Justificativa: %s",
            topic,
            justification or "(nenhuma)",
        )
        return False
    else:
        logger.info(
            "MOC aprovado (modo permissivo): topico '%s' fora da lista. Justificativa: %s",
            topic,
            justification or "(nenhuma)",
        )
        return True


# ── Incremental MOC Update ────────────────────────────────────────

# One entry line: "- [[ZTL - id - slug]] — why to start here" (ADR-060).
_ENTRY_LINE_RE = re.compile(r"^\s*-\s*(\[\[ZTL[^\]]*\]\])\s*(?:—\s*(.*\S))?\s*$")


def _parse_moc_structure(moc_path: Path) -> dict | None:
    """Parse an existing MOC file into topic, summary, frontmatter and subsections.

    Each subsection carries ``entries``: ``(note_id, orientation)`` pairs read
    back from the entry lines the pipeline writes.
    """
    if not moc_path.exists():
        logger.warning("MOC file not found: %s", moc_path)
        return None

    meta, body = parse_frontmatter(moc_path.read_text(encoding="utf-8"))
    summary_lines: list[str] = []
    subsections: list[dict] = []
    current_sub: dict | None = None
    in_summary = False

    for line in body.split("\n"):
        if line.startswith("# ") and not line.startswith("## "):
            in_summary = True
            continue
        if line.startswith("## "):
            in_summary = False
            current_sub = {"title": line[3:].strip(), "description": "", "entries": []}
            subsections.append(current_sub)
            continue
        if in_summary:
            summary_lines.append(line)
            continue
        if current_sub is None:
            continue
        entry = _ENTRY_LINE_RE.match(line)
        ids = NOTE_ID_RE.findall(entry.group(1)) if entry else []
        if ids:
            current_sub["entries"].append((ids[0], (entry.group(2) or "").strip()))
        elif line.strip() and not line.strip().startswith("-"):
            sep = " " if current_sub["description"] else ""
            current_sub["description"] += sep + line.strip()

    all_note_ids = {nid for sub in subsections for nid, _ in sub["entries"]}
    return {
        "topic": meta.get("topic", ""),
        "summary": "\n".join(summary_lines).strip(),
        "subsections": subsections,
        "all_note_ids": all_note_ids,
        "meta": meta,
        "body": body,
    }


def _update_existing_moc(
    cfg: AppConfig,
    db: StateDB,
    idx: VectorIndex,
    llm: Any,
    existing_moc: dict,
    note_ids: list[str],
    cluster_signature: str,
    *,
    stats: _GardenStats | None = None,
    origin: str = PIPELINE_ORIGIN,
    prompt_name: str = "moc_incremental.md",
) -> str | None:
    """Offer a cluster's entry candidates to an existing MOC, within its capacity.

    ``note_ids`` are the cluster's selected entry notes (ADR-060). A MOC already
    at ``max_entries_per_moc`` takes none and costs no LLM call: those notes are
    still reachable through the graph from the MOC's entries.
    """
    stats = stats if stats is not None else _GardenStats()
    moc_id = existing_moc["moc_id"]
    moc_path = Path(existing_moc["path"]) if existing_moc.get("path") else None

    if not moc_path or not moc_path.exists():
        logger.warning("MOC file not found for %s, skipping incremental update", moc_id)
        return None
    if not owns_moc(existing_moc, moc_path, origin):
        return None

    structure = _parse_moc_structure(moc_path)
    if structure is None:
        return None

    existing_ids = structure["all_note_ids"]
    candidates = [nid for nid in note_ids if nid not in existing_ids]
    capacity = max(0, cfg.gardener.max_entries_per_moc - len(existing_ids))
    if len(candidates) > capacity:
        stats.cap_reached += len(candidates) - capacity
        candidates = candidates[:capacity]

    if not candidates:
        logger.info("MOC %s: nenhuma entrada nova (cheio ou ja presente)", moc_id)
        _store_moc_row(db, moc_id, existing_moc["topic"], moc_path, cluster_signature, origin)
        return moc_id

    logger.info("MOC %s: %d entrada(s) candidata(s)", moc_id, len(candidates))
    prompt_parts = load_prompt_parts(cfg.prompts_path / prompt_name)
    alias_to_id = _build_note_alias_map(candidates)
    mapping = {
        "moc_topic": structure["topic"],
        "moc_summary": structure["summary"],
        "existing_subsections": _describe_subsections(db, structure["subsections"]),
        "new_notes_list": _build_notes_list(db, candidates, alias_to_id),
    }
    system = fill_template(prompt_parts.system, mapping) if prompt_parts.system else ""
    user = fill_template(prompt_parts.user_template, mapping)

    incremental_output = call_garden_llm(
        cfg, db, llm, system, user, _parse_incremental_output, subject_id=moc_id
    )
    if incremental_output is None:
        return None

    subsections, ignored, omitted = _merge_incremental(
        structure["subsections"],
        incremental_output,
        _allowed_note_ids(db, candidates),
        alias_to_id,
    )
    if ignored:
        stats.discarded[moc_id] = stats.discarded.get(moc_id, 0) + ignored
    stats.omitted += omitted

    summary = structure["summary"]
    meta = structure["meta"]
    entries_now = sum(len(sub["entries"]) for sub in subsections)
    if _summary_is_stale(cfg, meta, entries_now):
        refreshed = _refresh_summary(cfg, db, llm, structure["topic"], subsections, moc_id)
        if refreshed:
            summary = refreshed
            meta["summary_entries"] = entries_now

    _write_moc(
        cfg,
        db,
        moc_path,
        meta,
        structure["topic"],
        summary,
        subsections,
        previous_body=structure["body"],
        stats=stats,
    )
    _store_moc_row(db, moc_id, existing_moc["topic"], moc_path, cluster_signature, origin)
    metadata = {"topic": existing_moc["topic"], "note_count": entries_now}
    if structure["meta"].get("hub_note_id"):
        metadata["hub_note_id"] = structure["meta"]["hub_note_id"]
    idx.upsert_moc(moc_id, _moc_embeddable(structure["topic"], summary), metadata)
    logger.info(
        "MOC %s atualizado: %d entradas, %d deixadas de fora pelo LLM, %d omitidas",
        moc_id,
        entries_now,
        ignored,
        omitted,
    )
    return moc_id


def _parse_incremental_output(text: str) -> MOCIncrementalOutput:
    """Parse LLM response into MOCIncrementalOutput."""
    data = parse_llm_json(text)
    return MOCIncrementalOutput(**data)


def _merge_incremental(
    subsections: list[dict],
    incremental_output: MOCIncrementalOutput,
    allowed_ids: set[str],
    alias_to_id: dict[str, str],
) -> tuple[list[dict], int, int]:
    """Existing subsections plus the LLM's placements and new subsections.

    Returns ``(subsections, ignored, omitted)``: ``ignored`` notes the LLM left
    out on purpose (``"ignorar"``); ``omitted`` candidates it neither placed nor
    ignored. Neither is listed: a MOC is a door, not an inventory (ADR-060).
    A subsection title is matched case- and accent-insensitively.
    """
    merged = [
        {"title": s["title"], "description": s["description"], "entries": list(s["entries"])}
        for s in subsections
    ]
    by_title = {fold(s["title"]): s for s in merged}
    placed: set[str] = set()
    ignored = 0

    for placement in incremental_output.placements:
        nid = _resolve_note_ref(placement.note_id, allowed_ids, alias_to_id)
        if not nid or nid in placed:
            continue
        if fold(placement.subsection) == "ignorar":
            placed.add(nid)
            ignored += 1
            continue
        target = by_title.get(fold(placement.subsection))
        if target is None:
            continue
        target["entries"].append((nid, placement.reason.strip()))
        placed.add(nid)

    for new_sub in incremental_output.new_subsections:
        entries = _resolve_entries(new_sub.entries, allowed_ids, alias_to_id, placed)
        if entries:
            merged.append(
                {"title": new_sub.title, "description": new_sub.description, "entries": entries}
            )

    omitted = len(allowed_ids - placed)
    if omitted:
        logger.info("MOC incremental: %d entrada(s) omitida(s) pelo LLM", omitted)
    return merged, ignored, omitted


def _resolve_entries(
    entries: list[MOCEntry],
    allowed_ids: set[str],
    alias_to_id: dict[str, str],
    placed: set[str],
) -> list[tuple[str, str]]:
    """LLM entries resolved to known note ids, each placed at most once."""
    resolved: list[tuple[str, str]] = []
    for entry in entries:
        nid = _resolve_note_ref(entry.note_id, allowed_ids, alias_to_id)
        if not nid or nid in placed:
            continue
        placed.add(nid)
        resolved.append((nid, entry.orientation.strip()))
    return resolved


def _summary_is_stale(cfg: AppConfig, meta: dict, entries_now: int) -> bool:
    try:
        base = int(meta.get("summary_entries") or 0)
    except (TypeError, ValueError):
        base = 0
    if base <= 0:
        return False
    return (entries_now - base) / base >= cfg.gardener.summary_refresh_growth


def _refresh_summary(
    cfg: AppConfig, db: StateDB, llm: Any, topic: str, subsections: list[dict], moc_id: str
) -> str | None:
    """Rewrite a MOC summary from its current entries (one short call)."""
    prompt_parts = load_prompt_parts(cfg.prompts_path / "moc_summary_refresh.md")
    mapping = {
        "moc_topic": topic,
        "existing_subsections": _describe_subsections(db, subsections),
    }
    system = fill_template(prompt_parts.system, mapping) if prompt_parts.system else ""
    user = fill_template(prompt_parts.user_template, mapping)
    output = call_garden_llm(
        cfg, db, llm, system, user, _parse_summary_output, subject_id=f"{moc_id}:summary"
    )
    return output.summary.strip() if output and output.summary.strip() else None


def _parse_summary_output(text: str) -> MOCSummaryOutput:
    return MOCSummaryOutput(**parse_llm_json(text))


def _describe_subsections(db: StateDB, subsections: list[dict]) -> str:
    """Subsections as the incremental and summary prompts read them."""
    parts: list[str] = []
    for sub in subsections:
        parts.append(f"#### {sub['title']}")
        if sub["description"]:
            parts.append(sub["description"])
        for nid, orientation in sub["entries"]:
            link = _note_wikilink(db, nid)
            if link:
                parts.append(f"{link} — {orientation}" if orientation else link)
        parts.append("")
    return "\n".join(parts) if parts else "_(Nenhuma subsecao)_"


def _write_moc(
    cfg: AppConfig,
    db: StateDB,
    moc_path: Path,
    meta: dict,
    topic: str,
    summary: str,
    subsections: list[dict],
    *,
    previous_body: str | None,
    stats: _GardenStats,
) -> str:
    """Render, write and back-link one MOC file. Returns the body written."""
    from zettel.moc_backrefs import sync_moc_backrefs

    meta["updated_at"] = now_vault_iso(cfg.vault_timezone)
    body, truncated = render_moc_body(
        db, topic, summary, subsections, cfg.gardener.max_entries_per_subsection
    )
    stats.omitted += truncated
    safe_write_note(moc_path, meta, body)
    sync_moc_backrefs(
        db,
        meta.get("moc_id", ""),
        topic,
        moc_path,
        vault_timezone=cfg.vault_timezone,
        previous_body=previous_body,
    )
    return body


def _store_moc_row(
    db: StateDB,
    moc_id: str,
    topic: str,
    moc_path: Path,
    signature: str,
    origin: str = PIPELINE_ORIGIN,
) -> None:
    body_snap, fm_snap = _snapshot_moc_file(moc_path)
    db.upsert_moc(
        moc_id,
        topic,
        str(moc_path),
        signature,
        body=body_snap,
        frontmatter_json=fm_snap,
        origin=origin,
    )


# ── Helpers ───────────────────────────────────────────────────────────


def call_garden_llm[T](
    cfg: AppConfig,
    db: StateDB,
    llm: Any,
    system: str,
    user: str,
    parse: Callable[[str], T],
    *,
    subject_id: str,
) -> T | None:
    """One garden LLM call. Outages propagate (ADR-059); a bad answer is per cluster.

    A response that does not parse or validate is kept in ``llm_failures``
    (``phase='garden'``) with the raw text, and the cluster is skipped.
    """
    spec = llm_phase(cfg, "garden")
    try:
        response = call_llm(
            llm,
            user,
            system=system or None,
            provider=spec.provider,
            prompt_cache=cfg.llm.prompt_cache,
        )
    except Exception as e:
        if is_llm_unavailable(e):
            _raise_unavailable(e, phase="garden")
        logger.error("Erro na chamada de LLM do garden (%s): %s", subject_id, e)
        return None
    try:
        return parse(response)
    except Exception as e:
        logger.error("Resposta invalida do garden (%s): %s", subject_id, e)
        db.record_llm_failure(
            "garden", str(e), response, subject_id=subject_id, model=spec.model or ""
        )
        return None


def _moc_embeddable(topic: str, summary: str) -> str:
    """Canonical embeddable text for a MOC (unified across gardener and sync)."""
    return f"{topic}\n\n{summary}".strip()


def _snapshot_moc_file(moc_path: Path) -> tuple[str | None, str | None]:
    """Read a MOC file and return (body_without_frontmatter, frontmatter_json).

    Used to persist the MOC content into the DB so `zettel rebuild` can recreate the
    .md file without reprocessing the LLM. Returns (None, None) if the file is missing.
    """
    if not moc_path.exists():
        return None, None
    meta, body = parse_frontmatter(moc_path.read_text(encoding="utf-8"))
    return body, json.dumps(meta, ensure_ascii=False)


def _build_notes_list(
    db: StateDB,
    note_ids: list[str],
    alias_to_id: dict[str, str] | None = None,
) -> str:
    id_to_alias = {nid: alias for alias, nid in (alias_to_id or {}).items()}
    parts: list[str] = []
    for nid in note_ids:
        note = db.get_note(nid)
        if note:
            title = note.get("title", "Sem título")
            label = id_to_alias.get(nid, nid)
            parts.append(f"- **{label}**: {title}")
    return "\n".join(parts) if parts else "Nenhuma nota encontrada."


def _build_note_alias_map(note_ids: list[str]) -> dict[str, str]:
    """Map short aliases (N1, N2, ...) to note IDs for LLM prompts."""
    return {f"N{i}": nid for i, nid in enumerate(note_ids, start=1)}


def _allowed_note_ids(db: StateDB, note_ids: list[str]) -> set[str]:
    """Note IDs from the cluster that exist in StateDB and can be linked."""
    return {nid for nid in note_ids if db.get_note(nid)}


def _resolve_note_ref(
    ref: str,
    allowed_ids: set[str],
    alias_to_id: dict[str, str],
) -> str | None:
    """Resolve an LLM note reference (alias or ULID) to a known cluster ID."""
    token = ref.strip()
    if not token:
        return None

    if token in alias_to_id:
        return alias_to_id[token]
    if token in allowed_ids:
        return token

    fuzzy = _fuzzy_match_note_id(token, allowed_ids)
    if fuzzy:
        logger.warning("MOC: referencia '%s' corrigida para '%s' (typo)", token, fuzzy)
        return fuzzy

    logger.warning("MOC: referencia '%s' descartada (fora do cluster)", token)
    return None


def _fuzzy_match_note_id(ref: str, allowed_ids: set[str]) -> str | None:
    """Return the sole allowed ID within edit distance 1, if unambiguous."""
    matches = [nid for nid in allowed_ids if _within_edit_distance_one(ref, nid)]
    if len(matches) == 1:
        return matches[0]
    return None


def _within_edit_distance_one(a: str, b: str) -> bool:
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la == lb:
        return sum(x != y for x, y in zip(a, b, strict=False)) == 1
    if la > lb:
        a, b = b, a
        la, lb = lb, la
    i = j = diffs = 0
    while i < la and j < lb:
        if a[i] == b[j]:
            i += 1
            j += 1
        else:
            diffs += 1
            if diffs > 1:
                return False
            j += 1
    return diffs + (lb - j) <= 1


def _note_wikilink(db: StateDB, note_id: str) -> str | None:
    note = db.get_note(note_id)
    if not note:
        logger.warning("MOC: nota %s ausente no banco, link omitido", note_id)
        return None
    link = permanent_wikilink(
        note_id,
        note.get("title", ""),
        path=note.get("path"),
    )
    return f"- {link}"


def render_moc_body(
    db: StateDB,
    topic: str,
    summary: str,
    subsections: list[dict],
    max_per_subsection: int,
) -> tuple[str, int]:
    """Markdown body of a MOC. Returns ``(body, entries cut by the per-subsection cap)``.

    Each entry renders as ``- [[ZTL - …]] — orientation``; a subsection left
    with no linkable entry is dropped. Shared by the taxonomy and hub pipelines.
    """
    body = f"# {topic}\n\n{summary}\n\n"
    truncated = 0
    seen: set[str] = set()
    for sub in subsections:
        lines: list[str] = []
        for nid, orientation in sub["entries"]:
            if nid in seen:
                continue
            if len(lines) >= max_per_subsection:
                truncated += 1
                continue
            link = _note_wikilink(db, nid)
            if not link:
                continue
            seen.add(nid)
            lines.append(f"{link} — {orientation}" if orientation else link)
        if not lines:
            continue
        body += f"## {sub['title']}\n\n"
        if sub.get("description"):
            body += f"{sub['description']}\n\n"
        body += "\n".join(lines) + "\n\n"
    if truncated:
        logger.info("MOC: %d entrada(s) acima do teto por subsecao", truncated)
    return body, truncated


def _subsections_from_output(
    subsections: list[MOCSubsection],
    allowed_ids: set[str],
    alias_to_id: dict[str, str],
    placed: set[str],
) -> list[dict]:
    """LLM subsections resolved to ``{title, description, entries}`` dicts."""
    resolved: list[dict] = []
    for sub in subsections:
        entries = _resolve_entries(sub.entries, allowed_ids, alias_to_id, placed)
        if entries:
            resolved.append(
                {"title": sub.title, "description": sub.description, "entries": entries}
            )
    return resolved


def _parse_moc_output(text: str) -> MOCGenerationOutput:
    data = parse_llm_json(text)
    return MOCGenerationOutput(**data)
