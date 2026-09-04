"""Composition root: builds the ``(AppConfig, StateDB, VectorIndex)`` trio.

Typer models one invocation as one command execution (ADR-026), so there is no
long-lived process to hold these objects. Every command opens what it needs at
the top and closes the database at the end; this module is where that happens so
the commands themselves stay about their own logic.

The embedding-drift interaction also lives here. ``VectorIndex`` can only *detect*
that the configured embedding no longer matches the vectors on disk — it raises
``EmbeddingSpaceMismatch`` and stops. Deciding what to do about it (explain the
consequence, ask the human, then rebuild) is a UX decision, and UX belongs to the
entry point, not to the data layer. The web worker faces the same exception and
answers it differently, which is exactly why the resolution is not baked into
``VectorIndex``.
"""

from __future__ import annotations

import sys

import typer

from zettel.cli.app import console
from zettel.cli.formatting import fmt_embedding_id, metrics_table


def load_deps(config_path: str | None = None):
    """Load the config and configure logging. Returns the ``AppConfig``."""
    from zettel.config import load_config, setup_logging

    cfg = load_config(config_path)
    setup_logging(cfg.log_level)
    return cfg


def get_db(cfg):
    """Open the SQLite state database. Callers are responsible for ``close()``."""
    from zettel.state import StateDB
    return StateDB(cfg.state_db_path)


def warn_embedding_mismatch(exc) -> None:
    """Explain an ``EmbeddingSpaceMismatch`` in terms of what the user must do.

    The important part of the message is the last paragraph: regenerating vectors
    is cheap (no LLM, no rewriting of .md files) but it invalidates the thresholds
    that were calibrated against the old space — the relevance floor and the
    dedupe cutoffs are corpus- and model-specific numbers.
    """
    stored = fmt_embedding_id(
        exc.stored_provider, exc.stored_model, getattr(exc, "stored_dimensions", None),
    )
    current = fmt_embedding_id(
        exc.current_provider, exc.current_model, getattr(exc, "current_dimensions", None),
    )
    from rich.panel import Panel
    console.print(Panel(
        f"[yellow]O modelo de embedding mudou.[/yellow]\n\n"
        f"  Chroma (atual): [bold]{stored}[/bold]\n"
        f"  Config (novo):  [bold]{current}[/bold]\n\n"
        f"Os vetores existentes sao incompativeis com o novo espaco. "
        f"E necessario regenerar TODOS os embeddings a partir do SQLite "
        f"([bold]zettel reindex --force[/bold]).\n"
        f"Isso [bold]nao[/bold] reescreve notas .md nem chama o LLM.\n"
        f"Apos a troca, considere recalibrar "
        f"`retrieval.relevance_floor.min_vector_similarity` e os limiares de dedupe "
        f"se a qualidade da busca degradar.",
        title="Troca de embedding",
        border_style="yellow",
    ))


def confirm_embedding_reprocess(yes: bool) -> bool:
    """Ask before rebuilding every vector; ``--yes`` answers for scripts."""
    if yes:
        return True
    return typer.confirm("Reprocessar todos os embeddings agora?", default=False)


def get_idx(cfg, db=None, yes: bool = False):
    """Open the ``VectorIndex``; on embedding drift, warn, confirm, and reindex.

    Args:
        cfg: loaded application config.
        db: ``StateDB`` used to repopulate Chroma after a confirmed reprocess.
            Opened temporarily (and closed again) when omitted, so a command that
            has no database of its own can still recover.
        yes: skip the interactive confirmation (CI / ``--yes``).

    Raises:
        typer.Exit: the user declined the reprocess. Continuing would mean
            querying a store whose vectors live in a different space, which
            returns confident nonsense rather than an error.
    """
    from zettel.index import EmbeddingSpaceMismatch, VectorIndex, index_kwargs

    try:
        return VectorIndex(**index_kwargs(cfg))
    except EmbeddingSpaceMismatch as exc:
        warn_embedding_mismatch(exc)
        if not confirm_embedding_reprocess(yes):
            console.print(
                "[red]Abortado.[/red] Para regenerar os vetores depois: "
                "[bold]zettel reindex --force[/bold] "
                "(ou passe --yes em scripts)."
            )
            raise typer.Exit(1) from exc

        own_db = db is None
        if own_db:
            db = get_db(cfg)
        try:
            idx = VectorIndex(**index_kwargs(cfg, reset_mismatched=True))
            from zettel.rebuild import run_reindex
            with console.status(
                "[bold blue]Regenerando embeddings (reindex --force)...",
                spinner="dots",
            ):
                stats = run_reindex(cfg, db, idx, force=True)
            metrics_table(
                "Reindex (troca de embedding)", stats,
                key_label="Colecao", value_label="Vetores",
            )
            return idx
        finally:
            if own_db and db is not None:
                db.close()


def preflight_gate(estimate, yes: bool, db=None) -> None:
    """Show the LLM cost estimate and, when interactive, ask before spending.

    Lives next to the embedding-drift confirmation for the same reason: deciding
    what to ask a human is entry-point UX, not pipeline logic. The estimators in
    ``zettel/preflight.py`` stay pure, so ``run_extract`` / ``run_connect`` /
    ``run_article_graph`` are untouched and the web worker (a daemon with no
    stdin) cannot acquire a new way to block.

    ``--yes`` and a non-TTY stdin go straight through: the estimate is an
    operator courtesy, not a budget cap.
    """
    from rich.table import Table

    table = Table(title="Pre-voo LLM")
    table.add_column("Item", style="bold")
    table.add_column("Valor", justify="right")
    table.add_row("Fase", estimate.phase)
    table.add_row("Modelo", f"{estimate.provider}/{estimate.model}")
    table.add_row("Itens", f"{estimate.items} {estimate.item_label}")
    table.add_row("Tokens input (est.)", f"~{estimate.input_tokens:,}")
    table.add_row("Tokens output (est.)", f"~{estimate.output_tokens:,}")
    table.add_row("Custo est. (LiteLLM)", f"~${estimate.cost_usd:.4f}")
    console.print(table)
    for caveat in estimate.caveats:
        console.print(f"[dim]{caveat}[/dim]")
    console.print("[dim]Estimativa: precos mudam e o cache SQLite pode reduzir o total.[/dim]")

    if yes or not sys.stdin.isatty():
        return
    if typer.confirm("Prosseguir?", default=True):
        return
    console.print("[yellow]Abortado antes de qualquer chamada de LLM.[/yellow]")
    if db is not None:
        db.close()
    raise typer.Exit(1)
