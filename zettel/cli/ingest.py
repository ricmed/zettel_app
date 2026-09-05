"""Phase 1 commands: getting files into the pipeline, and repairing that work.

These five commands all operate on the *source* side of the vault — before any
LLM has read a chunk:

* ``harvest``          — the ingestion itself: scan the inbox, extract, chunk;
* ``rechunk``          — re-split an already-extracted source under the current
                         config, without touching the original file or the LLM;
* ``set-paging``       — fix the printed-page offset of a harvested source;
* ``dump-chunks``      — export the persisted chunks for inspection;
* ``dump-extraction``  — export the extracted Markdown for inspection.

The last three exist because harvest's decisions (how a document was split, which
printed page a chunk claims to be on) are expensive to redo and easy to get
subtly wrong, so each is separately inspectable and separately repairable.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from zettel.cli.app import app, console
from zettel.cli.deps import get_db, get_idx, load_deps
from zettel.cli.options import (
    ChunkDumpDirOption,
    ConfigOption,
    DumpAllOption,
    DumpChunksOption,
    DumpSourceIdOption,
    ForceDuplicatesOption,
    NonInteractiveYesOption,
    SkipBiblioOption,
    SkipDuplicatesOption,
    YesOption,
    resolve_chunk_dump_dir,
    resolve_duplicate_flags,
    resolve_extraction_dump_dir,
)


@app.command()
def harvest(
    config: ConfigOption = None,
    yes: NonInteractiveYesOption = False,
    skip_duplicates: SkipDuplicatesOption = False,
    force: ForceDuplicatesOption = False,
    skip_biblio: SkipBiblioOption = False,
    content_start_file: Annotated[
        int | None,
        typer.Option(
            "--content-start-file",
            help="Pagina do arquivo (PDF) onde o conteudo comeca (1-based)",
        ),
    ] = None,
    content_start_book: Annotated[
        int | None,
        typer.Option(
            "--content-start-book",
            help="Numero impresso nessa primeira pagina de conteudo (default 1)",
        ),
    ] = None,
    skip_paging: Annotated[
        bool,
        typer.Option(
            "--skip-paging",
            help="Nao detectar paginacao; arquivo p.1 = impressa p.1 (ignora heuristica)",
        ),
    ] = False,
    dump_chunks: DumpChunksOption = False,
    dump_dir: ChunkDumpDirOption = None,
    dump_extraction: Annotated[
        bool,
        typer.Option(
            "--dump-extraction",
            help="Salvar Markdown extraido (Docling/MD, headings H1-H6) para inspecao",
        ),
    ] = False,
    dump_extraction_dir: Annotated[
        str | None,
        typer.Option(
            "--dump-extraction-dir",
            help=(
                "Diretorio do dump de extracao (implica --dump-extraction; "
                "default: cache/extraction-dumps)"
            ),
        ),
    ] = None,
):
    """Escanear inbox, extrair texto, criar SRC + indice LIT e chunks."""
    cfg = load_deps(config)
    db = get_db(cfg)
    idx = get_idx(cfg, db=db, yes=yes)

    interactive, duplicate_action = resolve_duplicate_flags(yes, skip_duplicates, force)
    chunk_dump_dir = resolve_chunk_dump_dir(cfg, dump_chunks, dump_dir)
    extraction_dump_dir = resolve_extraction_dump_dir(
        cfg,
        dump_extraction,
        dump_extraction_dir,
    )

    from zettel.harvester import run_harvest

    if interactive:
        # Nao usar console.status aqui: prompts interativos (bibliografia / duplicatas)
        # precisam do terminal livre; o spinner engole o Prompt.ask e parece travado.
        console.print(
            "[dim]Coletando arquivos do inbox "
            "(pode solicitar metadados bibliograficos / inicio de paginacao)...[/dim]"
        )
        outcome = run_harvest(
            cfg,
            db,
            idx,
            interactive=True,
            skip_biblio=skip_biblio,
            content_start_file=content_start_file,
            content_start_book=content_start_book,
            skip_paging=skip_paging,
            dump_dir=chunk_dump_dir,
            extraction_dump_dir=extraction_dump_dir,
        )
    else:
        console.print(
            f"[dim]Modo nao-interativo — duplicatas suspeitas: '{duplicate_action}'[/dim]"
        )
        if skip_biblio:
            console.print("[dim]Bibliografia incompleta permitida (--skip-biblio)[/dim]")
        outcome = run_harvest(
            cfg,
            db,
            idx,
            interactive=False,
            duplicate_action=duplicate_action,
            skip_biblio=skip_biblio,
            content_start_file=content_start_file,
            content_start_book=content_start_book,
            skip_paging=skip_paging,
            dump_dir=chunk_dump_dir,
            extraction_dump_dir=extraction_dump_dir,
        )

    new_sources = outcome.source_ids
    if new_sources:
        console.print(f"[green]Fontes processadas: {len(new_sources)}[/green]")
        for sid in new_sources:
            console.print(f"  - {sid}")
        if chunk_dump_dir:
            console.print(f"[dim]Dump de chunks gravado em: {chunk_dump_dir}[/dim]")
        if extraction_dump_dir:
            console.print(f"[dim]Dump de extracao gravado em: {extraction_dump_dir}[/dim]")
    else:
        console.print("[yellow]Nenhum arquivo novo encontrado no inbox.[/yellow]")

    last_run = db.get_last_run()
    if last_run:
        dup_total = (
            last_run.get("duplicate_file_count", 0)
            + last_run.get("duplicate_content_count", 0)
            + last_run.get("duplicate_semantic_count", 0)
        )
        if dup_total:
            console.print(
                f"[yellow]Duplicatas detectadas nesta execucao: {dup_total}[/yellow] "
                f"(arquivo: {last_run.get('duplicate_file_count', 0)}, "
                f"conteudo: {last_run.get('duplicate_content_count', 0)}, "
                f"semantica: {last_run.get('duplicate_semantic_count', 0)})"
            )

    db.close()
    if outcome.skipped:
        console.print(f"[red]Arquivos ignorados: {len(outcome.skipped)}[/red]")
        for skip in outcome.skipped:
            console.print(f"  - {skip.path.name} ({skip.reason}): {skip.message}")
        raise typer.Exit(1)


@app.command()
def rechunk(
    config: ConfigOption = None,
    source_id: Annotated[
        str | None,
        typer.Option(
            "--source-id",
            help="Rechunk apenas esta fonte",
        ),
    ] = None,
    all_sources: Annotated[
        bool,
        typer.Option(
            "--all",
            help="Rechunk de todas as fontes",
        ),
    ] = False,
    yes: YesOption = False,
    dump_chunks: DumpChunksOption = False,
    dump_dir: ChunkDumpDirOption = None,
):
    """Re-chunkar fontes a partir do texto extraido persistido (aplica config atual)."""
    if not source_id and not all_sources:
        console.print("[red]Informe --source-id <id> ou --all.[/red]")
        raise typer.Exit(1)

    cfg = load_deps(config)
    db = get_db(cfg)
    idx = get_idx(cfg, db=db, yes=yes)
    chunk_dump_dir = resolve_chunk_dump_dir(cfg, dump_chunks, dump_dir)

    from zettel.harvester import run_rechunk

    with console.status("[bold blue]Re-chunkando fontes...", spinner="dots"):
        stats = run_rechunk(
            cfg,
            db,
            idx,
            source_id if source_id else None,
            dump_dir=chunk_dump_dir,
        )

    console.print(
        f"[green]Rechunk concluido:[/green] {stats['sources']} fonte(s), "
        f"{stats['chunks']} chunk(s), {stats['skipped']} pulada(s)."
    )
    if chunk_dump_dir and stats["sources"]:
        console.print(f"[dim]Dump de chunks gravado em: {chunk_dump_dir}[/dim]")
    if stats["skipped"]:
        console.print(
            "[yellow]Fontes puladas nao tem texto extraido persistido (anteriores a Fase 0). "
            "Reprocesse o arquivo original via harvest.[/yellow]"
        )
    db.close()


@app.command(name="dump-chunks")
def dump_chunks_cmd(
    source_id: DumpSourceIdOption = None,
    all_sources: DumpAllOption = False,
    dump_dir: Annotated[
        str | None,
        typer.Option(
            "--dump-dir",
            help="Diretorio de saida (default: cache/chunk-dumps)",
        ),
    ] = None,
    config: ConfigOption = None,
):
    """Exportar chunks persistidos como markdown para inspecionar o chunking."""
    if not source_id and not all_sources:
        console.print("[red]Informe --source-id <id> ou --all.[/red]")
        raise typer.Exit(1)

    cfg = load_deps(config)
    db = get_db(cfg)
    dest = Path(dump_dir).expanduser().resolve() if dump_dir else None

    from zettel.chunk_dump import default_dump_dir, run_dump_chunks

    dest = dest or default_dump_dir(cfg)
    try:
        stats = run_dump_chunks(
            cfg,
            db,
            source_id if source_id else None,
            dump_dir=dest,
        )
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        db.close()
        raise typer.Exit(1)

    console.print(f"[green]Dump concluido:[/green] {stats['sources']} fonte(s) em {dest}")
    db.close()


@app.command(name="dump-extraction")
def dump_extraction_cmd(
    source_id: DumpSourceIdOption = None,
    all_sources: DumpAllOption = False,
    dump_dir: Annotated[
        str | None,
        typer.Option(
            "--dump-dir",
            help="Diretorio de saida (default: cache/extraction-dumps)",
        ),
    ] = None,
    config: ConfigOption = None,
):
    """Exportar o Markdown extraido (Docling/MD) para inspecionar headings H1-H6."""
    if not source_id and not all_sources:
        console.print("[red]Informe --source-id <id> ou --all.[/red]")
        raise typer.Exit(1)

    cfg = load_deps(config)
    db = get_db(cfg)
    dest = Path(dump_dir).expanduser().resolve() if dump_dir else None

    from zettel.extraction_dump import default_dump_dir, run_dump_extraction

    dest = dest or default_dump_dir(cfg)
    try:
        stats = run_dump_extraction(
            cfg,
            db,
            source_id if source_id else None,
            dump_dir=dest,
        )
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        db.close()
        raise typer.Exit(1)

    console.print(f"[green]Dump concluido:[/green] {stats['sources']} fonte(s) em {dest}")
    if stats.get("skipped"):
        console.print(
            f"[yellow]{stats['skipped']} fonte(s) pulada(s) sem texto extraido persistido.[/yellow]"
        )
    db.close()


@app.command(name="set-paging")
def set_paging_cmd(
    source_id: Annotated[
        str,
        typer.Option(
            "--source-id",
            help="Fonte a corrigir (ex. @Citekey)",
        ),
    ],
    content_start_file: Annotated[
        int,
        typer.Option(
            "--content-start-file",
            help="Pagina do arquivo (PDF) onde o conteudo comeca (1-based)",
        ),
    ],
    content_start_book: Annotated[
        int,
        typer.Option(
            "--content-start-book",
            help="Numero impresso nessa primeira pagina de conteudo",
        ),
    ] = 1,
    drop_before_start: Annotated[
        bool,
        typer.Option(
            "--drop-before-start",
            help="Tambem remove chunks awaiting_review/aprovados antes do inicio",
        ),
    ] = False,
    config: ConfigOption = None,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            "-y",
            help="Confirmar automaticamente reprocessamento de embedding se necessario",
        ),
    ] = False,
):
    """Corrigir paginacao de uma fonte ja harvestada (sem re-chamar o LLM)."""
    cfg = load_deps(config)
    db = get_db(cfg)
    idx = get_idx(cfg, db=db, yes=yes)

    from zettel.harvester import run_set_paging

    try:
        stats = run_set_paging(
            cfg,
            db,
            idx,
            source_id,
            content_start_file=content_start_file,
            content_start_book=content_start_book,
            drop_before_start=drop_before_start,
        )
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)

    console.print(
        f"[green]Paginacao atualizada para {source_id}:[/green] "
        f"arquivo p.{content_start_file} = impressa p.{content_start_book}\n"
        f"  chunks atualizados: {stats['updated']}\n"
        f"  pending removidos (antes do inicio): {stats['dropped_pending']}\n"
        f"  outros removidos (--drop-before-start): {stats['dropped_other']}\n"
        f"  notas LIT patchadas: {stats['notes_patched']}"
    )
    db.close()
