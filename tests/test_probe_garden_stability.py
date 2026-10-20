"""Tests for scripts/probe_garden_stability.py (#294)."""

import sys
from pathlib import Path

import numpy as np
from zettel.config import GardenerConfig

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import probe_garden_stability as probe


def _blobs() -> tuple[np.ndarray, list[str]]:
    rng = np.random.default_rng(0)
    centers = np.eye(16)[:3] * 4.0
    points = np.vstack([c + rng.normal(scale=0.6, size=(20, 16)) for c in centers])
    return points, [f"n{i}" for i in range(len(points))]


def test_measure_stability_with_seed_is_perfect():
    emb, ids = _blobs()
    cfg = GardenerConfig(min_cluster_size=5, min_notes_for_moc=5, random_state=3)

    result = probe.measure_stability("teste", emb, ids, cfg, runs=3)

    assert result.notes == 60
    assert result.mean_ari == 1.0
    assert result.identical_cluster_share == 1.0
    assert len(set(result.clusters_per_run)) == 1


def test_labels_marks_noise_as_minus_one():
    assert probe._labels([["a", "b"]], ["a", "b", "c"]) == [0, 0, -1]
