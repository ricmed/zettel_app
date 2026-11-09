"""Edições de uma LIV vindas de um formulário: validam e mudam ``Book.meta`` / ``Book.body``.

Nada aqui grava: quem chama faz ``save_book`` com a versão da página, sob a
trava de operação. Um valor inválido levanta ``BookEditError`` com a mensagem
que a página mostra.
"""

from __future__ import annotations

import datetime
from typing import Any

from zettel.library.book_file import (
    FORMATS,
    IMPRESSIONS_HEADING,
    NATURES,
    READING_MODES,
    STATUSES,
    SUMMARY_HEADING,
    Book,
    as_date,
    as_int,
    as_str_list,
    replace_section,
)
from zettel.library.rating import rate
from zettel.library.schema import LibrarySchema

MAX_TEXT = 20_000
_TEXT_FIELDS = ("title", "original_title", "series", "country", "original_language", "isbn")
_INT_FIELDS = ("series_number", "pages", "original_publication_year", "code_number")


class BookEditError(ValueError):
    pass


def _clean(value: Any, limit: int = 300) -> str:
    return " ".join(str(value or "").split())[:limit]


def _lines(value: Any) -> list[str]:
    """Um item por linha (autores podem ter vírgula: "Tolkien, J. R. R.")."""
    return [s for s in (_clean(line) for line in str(value or "").splitlines()) if s]


def apply_metadata(book: Book, form: dict[str, Any], schema: LibrarySchema) -> None:
    """Identidade, classificação e leitura; ``genres`` vem dos checkboxes + texto livre."""
    title = _clean(form.get("title"))
    if not title:
        raise BookEditError("O livro precisa de um título.")
    for key in _TEXT_FIELDS:
        book.meta[key] = _clean(form.get(key)) or None
    book.meta["title"] = title
    for key in _INT_FIELDS:
        raw = str(form.get(key) or "").strip()
        value = as_int(raw)
        if raw and (value is None or value < 0):
            raise BookEditError(f"Valor inválido para {key}: {raw!r}.")
        if key == "code_number" and value is None:
            continue  # o número do código nunca some
        book.meta[key] = value
    book.meta["authors"] = _lines(form.get("authors"))
    chosen = [g for g in as_str_list(form.get("genres")) if g]
    extra = as_str_list(form.get("genres_extra"))
    book.meta["genres"] = list(dict.fromkeys(chosen + extra))
    subjects = as_str_list(form.get("subjects")) + as_str_list(form.get("subjects_extra"))
    book.meta["subjects"] = list(dict.fromkeys(s for s in subjects if s))
    for key, allowed in (("nature", NATURES), ("reading_mode", READING_MODES)):
        value = str(form.get(key) or "")
        if value and value not in allowed:
            raise BookEditError(f"Valor inválido para {key}.")
        book.meta[key] = value or None
    fmt = str(form.get("format") or "")
    if fmt not in FORMATS:
        raise BookEditError("Escolha o formato: físico, digital ou audiolivro.")
    book.meta["format"] = fmt
    set_status(book, str(form.get("status") or ""))
    book.meta["favorite"] = str(form.get("favorite") or "") in ("1", "on", "true")
    source_id = _clean(form.get("source_id"), 200)
    book.meta["source_id"] = source_id or None
    refresh_score(book, schema)


def set_status(book: Book, status: str) -> None:
    if status not in STATUSES:
        raise BookEditError("Status desconhecido.")
    book.meta["status"] = status


def _readings(book: Book) -> list[dict[str, str | None]]:
    return [{"start": r.start, "end": r.end} for r in book.readings]


def _date(raw: Any, today: datetime.date) -> str:
    text = str(raw or "").strip()
    if not text:
        return today.isoformat()
    value = as_date(text)
    if value is None:
        raise BookEditError(f"Data inválida: {text!r} (use AAAA-MM-DD).")
    return value


def start_reading(book: Book, raw_date: Any, today: datetime.date) -> None:
    """Abre uma leitura; um livro já lido passa a "relendo"."""
    readings = _readings(book)
    if readings and readings[-1]["start"] and not readings[-1]["end"]:
        raise BookEditError("Já existe uma leitura em andamento.")
    readings.append({"start": _date(raw_date, today), "end": None})
    book.meta["readings"] = readings
    finished_before = any(r["end"] for r in readings[:-1])
    book.meta["status"] = "relendo" if finished_before else "lendo"


def finish_reading(book: Book, raw_date: Any, today: datetime.date) -> None:
    """Fecha a leitura aberta (ou registra uma só com término) e marca "lido"."""
    end = _date(raw_date, today)
    readings = _readings(book)
    if readings and not readings[-1]["end"]:
        if readings[-1]["start"] and readings[-1]["start"] > end:
            raise BookEditError("O término é anterior ao início da leitura.")
        readings[-1]["end"] = end
    else:
        readings.append({"start": None, "end": end})
    book.meta["readings"] = readings
    book.meta["status"] = "lido"


def remove_reading(book: Book, position: Any) -> None:
    readings = _readings(book)
    index = as_int(position)
    if index is None or not 0 <= index < len(readings):
        raise BookEditError("Leitura não encontrada.")
    del readings[index]
    book.meta["readings"] = readings


def set_ratings(book: Book, form: dict[str, Any], schema: LibrarySchema) -> None:
    """``rating_<critério>`` por critério (vazio apaga) e ``rating_profile`` ('' = automático)."""
    profile = _clean(form.get("rating_profile"), 80)
    if profile and schema.profile(profile) is None:
        raise BookEditError("Perfil de avaliação desconhecido.")
    book.meta["rating_profile"] = profile or None
    ratings = dict(book.ratings)
    lo, hi = schema.scale.min, schema.scale.max
    for key, spec in schema.criteria.items():
        field = f"rating_{key}"
        if field not in form:
            continue
        raw = str(form.get(field) or "").strip().replace(",", ".")
        if not raw:
            ratings.pop(key, None)
            continue
        try:
            value = float(raw)
        except ValueError as exc:
            raise BookEditError(f"Nota inválida para {spec.label}: {raw!r}.") from exc
        if not lo <= value <= hi:
            raise BookEditError(f"{spec.label}: a nota vai de {lo:g} a {hi:g}.")
        ratings[key] = int(value) if value.is_integer() else value
    book.meta["ratings"] = ratings
    refresh_score(book, schema)


def refresh_score(book: Book, schema: LibrarySchema) -> None:
    """Grava a nota calculada na LIV, para o Obsidian (o espelho recalcula por conta)."""
    book.meta["score"] = rate(schema, book.meta).score


def set_texts(book: Book, summary: Any, impressions: Any) -> None:
    summary_text = str(summary or "").replace("\r\n", "\n")[:MAX_TEXT]
    impressions_text = str(impressions or "").replace("\r\n", "\n")[:MAX_TEXT]
    body = replace_section(book.body, SUMMARY_HEADING, summary_text)
    book.body = replace_section(body, IMPRESSIONS_HEADING, impressions_text)


def set_cover(book: Book, relpath: str) -> None:
    book.meta["cover"] = relpath


def fill_missing(book: Book, found: dict[str, Any]) -> list[str]:
    """Preenche só o que a LIV não tem com o que a pesquisa achou; devolve as chaves.

    Um campo que o usuário escreveu nunca é trocado: a pesquisa completa, não corrige.
    """
    changed = []
    if not book.authors and found.get("authors"):
        book.meta["authors"] = [_clean(a) for a in found["authors"] if _clean(a)]
        changed.append("authors")
    for key in ("isbn", "original_title", "country", "original_language"):
        if not _clean(book.meta.get(key)) and _clean(found.get(key)):
            book.meta[key] = _clean(found[key])
            changed.append(key)
    for key in ("pages", "original_publication_year"):
        value = as_int(found.get(key))
        if as_int(book.meta.get(key)) is None and value:
            book.meta[key] = value
            changed.append(key)
    return changed
