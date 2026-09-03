"""The two CLI singletons: the Typer application and the Rich console.

This module exists to break an import cycle, and it is the reason the package
does not simply create ``app`` in ``__init__.py``.

Every command module needs the ``app`` object to register itself against
(``@app.command()``), so every command module imports from here. If ``app`` lived
in ``__init__.py`` — which must import the command modules to trigger that
registration — the two would import each other and ``python -m zettel`` would die
on a partially-initialised module.

The rule that keeps this working: **this module imports nothing from the rest of
the package.** Adding ``from zettel.cli.ingest import ...`` here closes the cycle.
It also imports no pipeline module, so ``zettel --help`` does not pay for
chromadb/docling/langchain.
"""

from __future__ import annotations

import typer
from rich.console import Console

app = typer.Typer(
    name="zettel",
    help="Zettelkasten — Pipeline automatizado de geração de notas",
    add_completion=False,
)

# One console for the whole CLI: Rich tracks terminal width and live-display
# ownership per instance, and two consoles writing to the same stdout produce
# interleaved output (the bug behind the "spinner + progress bar flicker" notes
# in curation.py and synthesis.py).
console = Console()
