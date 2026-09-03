"""Phase 2 commands: turning chunks into literature notes, and the review gate.

* ``extract``      — Prompt 1 over every ``pending`` chunk, writing LIT drafts;
* ``review``       — the human approval gate that promotes drafts (and their
                     concepts) to ``approved``, which is what ``connect`` reads;
* ``retry-failed`` — put failed chunks (or image descriptions) back in the queue.

``extract`` deliberately does not auto-approve by default: ADR-016/ADR-017 place a
human between the LLM's reading of a chunk and its permanent note, and the
confidence thresholds that would bypass that human are tunable heuristics, not
calibrated numbers.
"""

from __future__ import annotations

from typing import Annotated, Optional

import typer

from zettel.cli.app import app, console
from zettel.cli.deps import get_db, get_idx, load_deps, preflight_gate
from zettel.cli.options import ConfigOption, SourceFilterOption, YesOption


@app.command()
def extract(
    config: ConfigOption = None,
    yes: YesOption = False,
    auto_approve: Annotated[bool, typer.Option(
        "--auto-approve",
        help="Aprovar automaticamente drafts com confianca >= limiar (literature_review)",
    )] = False,
):
    """Processar chunks pendentes com LLM (Prompt 1), gerar drafts de LIT granular."""
    cfg = load_deps(config)
    db = get_db(cfg)
    idx = get_idx(cfg, db=db, yes=yes)

    from zettel.preflight import estimate_extract
    preflight_gate(estimate_extract(cfg, db), yes, db)

    from zettel.extractor import run_extract
    # Nao usar console.status: o Progress interno de run_extract disputa o mesmo
    # stdout (dois Rich Live) e a barra Extract chunk i/N pisca. Ver #21.
    candidates = run_extract(cfg, db, idx, auto_approve=auto_approve)

    console.print(
        f"[green]Candidatos em awaiting_review: {len(candidates)}[/green] "
        "(use `zettel review` antes do connect)"
    )

    db.close()


@app.command()
def review(
    config: ConfigOption = None,
    source_id: SourceFilterOption = None,
    yes: Annotated[bool, typer.Option(
        "--yes", "-y",
        help="Nao-interativo: aprova todos com confianca >= limiar",
    )] = False,
    auto_approve: Annotated[bool, typer.Option(
        "--auto-approve",
        help="Aprovar automaticamente drafts com confianca >= limiar",
    )] = False,
    low_confidence_only: Annotated[bool, typer.Option(
        "--low-confidence-only",
        help="Listar apenas drafts abaixo do limiar",
    )] = False,
):
    """Aprovar/rejeitar Notas de Literatura granulares antes do connect."""
    cfg = load_deps(config)
    db = get_db(cfg)
    idx = get_idx(cfg, db=db, yes=yes)

    from zettel.review import run_review
    # Either flag means "decide without me", so the interactive report is skipped.
    interactive = not (yes or auto_approve)
    stats = run_review(
        cfg, db, idx,
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
    db.close()


@app.command(name="retry-failed")
def retry_failed(
    config: ConfigOption = None,
    source_id: Annotated[Optional[str], typer.Option(
        "--source-id", help="Filtrar por source_id",
    )] = None,
    assets: Annotated[bool, typer.Option(
        "--assets", help="Resetar imagens com falha de descricao",
    )] = False,
):
    """Resetar chunks (ou imagens) com falha para 'pending', permitindo reprocessar."""
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

    failed = db.get_failed_chunks(source_id if source_id else None)
    count = len(failed)

    if count == 0:
        console.print("[yellow]Nenhum chunk com falha encontrado.[/yellow]")
        db.close()
        return

    for chunk in failed:
        db.update_chunk_status(chunk["chunk_id"], "pending")

    console.print(
        f"[green]{count} chunk(s) resetado(s) para 'pending'. "
        f"Execute 'extract' para reprocessar.[/green]"
    )
    db.close()
