"""Tests for hybrid gardener assignment and graph helpers."""

import numpy as np
import pytest
from zettel.gardener_assign import (
    assign_notes_to_categories,
    extract_note_ids_from_moc_body,
    find_moc_by_note_overlap,
    graph_cohesion,
)
from zettel.state import StateDB

from tests.fakes import FakeVectorIndex


def test_assign_notes_to_categories():
    cat_a = np.array([1.0, 0.0])
    cat_b = np.array([0.0, 1.0])
    category_vectors = {"CatA": cat_a, "CatB": cat_b}
    embeddings_by_id = {
        "n1": np.array([0.9, 0.1]),
        "n2": np.array([0.1, 0.9]),
    }
    buckets = assign_notes_to_categories(
        ["n1", "n2"],
        embeddings_by_id,
        category_vectors,
    )
    assert buckets["CatA"] == ["n1"]
    assert buckets["CatB"] == ["n2"]


def test_extract_note_ids_from_moc_body():
    body = "- [[ZTL - NOTE001 - slug-a]]\n- [[ZTL - NOTE002 - slug-b]]"
    assert extract_note_ids_from_moc_body(body) == {"NOTE001", "NOTE002"}


def test_find_moc_by_note_overlap(tmp_path):
    db = StateDB(tmp_path / "test.db")
    body = "- [[ZTL - NOTE001 - a]]\n- [[ZTL - NOTE002 - b]]"
    db.upsert_moc(
        "MOC1",
        "Topic",
        "/p/moc.md",
        "sig",
        body=body,
        origin="pipeline",
    )
    db.upsert_moc(
        "MOC2",
        "Other",
        "/p/moc2.md",
        "sig2",
        body="- [[ZTL - NOTE999 - x]]",
        origin="pipeline",
    )

    match = find_moc_by_note_overlap(db, ["NOTE001", "NOTE003"], threshold=0.4)
    assert match is not None
    assert match["moc_id"] == "MOC1"

    no_match = find_moc_by_note_overlap(db, ["NOTE888", "NOTE777"], threshold=0.4)
    assert no_match is None
    db.close()


def test_overlap_counts_one_subsection_not_the_whole_moc(tmp_path):
    """A MOC that gave this cluster one subsection among three still owns it (#296)."""
    db = StateDB(tmp_path / "test.db")
    body = (
        "## A\n\n- [[ZTL - A1 - a]]\n- [[ZTL - A2 - a]]\n\n"
        "## B\n\n- [[ZTL - B1 - b]]\n- [[ZTL - B2 - b]]\n\n"
        "## C\n\n- [[ZTL - C1 - c]]\n- [[ZTL - C2 - c]]\n"
    )
    db.upsert_moc("M1", "Tema", "/p/m.md", "sig", body=body, origin="pipeline")
    cluster = ["A1", "A2", *(f"X{i}" for i in range(30))]

    assert find_moc_by_note_overlap(db, cluster, threshold=0.4)["moc_id"] == "M1"
    assert find_moc_by_note_overlap(db, ["A1", "B1", "C1"], threshold=0.6) is None
    db.close()


def test_graph_cohesion_internal_edges(tmp_path):
    db = StateDB(tmp_path / "test.db")
    for nid in ("A", "B", "C"):
        db.upsert_note(nid, "SRC", None, nid)
    db.upsert_note_connection("A", "B", "extends", "")
    db.upsert_note_connection("B", "C", "supports", "")

    score = graph_cohesion(db, ["A", "B", "C"])
    assert score > 0

    graph_cohesion(db, ["A", "B"])
    db.upsert_note_connection("A", "X", "related", "")
    assert graph_cohesion(db, ["A", "B"]) >= 0
    db.close()


# ── Reproducible clustering (#294) ───────────────────────────────────


def _blobs(n_per: int = 25, dims: int = 16, seed: int = 0) -> tuple[np.ndarray, list[str]]:
    rng = np.random.default_rng(seed)
    centers = np.eye(dims)[:3] * 4.0
    points = np.vstack([c + rng.normal(scale=0.6, size=(n_per, dims)) for c in centers])
    return points, [f"n{i}" for i in range(len(points))]


@pytest.mark.slow
def test_cluster_embeddings_is_deterministic_with_seed():
    from zettel.config import GardenerConfig
    from zettel.gardener_assign import _cluster_embeddings

    emb, ids = _blobs()
    cfg = GardenerConfig(min_cluster_size=5, min_notes_for_moc=5, random_state=7)

    first = _cluster_embeddings(emb, ids, cfg)
    second = _cluster_embeddings(emb, ids, cfg)

    assert first
    assert sorted(map(sorted, first)) == sorted(map(sorted, second))


def test_effective_min_cluster_size_never_below_moc_floor():
    from zettel.config import GardenerConfig
    from zettel.gardener_assign import effective_min_cluster_size

    assert (
        effective_min_cluster_size(GardenerConfig(min_cluster_size=5, min_notes_for_moc=10)) == 10
    )
    assert effective_min_cluster_size(GardenerConfig(min_cluster_size=5, min_notes_for_moc=3)) == 5


def test_no_accepted_cluster_is_smaller_than_moc_floor():
    from zettel.config import GardenerConfig
    from zettel.gardener_assign import ClusterReport, cluster_notes_within_buckets

    emb, ids = _blobs(n_per=12)
    cfg = GardenerConfig(min_cluster_size=5, min_notes_for_moc=10, random_state=7)
    report = ClusterReport()

    pairs = cluster_notes_within_buckets(
        {"Cat": ids}, dict(zip(ids, emb, strict=True)), cfg, report
    )

    assert pairs
    assert all(len(cluster) >= 10 for _cat, cluster in pairs)
    assert report.backends == {"umap+hdbscan"}
    assert report.clusters == len(pairs)


@pytest.mark.slow
def test_kmeans_fallback_is_reported(monkeypatch):
    import builtins

    from zettel.config import GardenerConfig
    from zettel.gardener_assign import ClusterReport, _cluster_embeddings

    real_import = builtins.__import__

    def no_umap(name, *args, **kwargs):
        if name in {"umap", "hdbscan"}:
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_umap)
    emb, ids = _blobs(n_per=10)
    report = ClusterReport()

    clusters = _cluster_embeddings(emb, ids, GardenerConfig(min_cluster_size=5), report)

    assert clusters
    assert report.backends == {"kmeans"}


def test_bucket_with_missing_embedding_keeps_ids_aligned():
    """A note without a vector must not shift the id/vector pairing."""
    from zettel.config import GardenerConfig
    from zettel.gardener_assign import cluster_notes_within_buckets

    emb, ids = _blobs(n_per=12)
    by_id = dict(zip(ids, emb, strict=True))
    cfg = GardenerConfig(min_cluster_size=5, min_notes_for_moc=10, random_state=7)

    pairs = cluster_notes_within_buckets({"Cat": ["ghost", *ids]}, by_id, cfg)

    assert all("ghost" not in cluster for _cat, cluster in pairs)


def test_graph_cohesion_is_a_weighted_density_between_zero_and_one(tmp_path):
    from zettel.state.connections import EdgePolicy

    db = StateDB(tmp_path / "test.db")
    for nid in "ABCD":
        db.upsert_note(nid, "SRC", None, nid)
    policy = EdgePolicy({"contradicts": 1.0, "related": 0.5, "heavy": 3.0})

    assert graph_cohesion(db, ["A"], policy) == 0.0
    assert graph_cohesion(db, ["A", "B", "C"], policy) == 0.0
    for a, b in (("A", "B"), ("A", "C"), ("B", "C")):
        db.upsert_note_connection(a, b, "contradicts", "")
    assert graph_cohesion(db, ["A", "B", "C"], policy) == 1.0
    db.upsert_note_connection("C", "D", "heavy", "")  # capped at 1 per pair
    assert graph_cohesion(db, ["A", "B", "C", "D"], policy) == pytest.approx(4 / 6)
    db.close()


def test_graph_cohesion_counts_proposed_edges_at_the_factor(tmp_path):
    from zettel.state.connections import EdgePolicy

    db = StateDB(tmp_path / "test.db")
    for nid in "AB":
        db.upsert_note(nid, "SRC", None, nid)
    db.upsert_note_connection("A", "B", "contradicts", "", status="proposed")

    assert graph_cohesion(db, ["A", "B"], EdgePolicy({"contradicts": 1.0})) == 0.0
    assert graph_cohesion(db, ["A", "B"], EdgePolicy({"contradicts": 1.0}, 0.5)) == 0.5
    db.close()


# ── Category label modes and threshold (#295) ────────────────────────


class _FakeIdx(FakeVectorIndex):
    """Embeds a text as the axis of the first known word it contains."""

    AXES = ("prompt", "linguagem", "kant", "agentes")

    def embed_texts(self, texts, *, label="embed_texts"):
        self._record("embed_texts", list(texts))
        out = []
        for text in texts:
            low = text.lower()
            vec = [0.0] * (len(self.AXES) + 1)
            for i, word in enumerate(self.AXES):
                if word in low:
                    vec[i] = 1.0
            vec[-1] = 0.0 if any(vec) else 1.0  # anything else lies on its own axis
            out.append(vec)
        return out


_CATS = [("IA", "Processamento de Linguagem"), ("IA", "Aplicacoes de LLMs")]
_LEAVES = {
    "Aplicacoes de LLMs": ["Engenharia de prompt", "Agentes"],
    "Processamento de Linguagem": [],
}


def test_label_texts_per_mode():
    from zettel.gardener_assign import category_label_texts

    def texts(mode):
        return category_label_texts(_CATS, "D", "{pilar}: {categoria}", mode=mode, leaves=_LEAVES)

    assert texts("label")["Aplicacoes de LLMs"] == ["IA: Aplicacoes de LLMs"]
    assert texts("leaves_in_label")["Aplicacoes de LLMs"] == [
        "IA: Aplicacoes de LLMs — Engenharia de prompt, Agentes"
    ]
    assert texts("max_leaf")["Aplicacoes de LLMs"] == [
        "IA: Aplicacoes de LLMs — Engenharia de prompt",
        "IA: Aplicacoes de LLMs — Agentes",
    ]
    # A category with no leaves keeps its plain label in every mode.
    assert texts("max_leaf")["Processamento de Linguagem"] == ["IA: Processamento de Linguagem"]


def test_max_leaf_sends_a_note_to_the_category_of_its_leaf():
    from zettel.gardener_assign import embed_category_labels

    note = {"n1": np.array([1.0, 0.0, 0.0, 0.0, 0.0])}  # a note about prompts
    idx = _FakeIdx()

    label = embed_category_labels(idx, _CATS, "D", "{pilar}: {categoria}", mode="label")
    leaf = embed_category_labels(
        idx, _CATS, "D", "{pilar}: {categoria}", mode="max_leaf", leaves=_LEAVES
    )

    assert leaf["Aplicacoes de LLMs"].shape == (2, 5)
    assert "n1" not in assign_notes_to_categories(["n1"], note, label)["Aplicacoes de LLMs"]
    assert assign_notes_to_categories(["n1"], note, leaf)["Aplicacoes de LLMs"] == ["n1"]


def test_note_below_the_threshold_goes_to_the_free_bucket():
    cats = {"CatA": np.array([1.0, 0.0]), "CatB": np.array([0.0, 1.0])}
    vectors = {"near": np.array([1.0, 0.05]), "far": np.array([-1.0, -1.0])}

    forced = assign_notes_to_categories(["near", "far"], vectors, cats)
    gated = assign_notes_to_categories(["near", "far"], vectors, cats, min_similarity=0.3)

    assert "_unassigned" not in forced
    assert gated["CatA"] == ["near"]
    assert gated["_unassigned"] == ["far"]


def test_unassigned_bucket_is_clustered_instead_of_dropped():
    from zettel.config import GardenerConfig
    from zettel.gardener_assign import cluster_notes_within_buckets

    vectors = {f"u{i}": np.array([1.0, 0.0]) for i in range(4)}
    cfg = GardenerConfig(min_cluster_size=5, min_notes_for_moc=3)

    pairs = cluster_notes_within_buckets({"_unassigned": list(vectors)}, vectors, cfg)

    assert pairs == [("_unassigned", list(vectors))]


def test_assignment_defaults_stay_off_until_the_labels():
    from zettel.config import GardenerConfig, load_config

    for gcfg in (GardenerConfig(), load_config().gardener):
        assert gcfg.category_label_mode == "label"
        assert gcfg.assign_min_similarity is None


# ── Persisted bucket models (#300) ────────────────────────────────────


def _two_groups(seed: int = 0):
    """Two well separated groups of 12 notes in 8 dimensions."""
    rng = np.random.default_rng(seed)
    centres = np.eye(8)[:2] * 5
    vectors = {}
    for g, centre in enumerate(centres):
        for i in range(12):
            vectors[f"g{g}n{i}"] = centre + rng.normal(scale=0.05, size=8)
    return vectors


def _store(tmp_path, **kwargs):
    from zettel.gardener_assign import BucketModelStore

    return BucketModelStore(tmp_path / "garden", "fp", **kwargs)


def _cfg():
    from zettel.config import GardenerConfig

    return GardenerConfig(min_cluster_size=5, min_notes_for_moc=5, random_state=42)


@pytest.mark.slow
def test_stored_model_gives_the_same_partition_without_refitting(tmp_path, monkeypatch):
    import zettel.gardener_assign as assign

    vectors = _two_groups()
    ids = list(vectors)
    first = assign.bucket_clusters("Cat", ids, vectors, _cfg(), store=_store(tmp_path))

    def no_fit(*args, **kwargs):
        raise AssertionError("a stored model must not be fitted again")

    monkeypatch.setattr(assign, "fit_bucket", no_fit)
    report = assign.ClusterReport()
    second = assign.bucket_clusters("Cat", ids, vectors, _cfg(), report, _store(tmp_path))

    assert sorted(map(sorted, first)) == sorted(map(sorted, second))
    assert (report.model_buckets, report.predicted_notes) == (1, 0)


@pytest.mark.slow
def test_new_note_joins_its_existing_cluster_without_refitting(tmp_path, monkeypatch):
    import zettel.gardener_assign as assign

    vectors = _two_groups()
    assign.bucket_clusters("Cat", list(vectors), vectors, _cfg(), store=_store(tmp_path))
    monkeypatch.setattr(assign, "fit_bucket", lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    vectors["new"] = vectors["g1n0"] + 0.01

    report = assign.ClusterReport()
    clusters = assign.bucket_clusters(
        "Cat", list(vectors), vectors, _cfg(), report, _store(tmp_path)
    )

    [home] = [c for c in clusters if "new" in c]
    assert {"g1n0", "g1n5"} <= set(home)
    assert report.predicted_notes == 1


@pytest.mark.slow
def test_refit_and_a_changed_fingerprint_fit_again(tmp_path):
    from zettel.gardener_assign import BucketModelStore, ClusterReport, bucket_clusters

    vectors = _two_groups()
    bucket_clusters("Cat", list(vectors), vectors, _cfg(), store=_store(tmp_path))

    for store in (
        _store(tmp_path, refit=True),
        BucketModelStore(tmp_path / "garden", "other-fingerprint"),
    ):
        report = ClusterReport()
        bucket_clusters("Cat", list(vectors), vectors, _cfg(), report, store)
        assert (report.refit_buckets, report.model_buckets) == (1, 0)


def test_readonly_store_never_writes(tmp_path):
    from zettel.gardener_assign import bucket_clusters

    vectors = _two_groups()
    bucket_clusters("Cat", list(vectors), vectors, _cfg(), store=_store(tmp_path, readonly=True))

    assert not (tmp_path / "garden").exists()
