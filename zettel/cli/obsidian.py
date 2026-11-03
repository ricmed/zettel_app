"""Writing notes in Obsidian with the vault's structure (#344).

* ``obsidian-templates`` — write the Templater templates and their script under
  ``99_Templates/``, generated from the same scaffolds ``new-note`` uses;
* ``obsidian-setup``     — the templates plus the Templater plugin and its
  settings in ``.obsidian/`` (what ``init`` does for a new vault, #345).

The templates work offline inside Obsidian: they prompt for the title (and the
source), generate the ULID or the next ``chunk_index`` and name the file
``PREFIX - ID - slug.md``. ``sync-manual`` then adopts the note like one written
by ``new-note``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from zettel.cli.app import app, console
from zettel.cli.deps import load_deps
from zettel.cli.options import ConfigOption


@app.command(name="obsidian-templates")
def obsidian_templates(
    config: ConfigOption = None,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Regravar templates e script existentes (rode apos atualizar o Zettel)",
        ),
    ] = False,
):
    """Gerar os templates do Obsidian (Templater) em 99_Templates/."""
    cfg = load_deps(config)

    from zettel.obsidian import TEMPLATES_DIR, write_templates

    written = write_templates(cfg.vault_path, cfg.vault_timezone, force=force)
    for path in written:
        console.print(f"[green]Gravado:[/green] {path.relative_to(cfg.vault_path)}")
    if not written:
        console.print(
            f"[yellow]Nada gravado:[/yellow] {TEMPLATES_DIR}/ ja esta completo "
            "(use --force para regravar)."
        )
    console.print(
        "[dim]No Obsidian: plugin Templater com a pasta de templates "
        f"'{TEMPLATES_DIR}' e a de scripts '{TEMPLATES_DIR}/scripts'. "
        "Depois: zettel sync-manual.[/dim]"
    )


@app.command(name="obsidian-setup")
def obsidian_setup(
    config: ConfigOption = None,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Regravar templates e reinstalar os arquivos do Templater",
        ),
    ] = False,
    templater_from: Annotated[
        Path | None,
        typer.Option(
            "--templater-from",
            help="Pasta com main.js, manifest.json e styles.css do Templater (sem rede)",
        ),
    ] = None,
):
    """Preparar o vault para o Obsidian: templates, plugin Templater e configuracao."""
    cfg = load_deps(config)

    from zettel.cli.formatting import print_obsidian_setup
    from zettel.obsidian import ObsidianSettingsError, setup_obsidian

    try:
        report = setup_obsidian(
            cfg.vault_path,
            cfg.cache_path,
            cfg.obsidian,
            cfg.vault_timezone,
            force=force,
            source=templater_from,
        )
    except ObsidianSettingsError as exc:
        console.print(f"[red]Obsidian nao configurado:[/red] {exc}")
        raise typer.Exit(1) from exc
    print_obsidian_setup(report, cfg.vault_path)
    if not report.plugin_ok:
        raise typer.Exit(1)
