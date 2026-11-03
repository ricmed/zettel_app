"""Writing notes in Obsidian with the vault's structure (#344).

* ``obsidian-templates`` — write the Templater templates and their script under
  ``99_Templates/``, generated from the same scaffolds ``new-note`` uses.

The templates work offline inside Obsidian: they prompt for the title (and the
source), generate the ULID or the next ``chunk_index`` and name the file
``PREFIX - ID - slug.md``. ``sync-manual`` then adopts the note like one written
by ``new-note``.
"""

from __future__ import annotations

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
