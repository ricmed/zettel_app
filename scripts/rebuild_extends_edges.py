"""List ``extends`` edges the dedupe LLM created, without calling the model.

The live re-ask waits until ``evals/preregistration/273-modelo-dedupe.md`` has
been scored. This script's default is a dry run: it counts the edges and
writes nothing.

    .venv/Scripts/python.exe scripts/rebuild_extends_edges.py
    .venv/Scripts/python.exe scripts/rebuild_extends_edges.py --state-db data/state.db
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def llm_extends_pairs(db_path: Path) -> list[tuple[str, str]]:
    """``(source_note_id, target_note_id)`` for ``extends`` edges the LLM wrote."""
    if not db_path.exists():
        return []
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            """SELECT source_note_id, target_note_id
                 FROM note_connections
                WHERE relation_type='extends' AND origin='llm'
                ORDER BY source_note_id, target_note_id"""
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()
    return [(row[0], row[1]) for row in rows]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--state-db", type=Path, default=Path("data/state.db"))
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Recusa: a reexecucao espera o resultado da regra #273",
    )
    args = parser.parse_args(argv)
    if args.yes:
        print(
            "A reexecucao das arestas espera evals/results/dedupe-model-273-r2-*.json "
            "e a regra em evals/preregistration/273-modelo-dedupe.md. Nada foi chamado."
        )
        return 2
    pairs = llm_extends_pairs(args.state_db)
    print(f"extends origin=llm: {len(pairs)}")
    for source, target in pairs:
        print(f"  {source} -> {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
