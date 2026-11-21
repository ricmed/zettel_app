"""Encrypted backup and restore of the vault, the database and the project ``.env``.

Both commands take the operation lock through :func:`zettel.cli.deps.load_deps`.
The passphrase comes from ``--passphrase-file`` or from the terminal. There is
no option that puts it on the command line.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

import typer

from zettel.cli.app import app, console
from zettel.cli.deps import get_db, load_deps
from zettel.cli.options import ConfigOption

_REINDEX_HINT = "O backup nao inclui o Chroma. Rode `zettel reindex` para reconstruir o indice."
_CRYPTO_FAILURE = "senha incorreta ou arquivo danificado"


def _stdin_is_tty() -> bool:
    isatty = getattr(sys.stdin, "isatty", None)
    if not callable(isatty):
        return False
    try:
        return bool(isatty())
    except OSError:
        return False


def _passphrase(passphrase_file: Path | None) -> str:
    import getpass

    from zettel.backup.crypto import BackupCryptoError, read_passphrase_file

    if passphrase_file is not None:
        try:
            return read_passphrase_file(passphrase_file)
        except BackupCryptoError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc
    if not _stdin_is_tty():
        console.print("[red]Sem terminal para ler a senha. Use --passphrase-file.[/red]")
        raise typer.Exit(2)
    phrase = getpass.getpass("Senha do backup: ")
    if phrase == "":
        console.print("[red]A senha esta vazia.[/red]")
        raise typer.Exit(1)
    return phrase


@app.command()
def backup(
    path: Annotated[Path, typer.Argument(help="Arquivo .zettelbak de saida")],
    config: ConfigOption = None,
    passphrase_file: Annotated[
        Path | None,
        typer.Option(
            "--passphrase-file",
            help="Arquivo com a senha (modo 0600). Sem ele, a senha e pedida no terminal.",
        ),
    ] = None,
    no_chroma: Annotated[
        bool,
        typer.Option("--no-chroma", help="Nao incluir o diretorio Chroma."),
    ] = False,
    with_inbox: Annotated[
        bool,
        typer.Option("--with-inbox", help="Incluir o inbox alem do vault."),
    ] = False,
):
    """Gravar um arquivo .zettelbak cifrado do acervo."""
    from zettel.backup.archive import BackupArchiveError, create_backup
    from zettel.backup.crypto import BackupCryptoError
    from zettel.config import env_file_path

    cfg = load_deps(config)
    phrase = _passphrase(passphrase_file)
    db = get_db(cfg)
    try:
        try:
            result = create_backup(
                cfg,
                db,
                path,
                phrase,
                env_path=env_file_path(config),
                include_chroma=not no_chroma,
                include_inbox=with_inbox,
            )
        except BackupArchiveError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc
        except BackupCryptoError:
            console.print("[red]Nao foi possivel cifrar o backup.[/red]")
            raise typer.Exit(1) from None
    finally:
        db.close()
    console.print(f"[green]Backup gravado:[/green] {result.path}")


@app.command()
def restore(
    path: Annotated[Path, typer.Argument(help="Arquivo .zettelbak a restaurar")],
    config: ConfigOption = None,
    passphrase_file: Annotated[
        Path | None,
        typer.Option(
            "--passphrase-file",
            help="Arquivo com a senha (modo 0600). Sem ele, a senha e pedida no terminal.",
        ),
    ] = None,
    force_env: Annotated[
        bool,
        typer.Option("--force-env", help="Substituir o .env existente pelo do backup."),
    ] = False,
    rotate_session: Annotated[
        bool,
        typer.Option(
            "--rotate-session",
            help="Trocar a chave de sessao web depois de restaurar o banco.",
        ),
    ] = False,
):
    """Restaurar um arquivo .zettelbak cifrado neste acervo."""
    from zettel.backup.archive import BackupArchiveError, restore_backup
    from zettel.backup.crypto import BackupCryptoError
    from zettel.config import env_file_path

    cfg = load_deps(config)
    phrase = _passphrase(passphrase_file)
    try:
        result = restore_backup(
            cfg,
            path,
            phrase,
            env_path=env_file_path(config),
            force_env=force_env,
            rotate_session=rotate_session,
        )
    except BackupArchiveError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    except BackupCryptoError:
        console.print(f"[red]{_CRYPTO_FAILURE}[/red]")
        raise typer.Exit(1) from None
    console.print("[green]Backup restaurado.[/green]")
    if not result.include_chroma:
        console.print(_REINDEX_HINT)
    if result.env_kept:
        console.print(
            "[yellow]O .env existente foi mantido. Use --force-env para substitui-lo.[/yellow]"
        )
