"""Typer + Rich command-line interface for the Zettelkasten pipeline (ADR-026).

This package is the assembly point. ``app`` and ``console`` are created in
``app.py``; each command module below imports them, registers its commands with
``@app.command()``, and this file imports those modules to trigger the
registration. Importing a module *is* how a command reaches the CLI — a module not
listed here has no commands, however correct its code.

Module map:

===================  =========================================================
``app.py``           the ``app``/``console`` singletons (imports nothing local)
``deps.py``          composition root + embedding-drift UX
``formatting.py``    Rich renderers (cost table, metric tables, formatters)
``options.py``       shared ``Annotated`` options + flag resolvers
``maintenance.py``   init, reindex, rebuild
``ingest.py``        harvest, rechunk, dump-chunks, dump-extraction, set-paging
``curation.py``      extract, review, retry-failed
``synthesis.py``     connect, garden
``purge.py``         purge-rejected, delete-source
``manual.py``        new-note, sync-manual
``pipeline.py``      run-all
``qa.py``            ask
``writing.py``       article
``export.py``        skill
``diagnostics.py``   status, doctor
===================  =========================================================

**The import order below is the order commands appear in ``zettel --help``.**
It follows the life of a vault — set it up, feed it, curate it, synthesise, prune,
write by hand, run it all, query it, inspect it — and ``tests/test_cli.py`` pins
both the names and the order, so changing this list is a deliberate act.

Two invariants a change here must not break:

1. ``app.py`` imports nothing else from this package. It holds the objects the
   command modules import, so any import back into it becomes a cycle and
   ``python -m zettel`` dies during module initialisation.
2. Pipeline modules stay imported *inside* the command functions, never at module
   top level. That is what keeps ``zettel --help`` from loading chromadb, docling
   and langchain — several seconds of startup for a help screen.
"""

from __future__ import annotations

from zettel.cli.app import app, console

# isort: off
# Importing a command module runs its @app.command() decorators, so the order of
# these statements is the order of `zettel --help`. Deliberately not alphabetical:
# do not let an import sorter touch this block.
from zettel.cli import maintenance   # init, reindex, rebuild
from zettel.cli import ingest        # harvest, rechunk, dumps, set-paging
from zettel.cli import curation      # extract, review, retry-failed
from zettel.cli import synthesis     # connect, garden
from zettel.cli import purge         # purge-rejected, delete-source
from zettel.cli import manual        # new-note, sync-manual
from zettel.cli import pipeline      # run-all
from zettel.cli import qa            # ask
from zettel.cli import writing       # article
from zettel.cli import export        # skill
from zettel.cli import diagnostics   # status, doctor
# isort: on

#: The command modules, in registration order. Bound to a name so the imports
#: above are real references rather than side-effect-only statements a linter or
#: an "unused import" cleanup would delete — removing one silently drops its
#: commands from the CLI.
COMMAND_MODULES = (
    maintenance, ingest, curation, synthesis, purge,
    manual, pipeline, qa, writing, export, diagnostics,
)

__all__ = ["COMMAND_MODULES", "app", "console", "main"]


def main() -> None:
    """Console entry point, used by ``zettel/__main__.py``."""
    app()
