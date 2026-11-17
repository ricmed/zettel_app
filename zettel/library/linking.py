"""Estante e fontes são a mesma nota SRC.

O harvest não cria uma segunda nota. Ele grava o SRC e, para os tipos que
entram na estante, completa código, formato e status quando ainda faltam. O
bloco de notas de leitura é refeito das LIT e ZTL da fonte.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from zettel.library.book_file import (
    code_number_of,
    format_code,
    format_for_letter,
    format_letter_of,
    load_book,
    next_code_number,
    save_book,
)
from zettel.library.mirror import refresh
from zettel.library.source_body import NOTES_BLOCK

logger = logging.getLogger(__name__)


@dataclass
class LibrarySyncReport:
    shelved: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"shelved": len(self.shelved)}


def same_work(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Mesmo título principal e ao menos um sobrenome em comum."""
    import json

    from zettel.harvester.biblio_dedupe import author_surnames, normalize_title

    def names(row: dict[str, Any]) -> list[str]:
        raw = row.get("authors")
        if raw is None:
            raw = row.get("authors_json")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                return []
        return [str(name) for name in raw] if isinstance(raw, list) else []

    title = normalize_title(left.get("title"))
    if not title or title != normalize_title(right.get("title")):
        return False
    return bool(author_surnames(names(left)) & author_surnames(names(right)))


_EMPTY_NOTES = "_Nenhuma nota de leitura ainda._"


def _lit_item(source: dict[str, Any], chunk: dict[str, Any]) -> dict[str, Any]:
    from zettel.vault import literature_chunk_wikilink_for_row

    link = literature_chunk_wikilink_for_row(source["citekey"], chunk, with_alias=True)
    label = link.split("|", 1)[1].rstrip("]") if "|" in link else link.strip("[]")
    return {
        "chunk_id": chunk["chunk_id"],
        "label": label,
        "wikilink": link,
        "manual": chunk.get("status") == "persisted",
        "page": chunk.get("page_in_book"),
    }


def _note_item(note: dict[str, Any]) -> dict[str, Any]:
    from zettel.vault import permanent_wikilink

    return {
        "note_id": note["note_id"],
        "title": note.get("title") or note["note_id"],
        "wikilink": permanent_wikilink(
            note["note_id"], note.get("title") or "", path=note.get("path")
        ),
    }


def _source_items(db: Any, source_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    source = db.get_source(source_id) or {}
    lits = [
        _lit_item(source, chunk)
        for chunk in db.get_chunks_for_source(source_id)
        if chunk.get("status") in ("approved", "persisted")
    ]
    notes = [_note_item(note) for note in db.get_notes_for_source(source_id)]
    return lits, notes


def note_groups(db: Any, source_id: str | None) -> list[dict[str, Any]]:
    """LIT e ZTL desta edição e, depois, de cada irmã com o rótulo dela.

    A nota continua com um único ``source_id``. O grupo só muda a lista.
    """
    from zettel.library.works import labels_for, sibling_source_ids

    if not source_id or not db.get_source(source_id):
        return []
    siblings = sibling_source_ids(db, source_id)
    ids = [source_id, *siblings]
    rows = [row for sid in ids if (row := db.get_source(sid))]
    labels = labels_for(rows) if siblings else {}
    groups = []
    for row in rows:
        sid = row["source_id"]
        lits, notes = _source_items(db, sid)
        heading = None
        if siblings:
            heading = "Desta edição" if sid == source_id else labels.get(sid, sid)
        groups.append(
            {
                "source_id": sid,
                "heading": heading,
                "own": sid == source_id,
                "lits": lits,
                "notes": notes,
            }
        )
    return groups


def _markdown_items(group: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    if group["lits"]:
        lines.append("**Notas de literatura**")
        lines += [f"- {item['wikilink']}" for item in group["lits"]]
    if group["notes"]:
        lines.append("**Notas permanentes**")
        lines += [f"- {item['wikilink']}" for item in group["notes"]]
    return lines


def source_notes_lines(db: Any, source_id: str | None) -> list[str]:
    """As LIT e ZTL da obra, como vão no bloco de notas de leitura.

    Sozinha, a ficha lista só as suas. Ligada, vem **Desta edição** e depois
    cada irmã com o rótulo (título e formato; ISBN se os títulos coincidirem).
    """
    groups = note_groups(db, source_id)
    if not groups:
        return [_EMPTY_NOTES]
    if len(groups) == 1 and not groups[0]["heading"]:
        return _markdown_items(groups[0]) or [_EMPTY_NOTES]
    lines: list[str] = []
    for group in groups:
        lines.append(f"**{group['heading']}**")
        lines.extend(_markdown_items(group) or [_EMPTY_NOTES])
    return lines


def write_book_notes_block(cfg: Any, db: Any, book: Any, *, refresh_siblings: bool = True) -> None:
    """Regrava o bloco de notas de leitura do SRC quando ele muda.

    Ao regravar um membro, regrava as irmãs: a nota nova aparece dos dois lados
    sem esperar o próximo sync. ``refresh_siblings=False`` evita o laço quando
    quem chama já percorre o grupo.
    """
    _write_notes_block(cfg, db, book)
    if not refresh_siblings:
        return
    from zettel.library.book_file import BookNotFound, load_book
    from zettel.library.works import sibling_source_ids

    for source_id in sibling_source_ids(db, book.source_id):
        try:
            other = load_book(cfg, source_id)
        except BookNotFound:
            continue
        _write_notes_block(cfg, db, other)


def _write_notes_block(cfg: Any, db: Any, book: Any) -> None:
    from zettel.vault import MalformedManagedBlock, upsert_managed_block

    inner = "\n".join(source_notes_lines(db, book.source_id))
    try:
        body = upsert_managed_block(book.body, NOTES_BLOCK, inner)
    except MalformedManagedBlock as exc:
        logger.warning("%s em %s; bloco de notas nao atualizado", exc, book.path)
        return
    if NOTES_BLOCK not in book.body:
        from zettel.library.source_body import assemble_body, author_sections, render_notes

        # O cabeçalho bibliográfico fica; as seções do autor também. A seção de
        # notas entra no fim quando o arquivo ainda não a tinha.
        header, _rest = _split_header(book.body)
        body = assemble_body(header, author_sections(book.body), render_notes(inner))
    if body != book.body:
        book.body = body
        save_book(cfg, book, expected_version=book.version)


def _split_header(body: str) -> tuple[str, str]:
    """Texto antes do primeiro H2 do autor, e o resto. Usado só para criar a seção."""
    from zettel.library.source_body import author_sections

    sections = author_sections(body)
    if not sections:
        return body, ""
    first = sections[0][1]
    start = body.find(first)
    if start < 0:
        return body, ""
    return body[:start], body[start:]


def ensure_shelf_defaults(cfg: Any, source_id: str) -> bool:
    """Código, formato e status numa fonte da estante que ainda não os tem."""
    try:
        book = load_book(cfg, source_id)
    except Exception:
        return False
    document_type = str(book.meta.get("document_type") or "")
    if document_type not in set(cfg.library.document_types):
        return False
    changed = False
    letters = cfg.library.format_letters
    if not code_number_of(book.meta):
        number = next_code_number(cfg)
        fmt = book.format or "digital"
        book.meta["format"] = fmt
        book.meta["code_number"] = number
        book.meta["code"] = format_code(number, fmt, letters)
        changed = True
    if not book.status:
        book.meta["status"] = cfg.library.initial_status
        changed = True
    if not book.format:
        # A letra de "0002 - F" já é o formato. Forçar digital trocaria o código.
        inferred = format_for_letter(format_letter_of(book.meta.get("code")), letters)
        if inferred:
            book.meta["format"] = inferred
        else:
            book.meta["format"] = "digital"
            number = code_number_of(book.meta)
            if number and not str(book.meta.get("code") or "").strip():
                book.meta["code_number"] = number
                book.meta["code"] = format_code(number, "digital", letters)
            changed = True
    if changed:
        save_book(cfg, book, expected_version=book.version)
    return changed


def _shelf_ids(db: Any, cfg: Any, source_ids: list[str] | None) -> list[str]:
    wanted = set(cfg.library.document_types)
    if source_ids is None:
        return [
            row["source_id"]
            for row in db.list_sources_with_authors()
            if (row.get("document_type") or "") in wanted
        ]
    found = []
    for source_id in dict.fromkeys(source_ids):
        source = db.get_source(source_id)
        if source and (source.get("document_type") or "") in wanted:
            found.append(source_id)
    return found


def sync_library(cfg: Any, db: Any, source_ids: list[str] | None = None) -> LibrarySyncReport:
    """Espelho em dia, código nas fontes novas da estante e bloco de notas refeito."""
    refresh(db, cfg)
    report = LibrarySyncReport()
    ids = _shelf_ids(db, cfg, source_ids)
    for source_id in ids:
        if ensure_shelf_defaults(cfg, source_id):
            report.shelved.append(source_id)
    refresh(db, cfg)
    for source_id in ids:
        try:
            book = load_book(cfg, source_id)
        except Exception:
            logger.warning("Estante: SRC %s nao encontrado para o bloco de notas", source_id)
            continue
        write_book_notes_block(cfg, db, book)
    refresh(db, cfg)
    return report


def safe_sync_library(cfg: Any, db: Any, source_ids: list[str] | None = None) -> dict[str, Any]:
    """``sync_library`` para quem não pode falhar por causa da estante (harvest, sync)."""
    from zettel.library.schema import LibrarySchemaError

    try:
        return sync_library(cfg, db, source_ids).as_dict()
    except LibrarySchemaError as exc:
        logger.warning("Estante nao sincronizada: %s", exc)
        return {"books_error": str(exc)}
