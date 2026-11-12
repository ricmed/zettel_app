"""Espelho SQLite da Estante: relê só as LIV que mudaram desde a última vez.

Roda no início das páginas da Estante, no ``sync-manual``, no harvest e antes do
extract/connect (que precisam do modo de leitura de cada fonte). Uma edição no
Obsidian aparece sem comando nenhum, e depois de um ``init --reset`` o espelho
se reconstrói inteiro no primeiro acesso.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from zettel.library.book_file import (
    Book,
    as_int,
    as_str_list,
    list_book_files,
    read_book,
)
from zettel.library.rating import rate
from zettel.library.reading_mode import effective_nature, effective_reading_mode
from zettel.library.schema import LibrarySchema, schema_for
from zettel.vault import frontmatter_json

logger = logging.getLogger(__name__)


@dataclass
class MirrorReport:
    read: int = 0
    removed: int = 0
    # Dois arquivos com o mesmo book_id (uma cópia feita no Obsidian): vale o primeiro.
    duplicate_ids: list[str] = field(default_factory=list)


def schema_stamp(cfg: Any) -> str:
    """Muda quando ``library.yaml`` muda: nota e modo de leitura são recalculados."""
    path = Path(cfg.library.rating_path)
    try:
        return f"{path}:{path.stat().st_mtime_ns}"
    except OSError:
        return str(path)


def book_row(
    book: Book, schema: LibrarySchema, *, mtime_ns: int, size: int, stamp: str
) -> tuple[dict[str, Any], list[tuple[str | None, str | None]]]:
    meta = book.meta
    rating = rate(schema, meta)
    row = {
        "book_id": book.book_id,
        "path": str(book.path),
        "file_mtime_ns": mtime_ns,
        "file_size": size,
        "schema_stamp": stamp,
        "code_number": as_int(meta.get("code_number")),
        "code": book.code or None,
        "title": book.title,
        "authors_json": json.dumps(book.authors, ensure_ascii=False),
        "source_id": book.source_id,
        "isbn": str(meta.get("isbn") or "").strip() or None,
        "pages": as_int(meta.get("pages")),
        "country": str(meta.get("country") or "").strip() or None,
        "original_language": str(meta.get("original_language") or "").strip() or None,
        "genres_json": json.dumps(as_str_list(meta.get("genres")), ensure_ascii=False),
        "nature": effective_nature(meta, schema),
        "reading_mode": effective_reading_mode(meta, schema),
        "format": book.format or None,
        "status": book.status or None,
        "cover": str(meta.get("cover") or "").strip() or None,
        "rating_profile": rating.profile.name if rating.profile else None,
        "score": rating.score,
        "favorite": 1 if meta.get("favorite") is True else 0,
        "created_at": str(meta.get("created_at") or "") or None,
        "frontmatter_json": frontmatter_json(meta),
    }
    readings = [(r.start, r.end) for r in book.readings]
    return row, readings


def library_health(cfg: Any) -> tuple[bool, str]:
    """``(ok, detalhe)`` da Estante para o ``doctor``: vocabulário e códigos únicos."""
    from collections import Counter

    from zettel.library.book_file import list_books
    from zettel.library.schema import LibrarySchemaError

    try:
        schema = schema_for(cfg)
    except LibrarySchemaError as exc:
        return False, str(exc)
    books = list_books(cfg)
    numbers = Counter(as_int(b.meta.get("code_number")) for b in books)
    repeated = sorted(n for n, count in numbers.items() if n and count > 1)
    ids = Counter(b.book_id for b in books)
    copies = sorted(book_id for book_id, count in ids.items() if count > 1)
    problems = []
    if repeated:
        problems.append("codigo repetido: " + ", ".join(f"{n:04d}" for n in repeated))
    if copies:
        problems.append(f"book_id em mais de um arquivo: {', '.join(copies)}")
    if problems:
        return False, "; ".join(problems)
    return True, f"{len(books)} livro(s), {len(schema.profiles)} perfil(is) de avaliacao"


def refresh(db: Any, cfg: Any) -> MirrorReport:
    """Põe o espelho em dia com ``60_Estante/``; devolve o que foi lido e removido."""
    report = MirrorReport()
    schema = schema_for(cfg)
    stamp = schema_stamp(cfg)
    known = db.book_file_index()
    by_path = {entry["path"]: (book_id, entry) for book_id, entry in known.items()}
    seen: set[str] = set()
    for path in list_book_files(cfg):
        try:
            st = path.stat()
        except OSError:
            continue
        cached = by_path.get(str(path))
        if cached:
            book_id, entry = cached
            unchanged = (
                entry["file_mtime_ns"] == st.st_mtime_ns
                and entry["file_size"] == st.st_size
                and entry["schema_stamp"] == stamp
            )
            if unchanged and book_id not in seen:
                seen.add(book_id)
                continue
        book = read_book(path)
        if book is None:
            continue
        if book.book_id in seen:
            report.duplicate_ids.append(book.book_id)
            logger.warning("book_id %s repetido em %s; mantido o primeiro", book.book_id, path)
            continue
        row, readings = book_row(
            book, schema, mtime_ns=st.st_mtime_ns, size=st.st_size, stamp=stamp
        )
        db.upsert_book(row, readings)
        seen.add(book.book_id)
        report.read += 1
    gone = sorted(set(known) - seen)
    db.delete_books(gone)
    report.removed = len(gone)
    return report
