"""Natureza e modo de leitura de um livro (ADR-069).

O modo decide quais prompts leem o livro: ``reflexao`` usa os prompts reflexivos
(metáfora, pensamento, analogia, questão filosófica), ``conceito`` os de sempre.
Uma fonte sem LIV é ``conceito``: nada muda para quem não usa a Estante.
"""

from __future__ import annotations

from typing import Any

from zettel.library.book_file import NATURES, READING_MODES, as_str_list
from zettel.library.schema import LibrarySchema

REFLECTIVE = "reflexao"
CONCEPTUAL = "conceito"


def effective_nature(meta: dict[str, Any], schema: LibrarySchema) -> str | None:
    """``nature`` da LIV; senão a dos gêneros quando não se contradizem; senão None."""
    explicit = str(meta.get("nature") or "")
    if explicit in NATURES:
        return explicit
    natures = {schema.genre(g).nature for g in as_str_list(meta.get("genres"))} - {"neutro"}
    return natures.pop() if len(natures) == 1 else None


def effective_reading_mode(meta: dict[str, Any], schema: LibrarySchema) -> str:
    """``reading_mode`` da LIV; senão reflexão para ficção ou gênero reflexivo."""
    explicit = str(meta.get("reading_mode") or "")
    if explicit in READING_MODES:
        return explicit
    if effective_nature(meta, schema) == "ficcao":
        return REFLECTIVE
    if any(schema.genre(g).reflective for g in as_str_list(meta.get("genres"))):
        return REFLECTIVE
    return CONCEPTUAL


def source_reading_mode(db: Any, source_id: str | None) -> str:
    """O modo de leitura de uma fonte, pelo espelho da Estante (sem LIV: conceito)."""
    if not source_id:
        return CONCEPTUAL
    row = db.get_book_by_source(source_id)
    return (row or {}).get("reading_mode") or CONCEPTUAL
