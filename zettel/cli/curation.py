"""Phase 2 commands: turning chunks into literature notes, and the review gate.

* ``extract``      — Prompt 1 over every ``pending`` chunk, writing LIT drafts;
* ``review``       — the human approval gate that promotes drafts (and their
                     concepts) to ``approved``, which is what ``connect`` reads;
* ``retry-failed`` — put failed (or extract-rejected) chunks back in the queue.

``extract`` deliberately does not auto-approve by default: ADR-016/ADR-017 place a
human between the LLM's reading of a chunk and its permanent note, and the
confidence thresholds that would bypass that human are tunable heuristics, not
calibrated numbers.
"""

from __future__ import annotations

from typing import Annotated

import typer

from zettel.cli.app import app, console
from zettel.cli.deps import exit_llm_unavailable, get_db, get_idx, load_deps, preflight_gate
from zettel.cli.options import ConfigOption, SourceFilterOption, YesOption


@app.command()
def extract(
    config: ConfigOption = None,
    yes: YesOption = False,
    auto_approve: Annotated[
        bool,
        typer.Option(
            "--auto-approve",
            help="Aprovar automaticamente drafts com confianca >= limiar (literature_review)",
        ),
    ] = False,
):
    """Processar chunks pendentes com LLM (Prompt 1), gerar drafts de LIT granular."""
    cfg = load_deps(config)
    db = get_db(cfg)
    idx = get_idx(cfg, db=db, yes=yes)

    from zettel.preflight import estimate_extract

    preflight_gate(estimate_extract(cfg, db), yes, db)

    if auto_approve:
        from zettel.review import AUTO_APPROVE_UNVALIDATED_WARNING

        console.print(f"[yellow]{AUTO_APPROVE_UNVALIDATED_WARNING}[/yellow]")

    from zettel.extractor import run_extract
    from zettel.llm import LLMUnavailableError

    # Nao usar console.status: o Progress interno de run_extract disputa o mesmo
    # stdout (dois Rich Live) e a barra Extract chunk i/N pisca. Ver #21.
    try:
        candidates = run_extract(cfg, db, idx, auto_approve=auto_approve)
    except LLMUnavailableError as exc:
        exit_llm_unavailable(exc, db)

    console.print(
        f"[green]Candidatos em awaiting_review: {len(candidates)}[/green] "
        "(use `zettel review` antes do connect)"
    )

    db.close()


@app.command()
def review(
    config: ConfigOption = None,
    source_id: SourceFilterOption = None,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            "-y",
            help="Nao-interativo: aprova todos com confianca >= limiar",
        ),
    ] = False,
    auto_approve: Annotated[
        bool,
        typer.Option(
            "--auto-approve",
            help="Aprovar automaticamente drafts com confianca >= limiar",
        ),
    ] = False,
    low_confidence_only: Annotated[
        bool,
        typer.Option(
            "--low-confidence-only",
            help="Listar apenas drafts abaixo do limiar",
        ),
    ] = False,
):
    """Aprovar/rejeitar Notas de Literatura granulares antes do connect."""
    cfg = load_deps(config)
    db = get_db(cfg)
    idx = get_idx(cfg, db=db, yes=yes)

    from zettel.review import AUTO_APPROVE_UNVALIDATED_WARNING, review_followups, run_review

    # Either flag means "decide without me", so the interactive report is skipped.
    interactive = not (yes or auto_approve)
    if not interactive:
        console.print(f"[yellow]{AUTO_APPROVE_UNVALIDATED_WARNING}[/yellow]")
    stats = run_review(
        cfg,
        db,
        idx,
        source_id=source_id,
        auto_approve=auto_approve or yes,
        interactive=interactive,
        low_confidence_only=low_confidence_only,
    )
    console.print(
        f"[green]Aprovados: {stats['approved']}[/green] | "
        f"[red]Rejeitados: {stats['rejected']}[/red] | "
        f"[yellow]Pulados: {stats['skipped']}[/yellow]"
    )
    for line in review_followups(stats):
        console.print(f"[yellow]{line}[/yellow]")
    db.close()


@app.command(name="retry-failed")
def retry_failed(
    config: ConfigOption = None,
    source_id: Annotated[
        str | None,
        typer.Option(
            "--source-id",
            help="Filtrar por source_id",
        ),
    ] = None,
    assets: Annotated[
        bool,
        typer.Option(
            "--assets",
            help="Resetar imagens com falha de descricao",
        ),
    ] = False,
    rejected: Annotated[
        bool,
        typer.Option(
            "--rejected",
            help="Resetar chunks rejected pelo extract (exige --source-id; apaga o cache LLM)",
        ),
    ] = False,
):
    """Resetar chunks (ou imagens) com falha para 'pending', permitindo reprocessar."""
    if rejected and assets:
        console.print("[red]--rejected e --assets sao mutuamente exclusivos.[/red]")
        raise typer.Exit(1)
    if rejected and not source_id:
        console.print(
            "[red]--rejected exige --source-id: rejeicao e o estado terminal do "
            "extract (sumario, codigo, irrelevante), nao uma falha. Sem filtro "
            "reescreveria o vault inteiro.[/red]"
        )
        raise typer.Exit(1)

    cfg = load_deps(config)
    db = get_db(cfg)

    if assets:
        n = db.reset_failed_assets()
        if n:
            console.print(
                f"[green]{n} imagem(ns) resetada(s) para 'pending'. "
                f"Execute 'extract' para redescreve-las.[/green]"
            )
        else:
            console.print("[yellow]Nenhuma imagem com falha encontrada.[/yellow]")
        db.close()
        return

    status = "rejected" if rejected else "failed"
    count = db.reset_chunks_to_pending(
        status,
        source_id=source_id,
        drop_llm_cache=rejected,
    )

    if count == 0:
        label = "rejeitado" if rejected else "com falha"
        console.print(f"[yellow]Nenhum chunk {label} encontrado.[/yellow]")
        db.close()
        return

    extra = " Cache LLM desta fonte invalidado." if rejected else ""
    console.print(
        f"[green]{count} chunk(s) resetado(s) para 'pending'.{extra} "
        f"Execute 'extract' para reprocessar.[/green]"
    )
    db.close()


@app.command()
def summarize(
    config: ConfigOption = None,
    source_id: SourceFilterOption = None,
    yes: YesOption = False,
):
    """Resumir capitulos (texto real) e reduzir num resumo geral por fonte."""
    cfg = load_deps(config)
    db = get_db(cfg)
    idx = get_idx(cfg, db=db, yes=yes)

    from zettel.preflight import estimate_summarize

    preflight_gate(estimate_summarize(cfg, db, source_id), yes, db)

    from zettel.summarize import generate_summaries

    with console.status("Resumindo capitulos..."):
        outcome = generate_summaries(cfg, db, idx, source_id)

    console.print(
        f"[green]Capitulos resumidos: {outcome.chapters_summarized}[/green] "
        f"(inalterados: {outcome.chapters_skipped}) | "
        f"resumos gerais: {outcome.sources_summarized} | "
        f"chamadas LLM: {outcome.llm_calls}, cache: {outcome.cache_hits}"
    )
    for msg in outcome.skipped:
        console.print(f"[yellow]{msg}[/yellow]")

    db.close()
