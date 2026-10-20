"""Taxonomy assignment, per-category clustering, and graph overlap for MOC gardener."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import numpy as np

from zettel.config import DEFAULT_RELATION_WEIGHTS, AppConfig, GardenerConfig
from zettel.index import VectorIndex
from zettel.state.connections import EdgePolicy, connections_for
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
    vectors: dict[str, np.ndarray] = field(default_factory=dict, repr=False)


NOTE_ID_RE = re.compile(r"\[\[ZTL\s*-\s*(\S+)\s*-\s*[^\]]*\]\]")


def extract_note_ids_from_moc_body(body: str) -> set[str]:
    return set(NOTE_ID_RE.findall(body or ""))


UNASSIGNED = "_unassigned"
CATEGORY_LABEL_MODES = ("label", "leaves_in_label", "max_leaf")


def category_label_texts(
    categories: list[tuple[str, str]],
    domain: str,
    template: str,
    *,
    mode: str = "label",
    leaves: Mapping[str, list[str]] | None = None,
) -> dict[str, list[str]]:
    """The texts embedded for each category under ``mode`` (#295).

    * ``label`` — one text, ``template`` as is (``{pilar}: {categoria}``).
    * ``leaves_in_label`` — one text, the leaf topics appended to the category:
      ``{pilar}: {categoria} — folha1, folha2``.
    * ``max_leaf`` — one text per leaf, ``{pilar}: {categoria} — folha``; a
      note's score for the category is its best leaf.

    A category without leaves falls back to ``label``. ``domain`` stays
    available on the template so a custom ``{domain}: {categoria}`` still works.
    """
    leaves = leaves or {}
    texts: dict[str, list[str]] = {}
    for pilar, cat in categories:

        def fmt(categoria: str, pilar: str = pilar) -> str:
            return template.format(
                pilar=pilar or domain or "Geral",
                categoria=categoria,
                domain=domain or "Geral",
            )

        topics = [leaf for leaf in leaves.get(cat, []) if leaf]
        if mode == "leaves_in_label" and topics:
            texts[cat] = [fmt(f"{cat} — {', '.join(topics)}")]
        elif mode == "max_leaf" and topics:
            texts[cat] = [fmt(f"{cat} — {leaf}") for leaf in topics]
        else:
            texts[cat] = [fmt(cat)]
    return texts


def embed_category_labels(
    idx: VectorIndex,
    categories: list[tuple[str, str]],
    domain: str,
    template: str,
    *,
    mode: str = "label",
    leaves: Mapping[str, list[str]] | None = None,
) -> dict[str, np.ndarray]:
    """Embed the category texts of ``mode``: category name -> ``(k, d)`` label vectors."""
    texts = category_label_texts(categories, domain, template, mode=mode, leaves=leaves)
    if not texts:
        return {}
    flat = [text for rows in texts.values() for text in rows]
    vectors = idx.embed_texts(flat)
    out: dict[str, np.ndarray] = {}
    pos = 0
    for cat, rows in texts.items():
        out[cat] = np.array(vectors[pos : pos + len(rows)], dtype=float)
        pos += len(rows)
    return out


class _LabelMatrix:
    """Every label vector stacked, so one product scores a note against all categories."""

    def __init__(self, category_vectors: Mapping[str, np.ndarray]):
        self.names = list(category_vectors)
        blocks = [np.atleast_2d(np.asarray(category_vectors[n], dtype=float)) for n in self.names]
        self.matrix = np.vstack(blocks)
        self.starts = np.cumsum([0, *(len(b) for b in blocks[:-1])])

    def scores(self, vec: np.ndarray) -> np.ndarray:
        """Best cosine of ``vec`` to each category (max over its label rows)."""
        sims = _cosine_similarity_batch(np.asarray(vec, dtype=float), self.matrix)
        return np.maximum.reduceat(sims, self.starts)

    def ranked(self, vec: np.ndarray) -> list[tuple[str, float]]:
        per = self.scores(vec)
        order = np.argsort(-per, kind="stable")
        return [(self.names[i], float(per[i])) for i in order]


def assign_notes_to_categories(
    note_ids: list[str],
    embeddings_by_id: dict[str, np.ndarray],
    category_vectors: dict[str, np.ndarray],
    *,
    min_similarity: float | None = None,
) -> dict[str, list[str]]:
    """Assign each note to the category with the highest cosine similarity.

    With ``min_similarity`` (cosine, ADR-058), a note whose best category
    scores below it goes to the ``_unassigned`` bucket, which the garden
    clusters freely instead of forcing it into a category (#295).
    """
    if not category_vectors:
        return {UNASSIGNED: list(note_ids)}
    buckets: dict[str, list[str]] = {cat: [] for cat in category_vectors}
    labels = _LabelMatrix(category_vectors)
    for nid in note_ids:
        vec = embeddings_by_id.get(nid)
        if vec is None:
            continue
        name, score = labels.ranked(vec)[0]
        if min_similarity is not None and score < min_similarity:
            buckets.setdefault(UNASSIGNED, []).append(nid)
        else:
            buckets[name].append(nid)
    return buckets


def rank_categories(
    vec: np.ndarray, category_vectors: dict[str, np.ndarray]
) -> list[tuple[str, float]]:
    """Every category with its best cosine to ``vec``, strongest first."""
    if not category_vectors:
        return []
    return _LabelMatrix(category_vectors).ranked(vec)


def assign_vector_to_category(
    vec: np.ndarray,
    category_vectors: dict[str, np.ndarray],
) -> str | None:
    """Return the category name with highest cosine similarity to ``vec``."""
    ranked = rank_categories(vec, category_vectors)
    return ranked[0][0] if ranked else None


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
    """Run UMAP+HDBSCAN (or KMeans) inside each category bucket.

    The ``_unassigned`` bucket (notes under ``gardener.assign_min_similarity``,
    #295) is clustered like any other: its clusters become MOCs whose topic the
    LLM proposes, with no category suggested.
    """
    results: list[tuple[str, list[str]]] = []
    min_cluster = effective_min_cluster_size(cfg)

    for category, note_ids in buckets.items():
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


def garden_edge_policy(cfg: AppConfig) -> EdgePolicy:
    """The garden's edge weights: the configured relation weights plus proposed edges (#299)."""
    return EdgePolicy(
        dict(cfg.retrieval.graph_expansion.relation_weights),
        cfg.gardener.proposed_edge_factor,
    )


def graph_cohesion(db: StateDB, note_ids: list[str], policy: EdgePolicy | None = None) -> float:
    """Weighted density of a note set, in ``[0, 1]``: ``2 * W_int / (n * (n - 1))``.

    ``W_int`` sums, over each linked pair inside the set, the strongest edge
    weight under ``policy`` (capped at 1). A clique of weight-1 edges scores 1;
    a set with no internal edge scores 0. The old score divided by ``n`` and
    had no ceiling (0.33 to 2.15 on the measured vault), which the ``ratio``
    knob could not express (#299).
    """
    n = len(set(note_ids))
    if n < 2:
        return 0.0

    policy = policy or EdgePolicy(dict(DEFAULT_RELATION_WEIGHTS))
    cluster = set(note_ids)
    best: dict[frozenset[str], float] = {}
    for edge in connections_for(db, list(cluster), policy):
        src = edge["source_note_id"]
        tgt = edge["target_note_id"]
        if src not in cluster or tgt not in cluster or src == tgt:
            continue
        pair = frozenset((src, tgt))
        best[pair] = max(best.get(pair, 0.0), min(1.0, policy.weight(edge)))

    return 2 * sum(best.values()) / (n * (n - 1))


def moc_entry_groups(body: str) -> list[set[str]]:
    """Entry note ids of each ``##`` section of a MOC body (text before the first heading too)."""
    groups: list[set[str]] = [set()]
    for line in (body or "").splitlines():
        if line.startswith("## "):
            groups.append(set())
            continue
        groups[-1].update(NOTE_ID_RE.findall(line))
    return [g for g in groups if g]


def find_moc_by_note_overlap(
    db: StateDB,
    note_ids: list[str],
    threshold: float,
) -> dict | None:
    """Pipeline MOC one of whose subsections lies mostly inside the cluster.

    A MOC lists only entry notes (ADR-060), so "share of the cluster already in
    the MOC" falls under any threshold for a large cluster. The score is the
    best share of one subsection's entries that belong to the cluster: a MOC
    made from this cluster scores 1.0, and so does a MOC that gave the cluster
    one subsection among several (#296). Ties go to the MOC sharing more entries.
    """
    if not note_ids:
        return None

    cluster_set = set(note_ids)
    best_moc: dict | None = None
    best_key = (0.0, 0)

    for moc in db.list_mocs(origin="pipeline"):
        groups = moc_entry_groups(moc.get("body") or "")
        if not groups:
            continue
        score = max(len(g & cluster_set) / len(g) for g in groups)
        shared = len(set().union(*groups) & cluster_set)
        key = (score, shared)
        if score >= threshold and key > best_key:
            best_key = key
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


def category_leaves(gcfg: GardenerConfig) -> dict[str, list[str]]:
    """Leaf topics of each category in the taxonomy file (empty when there is none)."""
    if gcfg.topics_path is None:
        return {}
    try:
        tax = load_moc_taxonomy(gcfg.topics_path)
    except Exception as e:
        logger.warning("Nao foi possivel carregar os topicos-folha: %s", e)
        return {}
    return {
        cat.nome: list(cat.topicos)
        for pilar in tax.taxonomia_conhecimento
        for cat in pilar.categorias
    }
