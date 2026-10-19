"""Recalculate derived ``corroborates`` edges with the measured cut.

The similarity is a live query of ``permanent_notes``: thesis + definition of
the note, on the query side, through ``Retriever.search_notes`` (``linking.topk``,
graph expansion off). That is the same hit list ``corroborating_note_ids``
reads during connect. ``decision_shadow`` is not the source. Those rows are on
the old ``(1+cos)/2`` scale, rounded to 4 decimals, and only exist for pairs
that were seeds in the 2026-10-06/07 shadow run, so they can neither drop an
edge the current top-k no longer returns nor create one the shadow never saw.

One query embedding per permanent note that has a thesis or a definition.
No LLM call. The default prints the diff and writes nothing.

    .venv/Scripts/python.exe scripts/rebuild_corroborates.py
    .venv/Scripts/python.exe scripts/rebuild_corroborates.py --apply
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from zettel.config import AppConfig, load_config
from zettel.connector.links import (
    CorroborationPlan,
    CorroborationSearch,
    apply_corroboration_plan,
    build_corroboration_plan,
)
from zettel.state import StateDB


def live_search(cfg: AppConfig, db: StateDB) -> CorroborationSearch:
    """Hits connect would hand the cut: seeds that cleared the relevance floor."""
    from zettel.index import VectorIndex, index_kwargs
    from zettel.retrieval import Retriever

    retriever = Retriever(cfg, db, VectorIndex(**index_kwargs(cfg)))
    topk = cfg.linking.topk

    def search(note_id: str, query: str):
        return retriever.search_notes(query, topk=topk, exclude_id=note_id, expand_graph=False).hits

    return search


def render_plan(plan: CorroborationPlan, *, apply: bool) -> str:
    """Portuguese report. Avoids characters the Windows console cannot encode."""
    lines = [
        "modo: apply" if apply else "modo: dry-run (nada foi escrito)",
        f"criar: {len(plan.create)}",
        f"remover: {len(plan.remove)}",
        f"manter: {len(plan.keep)}",
        f"sem tese nem definicao (arestas mantidas): {len(plan.unresolved_note_ids)}",
        f"arestas fora do recalculo: {plan.untouched_edges}",
        "por par de fontes:",
    ]
    pairs = plan.source_pair_counts()
    if not pairs:
        lines.append("  (nenhum)")
    for left, right, created, removed, kept in pairs:
        lines.append(f"  {left} x {right}  criar={created} remover={removed} manter={kept}")
    for label, changes in (("criar", plan.create), ("remover", plan.remove)):
        for change in changes:
            similarity = "" if change.similarity is None else f"  sim={change.similarity:.4f}"
            lines.append(f"{label}: {change.source_note_id} -> {change.target_note_id}{similarity}")
    return "\n".join(lines)


def execute(
    cfg: AppConfig,
    db: StateDB,
    *,
    apply: bool,
    search: CorroborationSearch | None = None,
) -> int:
    """Print the plan. Write it only when ``apply`` is true."""
    plan = build_corroboration_plan(cfg, db, search if search is not None else live_search(cfg, db))
    print(render_plan(plan, apply=apply))
    if apply:
        apply_corroboration_plan(cfg, db, plan)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--state-db", type=Path, default=Path("data/state.db"))
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Grava o diff. Sem esta flag o comando so lista.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Lista o diff sem gravar. E o comportamento padrao.",
    )
    args = parser.parse_args(argv)
    if args.apply and args.dry_run:
        print("Use so um de --apply e --dry-run.", file=sys.stderr)
        return 2
    cfg = load_config(args.config)
    db = StateDB(args.state_db)
    try:
        return execute(cfg, db, apply=args.apply)
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
