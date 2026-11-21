"""O SRC: bibliografia da edição (ADR-057) e a ficha da estante, no mesmo arquivo.

Editora, edição, local, tradutor e ano ficam no frontmatter do SRC, junto com
os campos de leitor. ``update_source`` muda a ficha em memória; quem chama grava
com ``save_book`` e leva a bibliografia ao SQLite com ``sync_source_note``.
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


def find_surviving_source(
    vault: Path, *, checksum: str = "", isbn: str = ""
) -> dict[str, Any] | None:
    """O SRC que sobreviveu a um reset, pelo checksum do arquivo ou pelo ISBN.

    Um único ISBN casa. Vários não escolhem ninguém. Checksum vazio não casa
    com a ficha manual, que ainda não tem arquivo.
    """
    from zettel.harvester.biblio_dedupe import normalize_isbn
    from zettel.vault import parse_frontmatter

    folder = Path(vault) / "10_Sources"
    if not folder.is_dir():
        return None
    checksum = (checksum or "").strip()
    wanted_isbn = normalize_isbn(isbn) if isbn else ""
    by_isbn: list[dict[str, Any]] = []
    for path in sorted(folder.glob("*.md")):
        meta, _ = parse_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
        if not isinstance(meta, dict) or meta.get("type") != "source":
            continue
        source_id = str(meta.get("source_id") or "").strip()
        if not source_id:
            continue
        found = {
            "source_id": source_id,
            "citekey": str(meta.get("citekey") or source_id.lstrip("@")),
            "path": path,
        }
        file_checksum = str(meta.get("checksum") or "").strip()
        if checksum and file_checksum == checksum:
            return found
        if wanted_isbn and normalize_isbn(str(meta.get("isbn") or "")) == wanted_isbn:
            by_isbn.append(found)
    return by_isbn[0] if len(by_isbn) == 1 else None


def update_source(
    cfg: Any, db: Any, book: Any, updates: dict[str, Any], *, only_missing: bool = False
) -> list[str]:
    """Põe a edição na ficha em memória. Quem chama grava o arquivo.

    ``only_missing`` só preenche o que a ficha não tem: é o modo da pesquisa,
    que completa sem trocar o que o usuário escreveu. ``cfg`` e ``db`` ficam na
    assinatura porque os chamados já os passam.
    """
    del cfg, db
    written = []
    for key, value in updates.items():
        if only_missing and book.meta.get(key):
            continue
        if value:
            book.meta[key] = value
            written.append(key)
        elif not only_missing and key != "year":
            book.meta.pop(key, None)
    return written
