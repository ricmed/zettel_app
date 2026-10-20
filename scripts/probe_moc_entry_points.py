"""Probe harness: which entry-point signal makes the best MOC doors? (#297, ADR-060)

MOCs and hubs are entry points, not complete indexes. This script compares the
entry signals of ``zettel/garden_entries.py`` (``internal_degree``, ``medoid``,
``first_in_source``, ``relevance``, ``corroboration``) at several caps, using
each existing MOC's notes as the subject, and reports **reachability**: the
share of all permanent notes within 1 and 2 hops of the selected entries, total
and per source, on two graphs:

  * ``all``      — every edge in ``note_connections``;
  * ``walkable`` — what the code walks (``edge_is_traversable``: accepted LLM
    edges plus derived and manual ones). On a database from before the
    ``status`` column, every row counts as accepted.

``--export-sheet DIR`` also writes a **blind** labelling sheet for the human
judgement pre-registered in ``evals/preregistration/297-portas-de-entrada.md``:
for each MOC, the union of every signal's top-k notes, shuffled, with title and
content, and a frozen key mapping each item to the signals that chose it.

Cost: **no LLM and no embedding calls.** SQLite is opened read-only; vectors
(for ``medoid``) come from ``--chroma-path`` (use a copy of ``data/chroma``).

Usage:
    .venv/Scripts/python.exe scripts/probe_moc_entry_points.py --chroma-path COPY
    .venv/Scripts/python.exe scripts/probe_moc_entry_points.py --caps 5 10 15 --json out.json
    .venv/Scripts/python.exe scripts/probe_moc_entry_points.py --export-sheet .eval-work/portas
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from zettel.config import GardenerConfig, load_config
from zettel.garden_entries import ENTRY_SIGNALS, select_entry_notes
from zettel.garden_report import adjacency, reach_shares
from zettel.gardener_assign import NOTE_ID_RE
from zettel.note_content import note_content
from zettel.state.connections import edge_is_traversable

_PERMANENT = "instr(path, '30_Permanent') > 0"


class ReadOnlyGraph:
    """The three StateDB reads ``select_entry_notes`` needs, on a read-only connection."""

    def __init__(self, conn: sqlite3.Connection, edges: list[dict]):
        self.conn = conn
        self.edges = edges

    def get_connections_for_notes(self, note_ids: list[str]) -> list[dict]:
        wanted = set(note_ids)
        return [
            e
            for e in self.edges
            if (e["source_note_id"] in wanted or e["target_note_id"] in wanted)
            and edge_is_traversable(e)
        ]

    def get_note(self, note_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM notes WHERE note_id=?", (note_id,)).fetchone()
        return dict(row) if row else None

    def get_concepts_for_notes(self, note_ids: list[str]) -> dict[str, dict]:
        if not note_ids:
            return {}
        marks = ",".join("?" * len(note_ids))
        rows = self.conn.execute(
            f"SELECT * FROM concepts WHERE note_id IN ({marks})", tuple(note_ids)
        ).fetchall()
        return {r["note_id"]: dict(r) for r in rows}


def _load(db_path: Path) -> tuple[sqlite3.Connection, dict[str, str], list[dict], dict[str, list]]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    notes = {
        r["note_id"]: r["source_id"] or "(sem fonte)"
        for r in conn.execute(f"SELECT note_id, source_id FROM notes WHERE {_PERMANENT}")
    }
    edges = [dict(r) for r in conn.execute("SELECT * FROM note_connections")]
    mocs = {
        r["topic"]: sorted(set(NOTE_ID_RE.findall(r["body"] or "")) & set(notes))
        for r in conn.execute("SELECT topic, body FROM mocs")
    }
    return conn, notes, edges, mocs


def _load_vectors(chroma_path: Path | None) -> dict:
    if chroma_path is None:
        return {}
    import chromadb
    import numpy as np

    got = (
        chromadb.PersistentClient(path=str(chroma_path))
        .get_collection("permanent_notes")
        .get(include=["embeddings"])
    )
    return {
        nid: np.asarray(v, dtype=float)
        for nid, v in zip(got["ids"], got["embeddings"], strict=True)
    }


def measure(
    graph: ReadOnlyGraph,
    notes: dict[str, str],
    edges: list[dict],
    mocs: dict[str, list[str]],
    vectors: dict,
    caps: list[int],
) -> list[dict]:
    """Reachability per (signal, cap, graph) with the MOC notes as subjects."""
    adj_all = adjacency(edges)
    adj_walk = adjacency([e for e in edges if edge_is_traversable(e)])
    by_source: dict[str, list[str]] = defaultdict(list)
    for nid, src in notes.items():
        by_source[src].append(nid)
    rows: list[dict] = []
    for signal in ENTRY_SIGNALS:
        for cap in caps:
            cfg = GardenerConfig(entry_selection=signal, max_entries_per_moc=cap)
            entries: set[str] = set()
            for members in mocs.values():
                entries.update(select_entry_notes(graph, members, cfg, vectors))
            row = {"signal": signal, "cap": cap, "entries": len(entries)}
            for name, adj in (("all", adj_all), ("walkable", adj_walk)):
                row[name] = reach_shares(entries, adj, notes)
                row[f"{name}_by_source"] = {
                    src: reach_shares(entries, adj, ids)["r1"]
                    for src, ids in sorted(by_source.items())
                }
            rows.append(row)
    return rows


def export_sheet(
    graph: ReadOnlyGraph,
    mocs: dict[str, list[str]],
    vectors: dict,
    cap: int,
    out_dir: Path,
    *,
    seed: int,
    force: bool,
) -> int:
    """Blind sheet + frozen key for the "is this a good door?" judgement."""
    sheet = out_dir / "portas-planilha.csv"
    key = out_dir / "portas-GABARITO-NAO-ABRIR.json"
    if not force and (sheet.exists() or key.exists()):
        raise SystemExit(f"{sheet} ou {key} ja existe; use --force para sobrescrever.")
    out_dir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)  # noqa: S311 - shuffling a sheet, not a secret
    items: list[dict] = []
    for topic, members in sorted(mocs.items()):
        chosen: dict[str, list[str]] = defaultdict(list)
        for signal in ENTRY_SIGNALS:
            cfg = GardenerConfig(entry_selection=signal, max_entries_per_moc=cap)
            for nid in select_entry_notes(graph, members, cfg, vectors):
                chosen[nid].append(signal)
        for nid, signals in chosen.items():
            note = graph.get_note(nid) or {}
            items.append(
                {
                    "subject": topic,
                    "note_id": nid,
                    "title": note.get("title") or "",
                    "content": note_content(note.get("title") or "", note.get("body") or "")[:1500],
                    "signals": sorted(signals),
                }
            )
    rng.shuffle(items)
    with sheet.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh, delimiter=";")
        writer.writerow(["item", "assunto", "titulo", "conteudo", "boa_porta (s/n)"])
        for i, item in enumerate(items, 1):
            writer.writerow([i, item["subject"], item["title"], item["content"], ""])
    key.write_text(
        json.dumps(
            [
                {"item": i, **{k: v for k, v in it.items() if k != "content"}}
                for i, it in enumerate(items, 1)
            ],
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return len(items)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", default=None)
    parser.add_argument("--caps", type=int, nargs="+", default=[5, 10, 15])
    parser.add_argument("--chroma-path", type=Path, default=None)
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument("--export-sheet", type=Path, default=None)
    parser.add_argument("--sheet-cap", type=int, default=10)
    parser.add_argument("--seed", type=int, default=297)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    conn, notes, edges, mocs = _load(cfg.state_db_path)
    graph = ReadOnlyGraph(conn, edges)
    vectors = _load_vectors(args.chroma_path)

    rows = measure(graph, notes, edges, mocs, vectors, args.caps)
    print(f"{len(notes)} notas, {len(edges)} arestas, {len(mocs)} MOCs (assuntos)")
    if not vectors:
        print("sem --chroma-path: 'medoid' cai no desempate por grau interno")
    head = ("sinal", "teto", "portas", "todas R1", "R2", "percorr. R1", "R2")
    print(f"{head[0]:16s} {head[1]:>4s} {head[2]:>6s} {head[3]:>9s} {head[4]:>6s} ", end="")
    print(f"{head[5]:>12s} {head[6]:>6s}")
    for r in rows:
        print(
            f"{r['signal']:16s} {r['cap']:4d} {r['entries']:6d} "
            f"{r['all']['r1']:9.1%} {r['all']['r2']:6.1%} "
            f"{r['walkable']['r1']:12.1%} {r['walkable']['r2']:6.1%}"
        )
    if args.json:
        args.json.write_text(
            json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    if args.export_sheet:
        n = export_sheet(
            graph,
            mocs,
            vectors,
            args.sheet_cap,
            args.export_sheet,
            seed=args.seed,
            force=args.force,
        )
        print(f"Planilha cega: {n} itens em {args.export_sheet}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
