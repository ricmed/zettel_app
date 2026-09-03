"""``ask``: short-form question answering over the vault.

The command's job beyond calling ``run_ask`` is to make the retrieval *auditable*,
which is why it prints two tables instead of just an answer:

* **Parametros de recuperacao** (``--show-context`` only) — every threshold the
  run was actually judged against. Reading a disappointing answer without knowing
  the floor it was measured against sends you to config.yaml to guess.
* **Notas recuperadas** — the raw ranked pool *before* the relevance floor, with a
  per-row verdict and reason. This is shown even when nothing passed the floor,
  because "here is what was closest, and why none of it counted" is the useful
  answer to a question the vault cannot support (ADR-003). When the pool is empty
  of qualifying hits the LLM is never called at all, and the answer is a
  deterministic "no evidence" line.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.panel import Panel
from rich.table import Table

from zettel.cli.app import app, console
from zettel.cli.deps import get_db, get_idx, load_deps
from zettel.cli.options import (
    ConfigOption,
    NoGraphOption,
    RetrievalModeOption,
    SeedTopkOption,
    YesOption,
)


@app.command()
def ask(
    question: Annotated[str, typer.Argument(help="Pergunta sobre o vault")],
    config: ConfigOption = None,
    topk: SeedTopkOption = None,
    no_graph: NoGraphOption = False,
    mode: RetrievalModeOption = None,
    show_context: Annotated[bool, typer.Option(
        "--show-context", help="Exibe as notas recuperadas (debug)",
    )] = False,
    save: Annotated[bool, typer.Option(
        "--save", help="Salva a resposta em .md no local padrao (sem perguntar)",
    )] = False,
    save_to: Annotated[Optional[str], typer.Option(
        "--save-to", help="Salva a resposta em .md no caminho informado",
    )] = None,
    no_save_prompt: Annotated[bool, typer.Option(
        "--no-save-prompt", help="Nao perguntar se deve salvar (para scripts)",
    )] = False,
    yes: YesOption = False,
):
    """Responder uma pergunta usando as notas do vault (recuperacao hibrida + grafo)."""
    cfg = load_deps(config)
    db = get_db(cfg)
    idx = get_idx(cfg, db=db, yes=yes)

    from zettel.ask import run_ask, save_ask_note

    with console.status("[bold blue]Consultando o acervo...", spinner="dots"):
        result = run_ask(
            cfg, db, idx, question,
            topk=topk,
            use_graph=not no_graph,
            mode=mode,
        )

    console.print(Panel(result.answer.strip() or "(sem resposta)", title="Resposta"))

    # Parameters actually used for this run — shown only with --show-context,
    # since it's a debug/internals view, not part of the default UX.
    if show_context and result.retrieval_params:
        p = result.retrieval_params
        params_table = Table(title="Parametros de recuperacao")
        params_table.add_column("Parametro", style="bold")
        params_table.add_column("Valor", justify="right")
        rows = [
            ("Modo", p["mode"]),
            ("Top-k sementes", p["topk"]),
            ("Max. notas no contexto", p["max_context_notes"]),
            ("RRF k", p["rrf_k"]),
            ("Piso de relevancia ativo", "sim" if p["relevance_floor_enabled"] else "nao"),
            ("Similaridade minima (piso)", f"{p['min_vector_similarity']:.2f}"),
            ("Similaridade minima absoluta", f"{p['absolute_min_similarity']:.2f}"),
            ("Bypass do BM25 ativo", "sim" if p["bm25_hit_bypasses_floor"] else "nao"),
            ("Rank max. para bypass do BM25", p["bm25_bypass_max_rank"]),
            ("Expansao por grafo", "sim" if p["graph_expansion_used"] else "nao"),
            ("Grafo: max. saltos", p["graph_max_hops"]),
            ("Grafo: decaimento por salto", p["graph_decay"]),
            ("Grafo: max. vizinhos", p["graph_max_neighbors"]),
        ]
        for label, value in rows:
            params_table.add_row(label, str(value))
        console.print(params_table)

    # `candidates` is the raw ranked pool (before the relevance floor), always
    # shown so the user can see what was closest even when nothing was relevant
    # enough to answer from (in which case `sources` is empty).
    if show_context or result.candidates:
        ctx_table = Table(title="Notas recuperadas")
        ctx_table.add_column("Nota", style="bold")
        ctx_table.add_column("Score RRF (posicao)", justify="right")
        ctx_table.add_column("Similaridade", justify="right")
        ctx_table.add_column("Rank BM25", justify="right")
        ctx_table.add_column("Salto", justify="right")
        ctx_table.add_column("Usada?")
        ctx_table.add_column("Motivo")
        ctx_table.add_column("Origem")
        for src in result.candidates:
            sim = f"{src.vector_similarity:.2f}" if src.vector_similarity is not None else "-"
            bm25 = str(src.bm25_rank) if src.bm25_rank is not None else "-"
            ctx_table.add_row(
                src.title or src.note_id,
                f"{src.rrf_score:.4f}",
                sim,
                bm25,
                str(src.hop),
                "sim" if src.passed_floor else "nao",
                src.floor_reason,
                src.origin,
                style="" if src.passed_floor else "dim",
            )
        console.print(ctx_table)

    # Save the answer with full provenance.
    saved_path = None
    if save_to:
        saved_path = save_ask_note(result, cfg.vault_path, Path(save_to))
    elif save:
        saved_path = save_ask_note(result, cfg.vault_path)
    elif not no_save_prompt:
        from rich.prompt import Confirm
        try:
            if Confirm.ask("Salvar esta resposta como nota .md?", default=False):
                saved_path = save_ask_note(result, cfg.vault_path)
        except (EOFError, KeyboardInterrupt):
            pass

    if saved_path:
        try:
            rel = saved_path.relative_to(cfg.vault_path)
            console.print(f"[green]Resposta salva em:[/green] {rel}")
        except ValueError:
            console.print(f"[green]Resposta salva em:[/green] {saved_path}")

    db.close()
