"""Corpo do SRC: cabeçalho bibliográfico, seções do autor e notas de leitura.

A colheita e o rebuild regravam o arquivo inteiro e, com isso, apagavam a análise.
Os dois passam a trocar só o cabeçalho (metadados, Referência ABNT, Índice de
Literatura) e a recolocar os H2 que o autor já tinha escrito, na ordem em que
estavam. O bloco de notas é refeito das LIT e ZTL da fonte.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from zettel.markdown_fences import headings_outside_fences

NOTES_HEADING = "Notas de leitura"
NOTES_BLOCK = "auto-book-notes"
BIBLIOGRAPHIC_HEADINGS = frozenset({"Referencia ABNT", "Indice de Literatura"})

# Campos de leitor. Título, autores, ISBN e a bibliografia não estão aqui:
# continuam uma vez só, nas chaves que o SRC já tinha.
READER_FIELDS = (
    "code_number",
    "code",
    "original_title",
    "series",
    "series_number",
    "country",
    "original_language",
    "original_publication_year",
    "genres",
    "subjects",
    "nature",
    "reading_mode",
    "format",
    "status",
    "readings",
    "cover",
    "ratings",
    "rating_profile",
    "score",
    "favorite",
    "pages",
    "work_id",
)
LIST_FIELDS = frozenset({"genres", "subjects", "readings"})

_H2 = re.compile(r"^##[ \t]+(.+?)[ \t]*$", re.MULTILINE)
_NOTES = re.compile(
    rf"<!--\s*zettel:{NOTES_BLOCK}:start\s*-->(.*?)<!--\s*zettel:{NOTES_BLOCK}:end\s*-->",
    re.DOTALL,
)


def blank(value: Any) -> bool:
    """Vazio para um campo de leitor: ausente, texto em branco, lista ou mapa vazio."""
    return value is None or value in ("", [], {})


def author_sections(body: str) -> list[tuple[str, str]]:
    """H2 do autor fora de fences, na ordem do arquivo: ``(título, bloco)``.

    O bloco inclui o marcador ``##``. Ficam de fora o cabeçalho bibliográfico
    e a seção de notas de leitura, que o código regrava.
    """
    matches = headings_outside_fences(_H2, body or "")
    found: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        title = match.group(1).strip()
        if title in BIBLIOGRAPHIC_HEADINGS or title == NOTES_HEADING:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        found.append((title, body[match.start() : end].strip()))
    return found


def section_body(block: str) -> str:
    """O texto sob o H2, sem o marcador."""
    lines = (block or "").split("\n", 1)
    return lines[1].strip() if len(lines) > 1 else ""


def notes_inner(body: str) -> str | None:
    """O miolo do bloco de notas, se a seção existir."""
    match = _NOTES.search(body or "")
    return match.group(1).strip("\n") if match else None


def render_notes(inner: str) -> str:
    text = (inner or "").strip("\n")
    return (
        f"## {NOTES_HEADING}\n\n"
        f"<!-- zettel:{NOTES_BLOCK}:start -->\n"
        f"{text}\n"
        f"<!-- zettel:{NOTES_BLOCK}:end -->\n"
    )


def assemble_body(
    header: str,
    sections: list[tuple[str, str]],
    notes: str | None,
) -> str:
    """Cabeçalho, seções do autor e, se houver, a seção de notas já renderizada."""
    parts = [header.strip()]
    parts.extend(block.strip() for _title, block in sections if block.strip())
    if notes:
        parts.append(notes.strip())
    return "\n\n".join(part for part in parts if part) + "\n"


def merge_frontmatter(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """O cabeçalho novo por cima do antigo, sem apagar leitura nem ``created_at``.

    Uma chave de leitor que o cabeçalho novo traz vazia não apaga a que já
    estava no arquivo. ``book_id`` não entra.
    """
    merged = {key: value for key, value in old.items() if key != "book_id"}
    for key, value in new.items():
        if key == "book_id":
            continue
        if key == "created_at" and old.get("created_at"):
            continue
        if key in READER_FIELDS and blank(value) and not blank(old.get(key)):
            continue
        merged[key] = value
    merged["type"] = "source"
    return merged


def merge_body(new_header: str, old_body: str, *, notes: str | None) -> str:
    """Cabeçalho bibliográfico novo, seções do autor antigas, notas refeitas.

    ``notes is None`` recoloca a seção de notas que já estava no arquivo, quando
    houver. Uma string (mesmo vazia) regrava a seção a partir das notas da fonte.
    """
    kept = author_sections(old_body)
    if notes is None:
        previous = _notes_block(old_body)
        rendered = previous
    else:
        rendered = render_notes(notes)
    return assemble_body(new_header, kept, rendered)


def _notes_block(body: str) -> str | None:
    matches = headings_outside_fences(_H2, body or "")
    for index, match in enumerate(matches):
        if match.group(1).strip() != NOTES_HEADING:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        return body[match.start() : end].strip()
    return None


def write_source_preserving(
    path: Path,
    meta: dict[str, Any],
    body: str,
    *,
    notes: str | None = None,
) -> None:
    """Grava o SRC. Se o arquivo existe, preserva leitura e seções do autor."""
    from zettel.vault import parse_frontmatter, safe_write_note

    if path.is_file():
        old_meta, old_body = parse_frontmatter(path.read_text(encoding="utf-8"))
        if isinstance(old_meta, dict) and old_meta:
            meta = merge_frontmatter(old_meta, meta)
            body = merge_body(body, old_body, notes=notes)
        elif notes is not None:
            body = assemble_body(body, [], render_notes(notes))
    elif notes is not None:
        body = assemble_body(body, [], render_notes(notes))
    path.parent.mkdir(parents=True, exist_ok=True)
    safe_write_note(path, meta, body)
