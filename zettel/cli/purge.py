"""The two irreversible deletion commands.

* ``purge-rejected`` — hard-delete chunks the reviewer rejected;
* ``delete-source``  — remove one source entirely: vault files, SQLite rows, vectors.

Both follow the same shape, and it is the shape any future destructive command
should copy:

1. **Count first.** Open the database, resolve what would be deleted, and print
   the real numbers. A prompt that cannot say how much it is about to destroy is
   not a confirmation.
2. **Name the collateral.** ``delete-source`` says whether permanent notes will be
   deleted or merely unlinked; ``purge-rejected`` says approved notes are untouched.
3. **Confirm**, unless ``--yes``. Default is ``False``: the safe answer wins a
   stray Enter.
4. **Execute, then report per store** — vault, SQLite, Chroma are three separate
   stores with no cross-store transaction (ADR-005), so the summary reports each.

VACUUM runs by default (``--no-compact`` opts out) because these are the only
commands that free significant space: SQLite and Chroma keep deleted pages
allocated, so a purge that reclaims nothing on disk looks like it did not work.
It only rewrites the file — remaining data is untouched — but it can take minutes
on a large store, which is why the prompt warns about it beforehand.
"""

from __future__ import annotations

from typing import Annotated

import typer

from zettel.cli.app import app, console
from zettel.cli.deps import get_db, get_idx, load_deps
from zettel.cli.options import (
    ConfigOption,
    ConfirmDeleteYesOption,
    SourceFilterOption,
)


@app.command(name="purge-rejected")
def purge_rejected_cmd(
    config: ConfigOption = None,
    source_id: SourceFilterOption = None,
    yes: ConfirmDeleteYesOption = False,
    no_compact: Annotated[bool, typer.Option(
        "--no-compact",
        help="Nao rodar VACUUM apos o purge (nao recupera espaco em disco)",
    )] = False,
):
    """Apagar chunks rejeitados do SQLite e do Chroma (irreversivel).

    Remove linhas em chunks/concepts, embeddings em Chroma chunks e
    literature_notes associados. Por padrao compacta state.db e chroma.sqlite3
    com VACUUM (nao altera dados logicos restantes). Nao afeta notas permanentes
    nem LITs aprovadas.
    """
    cfg = load_deps(config)
    db = get_db(cfg)
    pending = db.get_chunks_by_status("rejected", source_id=source_id)
    n = len(pending)
    if n == 0:
        console.print("[yellow]Nenhum chunk rejected para purge.[/yellow]")
        db.close()
        return

    console.print(
        f"[yellow]{n} chunk(s) rejected serao apagados do SQLite e do Chroma.[/yellow]"
    )
    if not no_compact:
        console.print(
            "[dim]Apos a exclusao, VACUUM compactara state.db e chroma.sqlite3 "
            "(seguro para o conteudo restante; pode levar alguns minutos).[/dim]"
        )
    if not yes and not typer.confirm("Excluir permanentemente?", default=False):
        console.print("[dim]Purge cancelado.[/dim]")
        db.close()
        return

    idx = get_idx(cfg, db=db, yes=yes)
    from zettel.review import purge_rejected
    result = purge_rejected(
        cfg, db, idx, source_id=source_id, compact=not no_compact,
    )
    console.print(
        f"[green]Removidos: {result['chunks']} chunks "
        f"({result['literature_notes']} literature_ids no Chroma).[/green]"
    )
    if result.get("compacted"):
        console.print(
            f"[green]Compactado: state.db "
            f"{result['state_mb_before']}→{result['state_mb_after']} MB; "
            f"chroma.sqlite3 "
            f"{result['chroma_mb_before']}→{result['chroma_mb_after']} MB.[/green]"
        )
    db.close()


@app.command(name="delete-source")
def delete_source_cmd(
    source_id: Annotated[str, typer.Argument(help="Fonte a apagar (ex. @Citekey)")],
    config: ConfigOption = None,
    yes: ConfirmDeleteYesOption = False,
    delete_permanent: Annotated[bool, typer.Option(
        "--delete-permanent",
        help="Apagar tambem notas permanentes (ZTL) ligadas a fonte",
    )] = False,
    no_compact: Annotated[bool, typer.Option(
        "--no-compact",
        help="Nao rodar VACUUM apos a exclusao (nao recupera espaco em disco)",
    )] = False,
):
    """Apagar uma fonte por completo do vault, SQLite e Chroma (irreversivel).

    Remove SRC, indice LIT, notas granulares e drafts em Review. Por padrao
    mantem notas permanentes (ZTL) mas limpa wikilinks mortos no restante do
    vault. Use --delete-permanent para remover ZTL ligadas a fonte.
    """
    from zettel.purge_source import normalize_source_id, purge_source

    cfg = load_deps(config)
    db = get_db(cfg)
    sid = normalize_source_id(source_id)
    source = db.get_source(sid)
    if not source:
        console.print(f"[red]Fonte nao encontrada: {sid}[/red]")
        db.close()
        raise typer.Exit(1)

    chunks = db.get_chunks_for_source(sid)
    permanent = db.get_note_ids_for_source(sid)
    console.print(
        f"[yellow]Fonte {sid} ({source.get('title') or source.get('citekey')}):[/yellow] "
        f"{len(chunks)} chunk(s), {len(permanent)} nota(s) permanente(s) ligada(s)."
    )
    if delete_permanent and permanent:
        console.print(
            f"[red]{len(permanent)} nota(s) permanente(s) serao apagadas.[/red]"
        )
    elif permanent:
        console.print(
            "[dim]Notas permanentes serao mantidas; wikilinks mortos serao limpos.[/dim]"
        )
    if not no_compact:
        console.print(
            "[dim]Apos a exclusao, VACUUM compactara state.db e chroma.sqlite3.[/dim]"
        )
    if not yes and not typer.confirm("Excluir fonte permanentemente?", default=False):
        console.print("[dim]Exclusao cancelada.[/dim]")
        db.close()
        return

    idx = get_idx(cfg, db=db, yes=yes)
    result = purge_source(
        cfg, db, idx, sid,
        delete_permanent=delete_permanent,
        compact=not no_compact,
    )
    vault = result["vault"]
    console.print(
        f"[green]Fonte {sid} removida:[/green] "
        f"SRC={vault['src']}, LIT index={vault['lit_index']}, "
        f"granulares={vault['lit_granular']}, drafts={vault['review_drafts']}, "
        f"assets={vault['assets']}."
    )
    sqlite = result["sqlite"]
    console.print(
        f"[green]SQLite:[/green] {sqlite.get('chunks', 0)} chunks, "
        f"{sqlite.get('chapters', 0)} chapters, "
        f"{sqlite.get('concepts', 0)} concepts."
    )
    console.print(
        f"[green]Chroma:[/green] {result['chunks_chroma']} chunks, "
        f"{result['literature_chroma']} literature_notes, 1 source."
    )
    if result["permanent_deleted"]:
        console.print(
            f"[green]Permanentes apagadas:[/green] {result['permanent_deleted']}"
        )
    if result["wikilinks_cleaned"]:
        console.print(
            f"[green]Wikilinks limpos em {result['wikilinks_cleaned']} arquivo(s).[/green]"
        )
    if result.get("compacted"):
        console.print(
            f"[green]Compactado: state.db "
            f"{result['state_mb_before']}->{result['state_mb_after']} MB; "
            f"chroma.sqlite3 "
            f"{result['chroma_mb_before']}->{result['chroma_mb_after']} MB.[/green]"
        )
    db.close()
