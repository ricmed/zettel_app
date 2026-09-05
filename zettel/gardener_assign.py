"""Taxonomy assignment, per-category clustering, and graph overlap for MOC gardener."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

import numpy as np

from zettel.config import DEFAULT_RELATION_WEIGHTS, GardenerConfig
from zettel.index import VectorIndex
from zettel.taxonomy import allowed_topic_names, load_moc_taxonomy

if TYPE_CHECKING:
    from zettel.state import StateDB

logger = logging.getLogger(__name__)

NOTE_ID_RE = re.compile(r"\[\[ZTL\s*-\s*(\S+)\s*-\s*[^\]]*\]\]")


def extract_note_ids_from_moc_body(body: str) -> set[str]:
    return set(NOTE_ID_RE.findall(body or ""))


def embed_category_labels(
    idx: VectorIndex,
    categories: list[str],
    domain: str,
    template: str,
) -> dict[str, np.ndarray]:
    """Embed category names for taxonomy-first assignment."""
    if not categories:
        return {}
    labels = [template.format(domain=domain or "Geral", categoria=cat) for cat in categories]
    vectors = idx.embed_texts(labels)
    return {cat: np.array(vec, dtype=float) for cat, vec in zip(categories, vectors, strict=False)}


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


def cluster_notes_within_buckets(
    buckets: dict[str, list[str]],
    embeddings_by_id: dict[str, np.ndarray],
    cfg: GardenerConfig,
) -> list[tuple[str, list[str]]]:
    """Run UMAP+HDBSCAN (or KMeans) inside each category bucket."""
    results: list[tuple[str, list[str]]] = []
    min_cluster = cfg.min_cluster_size

    for category, note_ids in buckets.items():
        if category == "_unassigned" or not note_ids:
            continue
        if len(note_ids) < cfg.min_notes_for_moc:
            continue

        ids_arr = note_ids
        emb = np.stack([embeddings_by_id[nid] for nid in ids_arr if nid in embeddings_by_id])
        if len(emb) < cfg.min_notes_for_moc:
            continue

        if len(emb) < min_cluster:
            results.append((category, ids_arr))
            continue

        subclusters = _cluster_embeddings(emb, ids_arr, cfg)
        for cluster_ids in subclusters:
            if len(cluster_ids) >= cfg.min_notes_for_moc:
                results.append((category, cluster_ids))

    return results


def cluster_notes_global(
    note_ids: list[str],
    embeddings: np.ndarray,
    cfg: GardenerConfig,
) -> list[list[str]]:
    """Cluster all notes globally (legacy path when cluster_within_category is false)."""
    return _cluster_embeddings(embeddings, note_ids, cfg)


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

    for moc in db.list_mocs():
        if moc.get("origin", "pipeline") != "pipeline":
            continue
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
) -> list[list[str]]:
    min_cluster_size = cfg.min_cluster_size
    try:
        import hdbscan
        import umap
    except ImportError:
        logger.warning("umap-learn ou hdbscan nao instalados. Usando KMeans.")
        return _cluster_kmeans(embeddings, ids, min_cluster_size)

    n_samples = embeddings.shape[0]
    if cfg.umap_n_neighbors is not None:
        n_neighbors = min(cfg.umap_n_neighbors, n_samples - 1)
    else:
        n_neighbors = min(15, n_samples - 1)
    if n_neighbors < 2:
        return [ids]

    n_components = min(5, n_samples - 2)
    if n_components < 2:
        return _cluster_kmeans(embeddings, ids, min_cluster_size)

    init_method = "spectral" if n_samples > n_components + 2 else "random"
    try:
        reducer = umap.UMAP(
            n_neighbors=n_neighbors,
            n_components=n_components,
            metric="cosine",
            init=init_method,
        )
        reduced = reducer.fit_transform(embeddings)
    except Exception as e:
        logger.warning("UMAP falhou (%s). Usando KMeans.", e)
        return _cluster_kmeans(embeddings, ids, min_cluster_size)

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
        return [ids] if len(ids) >= min_cluster_size else []

    return list(clusters.values())


def _cluster_kmeans(
    embeddings: np.ndarray,
    ids: list[str],
    min_cluster_size: int,
) -> list[list[str]]:
    try:
        from sklearn.cluster import KMeans
    except ImportError:
        logger.error("scikit-learn nao instalado.")
        return [ids] if len(ids) >= min_cluster_size else []

    n = len(ids)
    k = max(2, n // min_cluster_size)
    k = min(k, n)
    kmeans = KMeans(n_clusters=k, random_state=42, n_init=10)
    labels = kmeans.fit_predict(embeddings)

    clusters: dict[int, list[str]] = {}
    for i, label in enumerate(labels):
        clusters.setdefault(label, []).append(ids[i])

    return [c for c in clusters.values() if len(c) >= min_cluster_size]


def load_category_names(topics_path) -> list[str]:
    if topics_path is None:
        return []
    try:
        tax = load_moc_taxonomy(topics_path)
        return allowed_topic_names(tax)
    except Exception as e:
        logger.warning("Nao foi possivel carregar categorias: %s", e)
        return []
