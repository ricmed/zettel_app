"""Bibliographic metadata — ABNT types, inference, formatting, LLM enrichment."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from zettel.config import AppConfig
from zettel.hashing import compute_llm_call_checksum, normalize_text_for_hash, sha256_hex
from zettel.llm import call_llm, extract_json, get_llm, load_prompt
from zettel.state import StateDB

logger = logging.getLogger(__name__)

DocumentType = Literal[
    "livro",
    "capitulo_livro",
    "artigo_periodico",
    "artigo_internet",
    "material_curso",
    "tese",
    "anais_evento",
    "relatorio",
]

DOCUMENT_TYPES: tuple[DocumentType, ...] = (
    "livro",
    "capitulo_livro",
    "artigo_periodico",
    "artigo_internet",
    "material_curso",
    "tese",
    "anais_evento",
    "relatorio",
)

DOCUMENT_TYPE_LABELS: dict[DocumentType, str] = {
    "livro": "Livro / monografia",
    "capitulo_livro": "Capitulo de livro",
    "artigo_periodico": "Artigo de periodico",
    "artigo_internet": "Artigo / pagina da internet",
    "material_curso": "Material de curso",
    "tese": "Tese / dissertacao / TCC",
    "anais_evento": "Trabalho em anais de evento",
    "relatorio": "Relatorio tecnico",
}

REQUIRED_FIELDS: dict[DocumentType, tuple[str, ...]] = {
    "livro": ("authors", "title", "place", "publisher", "year"),
    "capitulo_livro": (
        "chapter_authors", "chapter_title", "book_title",
        "place", "publisher", "year", "pages",
    ),
    "artigo_periodico": ("authors", "title", "journal", "year"),
    "artigo_internet": ("title", "url", "accessed_at"),
    "material_curso": ("title", "institution"),
    "tese": ("authors", "title", "year", "institution", "degree"),
    "anais_evento": ("authors", "title", "event_name", "year", "place"),
    "relatorio": ("title", "year", "institution"),
}

# Fields shown in SRC frontmatter (besides core title/author/year/document_type).
BIBLIO_FRONTMATTER_FIELDS: tuple[str, ...] = (
    "subtitle",
    "edition",
    "place",
    "publisher",
    "translator",
    "isbn",
    "chapter_authors",
    "chapter_title",
    "book_title",
    "book_editors",
    "pages",
    "journal",
    "volume",
    "issue",
    "doi",
    "url",
    "accessed_at",
    "site_name",
    "published_at",
    "institution",
    "course",
    "discipline",
    "degree",
    "advisor",
    "event_name",
    "report_number",
)

FIELD_LABELS: dict[str, str] = {
    "authors": "Autores",
    "title": "Titulo",
    "subtitle": "Subtitulo",
    "year": "Ano",
    "edition": "Edicao",
    "place": "Cidade / local",
    "publisher": "Editora",
    "translator": "Traducao",
    "isbn": "ISBN",
    "chapter_authors": "Autores do capitulo",
    "chapter_title": "Titulo do capitulo",
    "book_title": "Titulo do livro",
    "book_editors": "Organizadores do livro",
    "pages": "Paginas",
    "journal": "Periodico",
    "volume": "Volume",
    "issue": "Numero",
    "doi": "DOI",
    "url": "URL",
    "accessed_at": "Data de acesso",
    "site_name": "Nome do site",
    "published_at": "Data de publicacao",
    "institution": "Instituicao",
    "course": "Curso",
    "discipline": "Disciplina",
    "degree": "Tipo (tese/dissertacao/TCC)",
    "advisor": "Orientador",
    "event_name": "Nome do evento",
    "report_number": "Numero do relatorio",
}

_MONTHS_PT = (
    "", "jan.", "fev.", "mar.", "abr.", "maio", "jun.",
    "jul.", "ago.", "set.", "out.", "nov.", "dez.",
)


class BibliographicMetadata(BaseModel):
    """Typed bibliographic record for ABNT references."""

    document_type: Optional[DocumentType] = None
    confidence: float = 0.0

    title: Optional[str] = None
    subtitle: Optional[str] = None
    authors: list[str] = Field(default_factory=list)
    year: Optional[int] = None
    edition: Optional[str] = None
    place: Optional[str] = None
    publisher: Optional[str] = None
    translator: Optional[str] = None
    isbn: Optional[str] = None

    chapter_authors: list[str] = Field(default_factory=list)
    chapter_title: Optional[str] = None
    book_title: Optional[str] = None
    book_editors: list[str] = Field(default_factory=list)
    pages: Optional[str] = None

    journal: Optional[str] = None
    volume: Optional[str] = None
    issue: Optional[str] = None
    doi: Optional[str] = None

    url: Optional[str] = None
    accessed_at: Optional[str] = None
    site_name: Optional[str] = None
    published_at: Optional[str] = None

    institution: Optional[str] = None
    course: Optional[str] = None
    discipline: Optional[str] = None
    degree: Optional[str] = None
    advisor: Optional[str] = None
    event_name: Optional[str] = None
    report_number: Optional[str] = None


# ── Field helpers ──────────────────────────────────────────────────────


def required_fields(document_type: DocumentType | None) -> list[str]:
    if not document_type:
        return ["document_type"]
    return list(REQUIRED_FIELDS[document_type])


def _field_empty(meta: BibliographicMetadata, field: str) -> bool:
    if field == "document_type":
        return not meta.document_type
    value = getattr(meta, field, None)
    if isinstance(value, list):
        return not bool(value)
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    return False


def missing_required(meta: BibliographicMetadata) -> list[str]:
    if not meta.document_type:
        return ["document_type"]
    return [f for f in REQUIRED_FIELDS[meta.document_type] if _field_empty(meta, f)]


def is_complete(meta: BibliographicMetadata, confidence_threshold: float = 0.7) -> bool:
    if not meta.document_type:
        return False
    if meta.confidence < confidence_threshold:
        return False
    return not missing_required(meta)


def primary_authors(meta: BibliographicMetadata) -> list[str]:
    if meta.document_type == "capitulo_livro" and meta.chapter_authors:
        return list(meta.chapter_authors)
    return list(meta.authors)


def primary_title(meta: BibliographicMetadata, fallback: str = "") -> str:
    if meta.document_type == "capitulo_livro" and meta.chapter_title:
        return meta.chapter_title
    return (meta.title or fallback).strip() or fallback


def bibliography_dict(meta: BibliographicMetadata) -> dict[str, Any]:
    """Serialize type-specific fields for SQLite / frontmatter extras."""
    data = meta.model_dump(exclude={"confidence"})
    # Drop empty lists / None for a compact JSON blob.
    out: dict[str, Any] = {}
    for key, value in data.items():
        if value is None:
            continue
        if isinstance(value, list) and not value:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        out[key] = value
    return out


def frontmatter_biblio_fields(meta: BibliographicMetadata) -> dict[str, Any]:
    """Flat frontmatter keys for the active document type (omit empties)."""
    data = bibliography_dict(meta)
    # Core fields are written explicitly by build_source_note.
    for key in ("document_type", "title", "authors", "year"):
        data.pop(key, None)
    return {k: v for k, v in data.items() if k in BIBLIO_FRONTMATTER_FIELDS}


# ── Author / ABNT formatting ───────────────────────────────────────────


def invert_author_name(name: str) -> str:
    """'João Silva Santos' -> 'SANTOS, João Silva'."""
    parts = name.strip().split()
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0].upper()
    surname = parts[-1].upper()
    given = " ".join(parts[:-1])
    return f"{surname}, {given}"


def format_authors_abnt(authors: list[str]) -> str:
    cleaned = [a.strip() for a in authors if a and a.strip()]
    if not cleaned:
        return ""
    if len(cleaned) > 3:
        return f"{invert_author_name(cleaned[0])} et al."
    return "; ".join(invert_author_name(a) for a in cleaned)


def _author_surname(name: str) -> str:
    parts = name.strip().split()
    return parts[-1].upper() if parts else ""


def format_abnt_in_text(
    authors: list[str],
    year: int | None,
    pages: str | None = None,
) -> str:
    """Parenthetical author-date citation (ABNT NBR 10520).

    Examples: ``(SANTOS, 2020)``, ``(SILVA; SOUZA, 2019)``,
    ``(NEGRO et al., 2026, p. 42)``.
    """
    cleaned = [a.strip() for a in authors if a and a.strip()]
    year_str = str(year) if year else "s.d."
    if not cleaned:
        return f"({year_str})" if year else ""

    if len(cleaned) == 1:
        cite_authors = _author_surname(cleaned[0])
    elif len(cleaned) == 2:
        cite_authors = (
            f"{_author_surname(cleaned[0])}; {_author_surname(cleaned[1])}"
        )
    elif len(cleaned) == 3:
        cite_authors = (
            f"{_author_surname(cleaned[0])}; "
            f"{_author_surname(cleaned[1])}; "
            f"{_author_surname(cleaned[2])}"
        )
    else:
        cite_authors = f"{_author_surname(cleaned[0])} et al."

    if pages and str(pages).strip():
        return f"({cite_authors}, {year_str}, {str(pages).strip()})"
    return f"({cite_authors}, {year_str})"


def display_author_natural(authors: list[str]) -> str:
    """First author(s) in natural order for light blog mentions."""
    cleaned = [a.strip() for a in authors if a and a.strip()]
    if not cleaned:
        return ""
    if len(cleaned) == 1:
        return cleaned[0]
    if len(cleaned) == 2:
        return f"{cleaned[0]} e {cleaned[1]}"
    return f"{cleaned[0]} et al."


def _fmt_accessed(accessed_at: str) -> str:
    """Normalize access date to ABNT-ish 'DD mês. AAAA' when possible."""
    raw = accessed_at.strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", raw)
    if m:
        year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        mon = _MONTHS_PT[month] if 1 <= month <= 12 else m.group(2)
        return f"{day:02d} {mon} {year}"
    m2 = re.match(r"^(\d{1,2})[./](\d{1,2})[./](\d{4})$", raw)
    if m2:
        day, month, year = int(m2.group(1)), int(m2.group(2)), int(m2.group(3))
        mon = _MONTHS_PT[month] if 1 <= month <= 12 else m2.group(2)
        return f"{day:02d} {mon} {year}"
    return raw


def format_abnt(meta: BibliographicMetadata) -> str:
    """Build a plain-text ABNT reference string for the given metadata."""
    if not meta.document_type:
        return ""
    handlers = {
        "livro": _abnt_livro,
        "capitulo_livro": _abnt_capitulo,
        "artigo_periodico": _abnt_artigo_periodico,
        "artigo_internet": _abnt_artigo_internet,
        "material_curso": _abnt_material_curso,
        "tese": _abnt_tese,
        "anais_evento": _abnt_anais,
        "relatorio": _abnt_relatorio,
    }
    return handlers[meta.document_type](meta)


def _title_with_subtitle(title: str | None, subtitle: str | None) -> str:
    t = (title or "").strip()
    if subtitle and subtitle.strip():
        return f"{t}: {subtitle.strip()}"
    return t


def _abnt_livro(meta: BibliographicMetadata) -> str:
    parts: list[str] = []
    authors = format_authors_abnt(meta.authors)
    if authors:
        parts.append(f"{authors}.")
    title = _title_with_subtitle(meta.title, meta.subtitle)
    if title:
        parts.append(f"{title}.")
    if meta.translator:
        parts.append(f"Traducao: {meta.translator}.")
    if meta.edition:
        ed = meta.edition.strip()
        if not ed.lower().endswith("ed.") and not ed.lower().endswith("ed"):
            ed = f"{ed} ed."
        parts.append(f"{ed}.")
    loc = []
    if meta.place:
        loc.append(meta.place)
    if meta.publisher:
        loc.append(meta.publisher)
    if loc and meta.year:
        parts.append(f"{': '.join(loc)}, {meta.year}.")
    elif loc:
        parts.append(f"{': '.join(loc)}.")
    elif meta.year:
        parts.append(f"{meta.year}.")
    return " ".join(parts)


def _abnt_capitulo(meta: BibliographicMetadata) -> str:
    parts: list[str] = []
    authors = format_authors_abnt(meta.chapter_authors or meta.authors)
    if authors:
        parts.append(f"{authors}.")
    if meta.chapter_title:
        parts.append(f"{meta.chapter_title}.")
    editors = format_authors_abnt(meta.book_editors) if meta.book_editors else ""
    book = meta.book_title or ""
    if editors and book:
        parts.append(f"In: {editors} (org.). {book}.")
    elif book:
        parts.append(f"In: {book}.")
    if meta.edition:
        parts.append(f"{meta.edition}.")
    loc = []
    if meta.place:
        loc.append(meta.place)
    if meta.publisher:
        loc.append(meta.publisher)
    if loc and meta.year:
        parts.append(f"{': '.join(loc)}, {meta.year}.")
    elif meta.year:
        parts.append(f"{meta.year}.")
    if meta.pages:
        p = meta.pages.strip()
        if not p.lower().startswith("p"):
            p = f"p. {p}"
        parts.append(f"{p}.")
    return " ".join(parts)


def _abnt_artigo_periodico(meta: BibliographicMetadata) -> str:
    parts: list[str] = []
    authors = format_authors_abnt(meta.authors)
    if authors:
        parts.append(f"{authors}.")
    if meta.title:
        parts.append(f"{meta.title}.")
    if meta.journal:
        parts.append(f"{meta.journal},")
    mid: list[str] = []
    if meta.place:
        mid.append(meta.place)
    if meta.volume:
        mid.append(f"v. {meta.volume}")
    if meta.issue:
        mid.append(f"n. {meta.issue}")
    if meta.pages:
        p = meta.pages.strip()
        if not p.lower().startswith("p"):
            p = f"p. {p}"
        mid.append(p)
    if mid:
        parts.append(", ".join(mid) + ",")
    if meta.year:
        parts.append(f"{meta.year}.")
    if meta.doi:
        parts.append(f"DOI: {meta.doi}.")
    return " ".join(parts).replace(" ,", ",").replace("..", ".")


def _abnt_artigo_internet(meta: BibliographicMetadata) -> str:
    parts: list[str] = []
    authors = format_authors_abnt(meta.authors)
    if authors:
        parts.append(f"{authors}.")
    if meta.title:
        parts.append(f"{meta.title}.")
    if meta.site_name:
        parts.append(f"{meta.site_name},")
    if meta.year:
        parts.append(f"{meta.year}.")
    elif meta.published_at:
        parts.append(f"{meta.published_at}.")
    if meta.url:
        parts.append(f"Disponivel em: {meta.url}.")
    if meta.accessed_at:
        parts.append(f"Acesso em: {_fmt_accessed(meta.accessed_at)}.")
    return " ".join(parts)


def _abnt_material_curso(meta: BibliographicMetadata) -> str:
    parts: list[str] = []
    authors = format_authors_abnt(meta.authors)
    if authors:
        parts.append(f"{authors}.")
    if meta.title:
        parts.append(f"{meta.title}.")
    detail: list[str] = []
    if meta.discipline:
        detail.append(meta.discipline)
    if meta.course:
        detail.append(meta.course)
    if detail:
        parts.append(f"{' — '.join(detail)}.")
    if meta.institution:
        inst = meta.institution
        if meta.place:
            inst = f"{inst}, {meta.place}"
        parts.append(f"{inst},")
    if meta.year:
        parts.append(f"{meta.year}.")
    if meta.url:
        parts.append(f"Disponivel em: {meta.url}.")
    if meta.accessed_at:
        parts.append(f"Acesso em: {_fmt_accessed(meta.accessed_at)}.")
    return " ".join(parts)


def _abnt_tese(meta: BibliographicMetadata) -> str:
    parts: list[str] = []
    authors = format_authors_abnt(meta.authors)
    if authors:
        parts.append(f"{authors}.")
    if meta.title:
        parts.append(f"{meta.title}.")
    degree = (meta.degree or "Tese").strip()
    year = meta.year or ""
    place = meta.place or ""
    inst = meta.institution or ""
    # e.g. "2020. Tese (Doutorado) — USP, São Paulo, 2020."
    parts.append(f"{year}." if year else "")
    loc_bits = [b for b in [inst, place] if b]
    if loc_bits:
        parts.append(f"{degree} — {', '.join(loc_bits)}, {year}." if year else f"{degree} — {', '.join(loc_bits)}.")
    else:
        parts.append(f"{degree}.")
    if meta.advisor:
        parts.append(f"Orientacao: {meta.advisor}.")
    if meta.pages:
        parts.append(f"{meta.pages} p.")
    return " ".join(p for p in parts if p)


def _abnt_anais(meta: BibliographicMetadata) -> str:
    parts: list[str] = []
    authors = format_authors_abnt(meta.authors)
    if authors:
        parts.append(f"{authors}.")
    if meta.title:
        parts.append(f"{meta.title}.")
    if meta.event_name:
        parts.append(f"In: {meta.event_name},")
    mid: list[str] = []
    if meta.year:
        mid.append(str(meta.year))
    if meta.place:
        mid.append(meta.place)
    if mid:
        parts.append(f"{', '.join(mid)}.")
    if meta.publisher:
        parts.append(f"{meta.publisher},")
    if meta.year and meta.publisher:
        parts.append(f"{meta.year}.")
    if meta.pages:
        p = meta.pages.strip()
        if not p.lower().startswith("p"):
            p = f"p. {p}"
        parts.append(f"{p}.")
    return " ".join(parts)


def _abnt_relatorio(meta: BibliographicMetadata) -> str:
    parts: list[str] = []
    authors = format_authors_abnt(meta.authors)
    if authors:
        parts.append(f"{authors}.")
    if meta.title:
        parts.append(f"{meta.title}.")
    if meta.report_number:
        parts.append(f"Relatorio {meta.report_number}.")
    loc = []
    if meta.place:
        loc.append(meta.place)
    if meta.institution:
        loc.append(meta.institution)
    if loc and meta.year:
        parts.append(f"{': '.join(loc)}, {meta.year}.")
    elif meta.year:
        parts.append(f"{meta.year}.")
    if meta.url:
        parts.append(f"Disponivel em: {meta.url}.")
    return " ".join(parts)


# ── Heuristic inference ────────────────────────────────────────────────


def _parse_year(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value if 1000 <= value <= 2100 else None
    text = str(value).strip()
    m = re.search(r"(19|20)\d{2}", text)
    if m:
        return int(m.group(0))
    return None


def infer_from_file_metadata(
    metadata: dict[str, Any],
    text_sample: str,
    filename: str,
) -> BibliographicMetadata:
    """Cheap heuristics from PDF/MD metadata + text sample."""
    title = metadata.get("title") or Path(filename).stem
    authors = list(metadata.get("authors") or [])
    year = _parse_year(metadata.get("year"))

    sample_lower = (text_sample or "").lower()
    fname_lower = filename.lower()

    doc_type: DocumentType | None = None
    confidence = 0.35

    url_match = re.search(r"https?://[^\s\)\]\>\"']+", text_sample or "")
    has_url = bool(url_match) or bool(metadata.get("url"))

    if any(k in sample_lower for k in ("tese (", "disserta", "trabalho de conclus")) or "tese" in fname_lower:
        doc_type = "tese"
        confidence = 0.55
    elif any(k in sample_lower for k in ("disciplina", "plano de aula", "material did")) or any(
        k in fname_lower for k in ("aula", "curso", "disciplina")
    ):
        doc_type = "material_curso"
        confidence = 0.5
    elif any(k in sample_lower for k in ("anais", "congresso", "simpósio", "simposio", "proceedings")):
        doc_type = "anais_evento"
        confidence = 0.5
    elif re.search(r"\bin:\s", sample_lower) or "capítulo" in sample_lower or "capitulo" in sample_lower:
        doc_type = "capitulo_livro"
        confidence = 0.45
    elif any(k in sample_lower for k in ("revista", "journal", "vol.", "v. ", "n. ", "doi:")):
        doc_type = "artigo_periodico"
        confidence = 0.5
    elif has_url and (".html" in fname_lower or "http" in sample_lower[:500] or metadata.get("url")):
        doc_type = "artigo_internet"
        confidence = 0.5
    elif any(k in sample_lower for k in ("relatório", "relatorio tecnico", "technical report")):
        doc_type = "relatorio"
        confidence = 0.45
    else:
        doc_type = "livro"
        confidence = 0.4 if authors and year else 0.3

    # Stronger confidence when MD frontmatter already carries biblio fields.
    if metadata.get("document_type") in DOCUMENT_TYPES:
        doc_type = metadata["document_type"]  # type: ignore[assignment]
        confidence = max(confidence, 0.85)

    meta = BibliographicMetadata(
        document_type=doc_type,
        confidence=confidence,
        title=str(title).strip() if title else None,
        authors=[str(a).strip() for a in authors if a],
        year=year,
        place=_str_or_none(metadata.get("place") or metadata.get("city")),
        publisher=_str_or_none(metadata.get("publisher") or metadata.get("editora")),
        edition=_str_or_none(metadata.get("edition") or metadata.get("edicao")),
        translator=_str_or_none(metadata.get("translator") or metadata.get("traducao")),
        subtitle=_str_or_none(metadata.get("subtitle") or metadata.get("subtitulo")),
        isbn=_str_or_none(metadata.get("isbn")),
        journal=_str_or_none(metadata.get("journal") or metadata.get("periodico")),
        volume=_str_or_none(metadata.get("volume")),
        issue=_str_or_none(metadata.get("issue") or metadata.get("number")),
        pages=_str_or_none(metadata.get("pages") or metadata.get("paginas")),
        doi=_str_or_none(metadata.get("doi")),
        url=_str_or_none(metadata.get("url")) or (url_match.group(0) if url_match else None),
        accessed_at=_str_or_none(metadata.get("accessed_at") or metadata.get("access_date")),
        site_name=_str_or_none(metadata.get("site_name")),
        institution=_str_or_none(metadata.get("institution") or metadata.get("instituicao")),
        course=_str_or_none(metadata.get("course") or metadata.get("curso")),
        discipline=_str_or_none(metadata.get("discipline") or metadata.get("disciplina")),
        degree=_str_or_none(metadata.get("degree")),
        advisor=_str_or_none(metadata.get("advisor") or metadata.get("orientador")),
        event_name=_str_or_none(metadata.get("event_name")),
        report_number=_str_or_none(metadata.get("report_number")),
        chapter_title=_str_or_none(metadata.get("chapter_title")),
        book_title=_str_or_none(metadata.get("book_title")),
        chapter_authors=_as_str_list(metadata.get("chapter_authors")),
        book_editors=_as_str_list(metadata.get("book_editors") or metadata.get("editors")),
    )
    return meta


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(a).strip() for a in value if a]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


# ── LLM enrichment ─────────────────────────────────────────────────────


def _merge_biblio(seed: BibliographicMetadata, llm_meta: BibliographicMetadata) -> BibliographicMetadata:
    """Prefer non-empty LLM values; keep seed when LLM leaves a field empty."""
    merged = seed.model_copy(deep=True)
    llm_dump = llm_meta.model_dump()
    for key, value in llm_dump.items():
        if key == "confidence":
            continue
        if value is None:
            continue
        if isinstance(value, list) and not value:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        setattr(merged, key, value)
    if llm_meta.document_type:
        merged.document_type = llm_meta.document_type
    merged.confidence = max(seed.confidence, llm_meta.confidence or 0.0)
    if llm_meta.confidence:
        merged.confidence = max(merged.confidence, llm_meta.confidence)
    # If LLM filled most required fields, bump confidence.
    if merged.document_type and not missing_required(merged):
        merged.confidence = max(merged.confidence, 0.85)
    elif merged.document_type:
        merged.confidence = max(merged.confidence, 0.6)
    return merged


def enrich_with_llm(
    cfg: AppConfig,
    db: StateDB,
    seed: BibliographicMetadata,
    text_sample: str,
    filename: str,
) -> BibliographicMetadata:
    """Call LLM to enrich bibliographic metadata; uses deterministic cache."""
    if not getattr(cfg.harvest, "biblio_llm_enabled", True):
        return seed

    prompt_path = cfg.prompts_path / "bibliographic_metadata.md"
    if not prompt_path.exists():
        logger.warning("Prompt bibliografico ausente: %s", prompt_path)
        return seed

    prompt_template = load_prompt(prompt_path)
    prompt_hash = sha256_hex(prompt_template)
    sample_checksum = sha256_hex(normalize_text_for_hash(text_sample))
    seed_checksum = sha256_hex(normalize_text_for_hash(json.dumps(
        seed.model_dump(), sort_keys=True, ensure_ascii=False,
    )))

    call_checksum = compute_llm_call_checksum(
        prompt_hash,
        sample_checksum,
        cfg.llm.model,
        cfg.llm.temperature,
        cfg.language,
        rag_context_checksum=seed_checksum,
    )
    cached = db.get_cached_llm_response(call_checksum)
    if cached:
        response_text = cached
    else:
        filled = prompt_template.replace("{filename}", filename)
        filled = filled.replace("{seed_json}", json.dumps(seed.model_dump(), ensure_ascii=False, indent=2))
        filled = filled.replace("{text_sample}", text_sample)
        filled = filled.replace(
            "{document_types}",
            ", ".join(DOCUMENT_TYPES),
        )
        try:
            llm = get_llm(cfg)
            response_text = call_llm(llm, filled)
            db.cache_llm_response(
                call_checksum,
                json.dumps({"prompt": filled}, ensure_ascii=False),
                response_text,
            )
        except Exception as e:
            logger.warning("Falha no LLM bibliografico para %s: %s", filename, e)
            return seed

    try:
        raw = json.loads(extract_json(response_text))
        if not isinstance(raw, dict):
            return seed
        llm_meta = BibliographicMetadata.model_validate(_coerce_llm_dict(raw))
        return _merge_biblio(seed, llm_meta)
    except Exception as e:
        logger.warning("Falha ao parsear metadados bibliograficos de %s: %s", filename, e)
        return seed


def _coerce_llm_dict(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize LLM JSON keys/types before Pydantic validation."""
    out = dict(raw)
    if "document_type" in out and out["document_type"] not in DOCUMENT_TYPES:
        out["document_type"] = None
    for list_key in ("authors", "chapter_authors", "book_editors"):
        if list_key in out:
            out[list_key] = _as_str_list(out[list_key])
    if "year" in out and out["year"] is not None:
        out["year"] = _parse_year(out["year"])
    if "confidence" in out:
        try:
            out["confidence"] = float(out["confidence"])
        except (TypeError, ValueError):
            out["confidence"] = 0.5
    # Accept common aliases
    if "city" in out and "place" not in out:
        out["place"] = out.pop("city")
    if "editora" in out and "publisher" not in out:
        out["publisher"] = out.pop("editora")
    return out


def build_bibliographic_metadata(
    cfg: AppConfig,
    db: StateDB,
    metadata: dict[str, Any],
    text: str,
    filename: str,
) -> BibliographicMetadata:
    """Full pipeline: heuristics then optional LLM enrichment."""
    sample_chars = getattr(cfg.harvest, "biblio_text_sample_chars", 5000)
    text_sample = (text or "")[:sample_chars]
    seed = infer_from_file_metadata(metadata, text_sample, filename)
    return enrich_with_llm(cfg, db, seed, text_sample, filename)
