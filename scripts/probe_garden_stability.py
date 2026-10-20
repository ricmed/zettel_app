"""Probe harness: is the garden's clustering reproducible? (#294, ADR-019)

The garden routes a cluster by its signature (SHA-256 of the sorted note ids,
ADR-021). That zero-cost branch only works if the same notes yield the same
partition on every run. This script re-runs the garden's own
``gardener_assign._cluster_embeddings`` N times on the same vectors and reports,
per scope:

  * clusters per run and the share of notes left as noise;
  * mean pairwise **ARI** between runs (1 = identical partitions; noise counts as
    its own label);
  * mean pairwise share of **identical clusters** (same exact note set), which is
    what the signature needs.

Scopes are the whole vault (the global path) and, as a proxy for the category
buckets, the note set of each existing MOC. The real buckets need the category
labels embedded, which this script deliberately does not do.

Cost: **no LLM and no embedding calls.** Vectors are read from Chroma, MOC bodies
from SQLite (opened read-only).

Usage:
    .venv/Scripts/python.exe scripts/probe_garden_stability.py
    .venv/Scripts/python.exe scripts/probe_garden_stability.py --runs 10
    .venv/Scripts/python.exe scripts/probe_garden_stability.py --random-state none
    .venv/Scripts/python.exe scripts/probe_garden_stability.py --json out.json
"""

from __future__ import annotations

import argparse
import itertools
import json
import sqlite3
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from zettel.config import GardenerConfig, load_config
from zettel.gardener_assign import NOTE_ID_RE, _cluster_embeddings


@dataclass
class StabilityResult:
    scope: str
    notes: int
    clusters_per_run: list[int]
    noise_share_per_run: list[float]
    mean_ari: float
    identical_cluster_share: float


def _labels(clusters: list[list[str]], ids: list[str]) -> list[int]:
    label = dict.fromkeys(ids, -1)
    for k, members in enumerate(clusters):
        for nid in members:
            label[nid] = k
    return [label[nid] for nid in ids]


def measure_stability(
    scope: str, embeddings: np.ndarray, ids: list[str], cfg: GardenerConfig, runs: int
) -> StabilityResult:
    """Cluster the same vectors ``runs`` times and compare the partitions."""
    from sklearn.metrics import adjusted_rand_score

    outputs = [_cluster_embeddings(embeddings, list(ids), cfg) for _ in range(runs)]
    labels = [_labels(out, ids) for out in outputs]
    sets = [{frozenset(c) for c in out} for out in outputs]
    pairs = list(itertools.combinations(range(runs), 2))
    aris = [adjusted_rand_score(labels[a], labels[b]) for a, b in pairs]
    identical = [len(sets[a] & sets[b]) / max(1, len(sets[a])) for a, b in pairs]
    return StabilityResult(
        scope=scope,
        notes=len(ids),
        clusters_per_run=[len(out) for out in outputs],
        noise_share_per_run=[
            round(1 - sum(len(c) for c in out) / max(1, len(ids)), 3) for out in outputs
        ],
        mean_ari=round(float(np.mean(aris)) if aris else 1.0, 3),
        identical_cluster_share=round(float(np.mean(identical)) if identical else 1.0, 3),
    )


def _load_vectors(chroma_path: Path) -> tuple[list[str], np.ndarray]:
    import chromadb

    client = chromadb.PersistentClient(path=str(chroma_path))
    got = client.get_collection("permanent_notes").get(include=["embeddings"])
    vectors = np.asarray(got["embeddings"], dtype=float)
    return list(got["ids"]), vectors


def _moc_scopes(db_path: Path) -> dict[str, list[str]]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT topic, body FROM mocs").fetchall()
    finally:
        conn.close()
    return {topic: sorted(set(NOTE_ID_RE.findall(body or ""))) for topic, body in rows}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", default=None)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument(
        "--random-state",
        default=None,
        help="Override gardener.random_state (an int, or 'none' for no seed)",
    )
    parser.add_argument(
        "--chroma-path", type=Path, default=None, help="Read vectors from a copy of data/chroma"
    )
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    gcfg = cfg.gardener.model_copy()
    if args.random_state is not None:
        gcfg.random_state = None if args.random_state.lower() == "none" else int(args.random_state)

    ids, vectors = _load_vectors(args.chroma_path or cfg.chroma_path)
    if len(ids) < gcfg.min_cluster_size:
        print(f"Poucas notas para medir ({len(ids)}).")
        return 1
    pos = {nid: i for i, nid in enumerate(ids)}

    results = [measure_stability("acervo inteiro", vectors, ids, gcfg, args.runs)]
    for topic, members in sorted(_moc_scopes(cfg.state_db_path).items()):
        members = [nid for nid in members if nid in pos]
        if len(members) >= gcfg.min_notes_for_moc:
            sub = vectors[[pos[nid] for nid in members]]
            results.append(measure_stability(f"MOC {topic}", sub, members, gcfg, args.runs))

    print(f"random_state={gcfg.random_state}  runs={args.runs}")
    print(
        f"{'escopo':45s} {'notas':>5s} {'clusters':>14s} {'ruido':>16s} {'ARI':>6s} {'iguais':>7s}"
    )
    for r in results:
        noise = f"{min(r.noise_share_per_run):.0%}-{max(r.noise_share_per_run):.0%}"
        clusters = f"{min(r.clusters_per_run)}-{max(r.clusters_per_run)}"
        print(
            f"{r.scope[:45]:45s} {r.notes:5d} {clusters:>14s} {noise:>16s} "
            f"{r.mean_ari:6.3f} {r.identical_cluster_share:7.1%}"
        )
    if args.json:
        args.json.write_text(
            json.dumps([asdict(r) for r in results], ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
