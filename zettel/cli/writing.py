"""``article``: long-form writing driven by the LangGraph pipeline.

The command is thin — ``run_article_graph`` owns the whole state machine
(ADR-028/ADR-029) — except for one real responsibility: it supplies the
**human-in-the-loop handler**. The graph pauses twice via LangGraph ``interrupt()``
and calls back into whatever handler the caller passed; ``_hitl`` below is the
terminal implementation of that callback, and the web UI deliberately has none,
which is why ``article`` is CLI-only.

The handler's contract with ``zettel.article_graph`` is a pair of dicts:

**``{"type": "context_review", ...}``** — the retrieved notes, before drafting.
    Payload: ``notes`` (list of dicts with ``title``/``note_id``/``score``/``hop``
    and a ``metadata`` dict carrying ``source_id``) and ``executed_queries``
    (the enriched queries actually searched).
    Return: ``{"context_decision": "approve" | "enrich" | "abort",
    "extra_queries": list[str]}``. ``enrich`` re-runs the search with the extra
    queries merged in; an ``enrich`` with no queries is downgraded to ``approve``
    so a stray Enter cannot loop the graph.

**``{"type": "outline_review", ...}``** — the proposed outline.
    Payload: ``preview`` (rendered outline text).
    Return: ``{"outline_decision": "approve" | "regenerate" | "abort",
    "outline_feedback": str}``. Feedback is optional and is fed back into the
    outline prompt.

Any other ``type`` returns ``{}``: an unknown interrupt must not crash a run that
may already have cost several LLM calls.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.panel import Panel
from rich.table import Table

from zettel.cli.app import app, console
from zettel.cli.deps import get_db, get_idx, load_deps, preflight_gate
from zettel.cli.options import (
    ConfigOption,
    NoGraphOption,
    RetrievalModeOption,
    SeedTopkOption,
    YesOption,
)


@app.command()
def article(
    topic: Annotated[str, typer.Argument(help="Tema do artigo")],
    style: Annotated[str, typer.Option(
        "--style", "-s", help="blog | academic",
    )] = "blog",
    config: ConfigOption = None,
    topk: SeedTopkOption = None,
    no_graph: NoGraphOption = False,
    mode: RetrievalModeOption = None,
    personality: Annotated[Optional[str], typer.Option(
        "--personality", "-p", help="Perfil em config/personalities.yaml",
    )] = None,
    style_notes: Annotated[Optional[str], typer.Option(
        "--style-notes", help="Override textual de estilo",
    )] = None,
    show_context: Annotated[bool, typer.Option(
        "--show-context", help="Exibe notas recuperadas (debug)",
    )] = False,
    outline_only: Annotated[bool, typer.Option(
        "--outline-only", help="Gera so o outline e encerra",
    )] = False,
    skip_context_review: Annotated[bool, typer.Option(
        "--skip-context-review", help="Pula revisao humana do contexto",
    )] = False,
    skip_judge: Annotated[bool, typer.Option(
        "--skip-judge", help="Pula o juiz automatico de qualidade",
    )] = False,
    max_judge_iterations: Annotated[Optional[int], typer.Option(
        "--max-judge-iterations", help="Max. ciclos de reescrita do juiz",
    )] = None,
    save: Annotated[bool, typer.Option(
        "--save", help="Salva o artigo em .md no local padrao (sem perguntar)",
    )] = False,
    save_to: Annotated[Optional[str], typer.Option(
        "--save-to", help="Salva o artigo em .md no caminho informado",
    )] = None,
    no_save_prompt: Annotated[bool, typer.Option(
        "--no-save-prompt", help="Nao perguntar se deve salvar (para scripts)",
    )] = False,
    yes: YesOption = False,
):
    """Gerar artigo estruturado (blog ou academico) via LangGraph."""
    style_norm = (style or "blog").strip().lower()
    if style_norm not in ("blog", "academic"):
        console.print("[red]--style deve ser 'blog' ou 'academic'[/red]")
        raise typer.Exit(1)

    cfg = load_deps(config)
    db = get_db(cfg)
    idx = get_idx(cfg, db=db, yes=yes)

    from rich.prompt import Prompt

    from zettel.preflight import estimate_article
    preflight_gate(estimate_article(cfg), yes, db)

    from zettel.article import parse_extra_queries, save_article_note
    from zettel.article_graph import run_article_graph

    def _hitl(payload: dict) -> dict:
        """Terminal implementation of the graph's two interrupts (see module docstring)."""
        itype = payload.get("type")
        if itype == "context_review":
            notes = payload.get("notes") or []
            table = Table(title="Notas recuperadas (contexto)")
            table.add_column("#", justify="right")
            table.add_column("Titulo")
            table.add_column("Score", justify="right")
            table.add_column("Hop", justify="right")
            table.add_column("Fonte")
            for i, n in enumerate(notes, 1):
                meta = n.get("metadata") or {}
                table.add_row(
                    str(i),
                    (n.get("title") or n.get("note_id") or "")[:60],
                    f"{float(n.get('score') or 0):.4f}",
                    str(n.get("hop") or 0),
                    str(meta.get("source_id") or "-"),
                )
            console.print(table)
            qs = payload.get("executed_queries") or []
            if qs:
                console.print("[dim]Queries usadas: " + ", ".join(qs) + "[/dim]")
            choice = Prompt.ask(
                "Contexto: [a]aprovar / [e]extras / [q]quit",
                choices=["a", "e", "q"],
                default="a",
            )
            if choice == "a":
                return {"context_decision": "approve", "extra_queries": []}
            if choice == "q":
                return {"context_decision": "abort", "extra_queries": []}
            raw = Prompt.ask(
                "Queries extras (separadas por ; ou linhas)", default=""
            )
            extras = parse_extra_queries(raw)
            # An "enrich" with nothing to enrich would re-run the identical search.
            if not extras:
                return {"context_decision": "approve", "extra_queries": []}
            return {"context_decision": "enrich", "extra_queries": extras}

        if itype == "outline_review":
            console.print(
                Panel(str(payload.get("preview") or ""), title="Outline proposto")
            )
            choice = Prompt.ask(
                "Outline: [a]provar / [r]egenerar / [q]uit",
                choices=["a", "r", "q"],
                default="a",
            )
            if choice == "a":
                return {"outline_decision": "approve", "outline_feedback": ""}
            if choice == "q":
                return {"outline_decision": "abort", "outline_feedback": ""}
            feedback = Prompt.ask(
                "Feedback para regenerar (opcional)", default=""
            )
            return {
                "outline_decision": "regenerate",
                "outline_feedback": feedback.strip(),
            }
        return {}

    console.print("[dim]Pipeline de artigo (LangGraph)...[/dim]")
    result = run_article_graph(
        cfg, db, idx, topic,
        style=style_norm,  # type: ignore[arg-type]
        topk=topk,
        use_graph=not no_graph,
        mode=mode,
        outline_only=outline_only,
        personality=personality,
        custom_style_notes=style_notes,
        skip_context_review=skip_context_review or outline_only,
        skip_judge=skip_judge or outline_only,
        max_judge_iterations=max_judge_iterations,
        hitl_handler=_hitl,
    )

    if result.no_evidence:
        console.print(Panel(result.body, title="Sem evidencia"))
        db.close()
        raise typer.Exit(0)

    if result.aborted:
        console.print("[yellow]Geracao abortada pelo usuario.[/yellow]")
        db.close()
        raise typer.Exit(0)

    if outline_only:
        console.print(Panel(result.body, title="Outline"))
        db.close()
        raise typer.Exit(0)

    console.print(Panel(result.body.strip() or "(vazio)", title=result.title or "Artigo"))

    for w in result.warnings:
        console.print(f"[yellow]Aviso:[/yellow] {w}")

    if show_context and result.note_ids:
        table = Table(title="Notas usadas no artigo")
        table.add_column("note_id")
        for nid in result.note_ids:
            table.add_row(nid)
        console.print(table)

    saved_path = None
    if save_to:
        saved_path = save_article_note(result, cfg.vault_path, Path(save_to))
    elif save:
        saved_path = save_article_note(result, cfg.vault_path)
    elif not no_save_prompt:
        from rich.prompt import Confirm
        try:
            if Confirm.ask("Salvar este artigo como nota .md?", default=True):
                saved_path = save_article_note(result, cfg.vault_path)
        except (EOFError, KeyboardInterrupt):
            pass

    if saved_path:
        try:
            rel = saved_path.relative_to(cfg.vault_path)
            console.print(f"[green]Artigo salvo em:[/green] {rel}")
        except ValueError:
            console.print(f"[green]Artigo salvo em:[/green] {saved_path}")

    db.close()
