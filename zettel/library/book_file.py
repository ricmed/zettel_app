"""A nota LIV (``60_Estante/LIV - <ULID> - <slug>.md``): leitura, criação e gravação.

O arquivo é o registro do livro: o espelho no SQLite (``library.mirror``) é
reconstruído a partir dele. A gravação é recusada quando o arquivo mudou desde
que foi lido (uma edição no Obsidian, outra aba), como na Mesa (ADR-067).
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

from zettel.markdown_fences import h2_section, headings_outside_fences
from zettel.vault import LIBRARY_DIR, _slug, compose_note, parse_frontmatter

BOOK_TYPE = "book"
BOOK_PREFIX = "LIV"
BOOK_NOTES_BLOCK = "auto-book-notes"
SUMMARY_HEADING = "Resumo"
IMPRESSIONS_HEADING = "Impressões"

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

# Ordem das propriedades no frontmatter: todas presentes, vazias quando não
# preenchidas, para o painel de propriedades do Obsidian mostrar cada uma.
FIELD_ORDER = (
    "type",
    "book_id",
    "code_number",
    "code",
    "title",
    "original_title",
    "series",
    "series_number",
    "authors",
    "source_id",
    "isbn",
    "pages",
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
    "origin",
    "created_at",
    "updated_at",
)
LIST_FIELDS = frozenset({"authors", "genres", "subjects", "readings"})
INT_FIELDS = frozenset({"code_number", "series_number", "pages", "original_publication_year"})

_BOOK_ID = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")
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
    book_id: str
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
        return as_str_list(self.meta.get("authors"))

    @property
    def genres(self) -> list[str]:
        return as_str_list(self.meta.get("genres"))

    @property
    def source_id(self) -> str | None:
        value = str(self.meta.get("source_id") or "").strip()
        return value or None

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


# ── Arquivo ────────────────────────────────────────────────────────────


def books_dir(cfg: Any) -> Path:
    return Path(cfg.vault_path) / LIBRARY_DIR


def version_of(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()[:16]


def read_book(path: Path) -> Book | None:
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    # Um editor no Windows pode gravar CRLF; as seções são lidas por linha.
    text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n")
    meta, body = parse_frontmatter(text)
    if not isinstance(meta, dict) or meta.get("type") != BOOK_TYPE:
        return None
    book_id = str(meta.get("book_id") or "")
    if not _BOOK_ID.fullmatch(book_id):
        return None
    return Book(book_id, path, meta, body, version_of(raw))


def list_book_files(cfg: Any) -> list[Path]:
    folder = books_dir(cfg)
    return sorted(folder.glob("*.md")) if folder.is_dir() else []


def list_books(cfg: Any) -> list[Book]:
    return [b for b in (read_book(p) for p in list_book_files(cfg)) if b]


def load_book(cfg: Any, book_id: str) -> Book:
    if not _BOOK_ID.fullmatch(book_id or ""):
        raise BookNotFound(book_id)
    for path in books_dir(cfg).glob(f"{BOOK_PREFIX} - {book_id}*.md"):
        book = read_book(path)
        if book and book.book_id == book_id:
            return book
    # Um arquivo renomeado no Obsidian guarda o id no frontmatter.
    for book in list_books(cfg):
        if book.book_id == book_id:
            return book
    raise BookNotFound(book_id)


def next_code_number(cfg: Any) -> int:
    """Próximo número da sequência universal: maior já usado + 1 (nunca reaproveita)."""
    numbers = [as_int(b.meta.get("code_number")) for b in list_books(cfg)]
    return max((n for n in numbers if n), default=0) + 1


def ordered_meta(meta: dict[str, Any]) -> dict[str, Any]:
    """As propriedades conhecidas na ordem de ``FIELD_ORDER``, depois as do autor."""
    out = {key: meta.get(key) for key in FIELD_ORDER}
    for key in LIST_FIELDS:
        if out[key] is None:
            out[key] = []
    if out["ratings"] is None:
        out["ratings"] = {}
    out.update({k: v for k, v in meta.items() if k not in out})
    return out


def initial_body(title: str) -> str:
    return (
        f"# {title}\n\n## {SUMMARY_HEADING}\n\n## {IMPRESSIONS_HEADING}\n\n"
        f"## Notas de leitura\n\n<!-- zettel:{BOOK_NOTES_BLOCK}:start -->\n"
        f"<!-- zettel:{BOOK_NOTES_BLOCK}:end -->\n"
    )


def _write(path: Path, content: str) -> str:
    data = content.encode("utf-8")
    partial = path.with_name(f".{path.name}.part")
    try:
        partial.write_bytes(data)
        os.replace(partial, path)
    except PermissionError as exc:
        # No Windows a troca falha com acesso negado quando o destino recém-criado
        # já está aberto (Obsidian, antivírus, indexador). O .part não é a nota.
        with contextlib.suppress(OSError):
            partial.unlink()
        raise BookFileBusy(path) from exc
    return version_of(data)


def create_book(cfg: Any, fields: dict[str, Any]) -> Book:
    """Cria a LIV com o próximo código; ``fields`` traz ao menos ``title``."""
    from ulid import ULID

    from zettel.time import now_vault_iso

    title = " ".join(str(fields.get("title") or "").split())[:300] or "Livro sem título"
    fmt = fields.get("format") if fields.get("format") in FORMATS else "fisico"
    number = next_code_number(cfg)
    now = now_vault_iso(cfg.vault_timezone)
    book_id = str(ULID())
    meta = ordered_meta(
        {
            **{k: v for k, v in fields.items() if k in FIELD_ORDER},
            "type": BOOK_TYPE,
            "book_id": book_id,
            "code_number": number,
            "code": format_code(number, fmt, cfg.library.format_letters),
            "title": title,
            "format": fmt,
            "status": fields.get("status")
            if fields.get("status") in STATUSES
            else cfg.library.initial_status,
            "origin": "manual",
            "created_at": now,
            "updated_at": now,
        }
    )
    folder = books_dir(cfg)
    folder.mkdir(parents=True, exist_ok=True)
    slug = _slug(title, max_len=60) or "livro"
    path = folder / f"{BOOK_PREFIX} - {book_id} - {slug}.md"
    body = initial_body(title)
    version = _write(path, compose_note(meta, body))
    return Book(book_id, path, meta, body, version)


def save_book(cfg: Any, book: Book, *, expected_version: str | None) -> Book:
    """Grava o livro editado; recusa quando o arquivo não é mais o que foi editado."""
    from zettel.time import now_vault_iso

    try:
        current = version_of(book.path.read_bytes())
    except OSError as exc:
        raise BookNotFound(book.book_id) from exc
    if expected_version is not None and current != expected_version:
        raise BookConflict(book.book_id)
    number = as_int(book.meta.get("code_number"))
    if number:
        book.meta["code"] = format_code(number, book.format, cfg.library.format_letters)
    book.meta["updated_at"] = now_vault_iso(cfg.vault_timezone)
    book.meta = ordered_meta(book.meta)
    book.version = _write(book.path, compose_note(book.meta, book.body))
    return book


# ── Seções do corpo escritas pelo autor ────────────────────────────────


def section_text(body: str, heading: str) -> str:
    return h2_section(body, heading)


def replace_section(body: str, heading: str, text: str) -> str:
    """Troca o texto sob ``## heading`` (até o próximo H2 fora de fence).

    A seção ausente é criada antes de ``## Notas de leitura``, ou no fim.
    """
    text = text.strip("\n")
    matches = headings_outside_fences(_H2, body)
    for i, m in enumerate(matches):
        if m.group(1) == heading:
            end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
            inner = f"\n\n{text}\n\n" if text else "\n\n"
            return body[: m.end()] + inner + body[end:].lstrip("\n")
    block = f"## {heading}\n\n{text}\n\n" if text else f"## {heading}\n\n"
    anchor = next((m for m in matches if m.group(1) == "Notas de leitura"), None)
    if anchor:
        return body[: anchor.start()] + block + body[anchor.start() :]
    return body.rstrip("\n") + "\n\n" + block
