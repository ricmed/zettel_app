"""Estante <-> fontes: o harvest cria o livro, um reset não perde a ligação.

A LIV guarda ``source_id`` mais a identidade mínima do livro (título, autores,
ISBN). Quando o ``source_id`` deixa de existir (``init --reset`` e um novo
harvest geram outro citekey, por exemplo), a LIV é religada pelo ISBN e depois
por título + sobrenome. Com mais de um candidato, nada é escolhido: o livro
aparece como "sem fonte", com os candidatos.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from zettel.library.book_file import (
    BOOK_NOTES_BLOCK,
    Book,
    create_book,
    load_book,
    read_book,
    save_book,
)
from zettel.library.mirror import refresh

logger = logging.getLogger(__name__)


@dataclass
class LibrarySyncReport:
    created: list[str] = field(default_factory=list)
    linked: list[str] = field(default_factory=list)
    relinked: list[str] = field(default_factory=list)
    # book_id -> source_ids candidatos quando a religação é ambígua.
    ambiguous: dict[str, list[str]] = field(default_factory=dict)
    duplicate_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "books_created": len(self.created),
            "books_linked": len(self.linked) + len(self.relinked),
            "books_ambiguous": len(self.ambiguous),
        }


def _source_authors(source: dict[str, Any]) -> list[str]:
    try:
        parsed = json.loads(source.get("authors") or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    return [str(a) for a in parsed] if isinstance(parsed, list) else []


def same_work(book: dict[str, Any], source: dict[str, Any]) -> bool:
    """Mesmo título principal e ao menos um sobrenome em comum."""
    from zettel.harvester.biblio_dedupe import author_surnames, normalize_title

    title = normalize_title(book.get("title"))
    if not title or title != normalize_title(source.get("title")):
        return False
    book_authors = book.get("authors")
    if book_authors is None:
        book_authors = json.loads(book.get("authors_json") or "[]")
    return bool(author_surnames(book_authors) & author_surnames(_source_authors(source)))


def source_candidates(db: Any, book: dict[str, Any]) -> list[str]:
    """Fontes que podem ser o livro: o ISBN decide; sem ele, título + sobrenome."""
    from zettel.harvester.biblio_dedupe import normalize_isbn

    isbn = normalize_isbn(book.get("isbn"))
    if isbn:
        row = db.get_source_by_isbn(isbn)
        if row:
            return [row["source_id"]]
    return [s["source_id"] for s in db.list_sources_with_authors() if same_work(book, s)]


def book_for_source(db: Any, source: dict[str, Any]) -> str | None:
    """A LIV que já é (ou parece ser) esta fonte, pelo espelho."""
    from zettel.harvester.biblio_dedupe import normalize_isbn

    identities = db.list_book_identities()
    for row in identities:
        if row.get("source_id") == source["source_id"]:
            return row["book_id"]
    isbn = normalize_isbn(source.get("isbn"))
    free = [r for r in identities if not r.get("source_id") or not db.get_source(r["source_id"])]
    if isbn:
        for row in free:
            if normalize_isbn(row.get("isbn")) == isbn:
                return row["book_id"]
    matches = [r["book_id"] for r in free if same_work(r, source)]
    return matches[0] if len(matches) == 1 else None


def _set_source(cfg: Any, book: Book, source_id: str) -> None:
    book.meta["source_id"] = source_id
    save_book(cfg, book, expected_version=book.version)


def fields_from_source(source: dict[str, Any]) -> dict[str, Any]:
    """O que a LIV copia da fonte ao nascer: identidade, não a bibliografia toda."""
    biblio: dict[str, Any] = {}
    try:
        biblio = json.loads(source.get("bibliography_json") or "{}") or {}
    except (json.JSONDecodeError, TypeError):
        biblio = {}
    pages = source.get("total_pages_book") or biblio.get("pages")
    return {
        "title": source.get("title") or source.get("citekey") or "",
        "authors": _source_authors(source),
        "source_id": source["source_id"],
        "isbn": source.get("isbn") or None,
        "pages": int(pages) if str(pages or "").isdigit() else None,
        "format": "digital",
    }


def ensure_books_for_sources(cfg: Any, db: Any, source_ids: list[str]) -> LibrarySyncReport:
    """Cria (ou liga) a LIV de cada fonte nova cujo tipo de documento é um livro."""
    report = LibrarySyncReport()
    wanted = set(cfg.library.document_types)
    for source_id in dict.fromkeys(source_ids):
        source = db.get_source(source_id)
        if not source or (source.get("document_type") or "") not in wanted:
            continue
        existing = book_for_source(db, source)
        if existing:
            book = load_book(cfg, existing)
            if book.source_id != source_id:
                _set_source(cfg, book, source_id)
                report.linked.append(book.book_id)
            continue
        book = create_book(cfg, fields_from_source(source))
        report.created.append(book.book_id)
        refresh(db, cfg)
    return report


def relink_orphans(cfg: Any, db: Any, report: LibrarySyncReport) -> None:
    """Religa a LIV cujo ``source_id`` não resolve mais (depois de um reset)."""
    for row in db.list_book_identities():
        source_id = row.get("source_id")
        if not source_id or db.get_source(source_id):
            continue
        candidates = source_candidates(db, row)
        taken = {r["source_id"] for r in db.list_book_identities() if r.get("source_id")}
        candidates = [c for c in candidates if c not in taken]
        if len(candidates) == 1:
            book = read_book(_path(row))
            if book:
                _set_source(cfg, book, candidates[0])
                report.relinked.append(book.book_id)
        elif candidates:
            report.ambiguous[row["book_id"]] = candidates


def _path(row: dict[str, Any]) -> Path:
    return Path(row["path"])


def book_notes_lines(db: Any, source_id: str | None) -> list[str]:
    """As LIT e ZTL da fonte do livro, como vão no bloco ``auto-book-notes``."""
    from zettel.vault import literature_chunk_wikilink_for_row, permanent_wikilink

    if not source_id:
        return ["_Sem fonte ligada: as notas de leitura criam a fonte._"]
    source = db.get_source(source_id)
    if not source:
        return [f"_Fonte {source_id} não encontrada._"]
    lines: list[str] = []
    chunks = [
        c
        for c in db.get_chunks_for_source(source_id)
        if c.get("status") in ("approved", "persisted")
    ]
    if chunks:
        lines.append("**Notas de literatura**")
        lines += [
            f"- {literature_chunk_wikilink_for_row(source['citekey'], c, with_alias=True)}"
            for c in chunks
        ]
    notes = db.get_notes_for_source(source_id)
    if notes:
        lines.append("**Notas permanentes**")
        lines += [
            f"- {permanent_wikilink(n['note_id'], n.get('title') or '', path=n.get('path'))}"
            for n in notes
        ]
    return lines or ["_Nenhuma nota de leitura ainda._"]


def write_book_notes_block(cfg: Any, db: Any, book: Book) -> None:
    from zettel.vault import safe_update_managed_blocks

    safe_update_managed_blocks(
        book.path,
        {BOOK_NOTES_BLOCK: "\n".join(book_notes_lines(db, book.source_id))},
        vault_timezone=cfg.vault_timezone,
    )


def sync_library(cfg: Any, db: Any, source_ids: list[str] | None = None) -> LibrarySyncReport:
    """Espelho em dia, LIV criadas para fontes novas, órfãs religadas, blocos de notas."""
    mirror = refresh(db, cfg)
    report = ensure_books_for_sources(cfg, db, source_ids or [])
    report.duplicate_ids = mirror.duplicate_ids
    relink_orphans(cfg, db, report)
    refresh(db, cfg)
    for row in db.list_book_rows():
        book = read_book(_path(row))
        if book and book.source_id:
            write_book_notes_block(cfg, db, book)
    refresh(db, cfg)
    return report


def safe_sync_library(cfg: Any, db: Any, source_ids: list[str] | None = None) -> dict[str, Any]:
    """``sync_library`` para quem não pode falhar por causa da Estante (harvest, sync)."""
    from zettel.library.schema import LibrarySchemaError

    try:
        return sync_library(cfg, db, source_ids).as_dict()
    except LibrarySchemaError as exc:
        logger.warning("Estante nao sincronizada: %s", exc)
        return {"books_error": str(exc)}
