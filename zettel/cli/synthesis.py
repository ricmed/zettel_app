"""Phases 3 and 4: writing permanent notes, then organising them into MOCs.

* ``connect`` — Prompt 2 over every approved candidate, with RAG context and
                typed relations, producing ZTL notes;
* ``reconnect`` — Prompt 2 again over notes that already exist, replacing only
                their LLM edges;
* ``garden``  — cluster the resulting notes and generate/update MOCs, either from
                the taxonomy pipeline or (``--hubs``) anchored on graph hubs.

The two are grouped because they are the synthesis half of the pipeline: unlike
Phase 1-2, which read documents, these read the vault's own graph and write into
it. They also share the pattern of overriding a config knob for a single run
(``--topk``, ``--min-cluster-size``) without persisting it to ``config.yaml``.
"""

from __future__ import annotations

from typing import Annotated

import typer

from zettel.cli.app import app, console
from zettel.cli.deps import exit_llm_unavailable, get_db, get_idx, load_deps, preflight_gate
from zettel.cli.options import ConfigOption, YesOption


@app.command()
def connect(
    config: ConfigOption = None,
    topk: Annotated[
        int | None,
        typer.Option(
            "--topk",
            help="Top-k notas similares",
        ),
    ] = None,
    yes: YesOption = False,
    requeue_rejected: Annotated[
        bool,
        typer.Option(
            "--requeue-rejected",
            help="Devolver conceitos recusados pelo connect para a fila approved",
        ),
    ] = False,
    force_rejected: Annotated[
        bool,
        typer.Option(
            "--force-rejected",
            help="Reenfileirar os recusados e pedir a nota mesmo assim",
        ),
    ] = False,
):
    """Gerar notas permanentes a partir dos candidatos aprovados no review."""
    cfg = load_deps(config)
    # Per-run overrides: never written back to config.yaml.
    if topk:
        cfg.linking.topk = topk

    db = get_db(cfg)
    idx = get_idx(cfg, db=db, yes=yes)

    from zettel.cli.formatting import print_connect_summary
    from zettel.connector import load_approved_candidates, run_connect
    from zettel.connector.run import requeue_connect_rejected

    if requeue_rejected or force_rejected:
        restored = requeue_connect_rejected(db, force=force_rejected)
        console.print(f"[green]{restored} conceito(s) devolvido(s) ao connect.[/green]")

    candidates = load_approved_candidates(db)

    if not candidates:
        console.print(
            "[red]Nenhum candidato aprovado. Execute 'extract' e 'review' primeiro.[/red]"
        )
        db.close()
        raise typer.Exit(1)

    from zettel.preflight import estimate_connect

    preflight_gate(estimate_connect(cfg, db, candidates), yes, db)

    from zettel.llm import LLMUnavailableError

    # Nao usar console.status: o Progress interno de run_connect disputa o mesmo
    # stdout (dois Rich Live) e a barra Connect nota i/N pisca. Ver #21.
    try:
        outcome = run_connect(cfg, db, idx, candidates)
    except LLMUnavailableError as exc:
        exit_llm_unavailable(exc, db)

    print_connect_summary(outcome)

    db.close()


@app.command()
def reconnect(
    config: ConfigOption = None,
    source_id: Annotated[
        str | None,
        typer.Option("--source-id", help="Limitar a uma fonte (@Citekey)"),
    ] = None,
    note_id: Annotated[
        str | None,
        typer.Option("--note-id", help="Limitar a uma nota (ULID)"),
    ] = None,
    relations_only: Annotated[
        bool,
        typer.Option(
            "--relations-only",
            help="So arestas. O corpo da nota nao e reescrito (unico modo deste comando)",
        ),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Lista arestas que sairiam e entrariam, sem gravar"),
    ] = False,
    yes: YesOption = False,
):
    """Refazer as arestas de LLM de notas permanentes ja escritas."""
    cfg = load_deps(config)
    db = get_db(cfg)

    from zettel.connector.note import ConnectSession
    from zettel.connector.reconnect import (
        candidate_from_note,
        make_session,
        render_diff,
        run_reconnect,
        select_reconnect_notes,
    )
    from zettel.preflight import estimate_reconnect

    notes = select_reconnect_notes(db, source_id=source_id, note_id=note_id)
    if not notes:
        console.print("[red]Nenhuma nota permanente do pipeline para religar.[/red]")
        db.close()
        raise typer.Exit(1)
    linkable = [note for note in notes if candidate_from_note(note) is not None]
    if not linkable:
        console.print("[red]Nenhuma nota com tese ou definicao para religar.[/red]")
        db.close()
        raise typer.Exit(1)

    preflight_gate(estimate_reconnect(cfg, db, linkable), yes, db)
    idx = get_idx(cfg, db=db, yes=yes)
    session: ConnectSession = make_session(cfg, db, idx)

    from zettel.llm import LLMUnavailableError

    try:
        diffs = run_reconnect(cfg, db, session, linkable, apply=not dry_run)
    except LLMUnavailableError as exc:
        exit_llm_unavailable(exc, db)

    kind = "arestas" if relations_only else "arestas, corpo preservado"
    for diff in diffs:
        console.print(render_diff(diff))
    if dry_run:
        console.print(f"[yellow]dry-run ({kind}): nada foi escrito[/yellow]")
    else:
        console.print(f"[green]Notas religadas ({kind}): {len(diffs)}[/green]")
    db.close()


@app.command()
def garden(
    config: ConfigOption = None,
    min_cluster_size: Annotated[
        int | None,
        typer.Option(
            "--min-cluster-size",
        ),
    ] = None,
    hubs: Annotated[
        bool,
        typer.Option(
            "--hubs",
            help="Gerar MOCs ancorados em notas-hub do grafo (complementar ao pipeline taxonomico)",
        ),
    ] = False,
    recreate: Annotated[
        bool,
        typer.Option(
            "--recreate",
            help="Apagar MOCs gerados pelo pipeline e regenerar do zero",
        ),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            "-y",
            help="Confirmar automaticamente (--recreate e reprocessamento de embedding)",
        ),
    ] = False,
):
    """Clusterizar notas e gerar/atualizar MOCs."""
    cfg = load_deps(config)
    if min_cluster_size:
        cfg.gardener.min_cluster_size = min_cluster_size

    # --recreate deletes MOCs, so it is confirmed before anything is opened.
    # The two pipelines are purged independently: --hubs --recreate leaves the
    # taxonomy MOCs alone, and vice versa.
    if recreate and not yes:
        target = "hub" if hubs else "taxonomia"
        if not typer.confirm(
            f"Apaga todos os MOCs do pipeline ({target}) "
            f"(vault, banco e indice) e regenera. Continuar?",
            default=False,
        ):
            raise typer.Exit(0)

    db = get_db(cfg)
    idx = get_idx(cfg, db=db, yes=yes)

    if hubs:
        from zettel.gardener_hub import run_garden_hubs

        with console.status("[bold blue]Cultivando MOCs hub...", spinner="dots"):
            moc_ids = run_garden_hubs(cfg, db, idx, recreate=recreate)
        if recreate:
            console.print("[dim]MOCs hub do pipeline foram removidos antes da geracao.[/dim]")
    else:
        from zettel.gardener import run_garden

        with console.status("[bold blue]Cultivando o jardim de notas...", spinner="dots"):
            moc_ids = run_garden(cfg, db, idx, recreate=recreate)
        if recreate:
            console.print("[dim]MOCs do pipeline foram removidos antes da geracao.[/dim]")

    if moc_ids:
        console.print(f"[green]MOCs gerados/atualizados: {len(moc_ids)}[/green]")
        for mid in moc_ids:
            console.print(f"  - {mid}")
    else:
        console.print("[yellow]Nenhum MOC gerado (notas insuficientes ou ja atualizados).[/yellow]")

    db.close()
