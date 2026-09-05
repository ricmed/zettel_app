"""Store lifecycle: creating them, and rebuilding them from SQLite.

* ``init``    — create the vault tree, the state database and the Chroma store;
* ``reindex`` — repopulate Chroma from SQLite;
* ``rebuild`` — regenerate the vault ``.md`` files and/or Chroma from SQLite.

The three share one premise, and it is the reason SQLite is the primary store
(ADR-001): **everything except the original inbox files can be regenerated from
it, with no LLM calls.** The vault is a rendering of the database, and Chroma is
an index over it. That is what makes an embedding-model swap a mechanical
operation rather than a re-run of the pipeline.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from zettel.cli.app import app, console
from zettel.cli.deps import (
    confirm_embedding_reprocess,
    get_db,
    get_idx,
    load_deps,
    warn_embedding_mismatch,
)
from zettel.cli.formatting import metrics_table
from zettel.cli.options import ConfigOption, YesOption


@app.command()
def init(
    config: Annotated[
        str | None,
        typer.Option(
            "--config",
            "-c",
            help="Caminho para config.yaml",
        ),
    ] = None,
    vault: Annotated[
        str | None,
        typer.Option(
            "--vault",
            help="Caminho do vault (override)",
        ),
    ] = None,
    inbox: Annotated[
        str | None,
        typer.Option(
            "--inbox",
            help="Caminho do inbox (override)",
        ),
    ] = None,
    reset: Annotated[
        bool,
        typer.Option(
            "--reset",
            help="Apagar e recriar todas as bases de dados",
        ),
    ] = False,
):
    """Inicializar o vault, banco de dados e índice vetorial."""
    cfg = load_deps(config)
    if vault:
        cfg.vault_path = Path(vault).resolve()
    if inbox:
        cfg.inbox_path = Path(inbox).resolve()

    if reset:
        import shutil

        confirmed = typer.confirm("Isso vai APAGAR o State DB, ChromaDB e cache. Continuar?")
        if not confirmed:
            console.print("[yellow]Reset cancelado.[/yellow]")
            raise typer.Exit(0)

        # Remove State DB
        if cfg.state_db_path.exists():
            cfg.state_db_path.unlink()
            # WAL/SHM companion files
            for suffix in ("-wal", "-shm"):
                companion = cfg.state_db_path.with_name(cfg.state_db_path.name + suffix)
                if companion.exists():
                    companion.unlink()
            console.print(f"[red]State DB removido:[/red] {cfg.state_db_path}")

        # Remove ChromaDB
        if cfg.chroma_path.exists():
            shutil.rmtree(cfg.chroma_path)
            console.print(f"[red]ChromaDB removido:[/red] {cfg.chroma_path}")

        # Remove cache
        if cfg.cache_path.exists():
            shutil.rmtree(cfg.cache_path)
            console.print(f"[red]Cache removido:[/red] {cfg.cache_path}")

    from zettel.vault import init_vault

    init_vault(cfg.vault_path)

    db = get_db(cfg)
    # Called for the side effect, not the value: constructing VectorIndex is what
    # creates the five Chroma collections and stamps them with the embedding
    # identity. Without this, the store only appears on the first pipeline run.
    get_idx(cfg, db=db, yes=False)
    db.close()

    # Ensure directories
    cfg.inbox_path.mkdir(parents=True, exist_ok=True)
    cfg.cache_path.mkdir(parents=True, exist_ok=True)

    from zettel.config import detect_device, get_gpu_info

    device = detect_device(cfg.device)
    gpu = get_gpu_info()
    device_line = f"[green]Device:[/green] {device.upper()}"
    if gpu["available"]:
        device_line += f" ({gpu.get('device_name', '?')} — {gpu.get('vram_gb', '?')} GB VRAM)"

    reset_line = "\n[red]Bases de dados recriadas do zero.[/red]" if reset else ""
    from rich.panel import Panel

    console.print(
        Panel(
            f"[green]Vault inicializado em:[/green] {cfg.vault_path}\n"
            f"[green]Inbox:[/green] {cfg.inbox_path}\n"
            f"[green]State DB:[/green] {cfg.state_db_path}\n"
            f"[green]ChromaDB:[/green] {cfg.chroma_path}\n"
            f"{device_line}{reset_line}",
            title="Zettelkasten -- Init",
            border_style="green",
        )
    )


@app.command()
def reindex(
    config: ConfigOption = None,
    collection: Annotated[
        str | None,
        typer.Option(
            "--collection",
            help="Reindexar apenas: sources|chunks|permanent_notes|mocs",
        ),
    ] = None,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Resetar a colecao antes de repovoar",
        ),
    ] = False,
    yes: YesOption = False,
):
    """Reconstruir o ChromaDB a partir do SQLite (sem chamadas de LLM).

    Se o provider/modelo de embedding no config diferir do marcado no Chroma,
    --force e aplicado automaticamente (aviso + confirmacao, ou --yes).
    Sem --force apos troca de modelo, sources/chunks antigos nao seriam regenerados.
    """
    from zettel.index import (
        EmbeddingSpaceMismatch,
        VectorIndex,
        index_kwargs,
        peek_stored_embedding_identity,
    )

    cfg = load_deps(config)
    db = get_db(cfg)

    # Drift is detected here rather than through get_idx() because reindex is the
    # command that *fixes* it: it must upgrade a plain run into a --force run and
    # explain why, instead of offering the generic "reprocess now?" flow.
    stored = peek_stored_embedding_identity(cfg.chroma_path)
    drift = any(x is not None for x in stored) and (
        stored[0] != cfg.embedding.provider
        or stored[1] != cfg.embedding.model
        or stored[2] != cfg.embedding.dimensions
    )
    if drift:
        exc = EmbeddingSpaceMismatch(
            stored[0],
            stored[1],
            cfg.embedding.provider,
            cfg.embedding.model,
            stored_dimensions=stored[2],
            current_dimensions=cfg.embedding.dimensions,
        )
        warn_embedding_mismatch(exc)
        if not force and not confirm_embedding_reprocess(yes):
            console.print(
                "[red]Abortado.[/red] Use [bold]zettel reindex --force[/bold] ou passe --yes."
            )
            db.close()
            raise typer.Exit(1)
        force = True
        console.print("[dim]Troca de embedding detectada — aplicando --force.[/dim]")
        idx = VectorIndex(**index_kwargs(cfg, reset_mismatched=True))
    else:
        idx = VectorIndex(**index_kwargs(cfg))

    from zettel.rebuild import run_reindex

    with console.status("[bold blue]Reconstruindo indice vetorial...", spinner="dots"):
        stats = run_reindex(cfg, db, idx, collection, force)

    metrics_table("Reindex", stats, key_label="Colecao", value_label="Vetores")
    db.close()


@app.command()
def rebuild(
    config: ConfigOption = None,
    what: Annotated[
        str,
        typer.Option(
            "--what",
            help="vault | chroma | all",
        ),
    ] = "vault",
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Sobrescrever arquivos existentes (nunca notas manuais)",
        ),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Simular sem escrever",
        ),
    ] = False,
    yes: YesOption = False,
):
    """Reconstruir o vault (.md) e/ou o ChromaDB a partir do SQLite, sem reprocessar LLM."""
    cfg = load_deps(config)
    db = get_db(cfg)

    if what not in ("vault", "chroma", "all"):
        console.print("[red]--what deve ser: vault | chroma | all[/red]")
        db.close()
        raise typer.Exit(1)

    if what in ("vault", "all"):
        from zettel.rebuild import run_rebuild_vault

        with console.status("[bold blue]Reconstruindo vault a partir do banco...", spinner="dots"):
            vstats = run_rebuild_vault(cfg, db, force=force, dry_run=dry_run)
        metrics_table(
            "Rebuild vault" + (" (dry-run)" if dry_run else ""),
            vstats,
        )

    if what in ("chroma", "all"):
        idx = get_idx(cfg, db=db, yes=yes)
        from zettel.rebuild import run_reindex

        with console.status("[bold blue]Reconstruindo indice vetorial...", spinner="dots"):
            rstats = run_reindex(cfg, db, idx, force=force)
        metrics_table(
            "Rebuild chroma",
            rstats,
            key_label="Colecao",
            value_label="Vetores",
        )

    db.close()
