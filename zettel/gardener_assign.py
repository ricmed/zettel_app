"""Taxonomy assignment, per-category clustering, and graph overlap for MOC gardener."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import numpy as np

from zettel.config import DEFAULT_RELATION_WEIGHTS, GardenerConfig
from zettel.index import VectorIndex
from zettel.taxonomy import category_pillar_pairs, load_moc_taxonomy

if TYPE_CHECKING:
    from zettel.state import StateDB

logger = logging.getLogger(__name__)


@dataclass
class ClusterReport:
    """What the clustering step actually did, for the run summary.

    ``backends`` names every algorithm that produced clusters in this run
    (``umap+hdbscan``, ``kmeans`` or ``single`` for a group kept whole);
    KMeans has no noise and forces every note into a cluster, so a run that
    fell back to it is not comparable to one that did not (ADR-019).
    """

    backends: set[str] = field(default_factory=set)
    clusters: int = 0
    noise_notes: int = 0
    small_buckets: int = 0
    notes_total: int = 0
    buckets: dict[str, list[str]] = field(default_factory=dict)


NOTE_ID_RE = re.compile(r"\[\[ZTL\s*-\s*(\S+)\s*-\s*[^\]]*\]\]")


def extract_note_ids_from_moc_body(body: str) -> set[str]:
    return set(NOTE_ID_RE.findall(body or ""))


def embed_category_labels(
    idx: VectorIndex,
    categories: list[tuple[str, str]],
    domain: str,
    template: str,
) -> dict[str, np.ndarray]:
    """Embed ``(pilar, categoria)`` labels for taxonomy-first assignment.

    ``domain`` stays available on the template so a custom
    ``{domain}: {categoria}`` still works; the default is ``{pilar}: {categoria}``.
    """
    if not categories:
        return {}
    labels = [
        template.format(
            pilar=pilar or domain or "Geral",
            categoria=cat,
            domain=domain or "Geral",
        )
        for pilar, cat in categories
    ]
    vectors = idx.embed_texts(labels)
    names = [cat for _pilar, cat in categories]
    return {cat: np.array(vec, dtype=float) for cat, vec in zip(names, vectors, strict=False)}


def assign_notes_to_categories(
    note_ids: list[str],
    embeddings_by_id: dict[str, np.ndarray],
    category_vectors: dict[str, np.ndarray],
) -> dict[str, list[str]]:
    """Assign each note to the category with highest cosine similarity."""
    buckets: dict[str, list[str]] = {cat: [] for cat in category_vectors}
    if not category_vectors:
        return {"_unassigned": list(note_ids)}

    cat_names = list(category_vectors.keys())
    cat_matrix = np.stack([category_vectors[c] for c in cat_names])

    for nid in note_ids:
        vec = embeddings_by_id.get(nid)
        if vec is None:
            continue
        sims = _cosine_similarity_batch(vec, cat_matrix)
        best_idx = int(np.argmax(sims))
        buckets[cat_names[best_idx]].append(nid)

    return buckets


def assign_vector_to_category(
    vec: np.ndarray,
    category_vectors: dict[str, np.ndarray],
) -> str | None:
    """Return the category name with highest cosine similarity to ``vec``."""
    if not category_vectors:
        return None
    cat_names = list(category_vectors.keys())
    cat_matrix = np.stack([category_vectors[c] for c in cat_names])
    sims = _cosine_similarity_batch(np.asarray(vec, dtype=float), cat_matrix)
    return cat_names[int(np.argmax(sims))]


def effective_min_cluster_size(cfg: GardenerConfig) -> int:
    """Smallest cluster the density step may accept.

    A cluster smaller than ``min_notes_for_moc`` can never become a MOC, so the
    clusterer is not allowed to produce one only for it to be dropped later.
    """
    return max(cfg.min_cluster_size, cfg.min_notes_for_moc)


def cluster_notes_within_buckets(
    buckets: dict[str, list[str]],
    embeddings_by_id: dict[str, np.ndarray],
    cfg: GardenerConfig,
    report: ClusterReport | None = None,
) -> list[tuple[str, list[str]]]:
    """Run UMAP+HDBSCAN (or KMeans) inside each category bucket."""
    results: list[tuple[str, list[str]]] = []
    min_cluster = effective_min_cluster_size(cfg)

    for category, note_ids in buckets.items():
        if category == "_unassigned":
            continue
        ids_arr = [nid for nid in note_ids if nid in embeddings_by_id]
        if len(ids_arr) < cfg.min_notes_for_moc:
            if report is not None and ids_arr:
                report.small_buckets += 1
            continue

        if len(ids_arr) < min_cluster:
            # Big enough for a MOC, too small for density clustering.
            _note_backend(report, "single")
            results.append((category, ids_arr))
            continue

        emb = np.stack([embeddings_by_id[nid] for nid in ids_arr])
        results.extend((category, ids) for ids in _cluster_embeddings(emb, ids_arr, cfg, report))

    return results


def cluster_notes_global(
    note_ids: list[str],
    embeddings: np.ndarray,
    cfg: GardenerConfig,
    report: ClusterReport | None = None,
) -> list[list[str]]:
    """Cluster all notes globally (legacy path when cluster_within_category is false)."""
    return _cluster_embeddings(embeddings, note_ids, cfg, report)


def dominant_category_for_cluster(
    cluster_ids: list[str],
    buckets: dict[str, list[str]],
) -> str:
    """Pick the category that owns the most notes in a global cluster."""
    counts: dict[str, int] = {}
    id_set = set(cluster_ids)
    for cat, members in buckets.items():
        if cat == "_unassigned":
            continue
        overlap = len(id_set & set(members))
        if overlap:
            counts[cat] = overlap
    if not counts:
        return "_unassigned"
    return max(counts, key=counts.get)


def graph_cohesion(
    db: StateDB,
    note_ids: list[str],
    relation_weights: dict[str, float] | None = None,
) -> float:
    """Weighted internal edge ratio for a note set (0..1 scale)."""
    if len(note_ids) < 2:
        return 0.0

    weights = relation_weights or DEFAULT_RELATION_WEIGHTS
    cluster = set(note_ids)
    edges = db.get_connections_for_notes(list(cluster))

    internal_weight = 0.0
    seen_pairs: set[frozenset[str]] = set()
    for edge in edges:
        src = edge["source_note_id"]
        tgt = edge["target_note_id"]
        if src not in cluster or tgt not in cluster:
            continue
        pair = frozenset((src, tgt))
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        rel = edge.get("relation_type") or "related"
        internal_weight += weights.get(rel, 0.5)

    # Normalize by cluster size (avg weighted degree proxy).
    return internal_weight / len(cluster)


def find_moc_by_note_overlap(
    db: StateDB,
    note_ids: list[str],
    threshold: float,
) -> dict | None:
    """Find pipeline MOC with highest note-id overlap fraction."""
    if not note_ids:
        return None

    cluster_set = set(note_ids)
    best_moc: dict | None = None
    best_score = 0.0

    for moc in db.list_mocs(origin="pipeline"):
        body = moc.get("body") or ""
        moc_ids = extract_note_ids_from_moc_body(body)
        if not moc_ids:
            continue
        overlap = len(cluster_set & moc_ids) / len(cluster_set)
        if overlap >= threshold and overlap > best_score:
            best_score = overlap
            best_moc = moc

    return best_moc


def build_embeddings_by_id(ids: list[str], embeddings: list[list[float]]) -> dict[str, np.ndarray]:
    return {nid: np.array(vec, dtype=float) for nid, vec in zip(ids, embeddings, strict=False)}


def _cosine_similarity_batch(vec: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    v_norm = np.linalg.norm(vec)
    if v_norm == 0:
        return np.zeros(matrix.shape[0])
    m_norms = np.linalg.norm(matrix, axis=1)
    m_norms = np.where(m_norms == 0, 1e-10, m_norms)
    return matrix @ vec / (m_norms * v_norm)


def _cluster_embeddings(
    embeddings: np.ndarray,
    ids: list[str],
    cfg: GardenerConfig,
    report: ClusterReport | None = None,
) -> list[list[str]]:
    min_cluster_size = effective_min_cluster_size(cfg)
    try:
        import hdbscan
        import umap
    except ImportError:
        logger.warning("umap-learn ou hdbscan nao instalados. Usando KMeans.")
        return _cluster_kmeans(embeddings, ids, min_cluster_size, cfg.random_state, report)

    n_samples = embeddings.shape[0]
    if cfg.umap_n_neighbors is not None:
        n_neighbors = min(cfg.umap_n_neighbors, n_samples - 1)
    else:
        n_neighbors = min(15, n_samples - 1)
    if n_neighbors < 2:
        _note_backend(report, "single")
        return [ids]

    n_components = min(5, n_samples - 2)
    if n_components < 2:
        return _cluster_kmeans(embeddings, ids, min_cluster_size, cfg.random_state, report)

    init_method = "spectral" if n_samples > n_components + 2 else "random"
    try:
        # A fixed seed makes the partition reproducible (umap-learn trades some
        # parallelism for it); None keeps the faster nondeterministic path.
        reducer = umap.UMAP(
            n_neighbors=n_neighbors,
            n_components=n_components,
            metric="cosine",
            init=init_method,
            random_state=cfg.random_state,
        )
        reduced = reducer.fit_transform(embeddings)
    except Exception as e:
        logger.warning("UMAP falhou (%s). Usando KMeans.", e)
        return _cluster_kmeans(embeddings, ids, min_cluster_size, cfg.random_state, report)

    hdbscan_kwargs: dict = {"min_cluster_size": min_cluster_size, "metric": "euclidean"}
    if cfg.hdbscan_min_samples is not None:
        hdbscan_kwargs["min_samples"] = cfg.hdbscan_min_samples

    clusterer = hdbscan.HDBSCAN(**hdbscan_kwargs)
    labels = clusterer.fit_predict(reduced)

    clusters: dict[int, list[str]] = {}
    for i, label in enumerate(labels):
        if label == -1:
            continue
        clusters.setdefault(label, []).append(ids[i])

    if not clusters:
        if len(ids) >= min_cluster_size:
            _note_backend(report, "single")
            return [ids]
        if report is not None:
            report.noise_notes += len(ids)
        return []

    _note_backend(report, "umap+hdbscan")
    if report is not None:
        report.clusters += len(clusters)
        report.noise_notes += int(sum(1 for label in labels if label == -1))
    return list(clusters.values())


def _cluster_kmeans(
    embeddings: np.ndarray,
    ids: list[str],
    min_cluster_size: int,
    random_state: int | None = 42,
    report: ClusterReport | None = None,
) -> list[list[str]]:
    try:
        from sklearn.cluster import KMeans
    except ImportError:
        logger.error("scikit-learn nao instalado.")
        _note_backend(report, "single")
        return [ids] if len(ids) >= min_cluster_size else []

    n = len(ids)
    k = max(2, n // min_cluster_size)
    k = min(k, n)
    kmeans = KMeans(n_clusters=k, random_state=random_state, n_init=10)
    labels = kmeans.fit_predict(embeddings)

    clusters: dict[int, list[str]] = {}
    for i, label in enumerate(labels):
        clusters.setdefault(label, []).append(ids[i])

    kept = [c for c in clusters.values() if len(c) >= min_cluster_size]
    _note_backend(report, "kmeans")
    if report is not None:
        report.clusters += len(kept)
        report.noise_notes += n - sum(len(c) for c in kept)
    return kept


def _note_backend(report: ClusterReport | None, backend: str) -> None:
    if report is not None:
        report.backends.add(backend)
        if backend == "single":
            report.clusters += 1


def category_pairs(gcfg: GardenerConfig) -> list[tuple[str, str]]:
    """``(pilar, categoria)`` pairs for label embedding.

    The taxonomy file wins; ``allowed_topics`` is the flat fallback (no pillar)
    when the file is absent or unreadable. Shared by `garden` and `connect`.
    """
    if gcfg.topics_path is not None:
        try:
            pairs = category_pillar_pairs(load_moc_taxonomy(gcfg.topics_path))
        except Exception as e:
            logger.warning("Nao foi possivel carregar categorias: %s", e)
        else:
            if pairs:
                return pairs
    return [("", name) for name in gcfg.allowed_topics]
