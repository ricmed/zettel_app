"""Notas de leitura escritas a partir do livro: LIT manuais granulares (ADR-030).

Um livro sem fonte ganha antes um SRC manual (com o ISBN, para que o PDF
colhido depois caia na mesma fonte, #392). A LIT é adotada na hora, sem passar
pelo portão de revisão, como toda nota manual.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from zettel.library.book_file import Book, as_int, save_book
from zettel.library.reading_mode import REFLECTIVE

REFLECTION_KINDS = {
    "metafora": "Metáfora",
    "pensamento": "Pensamento",
    "analogia": "Analogia",
    "questao_filosofica": "Questão filosófica",
    "personagem": "Personagem",
    "cena": "Cena",
}
MAX_FIELD = 20_000


class ReadingNoteError(ValueError):
    pass


@dataclass
class ReadingNote:
    topic: str
    page: int | None = None
    excerpt: str = ""
    # Modo conceito: o resumo, os conceitos e a tese candidata, como no /notes/new.
    summary: str = ""
    concepts: list[str] = field(default_factory=list)
    candidate: str = ""
    # Modo reflexão: o que o trecho oferece para pensar.
    reflection_kind: str = ""
    characters: list[str] = field(default_factory=list)
    reflection: str = ""
    analogy: str = ""

    @classmethod
    def from_form(cls, form: Any) -> ReadingNote:
        def text(key: str) -> str:
            return str(form.get(key) or "").replace("\r\n", "\n").strip()[:MAX_FIELD]

        def items(key: str) -> list[str]:
            return [s.strip() for s in text(key).replace("\n", ",").split(",") if s.strip()]

        note = cls(
            topic=" ".join(text("topic").split())[:200],
            page=as_int(text("page")),
            excerpt=text("excerpt"),
            summary=text("summary"),
            concepts=items("concepts"),
            candidate=text("candidate"),
            reflection_kind=text("reflection_kind"),
            characters=items("characters"),
            reflection=text("reflection"),
            analogy=text("analogy"),
        )
        if not note.topic:
            raise ReadingNoteError("Dê um título à nota de leitura.")
        if note.reflection_kind and note.reflection_kind not in REFLECTION_KINDS:
            raise ReadingNoteError("Tipo de reflexão desconhecido.")
        return note


def ensure_source(cfg: Any, db: Any, book: Book) -> str:
    """O ``source_id`` do livro; cria o SRC manual quando ele não tem fonte."""
    from zettel.harvester.citekey import generate_citekey
    from zettel.new_note import scaffold_manual_note
    from zettel.sync import sync_source_note

    if book.source_id and db.get_source(book.source_id):
        return book.source_id
    year = as_int(book.meta.get("original_publication_year"))
    citekey = generate_citekey(db, book.authors, year, book.title)
    result = scaffold_manual_note(
        cfg,
        "source",
        book.title,
        source_id=f"@{citekey}",
        authors=book.authors,
        year=year,
        document_type="livro",
        isbn=str(book.meta.get("isbn") or "") or None,
    )
    sync_source_note(cfg, db, result.path)
    book.meta["source_id"] = f"@{citekey}"
    save_book(cfg, book, expected_version=book.version)
    return f"@{citekey}"


def _scaffold_fields(note: ReadingNote, reflective: bool) -> dict[str, Any]:
    """O que vai em cada seção da LIT, que ``manual_lit`` lê de volta na adoção."""
    if not reflective:
        return {
            "summary": note.summary or None,
            "key_concepts": note.concepts or None,
            "candidates": [{"thesis": note.candidate}] if note.candidate else None,
        }
    summary = note.reflection
    if note.analogy:
        summary = f"{summary}\n\nUso como analogia: {note.analogy}".strip()
    concepts = []
    if note.reflection_kind:
        concepts.append(f"Tipo: {REFLECTION_KINDS[note.reflection_kind]}")
    concepts += [f"Personagem: {name}" for name in note.characters]
    idea = note.candidate or note.analogy or note.reflection
    return {
        "summary": summary or None,
        "key_concepts": concepts or None,
        "candidates": [{"thesis": idea}] if idea else None,
    }


def create_reading_note(cfg: Any, db: Any, book: Book, note: ReadingNote) -> Path:
    """Escreve e adota a LIT; devolve o caminho da nota."""
    from zettel.manual_lit import adopt_manual_literature
    from zettel.new_note import scaffold_manual_note
    from zettel.vault import compose_note, parse_frontmatter

    source_id = ensure_source(cfg, db, book)
    row = db.get_book(book.book_id)
    reflective = (row or {}).get("reading_mode") == REFLECTIVE
    result = scaffold_manual_note(
        cfg,
        "literature",
        note.topic,
        source_id=source_id,
        granular=True,
        page=note.page,
        source_text=note.excerpt or None,
        **_scaffold_fields(note, reflective),
    )
    meta, body = parse_frontmatter(result.path.read_text(encoding="utf-8"))
    if reflective:
        meta["reading_mode"] = REFLECTIVE
        meta["reflection_kind"] = note.reflection_kind or None
        meta["characters"] = note.characters
        result.path.write_text(compose_note(meta, body), encoding="utf-8")
    adopt_manual_literature(cfg, db, None, result.path, meta, body)
    return result.path
