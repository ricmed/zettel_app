"""Probe harness: how proposed LLM edges should weigh in the garden (#299).

Since #279 the connect phase writes LLM edges as ``proposed``; the reader walks
only accepted, derived and manual edges. The garden weighs proposed edges at
``gardener.proposed_edge_factor`` (``EdgePolicy``) in the hub degree, the hub
BFS, the cohesion and the entry selection. This script measures, on a copy of
the development graph, two regimes:

  * ``revisado``     — every LLM edge accepted (the queue fully reviewed);
  * ``nao_revisado`` — every LLM edge still proposed (right after connect).

For each factor it reports the hubs accepted, the notes their neighbourhoods
cover, the cohesion of the existing MOC subjects and the reachability within
1 and 2 hops of the doors (hub entries and MOC entries), over the graph the
reader actually walks.

It runs the real code (``gardener_hub._hub_neighbourhoods``, ``hub_entry_notes``,
``select_entry_notes``, ``graph_cohesion``) on a temporary ``StateDB`` filled
from the source database, which is opened read-only. Cost: **no LLM and no
embedding calls.**

Usage:
    .venv/Scripts/python.exe scripts/probe_hub_policy.py
    .venv/Scripts/python.exe scripts/probe_hub_policy.py --factors 0 0.5 1 --json out.json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from zettel.config import load_config
from zettel.garden_entries import select_entry_notes
from zettel.garden_report import adjacency, reach_shares
from zettel.gardener_assign import NOTE_ID_RE, garden_edge_policy, graph_cohesion
from zettel.gardener_hub import _hub_neighbourhoods, _HubGardenStats, hub_entry_notes
from zettel.state import StateDB
from zettel.state.connections import edge_is_traversable

REGIMES = ("revisado", "nao_revisado")
_PERMANENT = "instr(path, '30_Permanent') > 0"


def load_source(db_path: Path) -> tuple[list[dict], list[dict], dict[str, list[str]]]:
    """Permanent notes, edges and MOC subjects of the source database (read-only)."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    notes = [
        dict(r)
        for r in conn.execute(
            f"SELECT note_id, source_id, path, title FROM notes WHERE {_PERMANENT}"
        )
    ]
    edges = [dict(r) for r in conn.execute("SELECT * FROM note_connections")]
    known = {n["note_id"] for n in notes}
    mocs = {
        r["topic"]: sorted(set(NOTE_ID_RE.findall(r["body"] or "")) & known)
        for r in conn.execute("SELECT topic, body FROM mocs WHERE origin='pipeline'")
    }
    conn.close()
    return notes, edges, mocs


def build_regime(path: Path, notes: list[dict], edges: list[dict], regime: str) -> StateDB:
    """A fresh StateDB with the notes and the edges, LLM edges set to the regime's status."""
    db = StateDB(path)
    for n in notes:
        db.upsert_note(n["note_id"], n["source_id"], n["path"], n["title"] or "")
    llm_status = "accepted" if regime == "revisado" else "proposed"
    for e in edges:
        origin = e.get("origin") or "llm"
        db.upsert_note_connection(
            e["source_note_id"],
            e["target_note_id"],
            e["relation_type"],
            e.get("description") or "",
            origin=origin,
            status=llm_status if origin == "llm" else "accepted",
        )
    return db


def measure(db: StateDB, notes: list[dict], mocs: dict[str, list[str]], factor: float) -> dict:
    cfg = load_config()
    cfg.gardener.proposed_edge_factor = factor
    policy = garden_edge_policy(cfg)
    note_ids = [n["note_id"] for n in notes]
    adj = adjacency([e for e in db.list_note_connections() if edge_is_traversable(e)])

    stats = _HubGardenStats()
    _ranked, neighbourhoods = _hub_neighbourhoods(cfg, db, stats)
    hub_entries: set[str] = set()
    covered: set[str] = set()
    for hub_id, members in neighbourhoods:
        covered.update(members)
        hub_entries.update(hub_entry_notes(db, hub_id, members, cfg))

    moc_entries: set[str] = set()
    cohesion: list[float] = []
    for members in mocs.values():
        moc_entries.update(select_entry_notes(db, members, cfg.gardener, policy=policy))
        cohesion.append(graph_cohesion(db, members, policy))

    return {
        "factor": factor,
        "hubs": len(neighbourhoods),
        "hubs_small": stats.skipped_small,
        "hubs_dedup": stats.skipped_dedup,
        "hub_notes_covered": len(covered),
        "hub_reach": reach_shares(hub_entries, adj, note_ids),
        "moc_entries": len(moc_entries),
        "moc_reach": reach_shares(moc_entries, adj, note_ids),
        "doors_reach": reach_shares(hub_entries | moc_entries, adj, note_ids),
        "moc_cohesion": {
            "min": round(min(cohesion), 4) if cohesion else 0.0,
            "median": round(statistics.median(cohesion), 4) if cohesion else 0.0,
            "max": round(max(cohesion), 4) if cohesion else 0.0,
        },
    }


def run(db_path: Path, factors: list[float]) -> dict:
    notes, edges, mocs = load_source(db_path)
    result: dict = {
        "notes": len(notes),
        "edges": len(edges),
        "llm_edges": sum(1 for e in edges if (e.get("origin") or "llm") == "llm"),
        "moc_subjects": len(mocs),
        "regimes": {},
    }
    with tempfile.TemporaryDirectory() as tmp:
        for regime in REGIMES:
            db = build_regime(Path(tmp) / f"{regime}.db", notes, edges, regime)
            try:
                result["regimes"][regime] = [measure(db, notes, mocs, f) for f in factors]
            finally:
                db.close()
    return result


def _print(result: dict) -> None:
    print(
        f"{result['notes']} notas, {result['edges']} arestas ({result['llm_edges']} de LLM), "
        f"{result['moc_subjects']} MOCs como assuntos"
    )
    for regime, rows in result["regimes"].items():
        print(f"\n[{regime}]")
        print("fator  hubs  cobertas  R2-hubs  R2-MOCs  R2-portas  coesao(mediana)")
        for r in rows:
            print(
                f"{r['factor']:<6} {r['hubs']:<5} {r['hub_notes_covered']:<9} "
                f"{r['hub_reach']['r2']:<8.1%} {r['moc_reach']['r2']:<8.1%} "
                f"{r['doors_reach']['r2']:<10.1%} {r['moc_cohesion']['median']:.3f}"
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--db", type=Path, default=Path("data/state.db"))
    parser.add_argument("--factors", type=float, nargs="+", default=[0.0, 0.25, 0.5, 1.0])
    parser.add_argument("--json", type=Path, help="write the result as JSON")
    args = parser.parse_args()

    result = run(args.db, args.factors)
    _print(result)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
