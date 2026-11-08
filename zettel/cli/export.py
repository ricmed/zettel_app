"""``skill``: project an approved slice of the vault as a flat Agent Skill.

The third way to consume the vault, next to ``ask`` (a question now) and
``article`` (a long text). This one hands a coding agent a slice it can route
through on its own: a small always-loaded ``SKILL.md`` plus files it opens on
demand.

Unlike its neighbours, this command spends nothing — the export is a
deterministic projection of what `review`, `connect` and `garden` already
approved, so there is no LLM call, no write to SQLite or Chroma, and no
pre-flight to confirm (ADR-035). The only number worth printing back is the
estimated size of ``SKILL.md``: it is the file loaded into every session the
skill triggers, so an oversized slice should show up as a number rather than as
a surprise later.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.table import Table

from zettel.cli.app import app, console
from zettel.cli.deps import get_db, load_deps
from zettel.cli.options import ConfigOption


@app.command()
def skill(
    config: ConfigOption = None,
    source_id: Annotated[
        str | None,
        typer.Option(
            "--source-id",
            help="Recorte por fonte (@Citekey)",
        ),
    ] = None,
    moc_id: Annotated[
        str | None,
        typer.Option(
            "--moc-id",
            help="Recorte por MOC (ULID)",
        ),
    ] = None,
    topic: Annotated[
        str | None,
        typer.Option(
            "--topic",
            help="Recorte por categoria da taxonomia do gardener",
        ),
    ] = None,
    out: Annotated[
        str | None,
        typer.Option(
            "--out",
            help="Diretorio que guarda os packs; o pack vai para <out>/<slug> "
            "(default: <vault>/.claude/skills)",
        ),
    ] = None,
    slug: Annotated[
        str | None,
        typer.Option(
            "--slug",
            help="Nome do pack (default: derivado do seletor)",
        ),
    ] = None,
    overwrite: Annotated[
        bool,
        typer.Option(
            "--overwrite",
            help="Regenerar por cima de um pack existente",
        ),
    ] = False,
    include_excerpts: Annotated[
        bool,
        typer.Option(
            "--include-excerpts",
            help="Copiar o trecho da fonte para os arquivos do pack (default: nao)",
        ),
    ] = False,
):
    """Exportar um recorte aprovado do vault como Agent Skill plana."""
    cfg = load_deps(config)
    db = get_db(cfg)

    from zettel.skill_export import SkillExportError, run_skill_export

    try:
        pack_dir, pack = run_skill_export(
            cfg,
            db,
            source_id=source_id,
            moc_id=moc_id,
            topic=topic,
            out=Path(out).expanduser().resolve() if out else None,
            slug=slug,
            overwrite=overwrite,
            include_excerpts=include_excerpts,
        )
    except SkillExportError as e:
        console.print(f"[red]{e}[/red]")
        db.close()
        raise typer.Exit(1) from e

    console.print(f"[green]Skill gerada em:[/green] {pack_dir}")
    table = Table(title=f"Pack '{pack.slug}'")
    table.add_column("Item", style="bold")
    table.add_column("Valor", justify="right")
    table.add_row("Notas", str(len(pack.notes)))
    table.add_row("Tensoes (contradicts)", str(len(pack.contradictions)))
    table.add_row("SKILL.md (tokens est.)", str(_skill_md_tokens(pack_dir)))
    table.add_row("Trecho da fonte incluido", "sim" if pack.include_excerpts else "nao")
    console.print(table)
    db.close()


def _skill_md_tokens(pack_dir: Path) -> int:
    from zettel.skill_export import estimate_tokens

    skill_md = pack_dir / "SKILL.md"
    return estimate_tokens(skill_md.read_text(encoding="utf-8")) if skill_md.is_file() else 0


@app.command()
def manuscript(
    project: Annotated[str, typer.Argument(help="Mesa: o ULID (project_id) ou o arquivo PRJ")],
    config: ConfigOption = None,
    fmt: Annotated[
        str,
        typer.Option("--format", help="docx | md (pasta com figuras e .bib) | zip | canvas"),
    ] = "docx",
    out: Annotated[
        str | None,
        typer.Option(
            "--out",
            help="Diretorio de saida (default: o atual; canvas: ao lado da mesa no vault)",
        ),
    ] = None,
    style: Annotated[
        str | None,
        typer.Option("--style", help="academic | blog (default: o da mesa)"),
    ] = None,
    placeholders: Annotated[
        bool | None,
        typer.Option(
            "--placeholders/--no-placeholders",
            help="Marcar o que falta escrever [Complementar] (default: config manuscript)",
        ),
    ] = None,
    quotes: Annotated[
        bool | None, typer.Option("--quotes/--no-quotes", help="Trecho literal de cada nota")
    ] = None,
    limits: Annotated[
        bool | None, typer.Option("--limits/--no-limits", help="Limites de cada nota")
    ] = None,
    appendix: Annotated[
        bool | None,
        typer.Option("--appendix/--no-appendix", help="Apendice com as notas completas"),
    ] = None,
):
    """Exportar uma Mesa como manuscrito (DOCX, Markdown com figuras, canvas). Sem LLM."""
    cfg = load_deps(config)
    if fmt not in {"docx", "md", "zip", "canvas"}:
        console.print("[red]--format deve ser docx, md, zip ou canvas.[/red]")
        raise typer.Exit(1)

    from zettel.manuscript import build_manuscript
    from zettel.manuscript_export import (
        reference_doc,
        to_docx,
        write_bundle,
        write_canvas,
        zip_bundle,
    )
    from zettel.projects import ProjectNotFound, load_project, read_project
    from zettel.vault import _slug

    path = Path(project).expanduser()
    found = read_project(path) if path.is_file() else None
    if found is None:
        try:
            found = load_project(cfg, project)
        except ProjectNotFound:
            console.print(f"[red]Mesa nao encontrada: {project}[/red]")
            raise typer.Exit(1) from None
    db = get_db(cfg)
    target = Path(out).expanduser().resolve() if out else None
    try:
        if fmt == "canvas":
            written = write_canvas(
                cfg,
                db,
                found,
                (target / found.path.with_suffix(".canvas").name) if target else None,
            )
            console.print(f"[green]Canvas gravado em:[/green] {written}")
            return
        defaults = cfg.manuscript
        result = build_manuscript(
            cfg,
            db,
            found,
            style=style,
            placeholders=defaults.placeholders if placeholders is None else placeholders,
            quotes=defaults.quotes if quotes is None else quotes,
            limits=defaults.limits if limits is None else limits,
            appendix=defaults.appendix if appendix is None else appendix,
        )
        base = target or Path.cwd()
        name = _slug(found.title, max_len=60) or "manuscrito"
        bundle = write_bundle(result, db, base / name)
    finally:
        db.close()
    if fmt == "zip":
        written = zip_bundle(bundle, base / f"{name}.zip")
    elif fmt == "docx":
        written = to_docx(bundle, base / f"{name}.docx", reference_doc(cfg))
    else:
        written = bundle
    for warning in result.warnings:
        console.print(f"[yellow]Aviso:[/yellow] {warning}")
    console.print(f"[green]Manuscrito gravado em:[/green] {written}")
    console.print(
        f"{len(result.figures)} figura(s), {len(result.source_ids)} fonte(s) em Referencias."
    )
