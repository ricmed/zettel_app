"""Option types shared by more than one command, plus the flag resolvers.

Two kinds of thing live here.

**Annotated option aliases.** ``--config/-c`` appeared in 21 command signatures
and ``--yes/-y`` in 12, each re-typing its own help string — which is how the
texts drifted apart in the first place. An alias is created here only when two or
more commands declare the *same flags with the same help*; where a command gives
the flag a different meaning (``harvest --yes`` also picks the duplicate action,
``garden --yes`` also confirms ``--recreate``), it keeps its own inline
declaration. Collapsing those would delete information the user needs.

**Flag resolvers.** Small pure functions that turn a set of mutually-constraining
flags into the single value the pipeline actually takes. They are pure on purpose:
they are the part of argument handling worth unit-testing, and ``tests/test_cli.py``
covers them directly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

import typer

from zettel.cli.app import console

# ── Shared option aliases ─────────────────────────────────────────────

ConfigOption = Annotated[Optional[str], typer.Option("--config", "-c")]

YesOption = Annotated[bool, typer.Option(
    "--yes", "-y",
    help="Confirmar automaticamente: pre-voo de custo e reprocessamento de embedding",
)]

# harvest / run-all: --yes also selects the config default for suspected duplicates.
NonInteractiveYesOption = Annotated[bool, typer.Option(
    "--yes", "-y",
    help=(
        "Modo nao-interativo: default da config para duplicatas; "
        "confirma reprocessamento se embedding mudou"
    ),
)]

# purge-rejected / delete-source: --yes waives an irreversible-deletion prompt.
ConfirmDeleteYesOption = Annotated[bool, typer.Option(
    "--yes", "-y",
    help="Confirmar exclusao permanente sem prompt",
)]

SkipDuplicatesOption = Annotated[bool, typer.Option(
    "--skip-duplicates",
    help="Modo nao-interativo: sempre pula arquivos com suspeita de duplicidade",
)]

ForceDuplicatesOption = Annotated[bool, typer.Option(
    "--force",
    help="Modo nao-interativo: sempre trata arquivos suspeitos como novas fontes",
)]

SkipBiblioOption = Annotated[bool, typer.Option(
    "--skip-biblio",
    help="Modo nao-interativo: permite seguir com metadados bibliograficos incompletos",
)]

SourceFilterOption = Annotated[Optional[str], typer.Option(
    "--source-id", help="Filtrar por fonte",
)]

DumpChunksOption = Annotated[bool, typer.Option(
    "--dump-chunks",
    help="Salvar markdown com todos os chunks da fonte para inspecao",
)]

ChunkDumpDirOption = Annotated[Optional[str], typer.Option(
    "--dump-dir",
    help="Diretorio do dump de chunks (implica --dump-chunks; default: cache/chunk-dumps)",
)]

DumpSourceIdOption = Annotated[Optional[str], typer.Option(
    "--source-id", help="Exportar apenas esta fonte",
)]

DumpAllOption = Annotated[bool, typer.Option(
    "--all", help="Exportar todas as fontes",
)]

SeedTopkOption = Annotated[Optional[int], typer.Option(
    "--topk", help="Numero de notas semente",
)]

NoGraphOption = Annotated[bool, typer.Option(
    "--no-graph", help="Desliga expansao por grafo",
)]

RetrievalModeOption = Annotated[Optional[str], typer.Option(
    "--mode", help="vector | hybrid",
)]


# ── Flag resolvers ────────────────────────────────────────────────────


def resolve_duplicate_flags(
    yes: bool, skip_duplicates: bool, force: bool,
) -> tuple[bool, Optional[str]]:
    """Translate the duplicate flags into ``(interactive, duplicate_action)``.

    Layer 3 of duplicate detection (semantic similarity, ADR-011) is the only one
    that can be wrong, so it asks the user. These flags answer for them:

    * ``--skip-duplicates`` -> never ingest a suspected duplicate;
    * ``--force`` -> always treat it as a new source;
    * ``--yes`` -> non-interactive, but defer to ``harvest.non_interactive_duplicate_action``
      in the config (signalled by returning ``None`` as the action);
    * nothing -> interactive, prompt per file.

    Raises:
        typer.Exit: ``--skip-duplicates`` and ``--force`` together, which ask for
            opposite outcomes; guessing either way would silently lose files or
            silently duplicate them.
    """
    if skip_duplicates and force:
        console.print("[red]--skip-duplicates e --force sao mutuamente exclusivos.[/red]")
        raise typer.Exit(1)
    if skip_duplicates:
        return False, "skip"
    if force:
        return False, "continue"
    if yes:
        return False, None
    return True, None


def resolve_chunk_dump_dir(
    cfg, dump_chunks: bool, dump_dir: Optional[str],
) -> Optional[Path]:
    """Resolve ``--dump-chunks`` / ``--dump-dir`` to a directory, or None when off.

    An explicit ``--dump-dir`` implies ``--dump-chunks``: asking where to write the
    dump and then not getting one would be a surprise.
    """
    if dump_dir:
        return Path(dump_dir).expanduser().resolve()
    if dump_chunks:
        from zettel.chunk_dump import default_dump_dir
        return default_dump_dir(cfg)
    return None


def resolve_extraction_dump_dir(
    cfg, dump_extraction: bool, dump_extraction_dir: Optional[str],
) -> Optional[Path]:
    """Same contract as ``resolve_chunk_dump_dir``, for the extraction dump."""
    if dump_extraction_dir:
        return Path(dump_extraction_dir).expanduser().resolve()
    if dump_extraction:
        from zettel.extraction_dump import default_dump_dir
        return default_dump_dir(cfg)
    return None
