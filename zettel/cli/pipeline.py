"""``run-all``: the whole pipeline back to back in one process.

This is a convenience wrapper, not a separate implementation — it calls the same
``run_*`` functions the individual commands call, in the documented order:

    harvest -> extract -> review -> summarize -> connect -> garden

``summarize`` sits after ``review`` and before ``connect`` because it only needs
the chapter text, and because ``connect`` refreshes the chapter map it wrote —
so the per-chapter note counts are current by the time the run ends.

Three things differ from running the commands by hand, and they are deliberate:

* **One process, one set of handles.** ``StateDB`` and ``VectorIndex`` are opened
  once instead of five times, so an embedding-drift prompt happens at most once.
* **Review does not approve by threshold unless asked.** ``--yes`` and a missing
  TTY used to auto-approve every draft at or above the confidence limiar. That
  score does not separate keep from discard, so drafts stay ``awaiting_review``
  unless ``--auto-approve`` is passed. The flag prints the unvalidated-signal
  warning. A full run never silently discards a draft.
* **Connect is scoped to the harvest of this run.** The same ``source_ids`` that
  ``run_review`` received. Leftover approved concepts from other sources wait
  for ``zettel connect``. The CLI asks the connect pre-flight (``--yes`` and a
  non-TTY pass through); the web worker does not, because it has no stdin.

``--dry-run`` stops after review — before anything is written to the vault as a
permanent note — and still prints the cost table, which is the point: it answers
"what would this batch cost me" for the expensive half of the pipeline.
"""

from __future__ import annotations

from typing import Annotated

import typer

from zettel.cli.app import app, console
from zettel.cli.deps import (
    exit_llm_unavailable,
    get_db,
    get_idx,
    load_deps,
    preflight_gate,
)
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
    auto_approve: Annotated[
        bool,
        typer.Option(
            "--auto-approve",
            help=(
                "Aprovar drafts no limiar de confianca ou acima. "
                "O sinal nao foi validado; sem esta flag os drafts ficam para revisao."
            ),
        ),
    ] = False,
):
    """Executar pipeline completo: harvest > extract > review > summarize > connect > garden."""
    cfg = load_deps(config)
    db = get_db(cfg)
    idx = get_idx(cfg, db=db, yes=yes)

    interactive, duplicate_action = resolve_duplicate_flags(yes, skip_duplicates, force)

    from zettel.llm import LLMUnavailableError

    try:
        _run_all_phases(
            cfg,
            db,
            idx,
            interactive=interactive,
            duplicate_action=duplicate_action,
            skip_biblio=skip_biblio,
            yes=yes,
            dry_run=dry_run,
            auto_approve=auto_approve,
        )
    except LLMUnavailableError as exc:
        exit_llm_unavailable(exc, db)
    db.close()


def _run_all_phases(
    cfg, db, idx, *, interactive, duplicate_action, skip_biblio, yes, dry_run, auto_approve=False
):
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
    from zettel.review import AUTO_APPROVE_UNVALIDATED_WARNING, run_review

    if auto_approve:
        console.print(f"[yellow]{AUTO_APPROVE_UNVALIDATED_WARNING}[/yellow]")
    rev = run_review(
        cfg,
        db,
        idx,
        source_ids=list(harvest_outcome.source_ids),
        auto_approve=auto_approve,
        interactive=interactive and not yes and not auto_approve,
        event_mode="run_all" if auto_approve else None,
        surface="cli",
    )
    console.print(
        f"  Aprovados: {rev['approved']} (por limiar: {rev.get('approved_threshold', 0)}) | "
        f"Rejeitados: {rev['rejected']} | Pulados: {rev['skipped']}"
    )
    from zettel.review import review_followups

    for line in review_followups(rev):
        console.print(f"  [yellow]{line}[/yellow]")

    if dry_run:
        console.print("[yellow]Dry run — parando antes da geracao de notas.[/yellow]")
        print_cost_by_phase(db, title="Custo por fase desta execucao")
        return

    # Phase 2c: Summarize (chapter + source summaries)
    console.rule("[bold blue]Fase 2c — Summarize")
    from zettel.summarize import generate_summaries

    summary_outcome = generate_summaries(cfg, db, idx)
    console.print(
        f"  Capitulos resumidos: {summary_outcome.chapters_summarized} "
        f"(inalterados: {summary_outcome.chapters_skipped}) | "
        f"resumos gerais: {summary_outcome.sources_summarized}"
    )

    # Phase 3: Connect — same harvest sources as review (#274 / #289).
    console.rule("[bold blue]Fase 3 — Connect")
    from zettel.cli.formatting import print_connect_summary
    from zettel.connector import load_approved_candidates, run_connect
    from zettel.preflight import estimate_connect

    connect_cands = load_approved_candidates(db, source_ids=list(harvest_outcome.source_ids))
    if connect_cands:
        preflight_gate(estimate_connect(cfg, db, connect_cands), yes, db)
        outcome = run_connect(cfg, db, idx, connect_cands)
        print_connect_summary(outcome)
    else:
        console.print("  Nenhum conceito aprovado das fontes deste run.")

    # Phase 4: Garden
    console.rule("[bold blue]Fase 4 — Garden")
    from zettel.gardener import run_garden

    moc_ids = run_garden(cfg, db, idx)
    console.print(f"  MOCs: {len(moc_ids)}")

    console.rule("[bold green]Pipeline completo!")
    print_cost_by_phase(db, title="Custo por fase desta execucao")
