"""Hand-written notes: scaffolding them, and adopting them into the stores.

* ``new-note``    — write a note skeleton into the vault with ``origin: manual``;
* ``sync-manual`` — scan the vault for manual/hand-edited notes and index them.

The two are two halves of one flow, and the split between them is deliberate
(ADR-030): ``new-note`` touches **only** the filesystem, so a half-written note
never pollutes SQLite or Chroma; ``sync-manual`` is the single adoption point that
assigns IDs, computes checksums, indexes, derives graph edges from body wikilinks
and adopts referenced images.

The one exception is ``new-note ztl --from-lit``, which does reach the stores —
turning an existing literature note into a permanent one is a pipeline operation
wearing a scaffolding command's clothes, and with ``--llm`` it runs the real
Prompt 2 path. It is routed out to ``manual_lit.create_permanent_from_literature``
rather than to the scaffolder.
"""

from __future__ import annotations

from typing import Annotated

import typer

from zettel.cli.app import app, console
from zettel.cli.deps import get_db, get_idx, load_deps
from zettel.cli.formatting import metrics_table
from zettel.cli.options import ConfigOption, YesOption


def _new_note_from_literature(
    cfg,
    ref: str,
    *,
    use_llm: bool,
    thesis: str | None,
    force: bool,
) -> None:
    """Create a permanent note out of a literature note (LLM or hand-written).

    ``ref`` is either a path to the LIT ``.md`` or a ``chunk_id``. Every failure
    mode of the underlying call is a user error worth showing plainly (missing
    note, target already exists, unparseable note, LLM failure), so they collapse
    into one red line and exit 1 instead of a traceback.
    """
    from zettel.manual_lit import create_permanent_from_literature

    db = get_db(cfg)
    idx = get_idx(cfg, db=db)
    try:
        path, via_llm = create_permanent_from_literature(
            cfg,
            db,
            idx,
            ref,
            use_llm=use_llm,
            thesis=thesis,
            force=force,
        )
    except (FileNotFoundError, FileExistsError, ValueError, RuntimeError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    finally:
        db.close()

    console.print(f"[green]Nota permanente criada:[/green] {path}")
    if via_llm:
        console.print("[dim]Gerada com LLM e ja indexada (conexoes e backlinks aplicados).[/dim]")
    else:
        console.print("[dim]Preencha a nota e indexe com: zettel sync-manual[/dim]")


@app.command(name="new-note")
def new_note(
    note_type: Annotated[
        str,
        typer.Argument(
            help="Tipo: ztl|lit|src|moc (ou permanent|literature|source)",
        ),
    ],
    title: Annotated[
        str,
        typer.Argument(
            help="Titulo da nota (dispensavel com --from-lit: vem da tese)",
        ),
    ] = "",
    config: ConfigOption = None,
    citekey: Annotated[
        str | None,
        typer.Option(
            "--citekey",
            "-k",
            help="Citekey para SRC/LIT (sem @); alias de --source-id para SRC/ZTL",
        ),
    ] = None,
    source_id: Annotated[
        str | None,
        typer.Option(
            "--source-id",
            "-s",
            help="source_id (@Citekey) explicito para SRC ou vinculo de ZTL a uma SRC",
        ),
    ] = None,
    author: Annotated[
        list[str] | None,
        typer.Option(
            "--author",
            "-a",
            help="Autor(es) para SRC/LIT (repita a opcao para varios)",
        ),
    ] = None,
    year: Annotated[
        int | None,
        typer.Option(
            "--year",
            "-y",
            help="Ano para SRC/LIT",
        ),
    ] = None,
    document_type: Annotated[
        str | None,
        typer.Option(
            "--document-type",
            "-t",
            help="Tipo documental ABNT para SRC (ex.: livro, artigo_periodico)",
        ),
    ] = None,
    abnt_reference: Annotated[
        str | None,
        typer.Option(
            "--abnt-reference",
            help="Referencia ABNT pronta para copiar (SRC)",
        ),
    ] = None,
    publisher: Annotated[
        str | None,
        typer.Option(
            "--publisher",
            help="Editora (SRC)",
        ),
    ] = None,
    place: Annotated[
        str | None,
        typer.Option(
            "--place",
            help="Local de publicacao (SRC)",
        ),
    ] = None,
    doi: Annotated[str | None, typer.Option("--doi", help="DOI (SRC)")] = None,
    url: Annotated[str | None, typer.Option("--url", help="URL (SRC)")] = None,
    journal: Annotated[
        str | None,
        typer.Option(
            "--journal",
            help="Periodico (SRC)",
        ),
    ] = None,
    edition: Annotated[
        str | None,
        typer.Option(
            "--edition",
            help="Edicao (SRC)",
        ),
    ] = None,
    institution: Annotated[
        str | None,
        typer.Option(
            "--institution",
            help="Instituicao (SRC)",
        ),
    ] = None,
    pages: Annotated[
        str | None,
        typer.Option(
            "--pages",
            help="Paginas (SRC)",
        ),
    ] = None,
    granular: Annotated[
        bool,
        typer.Option(
            "--granular",
            help="LIT granular em 20_Literature/{citekey}/ (padrao: indice na raiz)",
        ),
    ] = False,
    chunk_index: Annotated[
        int,
        typer.Option(
            "--chunk-index",
            help="Indice do chunk para LIT granular (padrao: 1)",
        ),
    ] = 1,
    page: Annotated[
        int | None,
        typer.Option(
            "--page",
            "-p",
            help="Pagina impressa para LIT granular",
        ),
    ] = None,
    from_lit: Annotated[
        str | None,
        typer.Option(
            "--from-lit",
            help="ZTL a partir de uma nota de literatura (caminho do .md ou chunk_id)",
        ),
    ] = None,
    use_llm: Annotated[
        bool,
        typer.Option(
            "--llm",
            help="Com --from-lit: gerar o conteudo da ZTL com o LLM (Prompt 2 + RAG)",
        ),
    ] = False,
    thesis: Annotated[
        str | None,
        typer.Option(
            "--thesis",
            help="Com --from-lit: tese explicita (padrao: deduzida da nota de literatura)",
        ),
    ] = None,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Sobrescrever arquivo existente no mesmo caminho",
        ),
    ] = False,
):
    """Criar esqueleto de nota manual no vault (indexar depois com sync-manual)."""
    cfg = load_deps(config)

    from zettel.new_note import normalize_note_type, scaffold_manual_note

    try:
        normalized = normalize_note_type(note_type)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    if from_lit:
        if normalized != "permanent":
            console.print("[red]--from-lit so vale para notas permanentes (ztl).[/red]")
            raise typer.Exit(1)
        _new_note_from_literature(
            cfg,
            from_lit,
            use_llm=use_llm,
            thesis=thesis,
            force=force,
        )
        return
    if use_llm or thesis:
        console.print("[red]--llm e --thesis exigem --from-lit.[/red]")
        raise typer.Exit(1)
    if not title.strip():
        console.print("[red]Informe o titulo da nota (ou use --from-lit).[/red]")
        raise typer.Exit(1)

    # --citekey doubles as --source-id for the note types that link to a source,
    # so `new-note src "Titulo" -k Autor2020` does not need both flags.
    effective_source_id = source_id
    if not effective_source_id and citekey and normalized in ("permanent", "source", "literature"):
        effective_source_id = citekey

    try:
        result = scaffold_manual_note(
            cfg,
            note_type,
            title,
            citekey=citekey,
            source_id=effective_source_id,
            authors=list(author or []),
            year=year,
            document_type=document_type,
            abnt_reference=abnt_reference,
            place=place,
            publisher=publisher,
            doi=doi,
            url=url,
            journal=journal,
            edition=edition,
            institution=institution,
            pages=pages,
            granular=granular,
            chunk_index=chunk_index,
            page=page,
            force=force,
        )
    except FileExistsError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    console.print(f"[green]Nota criada:[/green] {result.path}")
    if result.warnings:
        for warning in result.warnings:
            console.print(f"[yellow]Aviso:[/yellow] {warning}")
    console.print("[dim]Indexe com: zettel sync-manual[/dim]")


@app.command(name="sync-manual")
def sync_manual(
    config: ConfigOption = None,
    rebuild_graph: Annotated[
        bool,
        typer.Option(
            "--rebuild-graph",
            help="Re-deriva arestas 'related' dos wikilinks no corpo de todas as notas",
        ),
    ] = False,
    yes: YesOption = False,
):
    """Sincronizar notas manuais do vault com o índice vetorial."""
    cfg = load_deps(config)
    db = get_db(cfg)
    idx = get_idx(cfg, db=db, yes=yes)

    from zettel.sync import rebuild_manual_edges, run_sync_manual

    # Runs before the scan: the backfill reads note bodies already in SQLite, and
    # the scan below can then suggest connections against a complete graph.
    if rebuild_graph:
        with console.status("[bold blue]Reconstruindo grafo de conexoes...", spinner="dots"):
            gstats = rebuild_manual_edges(db)
        console.print(
            f"[green]Grafo:[/green] {gstats['edges_created']} aresta(s) nova(s) "
            f"de {gstats['notes_scanned']} nota(s) com corpo."
        )

    with console.status("[bold blue]Sincronizando notas manuais...", spinner="dots"):
        stats = run_sync_manual(cfg, db, idx)

    metrics_table("Sync Manual", stats, key_label="Métrica", capitalize_keys=True)

    db.close()
