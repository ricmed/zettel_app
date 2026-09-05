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
