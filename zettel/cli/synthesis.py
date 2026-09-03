"""Phases 3 and 4: writing permanent notes, then organising them into MOCs.

* ``connect`` — Prompt 2 over every approved candidate, with RAG context and
                typed relations, producing ZTL notes;
* ``garden``  — cluster the resulting notes and generate/update MOCs, either from
                the taxonomy pipeline or (``--hubs``) anchored on graph hubs.

The two are grouped because they are the synthesis half of the pipeline: unlike
Phase 1–2, which read documents, these read the vault's own graph and write into
it. They also share the pattern of overriding a config knob for a single run
(``--topk``, ``--dedupe-threshold``, ``--min-cluster-size``) without persisting it
to ``config.yaml``.
"""

from __future__ import annotations

from typing import Annotated, Optional

import typer

from zettel.cli.app import app, console
from zettel.cli.deps import get_db, get_idx, load_deps
from zettel.cli.options import ConfigOption, YesOption


@app.command()
def connect(
    config: ConfigOption = None,
    topk: Annotated[Optional[int], typer.Option(
        "--topk", help="Top-k notas similares",
    )] = None,
    dedupe_threshold: Annotated[Optional[float], typer.Option(
        "--dedupe-threshold",
    )] = None,
    yes: YesOption = False,
):
    """Gerar notas permanentes a partir dos candidatos aprovados no review."""
    cfg = load_deps(config)
    # Per-run overrides: never written back to config.yaml.
    if topk:
        cfg.linking.topk = topk
    if dedupe_threshold:
        cfg.linking.dedupe_threshold = dedupe_threshold

    db = get_db(cfg)
    idx = get_idx(cfg, db=db, yes=yes)

    from zettel.connector import load_approved_candidates, run_connect
    candidates = load_approved_candidates(db)

    if not candidates:
        console.print(
            "[red]Nenhum candidato aprovado. Execute 'extract' e 'review' primeiro.[/red]"
        )
        db.close()
        raise typer.Exit(1)

    # Nao usar console.status: o Progress interno de run_connect disputa o mesmo
    # stdout (dois Rich Live) e a barra Connect nota i/N pisca. Ver #21.
    note_ids = run_connect(cfg, db, idx, candidates)

    console.print(f"[green]Notas permanentes criadas: {len(note_ids)}[/green]")
    for nid in note_ids:
        console.print(f"  - {nid}")

    db.close()


@app.command()
def garden(
    config: ConfigOption = None,
    min_cluster_size: Annotated[Optional[int], typer.Option(
        "--min-cluster-size",
    )] = None,
    hubs: Annotated[bool, typer.Option(
        "--hubs",
        help="Gerar MOCs ancorados em notas-hub do grafo (complementar ao pipeline taxonomico)",
    )] = False,
    recreate: Annotated[bool, typer.Option(
        "--recreate",
        help="Apagar MOCs gerados pelo pipeline e regenerar do zero",
    )] = False,
    yes: Annotated[bool, typer.Option(
        "--yes", "-y",
        help="Confirmar automaticamente (--recreate e reprocessamento de embedding)",
    )] = False,
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
            f"Apaga todos os MOCs do pipeline ({target}) (vault, banco e indice) e regenera. Continuar?",
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
