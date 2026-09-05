"""``run-all``: the five phases back to back in one process.

This is a convenience wrapper, not a separate implementation — it calls the same
``run_*`` functions the individual commands call, in the documented order:

    harvest -> extract -> review -> connect -> garden

Two things differ from running the commands by hand, and both are deliberate:

* **One process, one set of handles.** ``StateDB`` and ``VectorIndex`` are opened
  once instead of five times, so an embedding-drift prompt happens at most once.
* **Review defers to the flags.** With ``--yes`` (or any non-interactive mode) the
  gate auto-approves everything at or above the confidence threshold; the
  below-threshold drafts stay ``awaiting_review`` for a later ``zettel review``.
  A non-interactive full run never silently discards a draft.

``--dry-run`` stops after review — before anything is written to the vault as a
permanent note — and still prints the cost table, which is the point: it answers
"what would this batch cost me" for the expensive half of the pipeline.
"""

from __future__ import annotations

from typing import Annotated

import typer

from zettel.cli.app import app, console
from zettel.cli.deps import get_db, get_idx, load_deps
from zettel.cli.formatting import print_cost_by_phase
from zettel.cli.options import (
    ConfigOption,
    ForceDuplicatesOption,
    NonInteractiveYesOption,
    SkipBiblioOption,
    SkipDuplicatesOption,
    resolve_duplicate_flags,
)


@app.command(name="run-all")
def run_all(
    config: ConfigOption = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Simular sem escrever",
        ),
    ] = False,
    yes: NonInteractiveYesOption = False,
    skip_duplicates: SkipDuplicatesOption = False,
    force: ForceDuplicatesOption = False,
    skip_biblio: SkipBiblioOption = False,
):
    """Executar pipeline completo: harvest > extract > review > connect > garden."""
    cfg = load_deps(config)
    db = get_db(cfg)
    idx = get_idx(cfg, db=db, yes=yes)

    interactive, duplicate_action = resolve_duplicate_flags(yes, skip_duplicates, force)

    # Phase 1: Harvest
    console.rule("[bold blue]Fase 1 — Harvest")
    from zettel.harvester import run_harvest

    harvest_outcome = run_harvest(
        cfg,
        db,
        idx,
        interactive=interactive,
        duplicate_action=duplicate_action,
        skip_biblio=skip_biblio,
        skip_paging=False,
    )
    console.print(f"  Fontes: {len(harvest_outcome.source_ids)}")
    for skip in harvest_outcome.skipped:
        console.print(f"  [red]Ignorado: {skip.path.name} ({skip.reason})[/red] {skip.message}")
    last_run = db.get_last_run()
    if last_run:
        dup_total = (
            last_run.get("duplicate_file_count", 0)
            + last_run.get("duplicate_content_count", 0)
            + last_run.get("duplicate_semantic_count", 0)
        )
        if dup_total:
            console.print(f"  [yellow]Duplicatas detectadas: {dup_total}[/yellow]")

    # Phase 2: Extract
    console.rule("[bold blue]Fase 2 — Extract")
    from zettel.extractor import run_extract

    candidates = run_extract(cfg, db, idx, auto_approve=False)
    console.print(f"  Drafts / candidatos: {len(candidates)}")

    # Phase 2b: Review
    console.rule("[bold blue]Fase 2b — Review")
    from zettel.review import run_review

    rev = run_review(
        cfg,
        db,
        idx,
        auto_approve=yes or not interactive,
        interactive=interactive and not yes,
    )
    console.print(
        f"  Aprovados: {rev['approved']} | Rejeitados: {rev['rejected']} | "
        f"Pulados: {rev['skipped']}"
    )

    if dry_run:
        console.print("[yellow]Dry run — parando antes da geracao de notas.[/yellow]")
        print_cost_by_phase(db, title="Custo por fase desta execucao")
        db.close()
        return

    # Phase 3: Connect (from DB approved concepts)
    console.rule("[bold blue]Fase 3 — Connect")
    from zettel.connector import load_approved_candidates, run_connect

    connect_cands = load_approved_candidates(db)
    note_ids = run_connect(cfg, db, idx, connect_cands)
    console.print(f"  Notas permanentes: {len(note_ids)}")

    # Phase 4: Garden
    console.rule("[bold blue]Fase 4 — Garden")
    from zettel.gardener import run_garden

    moc_ids = run_garden(cfg, db, idx)
    console.print(f"  MOCs: {len(moc_ids)}")

    console.rule("[bold green]Pipeline completo!")
    print_cost_by_phase(db, title="Custo por fase desta execucao")
    db.close()
