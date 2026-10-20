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

from zettel.config import AppConfig, llm_phase
from zettel.decision import shadow as decision_shadow
from zettel.garden_entries import select_entry_notes
from zettel.garden_report import ClusterAction, GardenReport, entry_note_ids, fill_reachability
from zettel.gardener_assign import (
    NOTE_ID_RE,
    BucketModelStore,
    ClusterReport,
    assign_notes_to_categories,
    build_embeddings_by_id,
    category_leaves,
    category_pairs,
    cluster_notes_global,
    cluster_notes_within_buckets,
    dominant_category_for_cluster,
    effective_min_cluster_size,
    embed_category_labels,
    extract_note_ids_from_moc_body,
    find_moc_by_note_overlap,
    garden_edge_policy,
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
    known_discards: int = 0  # candidates not offered again: already discarded for that MOC
    routed: dict[str, set[str]] = field(default_factory=dict)  # moc_id -> notes routed to it
    departed: dict[str, int] = field(default_factory=dict)  # moc_id -> entries rebalanced out


# ── Public API ─────────────────────────────────────────────────────────


def run_garden(
    cfg: AppConfig,
    db: StateDB,
    idx: VectorIndex,
    *,
    recreate: bool = False,
    refit: bool = False,
    observer=None,
) -> list[str]:
    """Cluster permanent notes and generate/update MOCs. Returns moc_ids.

    The bucket models are persisted (#300): a run reuses them and places new
    notes into the existing clusters; ``refit`` fits every bucket again.

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
        moc_ids, report = _garden(cfg, db, idx, recreate=recreate, refit=refit, observer=observer)
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
    current MOC notes plus the entry notes of every cluster that would get or
    join a MOC.
    """
    _check_taxonomy(cfg)
    cluster_pairs, cluster_report = _cluster_pairs(cfg, idx, None, readonly=True)
    stats = _GardenStats()
    planned_entries = entry_note_ids(db)
    for category, clusters in group_subjects(cluster_pairs):
        for step in plan_subject(cfg, db, category, clusters):
            moc_id = step.moc["moc_id"] if step.moc else None
            if step.action == "split":
                stats.actions.append(ClusterAction(category, _size(step), "moc_of_mocs"))
                stats.actions.extend(
                    ClusterAction(category, len(ids), "submoc") for ids in step.clusters
                )
            else:
                stats.actions.append(ClusterAction(category, _size(step), step.action, moc_id))
            if step.action in ("signature_skip", "rejected_cohesion"):
                continue
            for note_ids in step.clusters:
                planned_entries.update(_entries(cfg, db, note_ids, cluster_report.vectors))
    report = _garden_report(cfg, db, cluster_report, stats, planned=True)
    return fill_reachability(
        report,
        db,
        planned_entries,
        buckets=cluster_report.buckets,
        min_notes=cfg.gardener.min_notes_for_moc,
        gcfg=cfg.gardener,
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
    cfg: AppConfig,
    db: StateDB,
    idx: VectorIndex,
    *,
    recreate: bool,
    refit: bool = False,
    observer=None,
) -> tuple[list[str], GardenReport]:
    from zettel.progress import report

    _check_taxonomy(cfg)
    llm = get_llm(cfg, "garden")
    cluster_pairs, cluster_report = _cluster_pairs(cfg, idx, observer, refit=refit)
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

    subjects = group_subjects(cluster_pairs)
    for subject_index, (category, clusters) in enumerate(subjects, 1):
        report(
            observer,
            "garden",
            f"Processando assunto {subject_index}/{len(subjects)}.",
            current_item=category,
            current_index=subject_index,
            total_items=len(subjects),
        )
        if decision_shadow.enabled(cfg, "moc_category"):
            for cluster_ids in clusters:
                decision_shadow.shadow_moc_category(
                    cfg,
                    db,
                    category=category,
                    note_ids=cluster_ids,
                    terms=_extract_cluster_terms(db, cluster_ids),
                )
        moc_ids.extend(
            _process_subject(cfg, db, idx, llm, category, clusters, stats, cluster_report.vectors)
        )
    moc_ids = list(dict.fromkeys(moc_ids))
    _rebalance(cfg, db, idx, stats)

    logger.info(
        "Garden: %d MOCs, %d incrementais, %d novos, %d skip assinatura, %d rejeitados coesao",
        len(moc_ids),
        stats.incremental,
        stats.created,
        stats.skipped_signature,
        stats.rejected_cohesion,
    )
    garden_report = fill_reachability(
        _garden_report(cfg, db, cluster_report, stats),
        db,
        entry_note_ids(db),
        buckets=cluster_report.buckets,
        min_notes=cfg.gardener.min_notes_for_moc,
        gcfg=cfg.gardener,
    )
    for warning in garden_report.warnings:
        logger.warning("Garden: %s", warning)
    return moc_ids, garden_report


def _garden_report(
    cfg: AppConfig,
    db: StateDB,
    cluster_report: ClusterReport,
    stats: _GardenStats,
    *,
    planned: bool = False,
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
        known_discards=stats.known_discards,
        refit_buckets=cluster_report.refit_buckets,
        model_buckets=cluster_report.model_buckets,
        predicted_notes=cluster_report.predicted_notes,
        departed=dict(stats.departed),
        mocs_without_cluster=[] if planned else _mocs_without_cluster(db, stats),
    )


def _garden_failure_count(db: StateDB) -> int:
    return sum(int(r["n"]) for r in db.llm_failure_counts() if r["phase"] == "garden")


def _cluster_pairs(
    cfg: AppConfig, idx: VectorIndex, observer, *, refit: bool = False, readonly: bool = False
) -> tuple[list[tuple[str, list[str]]], ClusterReport]:
    """``(category, note_ids)`` per cluster that can become a MOC, and how they were made.

    Per-category clustering goes through the persisted bucket models (#300);
    ``readonly`` (``garden --plan``) reads them but saves nothing.
    """
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
    buckets = (
        assign_notes_to_categories(
            ids, embeddings_by_id, cat_vectors, min_similarity=gcfg.assign_min_similarity
        )
        if cat_vectors
        else {}
    )
    cluster_report.buckets = buckets
    if buckets and gcfg.cluster_within_category:
        store = model_store(cfg, refit=refit, readonly=readonly)
        pairs = cluster_notes_within_buckets(buckets, embeddings_by_id, gcfg, cluster_report, store)
        if pairs:
            return pairs, cluster_report

    pairs = [
        (dominant_category_for_cluster(cluster_ids, buckets), cluster_ids)
        for cluster_ids in cluster_notes_global(ids, np.array(embeddings), gcfg, cluster_report)
    ]
    return pairs, cluster_report


def model_store(cfg: AppConfig, *, refit: bool = False, readonly: bool = False) -> BucketModelStore:
    """The bucket-model store, keyed by everything that invalidates a partition (#300)."""
    gcfg = cfg.gardener
    taxonomy = ""
    if gcfg.topics_path is not None and Path(gcfg.topics_path).is_file():
        taxonomy = sha256_hex(Path(gcfg.topics_path).read_text(encoding="utf-8"))
    fingerprint = sha256_hex(
        json.dumps(
            {
                "embedding": [
                    cfg.embedding.provider,
                    cfg.embedding.model,
                    cfg.embedding.dimensions,
                ],
                "taxonomy": taxonomy,
                "allowed_topics": gcfg.allowed_topics,
                "label": [gcfg.category_label_template, gcfg.category_label_mode],
                "assign_min_similarity": gcfg.assign_min_similarity,
                "min_cluster": effective_min_cluster_size(gcfg),
                "umap_n_neighbors": gcfg.umap_n_neighbors,
                "hdbscan_min_samples": gcfg.hdbscan_min_samples,
                "random_state": gcfg.random_state,
            },
            sort_keys=True,
            default=str,
        )
    )
    return BucketModelStore(cfg.cache_path / "garden", fingerprint, refit=refit, readonly=readonly)


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
        gcfg = cfg.gardener
        return embed_category_labels(
            idx,
            categories,
            cfg.domain.name,
            gcfg.category_label_template,
            mode=gcfg.category_label_mode,
            leaves=category_leaves(gcfg) if gcfg.category_label_mode != "label" else None,
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


def _rebalance(cfg: AppConfig, db: StateDB, idx: VectorIndex, stats: _GardenStats) -> None:
    """Take out of each MOC the entries no cluster of this run sent to it (#300).

    A note that moved to another cluster leaves its old MOC, and
    ``sync_moc_backrefs`` drops the backref; since each cluster is routed to one
    MOC, a note ends up in at most one taxonomy MOC. A MOC that no cluster
    reached this run is left untouched and listed in the report.
    """
    for moc_id, members in stats.routed.items():
        moc = db.get_moc(moc_id)
        if not moc or moc.get("origin") != PIPELINE_ORIGIN:
            continue
        path = _moc_vault_path(cfg, moc)
        if not owns_moc(moc, path, PIPELINE_ORIGIN):
            continue
        structure = _parse_moc_structure(path)
        if structure is None:
            continue
        leaving = structure["all_note_ids"] - members
        if not leaving:
            continue
        subsections = [
            {**sub, "entries": [(n, o) for n, o in sub["entries"] if n not in leaving]}
            for sub in structure["subsections"]
        ]
        _write_moc(
            cfg,
            db,
            path,
            structure["meta"],
            structure["topic"],
            structure["summary"],
            subsections,
            children=structure["children"],
            previous_body=structure["body"],
            stats=stats,
        )
        _store_moc_row(db, moc_id, moc["topic"], path, moc.get("cluster_signature") or "")
        remaining = len(structure["all_note_ids"]) - len(leaving)
        idx.upsert_moc(
            moc_id,
            _moc_embeddable(structure["topic"], structure["summary"]),
            {"topic": moc["topic"], "note_count": remaining},
        )
        stats.departed[moc_id] = len(leaving)
        logger.info("MOC %s: %d entrada(s) sairam (mudaram de cluster)", moc_id, len(leaving))


def _mocs_without_cluster(db: StateDB, stats: _GardenStats) -> list[str]:
    """Topics of pipeline MOCs listing notes that no cluster of this run reached."""
    return sorted(
        moc["topic"]
        for moc in db.list_mocs(origin=PIPELINE_ORIGIN)
        if moc["moc_id"] not in stats.routed and extract_note_ids_from_moc_body(moc.get("body"))
    )


class MocAdoptError(ValueError):
    """The MOC cannot be adopted (unknown, already manual, or its file is gone)."""


def adopt_moc(cfg: AppConfig, db: StateDB, moc_id: str) -> dict:
    """Hand a generated MOC to the user: ``origin: manual`` in the file and in SQLite (#300).

    The routing signature is dropped, so ``sync-manual`` takes the MOC as an
    edited manual note on its next pass; the body, the ``auto-moc-backrefs``
    blocks and the ``topic_index_terms`` rows stay. From then on no garden
    pipeline rewrites or purges it (``owns_moc``, ``purge_mocs_by_origin``).
    """
    moc = db.get_moc(moc_id)
    if not moc:
        raise MocAdoptError(f"MOC nao encontrado: {moc_id}")
    if moc.get("origin") not in (PIPELINE_ORIGIN, "hub_pipeline"):
        raise MocAdoptError(f"MOC {moc_id} ja e {moc.get('origin')}; nada a adotar")
    path = _moc_vault_path(cfg, moc)
    if not path.is_file():
        raise MocAdoptError(f"Arquivo do MOC nao encontrado: {path}")
    meta, body = parse_frontmatter(path.read_text(encoding="utf-8"))
    previous = moc["origin"]
    meta["origin"] = "manual"
    meta.pop("cluster_signature", None)
    meta["updated_at"] = now_vault_iso(cfg.vault_timezone)
    safe_write_note(path, meta, body)
    db.upsert_moc(
        moc_id,
        moc["topic"],
        str(path),
        None,
        body=body,
        frontmatter_json=json.dumps(meta, ensure_ascii=False),
        origin="manual",
    )
    logger.info("MOC %s adotado (%s -> manual)", moc_id, previous)
    return {"moc_id": moc_id, "topic": moc["topic"], "previous_origin": previous, "path": str(path)}


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


# ── Subjects and routing (ADR-021, #296) ─────────────────────────────


@dataclass
class _Route:
    """Where a cluster goes, decided without calling the LLM or writing anything."""

    action: str  # signature_skip | incremental | created | rejected_cohesion
    signature: str
    moc: dict | None = None


@dataclass
class _Step:
    """One routing decision of a subject: a cluster, or its fresh clusters together."""

    action: str  # signature_skip | incremental | rejected_cohesion | created | split | home
    clusters: list[list[str]]
    signature: str
    moc: dict | None = None


def group_subjects(
    cluster_pairs: list[tuple[str, list[str]]],
) -> list[tuple[str, list[list[str]]]]:
    """Clusters grouped by category, in first-seen order (#296).

    A category is one subject. A cluster with no category (``_unassigned``,
    the global path without taxonomy) is a subject on its own.
    """
    subjects: list[tuple[str, list[list[str]]]] = []
    by_category: dict[str, list[list[str]]] = {}
    for category, note_ids in cluster_pairs:
        if not _is_category(category):
            subjects.append((category, [note_ids]))
            continue
        if category not in by_category:
            by_category[category] = []
            subjects.append((category, by_category[category]))
        by_category[category].append(note_ids)
    return subjects


def _entries(
    cfg: AppConfig,
    db: StateDB,
    note_ids: list[str],
    vectors: dict[str, np.ndarray],
    *,
    limit: int | None = None,
) -> list[str]:
    """``select_entry_notes`` under the garden's edge policy (#299)."""
    return select_entry_notes(
        db, note_ids, cfg.gardener, vectors, limit=limit, policy=garden_edge_policy(cfg)
    )


def _is_category(category: str) -> bool:
    return bool(category) and category != "_unassigned"


def _signature(*clusters: list[str]) -> str:
    return sha256_hex("|".join(sorted(set().union(*clusters))))


def _size(step: _Step) -> int:
    return sum(len(note_ids) for note_ids in step.clusters)


def route_cluster(cfg: AppConfig, db: StateDB, note_ids: list[str]) -> _Route:
    """Signature, overlap and cohesion for one cluster, read-only (ADR-021).

    ``created`` means the cluster has no MOC yet; whether it gets a MOC of its
    own, a subsection or a sub-MOC is decided for the whole subject by
    ``plan_subject``. The category name is not a route here: it used to send
    every fresh sub-cluster into the first MOC of the category (#296).
    """
    signature = _signature(note_ids)

    existing_sig = db.get_moc_by_signature(signature)
    if existing_sig:
        return _Route("signature_skip", signature, existing_sig)

    overlap_moc = find_moc_by_note_overlap(db, note_ids, cfg.gardener.overlap_threshold)
    if overlap_moc:
        return _Route("incremental", signature, overlap_moc)

    if cfg.gardener.graph_cohesion_enabled:
        cohesion = graph_cohesion(db, note_ids, garden_edge_policy(cfg))
        logger.debug("Coesao de grafo do cluster: %.3f", cohesion)
        min_ratio = cfg.gardener.graph_cohesion_min_ratio
        if min_ratio > 0 and cohesion < min_ratio:
            logger.warning("Cluster rejeitado: coesao de grafo %.3f < %.3f", cohesion, min_ratio)
            return _Route("rejected_cohesion", signature)

    return _Route("created", signature)


def plan_subject(
    cfg: AppConfig, db: StateDB, category: str, clusters: list[list[str]]
) -> list[_Step]:
    """The routing of one subject, read-only (``garden --plan`` reuses it).

    A cluster with a MOC already (signature or overlap) keeps it. The fresh
    ones: when the category already has its MOC, each joins it (``home``);
    otherwise they become one MOC with a subsection each while
    ``k * min_entries_per_subtopic`` fits in ``max_entries_per_moc``
    (``created``), or a MOC of MOCs with one sub-MOC each (``split``).
    """
    steps: list[_Step] = []
    fresh: list[list[str]] = []
    for note_ids in clusters:
        route = route_cluster(cfg, db, note_ids)
        if route.action == "created":
            fresh.append(note_ids)
        else:
            steps.append(_Step(route.action, [note_ids], route.signature, route.moc))
    if not fresh:
        return steps
    home = _home_moc(db, category)
    if home is not None:
        steps.extend(_Step("home", [note_ids], _signature(note_ids), home) for note_ids in fresh)
    elif len(fresh) == 1 or _fits_one_moc(cfg, len(fresh)):
        steps.append(_Step("created", fresh, _signature(*fresh)))
    else:
        steps.append(_Step("split", fresh, _signature(*fresh)))
    return steps


def _home_moc(db: StateDB, category: str) -> dict | None:
    """The pipeline MOC named after the category: the subject's door, if any."""
    if not _is_category(category):
        return None
    return db.find_moc_by_topic(category, origins=(PIPELINE_ORIGIN,))


def _fits_one_moc(cfg: AppConfig, clusters: int) -> bool:
    gcfg = cfg.gardener
    return clusters * gcfg.min_entries_per_subtopic <= gcfg.max_entries_per_moc


def _process_subject(
    cfg: AppConfig,
    db: StateDB,
    idx: VectorIndex,
    llm: Any,
    category: str,
    clusters: list[list[str]],
    stats: _GardenStats,
    vectors: dict[str, np.ndarray] | None = None,
) -> list[str]:
    """Give one subject its door(s), at most one LLM call per step (ADR-021).

    Routing uses whole clusters; a MOC receives only their entry notes
    (ADR-060). ``vectors`` feed the ``medoid`` entry signal.
    """
    vectors = vectors or {}
    moc_ids: list[str] = []
    for step in plan_subject(cfg, db, category, clusters):
        moc_ids.extend(_run_step(cfg, db, idx, llm, category, step, stats, vectors))
    return list(dict.fromkeys(moc_ids))


def _run_step(
    cfg: AppConfig,
    db: StateDB,
    idx: VectorIndex,
    llm: Any,
    category: str,
    step: _Step,
    stats: _GardenStats,
    vectors: dict[str, np.ndarray],
) -> list[str]:
    gcfg = cfg.gardener
    if step.action == "signature_skip":
        logger.debug("MOC ja existe para assinatura: %s", step.moc["moc_id"])
        stats.skipped_signature += 1
        _route(stats, step.moc["moc_id"], step.clusters)
        return _record(stats, category, _size(step), step.action, step.moc["moc_id"])
    if step.action == "rejected_cohesion":
        stats.rejected_cohesion += 1
        return _record(stats, category, _size(step), step.action, None)
    if step.action == "incremental":
        logger.info("Cluster roteado para MOC %s", step.moc["moc_id"])
        stats.incremental += 1
        # A MOC shared by k clusters gave each one k-th of the cap (#300): asking
        # for more would offer the same notes again on every run.
        share = gcfg.max_entries_per_moc // _moc_clusters(step.moc)
        entries = _entries(cfg, db, step.clusters[0], vectors, limit=share)
        moc_id = _update_existing_moc(
            cfg, db, idx, llm, step.moc, entries, step.signature, stats=stats
        )
        _route(stats, moc_id, step.clusters)
        return _record(stats, category, _size(step), step.action, moc_id)
    if step.action == "home":
        return _join_home(cfg, db, idx, llm, category, step, stats, vectors)
    if step.action == "split":
        return _create_moc_of_mocs(cfg, db, idx, llm, category, step, stats, vectors)

    limit = gcfg.max_entries_per_moc // len(step.clusters)
    groups = [_entries(cfg, db, ids, vectors, limit=limit) for ids in step.clusters]
    moc_id = _create_new_moc(cfg, db, idx, llm, category, groups, step.signature, stats=stats)
    if moc_id:
        stats.created += 1
    _route(stats, moc_id, step.clusters)
    return _record(stats, category, _size(step), "created", moc_id)


def _route(stats: _GardenStats, moc_id: str | None, clusters: list[list[str]]) -> None:
    """Remember which notes reached ``moc_id`` this run (the rebalance keeps only them)."""
    if moc_id:
        members = stats.routed.setdefault(moc_id, set())
        for note_ids in clusters:
            members.update(note_ids)


def _moc_clusters(moc: dict) -> int:
    """How many clusters share a MOC (``clusters`` in its frontmatter mirror, default 1)."""
    try:
        meta = json.loads(moc.get("frontmatter_json") or "{}")
        return max(1, int(meta.get("clusters") or 1))
    except (TypeError, ValueError):
        return 1


def _record(
    stats: _GardenStats, category: str, size: int, action: str, moc_id: str | None
) -> list[str]:
    """Append the step's ``ClusterAction``; a step that produced no MOC is ``failed``."""
    if moc_id is None and action != "rejected_cohesion":
        action = "failed"
    stats.actions.append(ClusterAction(category, size, action, moc_id))
    return [moc_id] if moc_id else []


def _join_home(
    cfg: AppConfig,
    db: StateDB,
    idx: VectorIndex,
    llm: Any,
    category: str,
    step: _Step,
    stats: _GardenStats,
    vectors: dict[str, np.ndarray],
) -> list[str]:
    """A fresh cluster of a subject whose category MOC already exists.

    It joins that MOC as entries while the MOC lists notes only and has room
    for a subtopic. Otherwise, or when the update places none of its notes, it
    gets a sub-MOC listed under the category MOC, so no cluster of a subject is
    left without a door (#296). The fallback is the one case of two calls.
    """
    gcfg = cfg.gardener
    note_ids = step.clusters[0]
    home = db.get_moc(step.moc["moc_id"]) or step.moc
    home_path = _moc_vault_path(cfg, home)
    if not owns_moc(home, home_path, PIPELINE_ORIGIN):
        return _record(stats, category, _size(step), "home", None)
    before = _parse_moc_structure(home_path)
    if before is None:
        return _record(stats, category, _size(step), "home", None)
    room = gcfg.max_entries_per_moc - len(before["all_note_ids"])
    if not before["children"] and room >= gcfg.min_entries_per_subtopic:
        stats.incremental += 1
        share = gcfg.max_entries_per_moc // (_moc_clusters(home) + 1)
        entries = _entries(cfg, db, note_ids, vectors, limit=share)
        moc_id = _update_existing_moc(
            cfg, db, idx, llm, home, entries, step.signature, stats=stats, joins_cluster=True
        )
        if moc_id is None:
            return _record(stats, category, _size(step), "incremental", None)
        after = _parse_moc_structure(home_path)
        if after and (after["all_note_ids"] - before["all_note_ids"]) & set(note_ids):
            _route(stats, moc_id, step.clusters)
            return _record(stats, category, _size(step), "incremental", moc_id)
        logger.info("Nenhuma entrada do cluster entrou no MOC %s: sub-MOC", moc_id)

    entries = _entries(cfg, db, note_ids, vectors)
    child_id = _create_new_moc(
        cfg, db, idx, llm, category, [entries], step.signature, stats=stats, parent=home
    )
    if child_id is None:
        return _record(stats, category, _size(step), "submoc", None)
    stats.created += 1
    _route(stats, child_id, step.clusters)
    _attach_children(cfg, db, home, [child_id], stats=stats)
    return [*_record(stats, category, _size(step), "submoc", child_id), home["moc_id"]]


def _create_moc_of_mocs(
    cfg: AppConfig,
    db: StateDB,
    idx: VectorIndex,
    llm: Any,
    category: str,
    step: _Step,
    stats: _GardenStats,
    vectors: dict[str, np.ndarray],
) -> list[str]:
    """A subject too large for one MOC: a sub-MOC per cluster, listed by the category MOC.

    The category MOC lists no notes, only its sub-MOCs ("Subtemas"), each with
    the first sentence of its summary as the orientation (#296).
    """
    parent_id = str(ULID())
    parent = {"moc_id": parent_id, "topic": category}
    children: list[str] = []
    for note_ids in step.clusters:
        entries = _entries(cfg, db, note_ids, vectors)
        child_id = _create_new_moc(
            cfg, db, idx, llm, category, [entries], _signature(note_ids), stats=stats, parent=parent
        )
        _record(stats, category, len(note_ids), "submoc", child_id)
        _route(stats, child_id, [note_ids])
        if child_id:
            stats.created += 1
            children.append(child_id)
    if not children:
        return []

    child_entries = _child_entries(cfg, db, children)
    summary = _refresh_summary(cfg, db, llm, category, [], parent_id, children=child_entries) or (
        f"Subtemas de {category}."
    )
    now = now_vault_iso(cfg.vault_timezone)
    meta = {
        "type": "moc",
        "moc_id": parent_id,
        "topic": category,
        "origin": PIPELINE_ORIGIN,
        "summary_entries": len(children),
        "created_at": now,
        "updated_at": now,
    }
    moc_path = cfg.vault_path / "40_MOCs" / note_filename("MOC", parent_id, category)
    _write_moc(
        cfg,
        db,
        moc_path,
        meta,
        category,
        summary,
        [],
        children=child_entries,
        previous_body=None,
        stats=stats,
    )
    _store_moc_row(db, parent_id, category, moc_path, step.signature)
    idx.upsert_moc(
        parent_id,
        _moc_embeddable(category, summary),
        {"topic": category, "note_count": 0, "sub_mocs": len(children)},
    )
    _record(stats, category, _size(step), "moc_of_mocs", parent_id)
    logger.info("MOC de MOCs criado: %s — %s (%d sub-MOCs)", parent_id, category, len(children))
    return [parent_id, *children]


def _child_entries(cfg: AppConfig, db: StateDB, child_ids: list[str]) -> list[tuple[str, str]]:
    """``(moc_id, orientation)`` for sub-MOCs: the first sentence of each summary."""
    entries: list[tuple[str, str]] = []
    for child_id in child_ids:
        child = db.get_moc(child_id)
        structure = _parse_moc_structure(_moc_vault_path(cfg, child)) if child else None
        entries.append((child_id, _first_sentence(structure["summary"]) if structure else ""))
    return entries


def _first_sentence(text: str) -> str:
    flat = " ".join(text.split())
    match = re.match(r"(.+?[.!?])(?:\s|$)", flat)
    return match.group(1) if match else flat


def _attach_children(
    cfg: AppConfig, db: StateDB, moc: dict, child_ids: list[str], *, stats: _GardenStats
) -> None:
    """List new sub-MOCs under an existing MOC; its entries and signature stay."""
    path = _moc_vault_path(cfg, moc)
    if not owns_moc(moc, path, PIPELINE_ORIGIN):
        return
    structure = _parse_moc_structure(path)
    if structure is None:
        return
    known = {child_id for child_id, _ in structure["children"]}
    children = structure["children"] + [
        entry for entry in _child_entries(cfg, db, child_ids) if entry[0] not in known
    ]
    _write_moc(
        cfg,
        db,
        path,
        structure["meta"],
        structure["topic"],
        structure["summary"],
        structure["subsections"],
        children=children,
        previous_body=structure["body"],
        stats=stats,
    )
    _store_moc_row(db, moc["moc_id"], moc["topic"], path, moc.get("cluster_signature") or "")


def _create_new_moc(
    cfg: AppConfig,
    db: StateDB,
    idx: VectorIndex,
    llm: Any,
    category: str,
    groups: list[list[str]],
    cluster_signature: str,
    *,
    stats: _GardenStats | None = None,
    parent: dict | None = None,
) -> str | None:
    """Generate a new MOC via LLM (single call) from entry notes.

    ``groups`` holds each cluster's entry notes; several groups ask for one
    subsection each. With ``parent`` (the category MOC), the new MOC is a
    sub-MOC titled "Categoria — subtema" (#296).
    """
    from zettel.moc_backrefs import moc_wikilink

    stats = stats if stats is not None else _GardenStats()
    note_ids = [nid for group in groups for nid in group]
    alias_to_id = _build_note_alias_map(note_ids)
    notes_list_text = _build_grouped_notes_list(db, groups, alias_to_id)
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
        "moc_structure": _structure_instruction(len(groups), parent),
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
    if parent is not None:
        moc_output.topic = _subtopic_title(parent["topic"], moc_output)

    if not _validate_moc_topic(cfg, moc_output):
        return None

    allowed = _allowed_note_ids(db, note_ids)
    placed: set[str] = set()
    subsections = _subsections_from_output(moc_output.subsections, allowed, alias_to_id, placed)
    if not subsections:
        logger.error("MOC nao criado: o LLM nao posicionou nenhuma entrada (%s)", category)
        return None
    stats.omitted += len(allowed - placed)

    moc_id = str(ULID())
    _keep_discards(
        cfg, db, moc_id, allowed - placed, "nao posicionada na criacao", prompt_parts=prompt_parts
    )
    topic = moc_output.topic
    now = now_vault_iso(cfg.vault_timezone)
    meta = {
        "type": "moc",
        "moc_id": moc_id,
        "topic": topic,
        "origin": PIPELINE_ORIGIN,
        "clusters": len(groups),
        "summary_entries": len(placed),
        "created_at": now,
        "updated_at": now,
    }
    parent_id = parent["moc_id"] if parent is not None else None
    if parent is not None:
        meta["parent_moc_id"] = parent_id
        meta["parent_moc"] = moc_wikilink(parent_id, parent["topic"], path=parent.get("path"))
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
    _store_moc_row(db, moc_id, topic, moc_path, cluster_signature, parent_moc_id=parent_id)
    metadata: dict[str, Any] = {"topic": topic, "note_count": len(placed)}
    if parent_id:
        metadata["parent_moc_id"] = parent_id
    idx.upsert_moc(moc_id, _moc_embeddable(topic, moc_output.summary), metadata)
    logger.info("MOC criado: %s — %s (%d entradas)", moc_id, topic, len(placed))
    return moc_id


def _structure_instruction(groups: int, parent: dict | None) -> str:
    parts: list[str] = []
    if groups > 1:
        parts.append(f"As notas vem em {groups} grupos: crie uma subsecao por grupo.")
    if parent is not None:
        parts.append(
            f'Este MOC e um subtema da categoria "{parent["topic"]}": preencha `subtopic` '
            "com um nome curto para o subtema, sem repetir a categoria."
        )
    return " ".join(parts) or "MOC unico do grupo; deixe `subtopic` vazio."


def _subtopic_title(category: str, output: MOCGenerationOutput) -> str:
    """``Categoria — subtema`` for a sub-MOC; the subtheme falls back to the first subsection."""
    subtopic = output.subtopic.strip()
    if not subtopic and output.subsections:
        subtopic = output.subsections[0].title.strip()
    if fold(subtopic).startswith(fold(category)):
        subtopic = subtopic[len(category) :].lstrip(" —-:")
    return f"{category} — {subtopic}" if subtopic else category


def _topic_matches_allowed(topic: str, allowed: list[str]) -> bool:
    if not allowed:
        return True
    topic_lower = topic.lower()
    for allowed_topic in allowed:
        al = allowed_topic.lower()
        if al in topic_lower or topic_lower in al:
            return True
    return False


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
# One sub-MOC line of a MOC of MOCs: "- [[MOC - id - slug]] — orientation" (#296).
_CHILD_LINE_RE = re.compile(r"^\s*-\s*\[\[MOC\s*-\s*(\S+)\s*-[^\]]*\]\]\s*(?:—\s*(.*\S))?\s*$")
SUBTOPICS_HEADING = "Subtemas"


def _parse_moc_structure(moc_path: Path) -> dict | None:
    """Parse an existing MOC file into topic, summary, frontmatter and subsections.

    Each subsection carries ``entries``: ``(note_id, orientation)`` pairs read
    back from the entry lines the pipeline writes. ``children`` holds the
    ``(moc_id, orientation)`` sub-MOC lines of a MOC of MOCs (#296).
    """
    if not moc_path.exists():
        logger.warning("MOC file not found: %s", moc_path)
        return None

    meta, body = parse_frontmatter(moc_path.read_text(encoding="utf-8"))
    summary_lines: list[str] = []
    subsections: list[dict] = []
    children: list[tuple[str, str]] = []
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
        child = _CHILD_LINE_RE.match(line)
        if child:
            children.append((child.group(1), (child.group(2) or "").strip()))
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

    subsections = [sub for sub in subsections if sub["entries"]]
    all_note_ids = {nid for sub in subsections for nid, _ in sub["entries"]}
    return {
        "topic": meta.get("topic", ""),
        "summary": "\n".join(summary_lines).strip(),
        "subsections": subsections,
        "children": children,
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
    joins_cluster: bool = False,
) -> str | None:
    """Offer a cluster's entry candidates to an existing MOC, within its capacity.

    ``joins_cluster``: the cluster is new to this MOC, so its ``clusters``
    count grows when one of its notes enters.

    ``note_ids`` are the cluster's selected entry notes (ADR-060). A note the
    LLM already answered "ignorar" for this MOC is not offered again while its
    semantic checksum is unchanged (#296). A MOC already at
    ``max_entries_per_moc`` takes none and costs no LLM call: those notes are
    still reachable through the graph from the MOC's entries. The MOC row and
    its signature change only when the entries change.
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
    candidates = _fresh_candidates(
        db, moc_id, [n for n in note_ids if n not in existing_ids], stats
    )
    capacity = max(0, cfg.gardener.max_entries_per_moc - len(existing_ids))
    if len(candidates) > capacity:
        stats.cap_reached += len(candidates) - capacity
        candidates = candidates[:capacity]

    if not candidates:
        logger.info("MOC %s: nenhuma entrada nova (cheio, ja presente ou descartada)", moc_id)
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
        stats.discarded[moc_id] = stats.discarded.get(moc_id, 0) + len(ignored)
        db.record_moc_discards(
            moc_id,
            [
                {"note_id": nid, "reason": reason, "note_checksum": _note_checksum(db, nid)}
                for nid, reason in ignored
            ],
            prompt_checksum=sha256_hex(prompt_parts.system + prompt_parts.user_template),
            model=llm_phase(cfg, "garden").model or "",
        )
    stats.omitted += len(omitted)
    _keep_discards(cfg, db, moc_id, omitted, "nao posicionada", prompt_parts=prompt_parts)

    entries_now = sum(len(sub["entries"]) for sub in subsections)
    if entries_now == len(existing_ids):
        logger.info("MOC %s: nenhuma entrada posicionada; arquivo e assinatura mantidos", moc_id)
        return moc_id

    summary = structure["summary"]
    meta = structure["meta"]
    if joins_cluster:
        meta["clusters"] = _moc_clusters({"frontmatter_json": json.dumps(meta)}) + 1
    if _summary_is_stale(cfg, meta, entries_now + len(structure["children"])):
        refreshed = _refresh_summary(
            cfg, db, llm, structure["topic"], subsections, moc_id, children=structure["children"]
        )
        if refreshed:
            summary = refreshed
            meta["summary_entries"] = entries_now + len(structure["children"])

    _write_moc(
        cfg,
        db,
        moc_path,
        meta,
        structure["topic"],
        summary,
        subsections,
        children=structure["children"],
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
        len(ignored),
        len(omitted),
    )
    return moc_id


def _keep_discards(
    cfg: AppConfig,
    db: StateDB,
    moc_id: str,
    note_ids: set[str] | list[str],
    reason: str,
    *,
    prompt_parts: Any = None,
) -> None:
    """Remember notes considered for ``moc_id`` and left out (#300), like ``ignorar``.

    Without it an omitted or truncated candidate is offered, and paid for,
    again on every run, so a run with no new note would still change MOCs.
    """
    if not note_ids:
        return
    prompt = sha256_hex(prompt_parts.system + prompt_parts.user_template) if prompt_parts else ""
    db.record_moc_discards(
        moc_id,
        [
            {"note_id": nid, "reason": reason, "note_checksum": _note_checksum(db, nid)}
            for nid in sorted(note_ids)
        ],
        prompt_checksum=prompt,
        model=llm_phase(cfg, "garden").model or "",
    )


def _fresh_candidates(
    db: StateDB, moc_id: str, candidates: list[str], stats: _GardenStats
) -> list[str]:
    """Candidates minus the notes already discarded for this MOC and unchanged since."""
    discarded = db.moc_discard_checksums(moc_id)
    fresh = [
        nid
        for nid in candidates
        if nid not in discarded or discarded[nid] != _note_checksum(db, nid)
    ]
    stats.known_discards += len(candidates) - len(fresh)
    return fresh


def _note_checksum(db: StateDB, note_id: str) -> str:
    note = db.get_note(note_id) or {}
    return note.get("note_semantic_checksum") or ""


def _parse_incremental_output(text: str) -> MOCIncrementalOutput:
    """Parse LLM response into MOCIncrementalOutput."""
    data = parse_llm_json(text)
    return MOCIncrementalOutput(**data)


def _merge_incremental(
    subsections: list[dict],
    incremental_output: MOCIncrementalOutput,
    allowed_ids: set[str],
    alias_to_id: dict[str, str],
) -> tuple[list[dict], list[tuple[str, str]], set[str]]:
    """Existing subsections plus the LLM's placements and new subsections.

    Returns ``(subsections, ignored, omitted)``: ``ignored`` holds the
    ``(note_id, reason)`` the LLM left out on purpose (``"ignorar"``);
    ``omitted`` the candidates it neither placed nor ignored. Neither is
    listed: a MOC is a door, not an inventory (ADR-060). A subsection title
    is matched case- and accent-insensitively.
    """
    merged = [
        {"title": s["title"], "description": s["description"], "entries": list(s["entries"])}
        for s in subsections
    ]
    by_title = {fold(s["title"]): s for s in merged}
    placed: set[str] = set()
    ignored: list[tuple[str, str]] = []

    for placement in incremental_output.placements:
        nid = _resolve_note_ref(placement.note_id, allowed_ids, alias_to_id)
        if not nid or nid in placed:
            continue
        if fold(placement.subsection) == "ignorar":
            placed.add(nid)
            ignored.append((nid, placement.reason.strip()))
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

    omitted = allowed_ids - placed
    if omitted:
        logger.info("MOC incremental: %d entrada(s) omitida(s) pelo LLM", len(omitted))
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
    cfg: AppConfig,
    db: StateDB,
    llm: Any,
    topic: str,
    subsections: list[dict],
    moc_id: str,
    *,
    children: list[tuple[str, str]] | None = None,
) -> str | None:
    """Rewrite a MOC summary from its current entries and sub-MOCs (one short call)."""
    prompt_parts = load_prompt_parts(cfg.prompts_path / "moc_summary_refresh.md")
    described = _describe_subsections(db, subsections) if subsections else ""
    if children:
        described = (described + "\n" + _describe_children(db, children)).strip()
    mapping = {
        "moc_topic": topic,
        "existing_subsections": described or "_(Nenhuma subsecao)_",
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


def _describe_children(db: StateDB, children: list[tuple[str, str]]) -> str:
    """Sub-MOCs as the summary prompt reads them."""
    lines = [f"#### {SUBTOPICS_HEADING}"]
    for child_id, orientation in children:
        child = db.get_moc(child_id)
        if child:
            lines.append(
                f"- {child['topic']} — {orientation}" if orientation else f"- {child['topic']}"
            )
    return "\n".join(lines)


def _write_moc(
    cfg: AppConfig,
    db: StateDB,
    moc_path: Path,
    meta: dict,
    topic: str,
    summary: str,
    subsections: list[dict],
    *,
    children: list[tuple[str, str]] | None = None,
    previous_body: str | None,
    stats: _GardenStats,
) -> str:
    """Render, write and back-link one MOC file. Returns the body written."""
    from zettel.moc_backrefs import sync_moc_backrefs

    meta["updated_at"] = now_vault_iso(cfg.vault_timezone)
    # The routing signature lives in SQLite only: the file copy went stale on
    # every incremental update (#300).
    meta.pop("cluster_signature", None)
    body, truncated = render_moc_body(
        db,
        topic,
        summary,
        subsections,
        cfg.gardener.max_entries_per_subsection,
        children=children,
    )
    stats.omitted += len(truncated)
    _keep_discards(cfg, db, meta.get("moc_id", ""), truncated, "subsecao cheia")
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
    *,
    parent_moc_id: str | None = None,
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
        parent_moc_id=parent_moc_id,
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


def _build_grouped_notes_list(
    db: StateDB, groups: list[list[str]], alias_to_id: dict[str, str]
) -> str:
    """Notes list for a new MOC; several clusters are shown as numbered groups."""
    if len(groups) == 1:
        return _build_notes_list(db, groups[0], alias_to_id)
    return "\n\n".join(
        f"### Grupo {i}\n{_build_notes_list(db, group, alias_to_id)}"
        for i, group in enumerate(groups, start=1)
    )


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
    *,
    children: list[tuple[str, str]] | None = None,
) -> tuple[str, list[str]]:
    """Markdown body of a MOC. Returns ``(body, entries cut by the per-subsection cap)``.

    Each entry renders as ``- [[ZTL - …]] — orientation``; a subsection left
    with no linkable entry is dropped. Sub-MOCs of a MOC of MOCs (#296) follow
    under "Subtemas" as ``- [[MOC - …]] — orientation``. Shared by the taxonomy
    and hub pipelines.
    """
    from zettel.moc_backrefs import moc_link_line

    body = f"# {topic}\n\n{summary}\n\n"
    truncated: list[str] = []
    seen: set[str] = set()
    for sub in subsections:
        lines: list[str] = []
        for nid, orientation in sub["entries"]:
            if nid in seen:
                continue
            if len(lines) >= max_per_subsection:
                truncated.append(nid)
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
    child_lines: list[str] = []
    for child_id, orientation in children or []:
        child = db.get_moc(child_id)
        if not child:
            continue
        link = moc_link_line(child_id, child["topic"], path=child.get("path"))
        child_lines.append(f"{link} — {orientation}" if orientation else link)
    if child_lines:
        body += f"## {SUBTOPICS_HEADING}\n\n" + "\n".join(child_lines) + "\n\n"
    if truncated:
        logger.info("MOC: %d entrada(s) acima do teto por subsecao", len(truncated))
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
