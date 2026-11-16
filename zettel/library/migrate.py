"""Passa a estante da nota LIV para o SRC e só então apaga a nota antiga.

Nada é apagado antes da conferência. Um campo de leitor vazio no SRC recebe o
da nota antiga. Título, autores, ISBN e bibliografia que já estão no SRC não
são trocados: uma diferença entra no relatório e a nota antiga fica no disco
até ``zettel migrate-acervo --accept``. Sem resposta do disco (um valor que
devia ter sido copiado não está no SRC, ou a nota ainda não tem SRC), o comando
não apaga nada.

O nome da pasta e o prefixo do arquivo antigo existem só neste módulo: é ele
quem os remove.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from zettel.library.book_file import as_str_list, load_book, same_reader_value, save_book
from zettel.library.source_body import (
    BIBLIOGRAPHIC_HEADINGS,
    READER_FIELDS,
    assemble_body,
    author_sections,
    blank,
    section_body,
)
from zettel.markdown_fences import headings_outside_fences
from zettel.vault import compose_note, parse_frontmatter

LEGACY_DIR = "60_Estante"
LEGACY_GLOB = "LIV - *.md"
_IDENTITY = ("title", "isbn")
_BIBLIO = (
    "publisher",
    "place",
    "year",
    "edition",
    "translator",
    "doi",
    "journal",
    "volume",
    "issue",
)


@dataclass
class Divergence:
    path: str
    field: str
    kept: str
    incoming: str


@dataclass
class MigrationReport:
    copied: list[str] = field(default_factory=list)
    created: list[str] = field(default_factory=list)
    conflicts: list[Divergence] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    finished: bool = False

    @property
    def ok(self) -> bool:
        """Terminou sem falha de conferência. Divergência aceita ainda aparece no texto."""
        return self.finished and not self.problems

    def text(self) -> str:
        lines = [
            f"Copiadas para um SRC existente: {len(self.copied)}",
            f"SRC criados a partir de ficha sem fonte: {len(self.created)}",
            f"Divergências: {len(self.conflicts)}",
            f"Notas antigas apagadas: {len(self.deleted)}",
        ]
        for item in self.conflicts:
            lines.append(
                f"  {item.path}: {item.field} mantido no SRC ({item.kept!r}); "
                f"a ficha antiga tinha {item.incoming!r}"
            )
        for item in self.problems:
            lines.append(f"  falha: {item}")
        if self.finished:
            lines.append("A pasta antiga foi removida e o espelho antigo foi derrubado.")
        elif self.conflicts and not self.problems:
            lines.append(
                "Nada mais foi apagado enquanto houver divergência. "
                "Confira o relatório e rode `zettel migrate-acervo --accept` "
                "para manter o valor do SRC e apagar a ficha antiga."
            )
        elif self.problems:
            lines.append("Nada foi apagado: a conferência não fechou.")
        return "\n".join(lines)


def legacy_dir(cfg: Any) -> Path:
    return Path(cfg.vault_path) / LEGACY_DIR


def legacy_notes(cfg: Any) -> list[Path]:
    folder = legacy_dir(cfg)
    if not folder.is_dir():
        return []
    return sorted(folder.glob(LEGACY_GLOB))


def migrate_shelf(cfg: Any, db: Any, *, accept: bool = False) -> MigrationReport:
    """Copia, confere e só então apaga. ``accept`` registra a divergência como aceita.

    A primeira passagem só grava o SRC. A exclusão acontece depois, e só se
    nenhuma conferência falhou. Sem ``accept``, a nota com divergência fica.
    """
    report = MigrationReport()
    notes = legacy_notes(cfg)
    if not notes:
        _finish(cfg, db, report)
        return report

    outcomes: list[tuple[Path, str]] = []
    for path in notes:
        outcomes.append((path, _migrate_one(cfg, db, path, report)))
    if report.problems or any(status == "problem" for _, status in outcomes):
        report.finished = False
        return report

    blocked = {path for path, status in outcomes if status == "conflict" and not accept}
    for path, _status in outcomes:
        if path in blocked:
            continue
        path.unlink()
        report.deleted.append(path.name)
    if legacy_notes(cfg):
        report.finished = False
        return report
    _finish(cfg, db, report)
    return report


def _migrate_one(cfg: Any, db: Any, path: Path, report: MigrationReport) -> str:
    meta, body = parse_frontmatter(path.read_text(encoding="utf-8"))
    if not isinstance(meta, dict):
        report.problems.append(f"{path.name}: frontmatter ilegível")
        return "problem"
    sections = author_sections(body)
    source_id = str(meta.get("source_id") or "").strip()
    book = _load_existing(cfg, source_id)
    if book is None:
        book = _create_from_legacy(cfg, db, meta, sections)
        if book is None:
            report.problems.append(f"{path.name}: não virou SRC")
            return "problem"
        meta["source_id"] = book.source_id
        path.write_text(compose_note(meta, body), encoding="utf-8", newline="\n")
        report.created.append(book.source_id)
    conflicts = _merge_into(cfg, book, meta, sections, path)
    if not _verify(cfg, book.source_id, meta, sections, conflicts):
        report.problems.append(f"{path.name}: o SRC {book.source_id} não recebeu a cópia")
        return "problem"
    if conflicts:
        report.conflicts.extend(conflicts)
        return "conflict"
    report.copied.append(book.source_id)
    return "ok"


def _finish(cfg: Any, db: Any, report: MigrationReport) -> None:
    folder = legacy_dir(cfg)
    if folder.is_dir() and not any(folder.iterdir()):
        folder.rmdir()
    elif folder.is_dir():
        report.problems.append(f"{LEGACY_DIR} ainda tem arquivos; as tabelas antigas ficam")
        report.finished = False
        return
    db.mark_shelf_migrated()
    db.drop_legacy_shelf_tables()
    report.finished = True


def _load_existing(cfg: Any, source_id: str):
    from zettel.library.book_file import BookNotFound

    if not source_id:
        return None
    try:
        return load_book(cfg, source_id)
    except BookNotFound:
        return None


def _create_from_legacy(cfg: Any, db: Any, meta: dict[str, Any], sections: list[tuple[str, str]]):
    from zettel.library.book_file import BookFileBusy, create_book

    source_id = str(meta.get("source_id") or "").strip()
    citekey = source_id[1:] if source_id.startswith("@") else ""
    fields: dict[str, Any] = {
        "title": meta.get("title") or "Sem título",
        "authors": as_str_list(meta.get("authors") or meta.get("author")),
        "isbn": meta.get("isbn") or None,
        "document_type": meta.get("document_type") or "livro",
        "format": meta.get("format") or "fisico",
        "status": meta.get("status"),
        "year": meta.get("year") or meta.get("original_publication_year"),
    }
    if source_id:
        fields["source_id"] = source_id
    if citekey:
        fields["citekey"] = citekey
    for key in ("code_number", "code", *READER_FIELDS):
        if key in meta and not blank(meta.get(key)):
            fields[key] = meta.get(key)
    try:
        book = create_book(cfg, fields, db=db)
    except BookFileBusy:
        return None
    return _merge_sections(cfg, book, sections, [], str(book.path))


def _merge_into(cfg, book, meta: dict[str, Any], sections, legacy: Path) -> list[Divergence]:
    conflicts: list[Divergence] = []
    path = str(legacy)
    _identity_conflicts(book, meta, path, conflicts)
    for key in READER_FIELDS:
        incoming = meta.get(key)
        current = book.meta.get(key)
        if blank(incoming):
            continue
        if blank(current):
            book.meta[key] = incoming
        elif not same_reader_value(current, incoming):
            conflicts.append(Divergence(path, key, _show(current), _show(incoming)))
    _merge_sections(cfg, book, sections, conflicts, path)
    return conflicts


def _merge_sections(cfg, book, sections, conflicts: list[Divergence], legacy_path: str):
    """Regrava o corpo com as seções do autor. Devolve o livro gravado."""
    header = _header(book.body)
    current = dict(author_sections(book.body))
    merged: list[tuple[str, str]] = []
    seen: set[str] = set()
    for title, block in sections:
        seen.add(title)
        existing = current.get(title)
        if existing is None or blank(section_body(existing)):
            merged.append((title, block))
            continue
        if section_body(existing) != section_body(block):
            conflicts.append(
                Divergence(legacy_path, f"## {title}", section_body(existing), section_body(block))
            )
        merged.append((title, existing))
    for title, block in author_sections(book.body):
        if title not in seen:
            merged.append((title, block))
    notes = _notes(book.body)
    book.body = assemble_body(header, merged, notes)
    return save_book(cfg, book, expected_version=book.version)


def _identity_conflicts(book, meta: dict[str, Any], path: str, conflicts: list[Divergence]) -> None:
    from zettel.harvester.biblio_dedupe import normalize_isbn
    from zettel.search_terms import fold

    src_title = fold(str(book.meta.get("title") or ""))
    liv_title = fold(str(meta.get("title") or ""))
    if src_title and liv_title and src_title != liv_title:
        conflicts.append(
            Divergence(path, "title", str(book.meta.get("title")), str(meta.get("title")))
        )
    src_authors = {fold(name) for name in as_str_list(book.meta.get("author"))}
    liv_authors = {fold(name) for name in as_str_list(meta.get("authors") or meta.get("author"))}
    if src_authors and liv_authors and src_authors != liv_authors:
        conflicts.append(
            Divergence(
                path, "authors", ", ".join(sorted(src_authors)), ", ".join(sorted(liv_authors))
            )
        )
    src_isbn = normalize_isbn(str(book.meta.get("isbn") or ""))
    liv_isbn = normalize_isbn(str(meta.get("isbn") or ""))
    if src_isbn and liv_isbn and src_isbn != liv_isbn:
        conflicts.append(Divergence(path, "isbn", src_isbn, liv_isbn))
    for key in _BIBLIO:
        current = book.meta.get(key)
        incoming = meta.get(key)
        if blank(current) or blank(incoming) or same_reader_value(current, incoming):
            continue
        conflicts.append(Divergence(path, key, _show(current), _show(incoming)))


def _verify(
    cfg, source_id: str, meta: dict[str, Any], sections, conflicts: list[Divergence]
) -> bool:
    from zettel.library.book_file import BookNotFound

    try:
        book = load_book(cfg, source_id)
    except BookNotFound:
        return False
    blocked = {item.field for item in conflicts}
    for key in READER_FIELDS:
        if blank(meta.get(key)) or key in blocked:
            continue
        if not same_reader_value(book.meta.get(key), meta.get(key)):
            return False
    present = {title: section_body(block) for title, block in author_sections(book.body)}
    for title, block in sections:
        if f"## {title}" in blocked:
            continue
        if present.get(title) != section_body(block):
            return False
    return True


def _header(body: str) -> str:
    from zettel.library.source_body import _H2

    for match in headings_outside_fences(_H2, body or ""):
        title = match.group(1).strip()
        if title not in BIBLIOGRAPHIC_HEADINGS:
            return body[: match.start()].strip()
    return (body or "").strip()


def _notes(body: str) -> str | None:
    from zettel.library.source_body import _notes_block

    return _notes_block(body)


def _show(value: Any) -> str:
    return str(value)[:180]
