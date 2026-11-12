"""O SRC de um livro: a bibliografia da edição (ADR-057) e a cópia do código da LIV.

A LIV guarda a obra; editora, edição, local, tradutor e ano desta edição ficam
na nota SRC, criada como fonte manual quando o livro ainda não tem uma.

O código (``0001 - F``) é alocado só na LIV, uma sequência para toda a Estante.
O SRC de um livro carrega uma cópia em ``code``, posta em dia por
``sync_source_codes``; um SRC sem LIV não tem código.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

SOURCE_KEYS = ("publisher", "edition", "place", "translator", "isbn")


def source_note_path(cfg: Any, source_id: str) -> Path | None:
    """A nota SRC de ``source_id``: o nome previsto pelo citekey, senão uma busca no frontmatter."""
    from zettel.vault import author_year_label, parse_frontmatter

    folder = Path(cfg.vault_path) / "10_Sources"
    if not folder.is_dir():
        return None
    predicted = sorted(folder.glob(f"SRC - {author_year_label(source_id)} - *.md"))
    for path in predicted + sorted(folder.glob("*.md")):
        meta, _ = parse_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
        if meta.get("type") == "source" and meta.get("source_id") == source_id:
            return path
    return None


def source_note_paths(cfg: Any) -> dict[str, Path]:
    """Toda nota SRC do vault por ``source_id``, numa leitura de ``10_Sources/``."""
    from zettel.vault import parse_frontmatter

    folder = Path(cfg.vault_path) / "10_Sources"
    paths: dict[str, Path] = {}
    for path in sorted(folder.glob("*.md")) if folder.is_dir() else []:
        meta, _ = parse_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
        source_id = meta.get("source_id")
        if meta.get("type") == "source" and source_id:
            paths.setdefault(str(source_id), path)
    return paths


def _write_code(path: Path, code: str | None) -> bool:
    """Põe (ou tira) ``code`` no frontmatter do SRC, logo depois de ``source_id``."""
    from zettel.vault import compose_note, parse_frontmatter

    meta, body = parse_frontmatter(path.read_text(encoding="utf-8"))
    if (meta.get("code") or None) == code:
        return False
    meta.pop("code", None)
    out: dict[str, Any] = {}
    for key, value in meta.items():
        out[key] = value
        if key == "source_id" and code:
            out["code"] = code
    path.write_text(compose_note(out, body), encoding="utf-8")
    return True


def sync_source_code(cfg: Any, db: Any, book: Any, previous_source_id: str | None = None) -> None:
    """O código desta LIV no SRC ligado a ela (depois de criar, ligar ou mudar o formato).

    ``previous_source_id`` é a fonte de antes da edição: desligada, perde o código.
    """
    if previous_source_id and previous_source_id != book.source_id:
        old = source_note_path(cfg, previous_source_id)
        if old is not None:
            _write_code(old, None)
    if not book.source_id or not db.get_source(book.source_id):
        return
    path = source_note_path(cfg, book.source_id)
    if path is not None:
        _write_code(path, book.code or None)


def sync_source_codes(cfg: Any, db: Any) -> int:
    """Cada SRC com o código da sua LIV, e sem código quando não tem LIV; devolve as gravações."""
    # Duas LIV na mesma fonte (doctor avisa): vale a de menor número, como em get_book_by_source.
    rows = sorted(db.list_book_identities(), key=lambda r: r.get("code_number") or 0, reverse=True)
    codes = {r["source_id"]: r.get("code") or None for r in rows if r.get("source_id")}
    written = 0
    for source_id, path in source_note_paths(cfg).items():
        written += _write_code(path, codes.get(source_id))
    return written


def update_source(
    cfg: Any, db: Any, book: Any, updates: dict[str, Any], *, only_missing: bool = False
) -> list[str]:
    """Grava a edição no SRC, criando o SRC manual se faltar; devolve as chaves gravadas.

    ``only_missing`` só preenche o que o SRC não tem: é o modo da pesquisa, que
    completa sem nunca trocar o que o usuário escreveu.
    """
    from zettel.library.editing import BookEditError
    from zettel.library.reading_notes import ensure_source
    from zettel.sync import sync_source_note
    from zettel.vault import compose_note, parse_frontmatter

    source_id = ensure_source(cfg, db, book)
    path = source_note_path(cfg, source_id)
    if path is None:
        raise BookEditError("A nota SRC desta fonte não está no vault.")
    meta, body = parse_frontmatter(path.read_text(encoding="utf-8"))
    written = []
    for key, value in updates.items():
        if only_missing and meta.get(key):
            continue
        if value:
            meta[key] = value
            written.append(key)
        elif not only_missing and key != "year":
            meta.pop(key, None)
    path.write_text(compose_note(meta, body), encoding="utf-8")
    sync_source_note(cfg, db, path)
    return written
