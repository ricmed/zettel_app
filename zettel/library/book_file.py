"""A ficha da estante é a nota SRC (``10_Sources/SRC - AuthorYear - slug.md``).

O arquivo guarda a bibliografia e o que é do leitor (código, formato, status,
leituras, gêneros, capa, resumo, avaliação). O espelho no SQLite é reconstruído
a partir dele. A gravação é recusada quando o arquivo mudou desde que foi lido
(uma edição no Obsidian, outra aba), como na Mesa.
"""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zettel.library.source_body import (
    LIST_FIELDS,
    NOTES_BLOCK,
    READER_FIELDS,
    assemble_body,
    render_notes,
)
from zettel.markdown_fences import h2_section, headings_outside_fences, unclosed_fence
from zettel.vault import compose_note, parse_frontmatter

SUMMARY_HEADING = "Resumo"
IMPRESSIONS_HEADING = "Impressões"
BOOK_NOTES_BLOCK = NOTES_BLOCK

FORMATS = ("fisico", "digital", "audiolivro")
STATUSES = ("quero_ler", "lendo", "lido", "abandonado", "relendo")
NATURES = ("ficcao", "nao_ficcao")
READING_MODES = ("reflexao", "conceito")

FORMAT_LABELS = {"fisico": "Físico", "digital": "Digital", "audiolivro": "Audiolivro"}
STATUS_LABELS = {
    "quero_ler": "Quero ler",
    "lendo": "Lendo",
    "lido": "Lido",
    "abandonado": "Abandonado",
    "relendo": "Relendo",
}
NATURE_LABELS = {"ficcao": "Ficção", "nao_ficcao": "Não ficção"}
READING_MODE_LABELS = {"reflexao": "Reflexão", "conceito": "Conceito"}

_HEAD = (
    "type",
    "source_id",
    "citekey",
    "title",
    "author",
    "year",
    "document_type",
)
_TAIL = ("origin", "origin_path", "origin_type", "checksum", "created_at", "updated_at")
FIELD_ORDER = _HEAD + READER_FIELDS + _TAIL
_H2 = re.compile(r"^##[ \t]+(.+?)[ \t]*$", re.MULTILINE)


class BookNotFound(LookupError):
    pass


class BookConflict(RuntimeError):
    """O arquivo mudou no disco depois que a página que o edita foi gerada."""


class BookFileBusy(RuntimeError):
    """O sistema negou gravar a nota; outro processo segura o arquivo."""

    def __init__(self, path: Path):
        self.path = path
        super().__init__(
            f"O Windows negou acesso a «{path.name}». Outro programa mantém o arquivo "
            "aberto — em geral o Obsidian, o antivírus ou a busca do Windows."
        )


@dataclass
class Reading:
    start: str | None
    end: str | None


@dataclass
class Book:
    """Uma nota SRC vista pela estante. A identidade é o ``source_id``."""

    source_id: str
    path: Path
    meta: dict[str, Any]
    body: str
    version: str

    @property
    def title(self) -> str:
        return str(self.meta.get("title") or "Sem título")

    @property
    def code(self) -> str:
        return str(self.meta.get("code") or "")

    @property
    def format(self) -> str:
        value = str(self.meta.get("format") or "")
        return value if value in FORMATS else ""

    @property
    def status(self) -> str:
        value = str(self.meta.get("status") or "")
        return value if value in STATUSES else ""

    @property
    def authors(self) -> list[str]:
        raw = self.meta.get("author")
        if raw is None:
            raw = self.meta.get("authors")
        return as_str_list(raw)

    @property
    def genres(self) -> list[str]:
        return as_str_list(self.meta.get("genres"))

    @property
    def readings(self) -> list[Reading]:
        return readings_of(self.meta)

    @property
    def ratings(self) -> dict[str, float]:
        return ratings_of(self.meta)


# ── Normalização de valores lidos do YAML ──────────────────────────────


def as_str_list(value: Any) -> list[str]:
    """Lista de textos não vazios; aceita uma string única ou separada por vírgula."""
    if value is None:
        return []
    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, (list, tuple)):
        items = [str(v) for v in value if v is not None]
    else:
        items = [str(value)]
    return [s.strip() for s in items if str(s).strip()]


def as_int(value: Any) -> int | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        return int(str(value).strip())
    except ValueError:
        return None


def as_date(value: Any) -> str | None:
    """Data ISO (``AAAA-MM-DD``); o YAML do Obsidian já entrega ``date``."""
    if isinstance(value, datetime.datetime):
        return value.date().isoformat()
    if isinstance(value, datetime.date):
        return value.isoformat()
    text = str(value or "").strip()[:10]
    try:
        return datetime.date.fromisoformat(text).isoformat()
    except ValueError:
        return None


def readings_of(meta: dict[str, Any]) -> list[Reading]:
    raw = meta.get("readings")
    if not isinstance(raw, list):
        return []
    found = []
    for item in raw:
        if isinstance(item, dict):
            reading = Reading(as_date(item.get("start")), as_date(item.get("end")))
            if reading.start or reading.end:
                found.append(reading)
    return found


def ratings_of(meta: dict[str, Any]) -> dict[str, float]:
    raw = meta.get("ratings")
    if not isinstance(raw, dict):
        return {}
    out: dict[str, float] = {}
    for key, value in raw.items():
        if value is None or value == "" or isinstance(value, bool):
            continue
        try:
            out[str(key)] = float(value)
        except (TypeError, ValueError):
            continue
    return out


def format_code(number: int, fmt: str, letters: dict[str, str]) -> str:
    """``0001 - F``: número sequencial universal mais a letra do formato."""
    letter = letters.get(fmt, "")
    return f"{number:04d} - {letter}" if letter else f"{number:04d}"


def code_number_of(meta: dict[str, Any]) -> int | None:
    """O número do código, ou o prefixo de ``code`` (``0002 - F``) quando a chave falta.

    Fichas gravadas antes de ``code_number`` existir trazem só a linha ``code``.
    Tratar isso como "sem código" recomeçaria a sequência em 0001.
    """
    number = as_int(meta.get("code_number"))
    if number:
        return number
    text = str(meta.get("code") or "").strip()
    if not text:
        return None
    return as_int(text.split("-", 1)[0])


def format_letter_of(code: Any) -> str:
    text = str(code or "")
    if "-" not in text:
        return ""
    letter = text.split("-", 1)[1].strip()
    return letter[:1].upper()


def format_for_letter(letter: str, letters: dict[str, str]) -> str:
    """O formato cuja letra é ``letter``; vazio quando a letra não está no mapa."""
    inverse = {str(value).strip().upper(): key for key, value in letters.items()}
    fmt = inverse.get(letter.upper(), "")
    return fmt if fmt in FORMATS else ""


def adopt_legacy_code(meta: dict[str, Any], letters: dict[str, str] | None = None) -> None:
    """Preenche número e formato que o texto ``code`` já diz, sem gravar o arquivo."""
    number = code_number_of(meta)
    if number and not as_int(meta.get("code_number")):
        meta["code_number"] = number
    if letters is None or str(meta.get("format") or "") in FORMATS:
        return
    fmt = format_for_letter(format_letter_of(meta.get("code")), letters)
    if fmt:
        meta["format"] = fmt


def same_reader_value(left: Any, right: Any) -> bool:
    """Os dois valores de leitor contam como o mesmo, para a migração e o espelho."""
    if isinstance(left, bool) or isinstance(right, bool):
        return bool(left) == bool(right)
    if isinstance(left, list) or isinstance(right, list):
        return as_str_list(left) == as_str_list(right)
    if isinstance(left, dict) or isinstance(right, dict):
        return ratings_of({"ratings": left}) == ratings_of({"ratings": right})
    left_int, right_int = as_int(left), as_int(right)
    if left_int is not None and right_int is not None:
        return left_int == right_int
    return str(left or "").strip() == str(right or "").strip()


# ── Arquivo ────────────────────────────────────────────────────────────


def sources_dir(cfg: Any) -> Path:
    return Path(cfg.vault_path) / "10_Sources"


def version_of(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()[:16]


def read_book(path: Path) -> Book | None:
    """A nota SRC neste caminho, ou ``None`` quando não é uma fonte legível."""
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n")
    meta, body = parse_frontmatter(text)
    if not isinstance(meta, dict) or meta.get("type") != "source":
        return None
    source_id = str(meta.get("source_id") or "").strip()
    if not source_id:
        return None
    adopt_legacy_code(meta)
    return Book(source_id, path, meta, body, version_of(raw))


def list_source_files(cfg: Any) -> list[Path]:
    folder = sources_dir(cfg)
    return sorted(folder.glob("*.md")) if folder.is_dir() else []


def list_books(cfg: Any) -> list[Book]:
    """As fontes cujo tipo entra na estante."""
    wanted = set(cfg.library.document_types)
    books = []
    for path in list_source_files(cfg):
        book = read_book(path)
        if book and str(book.meta.get("document_type") or "") in wanted:
            adopt_legacy_code(book.meta, cfg.library.format_letters)
            books.append(book)
    return books


def load_book(cfg: Any, source_id: str) -> Book:
    """A ficha desta fonte. ``BookNotFound`` quando o SRC não está no vault."""
    from zettel.library.sources import source_note_path

    source_id = str(source_id or "").strip()
    if not source_id:
        raise BookNotFound(source_id)
    path = source_note_path(cfg, source_id)
    book = read_book(path) if path else None
    if book is None or book.source_id != source_id:
        raise BookNotFound(source_id)
    adopt_legacy_code(book.meta, cfg.library.format_letters)
    return book


def next_code_number(cfg: Any) -> int:
    """Próximo número da sequência universal: maior já usado + 1 (nunca reaproveita)."""
    numbers = []
    for path in list_source_files(cfg):
        book = read_book(path)
        if book:
            numbers.append(code_number_of(book.meta))
    return max((n for n in numbers if n), default=0) + 1


def ordered_meta(meta: dict[str, Any]) -> dict[str, Any]:
    """Identidade, campos de leitor e o que mais o arquivo já tinha."""
    cleaned = dict(meta)
    if cleaned.get("author") is None and cleaned.get("authors") is not None:
        cleaned["author"] = cleaned.get("authors")
    cleaned.pop("authors", None)
    cleaned.pop("book_id", None)
    cleaned["type"] = "source"
    out: dict[str, Any] = {}
    for key in _HEAD:
        if key in cleaned:
            out[key] = cleaned[key]
    for key in READER_FIELDS:
        out[key] = cleaned.get(key)
    for key in _TAIL:
        if key in cleaned:
            out[key] = cleaned[key]
    for key, value in cleaned.items():
        if key not in out:
            out[key] = value
    for key in LIST_FIELDS:
        if out.get(key) is None:
            out[key] = []
    if out.get("ratings") is None:
        out["ratings"] = {}
    if out.get("favorite") is None:
        out["favorite"] = False
    return out


def _write(path: Path, content: str) -> str:
    data = content.encode("utf-8")
    partial = path.with_name(f".{path.name}.part")
    try:
        partial.write_bytes(data)
        os.replace(partial, path)
    except PermissionError as exc:
        with contextlib.suppress(OSError):
            partial.unlink()
        raise BookFileBusy(path) from exc
    return version_of(data)


def initial_body(title: str = "") -> str:
    """Corpo de uma ficha nova: resumo, impressões e o bloco de notas."""
    del title
    return _shelf_body("")


def _shelf_body(header: str) -> str:
    sections = [
        (SUMMARY_HEADING, f"## {SUMMARY_HEADING}"),
        (IMPRESSIONS_HEADING, f"## {IMPRESSIONS_HEADING}"),
    ]
    return assemble_body(header, sections, render_notes("_Nenhuma nota de leitura ainda._"))


def _unused_citekey(db: Any, citekey: str, title: str, folder: Path) -> tuple[str, Path]:
    """Citekey e caminho livres.

    O nome curto do SRC usa só autor e ano, então duas fichas do mesmo título
    cairiam no mesmo arquivo. A segunda leva o citekey inteiro no nome.
    """
    from zettel.harvester.citekey import _disambiguation_suffix
    from zettel.vault import note_filename, source_note_filename

    base = citekey
    index = 0
    while db.get_source_by_citekey(citekey):
        index += 1
        citekey = f"{base}{_disambiguation_suffix(index)}"
    path = folder / source_note_filename(citekey, title)
    while path.exists():
        index += 1
        citekey = f"{base}{_disambiguation_suffix(index)}"
        if db.get_source_by_citekey(citekey):
            continue
        path = folder / note_filename("SRC", citekey, title)
    return citekey, path


def create_book(cfg: Any, fields: dict[str, Any], db: Any | None = None) -> Book:
    """Cria o SRC manual na hora, com o próximo código. ``fields`` traz o título."""
    from zettel.harvester.citekey import generate_citekey
    from zettel.time import now_vault_iso
    from zettel.vault import build_source_note, source_note_filename

    title = " ".join(str(fields.get("title") or "").split())[:300] or "Sem título"
    authors = as_str_list(fields.get("authors") or fields.get("author"))
    year = as_int(fields.get("year") or fields.get("original_publication_year"))
    fmt = fields.get("format") if fields.get("format") in FORMATS else "fisico"
    document_type = str(fields.get("document_type") or "livro")
    status = (
        fields.get("status") if fields.get("status") in STATUSES else cfg.library.initial_status
    )
    folder = sources_dir(cfg)
    folder.mkdir(parents=True, exist_ok=True)
    if db is None:
        from zettel.state import StateDB

        db = StateDB(cfg.state_db_path)
        own_db = True
    else:
        own_db = False
    reserved: Path | None = None
    try:
        explicit = bool(str(fields.get("citekey") or "") or str(fields.get("source_id") or ""))
        citekey = str(fields.get("citekey") or "") or generate_citekey(db, authors, year, title)
        # Duas fichas órfãs com o mesmo título nascem juntas, antes de existir
        # linha no SQLite. O arquivo que a primeira acabou de gravar também
        # ocupa o nome curto: a segunda não pode reescrever a primeira.
        if not explicit:
            citekey, reserved = _unused_citekey(db, citekey, title, folder)
    finally:
        if own_db:
            db.close()
    source_id = str(fields.get("source_id") or f"@{citekey}")
    number = as_int(fields.get("code_number")) or next_code_number(cfg)
    now = now_vault_iso(cfg.vault_timezone)
    meta, body = build_source_note(
        source_id,
        citekey,
        title,
        authors,
        year,
        str(fields.get("origin_path") or ""),
        "md",
        str(fields.get("checksum") or ""),
        origin="manual",
        document_type=document_type,
        vault_timezone=cfg.vault_timezone,
    )
    meta["citekey"] = citekey
    meta["author"] = authors
    meta["code_number"] = number
    meta["format"] = fmt
    meta["code"] = str(fields.get("code") or "") or format_code(
        number, fmt, cfg.library.format_letters
    )
    meta["status"] = status
    for key in READER_FIELDS:
        if key in fields and fields[key] not in (None, ""):
            meta[key] = fields[key]
    meta["code_number"] = number
    meta["format"] = fmt
    meta["code"] = meta["code"]
    meta["status"] = status
    if fields.get("isbn"):
        meta["isbn"] = fields["isbn"]
    meta["created_at"] = fields.get("created_at") or now
    meta["updated_at"] = now
    meta = ordered_meta(meta)
    path = reserved or (folder / source_note_filename(citekey, title))
    if not meta.get("origin_path"):
        meta["origin_path"] = str(path)
    body = _shelf_body(body)
    version = _write(path, compose_note(meta, body))
    return Book(source_id, path, meta, body, version)


def save_book(cfg: Any, book: Book, *, expected_version: str | None) -> Book:
    """Grava a ficha editada; recusa quando o arquivo não é mais o que foi editado."""
    from zettel.time import now_vault_iso

    try:
        current = version_of(book.path.read_bytes())
    except OSError as exc:
        raise BookNotFound(book.source_id) from exc
    if expected_version is not None and current != expected_version:
        raise BookConflict(book.source_id)
    number = code_number_of(book.meta)
    fmt = book.format or str(book.meta.get("format") or "")
    # Sem formato, um código que já tem letra ("0002 - F") fica como está.
    if number and (fmt or not str(book.meta.get("code") or "").strip()):
        book.meta["code_number"] = number
        book.meta["code"] = format_code(number, fmt, cfg.library.format_letters)
    book.meta["type"] = "source"
    book.meta["source_id"] = book.source_id
    book.meta["updated_at"] = now_vault_iso(cfg.vault_timezone)
    book.meta = ordered_meta(book.meta)
    book.version = _write(book.path, compose_note(book.meta, book.body))
    return book


def section_text(body: str, heading: str) -> str:
    return h2_section(body, heading)


def replace_section(body: str, heading: str, text: str) -> str:
    """Troca o texto sob ``## heading`` (até o próximo H2 fora de fence).

    A seção ausente é criada antes de ``## Notas de leitura``, ou no fim. Um
    bloco de código que o texto deixa aberto é fechado no fim dele: aberto, ele
    engoliria as seções seguintes e o bloco de notas.
    """
    text = text.strip("\n")
    marker = unclosed_fence(text)
    if marker:
        text = f"{text}\n{marker}"
    matches = headings_outside_fences(_H2, body)
    for i, match in enumerate(matches):
        if match.group(1) == heading:
            end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
            inner = f"\n\n{text}\n\n" if text else "\n\n"
            return body[: match.end()] + inner + body[end:].lstrip("\n")
    block = f"## {heading}\n\n{text}\n\n" if text else f"## {heading}\n\n"
    anchor = next((match for match in matches if match.group(1) == "Notas de leitura"), None)
    if anchor:
        return body[: anchor.start()] + block + body[anchor.start() :]
    return body.rstrip("\n") + "\n\n" + block
