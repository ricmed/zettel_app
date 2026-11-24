"""Espelho SQLite da estante: relê o SRC que mudou desde a última vez.

Roda no início das páginas da estante, no ``sync-manual``, no harvest e antes do
extract/connect (que precisam do modo de leitura de cada fonte). Uma edição no
Obsidian aparece sem comando nenhum, e depois de um ``init --reset`` o espelho
se reconstrói do arquivo no primeiro acesso.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from zettel.library.book_file import (
    as_int,
    as_str_list,
    code_number_of,
    list_books,
    list_source_files,
    ratings_of,
    read_book,
    readings_of,
)
from zettel.library.rating import rate
from zettel.library.reading_mode import effective_nature, effective_reading_mode
from zettel.library.schema import LibrarySchema, schema_for
from zettel.stored_path import relativize

logger = logging.getLogger(__name__)


@dataclass
class MirrorReport:
    read: int = 0
    removed: int = 0
    duplicate_ids: list[str] = field(default_factory=list)


def schema_stamp(cfg: Any) -> str:
    """Muda quando ``library.yaml`` muda: nota e modo de leitura são recalculados."""
    path = Path(cfg.library.rating_path)
    try:
        return f"{path}:{path.stat().st_mtime_ns}"
    except OSError:
        return str(path)


def shelf_fields(
    book: Any, schema: LibrarySchema, *, mtime_ns: int, size: int, stamp: str
) -> dict[str, Any]:
    """Colunas de leitor desta ficha, mais o carimbo do arquivo."""
    meta = book.meta
    rating = rate(schema, meta)
    pages = as_int(meta.get("pages"))
    return {
        "code_number": code_number_of(meta),
        "code": book.code or None,
        "format": book.format or None,
        "status": book.status or None,
        "readings_json": json.dumps(
            [{"start": r.start, "end": r.end} for r in readings_of(meta)],
            ensure_ascii=False,
        ),
        "genres_json": json.dumps(as_str_list(meta.get("genres")), ensure_ascii=False),
        "subjects_json": json.dumps(as_str_list(meta.get("subjects")), ensure_ascii=False),
        "nature": effective_nature(meta, schema),
        "reading_mode": effective_reading_mode(meta, schema),
        "country": str(meta.get("country") or "").strip() or None,
        "original_language": str(meta.get("original_language") or "").strip() or None,
        "original_publication_year": as_int(meta.get("original_publication_year")),
        "original_title": str(meta.get("original_title") or "").strip() or None,
        "series": str(meta.get("series") or "").strip() or None,
        "series_number": as_int(meta.get("series_number")),
        "pages": pages,
        "cover": str(meta.get("cover") or "").strip() or None,
        "ratings_json": json.dumps(ratings_of(meta), ensure_ascii=False),
        "rating_profile": rating.profile.name if rating.profile else None,
        "score": rating.score,
        "favorite": 1 if meta.get("favorite") is True else 0,
        "work_id": str(meta.get("work_id") or "").strip() or None,
        "shelf_mtime_ns": mtime_ns,
        "shelf_size": size,
        "shelf_schema_stamp": stamp,
    }


def _stored_origin(meta: dict, book: Any, cfg: Any) -> str:
    raw = str(meta.get("origin_path") or book.path)
    canonical, status = relativize(raw, kind="origin", vault=cfg.vault_path, inbox=cfg.inbox_path)
    return raw if status == "invalid" else canonical


def _ensure_row(db: Any, book: Any, cfg: Any) -> None:
    """Depois de um reset a linha some e o arquivo fica. O espelho a refaz."""
    if db.get_source(book.source_id):
        return
    meta = book.meta
    year = as_int(meta.get("year"))
    db.upsert_source(
        source_id=book.source_id,
        citekey=str(meta.get("citekey") or book.source_id.lstrip("@")),
        title=book.title,
        authors=book.authors,
        year=year,
        file_checksum=str(meta.get("checksum") or ""),
        origin_path=_stored_origin(meta, book, cfg),
        origin_type=str(meta.get("origin_type") or "md"),
        origin=str(meta.get("origin") or "manual"),
        document_type=meta.get("document_type"),
        isbn=str(meta.get("isbn") or "").strip() or None,
        doi=str(meta.get("doi") or "").strip() or None,
    )


def library_health(cfg: Any) -> tuple[bool, str]:
    """``(ok, detalhe)`` da estante para o ``doctor``: vocabulário e códigos únicos."""
    from collections import Counter

    from zettel.library.schema import LibrarySchemaError

    try:
        schema = schema_for(cfg)
    except LibrarySchemaError as exc:
        return False, str(exc)
    books = list_books(cfg)
    numbers = Counter(code_number_of(book.meta) for book in books)
    repeated = sorted(number for number, count in numbers.items() if number and count > 1)
    works = Counter(
        str(book.meta.get("work_id") or "").strip()
        for book in books
        if str(book.meta.get("work_id") or "").strip()
    )
    lonely = sorted(work for work, count in works.items() if count == 1)
    problems = []
    if repeated:
        problems.append("codigo repetido: " + ", ".join(f"{n:04d}" for n in repeated))
    if lonely:
        problems.append("obra com uma so edicao: " + ", ".join(lonely))
    if problems:
        return False, "; ".join(problems)
    return True, f"{len(books)} item(ns) na estante, {len(schema.profiles)} perfil(is) de avaliacao"


def _sync_document_type(db: Any, book: Any) -> None:
    """O tipo do arquivo manda. Um capítulo deixa de aparecer na estante."""
    row = db.get_source(book.source_id)
    if not row:
        return
    document_type = str(book.meta.get("document_type") or "").strip()
    if not document_type or document_type == str(row.get("document_type") or ""):
        return
    try:
        authors = json.loads(row.get("authors") or "[]")
    except json.JSONDecodeError:
        authors = []
    db.update_source_bibliography(
        book.source_id,
        title=str(row.get("title") or book.title),
        authors=authors if isinstance(authors, list) else [],
        year=row.get("year"),
        document_type=document_type,
        bibliography_json=None,
        abnt_reference=None,
        doi=None,
        isbn=None,
    )


def refresh(db: Any, cfg: Any) -> MirrorReport:
    """Põe o espelho em dia com os SRC da estante."""
    report = MirrorReport()
    schema = schema_for(cfg)
    stamp = schema_stamp(cfg)
    known = db.shelf_stamps()
    wanted = set(cfg.library.document_types)
    seen: set[str] = set()
    for path in list_source_files(cfg):
        book = read_book(path)
        if book is None:
            continue
        if str(book.meta.get("document_type") or "") not in wanted:
            _sync_document_type(db, book)
            continue
        try:
            st = book.path.stat()
        except OSError:
            continue
        if book.source_id in seen:
            report.duplicate_ids.append(book.source_id)
            logger.warning(
                "source_id %s repetido em %s; mantido o primeiro", book.source_id, book.path
            )
            continue
        cached = known.get(book.source_id)
        unchanged = (
            cached is not None
            and cached["shelf_mtime_ns"] == st.st_mtime_ns
            and cached["shelf_size"] == st.st_size
            and cached["shelf_schema_stamp"] == stamp
        )
        if unchanged:
            seen.add(book.source_id)
            continue
        _sync_document_type(db, book)
        _ensure_row(db, book, cfg)
        db.upsert_shelf(
            book.source_id,
            shelf_fields(book, schema, mtime_ns=st.st_mtime_ns, size=st.st_size, stamp=stamp),
        )
        seen.add(book.source_id)
        report.read += 1
    return report
