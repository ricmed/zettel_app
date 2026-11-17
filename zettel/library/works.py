"""Edições da mesma obra (ADR-071).

Duas fichas são a mesma obra só quando alguém declara. O ``work_id`` é um ULID
no frontmatter do SRC. Vazio significa sozinho. Nada é agrupado por título,
ISBN ou Wikidata.

O connect importa só este módulo, para saber quais fontes são edições irmãs.
"""

from __future__ import annotations

import logging
from typing import Any

from ulid import ULID

from zettel.library.book_file import FORMAT_LABELS, list_books, load_book, save_book
from zettel.search_terms import fold

logger = logging.getLogger(__name__)


class WorkLinkError(ValueError):
    """A ligação pedida não pode ser feita."""


def edition_label(row: dict[str, Any], *, show_isbn: bool = False) -> str:
    """Título e formato. O ISBN entra quando os títulos das edições coincidem."""
    title = str(row.get("title") or "").strip() or "Sem título"
    fmt = FORMAT_LABELS.get(str(row.get("format") or ""), "")
    label = f"{title} — {fmt}" if fmt else title
    isbn = str(row.get("isbn") or "").strip()
    if show_isbn and isbn:
        label = f"{label}, ISBN {isbn}"
    return label


def labels_for(rows: list[dict[str, Any]]) -> dict[str, str]:
    """Rótulo de cada fonte. O ISBN aparece quando dois títulos, dobrados, coincidem."""
    folded = [fold(str(row.get("title") or "")) for row in rows]
    labels: dict[str, str] = {}
    for row, title in zip(rows, folded, strict=True):
        collide = bool(title) and folded.count(title) > 1
        labels[str(row.get("source_id") or "")] = edition_label(row, show_isbn=collide)
    return labels


def members(db: Any, work_id: str | None) -> list[dict]:
    """As fontes que declaram este ``work_id``, em ordem estável."""
    return db.sources_by_work(str(work_id or "").strip())


def sibling_source_ids(db: Any, source_id: str | None) -> list[str]:
    """As outras fontes da mesma obra. Vazio quando a fonte está sozinha."""
    if not source_id:
        return []
    getter = getattr(db, "get_source", None)
    if getter is None:
        return []
    row = getter(source_id) or {}
    work_id = str(row.get("work_id") or "").strip()
    if not work_id:
        return []
    return [item["source_id"] for item in members(db, work_id) if item["source_id"] != source_id]


def sibling_views(db: Any, source_id: str) -> list[dict[str, str]]:
    """As outras edições, com o rótulo, para a seção Mesma obra."""
    row = db.get_source(source_id) or {}
    group = members(db, str(row.get("work_id") or ""))
    labels = labels_for(group)
    return [
        {"source_id": item["source_id"], "label": labels.get(item["source_id"], item["source_id"])}
        for item in group
        if item["source_id"] != source_id
    ]


def shelf_choices(cfg: Any, db: Any, source_id: str) -> list[dict[str, str]]:
    """As outras fichas da estante, com as edições do alvo no rótulo.

    Quem já está na mesma obra não entra: a seção lista essas edições à parte.
    """
    current = str((db.get_source(source_id) or {}).get("work_id") or "").strip()
    choices: list[dict[str, str]] = []
    for book in list_books(cfg):
        if book.source_id == source_id:
            continue
        other = db.get_source(book.source_id) or {}
        work = str(other.get("work_id") or book.meta.get("work_id") or "").strip()
        if current and work == current:
            continue
        group = members(db, work) if work else []
        info = {
            "source_id": book.source_id,
            "title": book.title,
            "format": book.format,
            "isbn": other.get("isbn") or book.meta.get("isbn") or "",
        }
        if group:
            named = labels_for(group)
            label = named.get(book.source_id) or edition_label(info)
            others = [
                named[item["source_id"]]
                for item in group
                if item["source_id"] not in (book.source_id, source_id)
                and item["source_id"] in named
            ]
        else:
            label = edition_label(info)
            others = []
        if others:
            label = f"{label} (também {'; '.join(others)})"
        choices.append({"source_id": book.source_id, "label": label})
    choices.sort(key=lambda item: fold(item["label"]))
    return choices


def link_editions(cfg: Any, db: Any, source_id: str, other_id: str) -> str:
    """Liga duas edições. Devolve o ``work_id`` da obra.

    Dois avulsos ganham um ULID novo. Um agrupado recebe o outro. Dois grupos
    se unem no ``work_id`` do alvo (``other_id``). Em seguida somem as
    ``corroborates`` derivadas entre as fontes do grupo.
    """
    from zettel.library.mirror import refresh

    other_id = str(other_id or "").strip()
    if not other_id or other_id == source_id:
        raise WorkLinkError("Escolha outra edição.")
    refresh(db, cfg)
    left = load_book(cfg, source_id)
    right = load_book(cfg, other_id)
    left_work = str(left.meta.get("work_id") or "").strip()
    right_work = str(right.meta.get("work_id") or "").strip()
    if left_work and left_work == right_work:
        return left_work
    if right_work:
        work_id = right_work
    elif left_work:
        work_id = left_work
    else:
        work_id = str(ULID())
    group = set(members_ids(db, left_work)) | set(members_ids(db, right_work))
    group.update((source_id, other_id))
    for sid in sorted(group):
        _set_work(cfg, sid, work_id)
    refresh(db, cfg)
    removed = db.delete_derived_corroborates_between(sorted(group))
    if removed:
        logger.info("Obra %s: %d corroboracao(oes) derivada(s) removida(s)", work_id, removed)
    _refresh_note_blocks(cfg, db, sorted(group))
    return work_id


def unlink_edition(cfg: Any, db: Any, source_id: str) -> None:
    """Tira esta ficha da obra. Se sobrar uma, o ``work_id`` dela também sai.

    Desligar não recria as ``corroborates`` que a ligação apagou.
    """
    from zettel.library.mirror import refresh

    refresh(db, cfg)
    book = load_book(cfg, source_id)
    work_id = str(book.meta.get("work_id") or "").strip()
    if not work_id:
        raise WorkLinkError("Esta ficha não está ligada a outra edição.")
    group = members_ids(db, work_id) or [source_id]
    _set_work(cfg, source_id, "")
    remaining = [sid for sid in group if sid != source_id]
    if len(remaining) == 1:
        _set_work(cfg, remaining[0], "")
    refresh(db, cfg)
    _refresh_note_blocks(cfg, db, group)


def members_ids(db: Any, work_id: str) -> list[str]:
    return [item["source_id"] for item in members(db, work_id)]


def _set_work(cfg: Any, source_id: str, work_id: str) -> None:
    book = load_book(cfg, source_id)
    current = str(book.meta.get("work_id") or "").strip()
    wanted = work_id.strip()
    if current == wanted:
        return
    book.meta["work_id"] = wanted
    save_book(cfg, book, expected_version=book.version)


def _refresh_note_blocks(cfg: Any, db: Any, source_ids: list[str]) -> None:
    """Regrava o bloco de notas de cada membro. Import tardio: linking usa este módulo."""
    from zettel.library.book_file import BookNotFound
    from zettel.library.linking import write_book_notes_block

    for source_id in source_ids:
        try:
            book = load_book(cfg, source_id)
        except BookNotFound:
            continue
        write_book_notes_block(cfg, db, book, refresh_siblings=False)
