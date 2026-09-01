"""Text extraction from PDF and Markdown files."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from zettel.config import AppConfig
from zettel.paging import PAGE_BREAK_MARKER, page_map_from_marked_markdown

logger = logging.getLogger(__name__)


# ── Year Extraction Helpers ────────────────────────────────────────────


def extract_year_from_pdf_date(pdf_date: str | None) -> int | None:
    """Extract year from PDF date strings like 'D:20230415...' or '2023-04-15'."""
    if not pdf_date:
        return None
    m = re.match(r"D:(\d{4})", pdf_date)
    if m:
        return int(m.group(1))
    return extract_year_from_string(pdf_date)


def extract_year_from_string(s: str | None) -> int | None:
    """Extract a plausible 4-digit year (1900-2099) from any string."""
    if not s:
        return None
    m = re.search(r"\b(19\d{2}|20\d{2})\b", s)
    return int(m.group(1)) if m else None


# ── Text Extraction Dispatch ────────────────────────────────────────────


def extract_text(
    cfg: AppConfig, file_path: Path, origin_type: str
) -> tuple[str, dict[str, Any]]:
    """Extract text and basic metadata from a file.

    Extracted images (when images.enabled) are stashed in metadata["_images"] as a
    list of {checksum, path, context_snippet} for later DB registration.
    """
    if origin_type == "pdf":
        return extract_pdf(cfg, file_path)
    return extract_markdown(cfg, file_path)


# ── PDF Extraction ─────────────────────────────────────────────────────


class PdfExtractionError(RuntimeError):
    """Fatal PDF extraction failure. Docling is mandatory; there is no fallback (ADR-012)."""


def extract_pdf(cfg: AppConfig, file_path: Path) -> tuple[str, dict[str, Any]]:
    """Extract text from PDF using Docling (the only supported extractor)."""
    return extract_pdf_docling(cfg, file_path)


def extract_pdf_docling(cfg: AppConfig, file_path: Path) -> tuple[str, dict[str, Any]]:
    """Extract text from PDF using Docling, with GPU acceleration when available.

    Docling is mandatory (ADR-012, no PyMuPDF fallback): a missing install or a
    failed conversion raises ``PdfExtractionError`` instead of degrading to
    plain text or an empty source.
    """
    try:
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import (
            AcceleratorDevice,
            AcceleratorOptions,
            PdfPipelineOptions,
        )
        from docling.document_converter import DocumentConverter, PdfFormatOption
    except ImportError as e:
        raise PdfExtractionError(
            f"Extracao de PDF falhou para '{file_path.name}': Docling nao esta "
            "instalado. Docling e obrigatorio e nao ha fallback. Verifique a "
            "instalacao ('uv sync' / 'pip install docling') e a disponibilidade "
            "de GPU/CPU."
        ) from e

    from zettel.config import detect_device
    device = detect_device(cfg.device)

    accel_device = AcceleratorDevice.CUDA if device == "cuda" else AcceleratorDevice.CPU
    pipeline_options = PdfPipelineOptions()
    pipeline_options.accelerator_options = AcceleratorOptions(
        num_threads=4,
        device=accel_device,
    )
    if cfg.images.enabled:
        pipeline_options.generate_picture_images = True
        pipeline_options.images_scale = cfg.images.scale

    converter = DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options),
        }
    )
    logger.info(
        "Docling: Iniciando conversao de %s (dispositivo: %s)", file_path.name, device.upper()
    )

    try:
        result = converter.convert(str(file_path))
    except Exception as e:
        raise PdfExtractionError(
            f"Extracao de PDF falhou para '{file_path.name}': a conversao "
            f"Docling lancou um erro ({e}). Docling e obrigatorio e nao ha "
            "fallback. Verifique a instalacao e a disponibilidade de GPU/CPU."
        ) from e

    # Page-break comments keep file-page provenance in the same Markdown
    # dialect as the chunked text. export_to_markdown() without them drops
    # page boundaries.
    text = result.document.export_to_markdown(
        page_break_placeholder=f"\n\n{PAGE_BREAK_MARKER}\n\n",
    )

    images: list[dict[str, Any]] = []
    if cfg.images.enabled:
        from zettel.assets import extract_docling_images
        text, images = extract_docling_images(cfg, result.document, text)

    metadata: dict[str, Any] = {
        "title": file_path.stem, "authors": [], "year": None, "_images": images,
    }
    try:
        origin = getattr(result.document, "origin", None)
        if origin:
            if getattr(origin, "title", None):
                metadata["title"] = origin.title
            if getattr(origin, "author", None):
                metadata["authors"] = [
                    a.strip() for a in origin.author.split(",") if a.strip()
                ]
            if getattr(origin, "date", None):
                metadata["year"] = extract_year_from_string(origin.date)
    except Exception:
        pass

    num_pages = docling_num_pages(result.document)
    if num_pages:
        metadata["total_pages_file"] = num_pages

    page_map = page_map_from_marked_markdown(text)
    if not page_map:
        page_map = docling_page_map_by_export(result.document, num_pages)
    if page_map:
        metadata["_page_map"] = page_map
        if "total_pages_file" not in metadata:
            metadata["total_pages_file"] = len(page_map)
        logger.info("Mapa de paginas (Docling): %d paginas do arquivo", len(page_map))
    else:
        logger.warning(
            "Nenhum mapa de paginas Docling disponivel para %s; paging.py cai "
            "para regex/interpolacao (sem PyMuPDF).", file_path.name,
        )

    logger.info(
        "Docling: Conversao concluida - %s (%d caracteres, %s paginas)",
        file_path.name, len(text), metadata.get("total_pages_file", "?"),
    )
    return text, metadata


# ── PDF Page Mapping Helpers ───────────────────────────────────────────


def docling_num_pages(document: Any) -> int | None:
    raw = getattr(document, "num_pages", None)
    if callable(raw):
        try:
            raw = raw()
        except Exception:
            raw = None
    if isinstance(raw, int) and raw > 0:
        return raw
    pages = getattr(document, "pages", None)
    if isinstance(pages, dict) and pages:
        try:
            return max(int(k) for k in pages)
        except (TypeError, ValueError):
            return len(pages)
    return None


def docling_page_map_by_export(document: Any, num_pages: int | None) -> list[tuple[int, str]]:
    """Per-page Markdown export when page-break placeholders were not emitted."""
    n = num_pages or docling_num_pages(document) or 0
    if n <= 0:
        return []
    if n == 1:
        md = document.export_to_markdown()
        return [(1, md)] if md else []
    page_map: list[tuple[int, str]] = []
    for page_no in range(1, n + 1):
        try:
            md = document.export_to_markdown(page_no=page_no)
        except TypeError:
            return []
        page_map.append((page_no, md or ""))
    return page_map


def page_map_for_source(src: dict[str, Any]) -> list[tuple[int, str]]:
    """Rebuild a page map for rechunk / resume from persisted marked text.

    Only the Docling page-break markers in the persisted ``extracted_text``
    are used — there is no PyMuPDF fallback to reopen the original PDF.
    """
    text = src.get("extracted_text") or ""
    return page_map_from_marked_markdown(text)


# ── Markdown Extraction ────────────────────────────────────────────────


def extract_markdown(cfg: AppConfig, file_path: Path) -> tuple[str, dict[str, Any]]:
    """Extract text and metadata from a Markdown file.

    Reads YAML frontmatter to populate author, year, and title so that
    Markdown sources generate proper citekeys and SRC notes — instead of
    discarding the frontmatter entirely.
    """
    import yaml

    content = file_path.read_text(encoding="utf-8")
    fm_meta: dict[str, Any] = {}
    body = content

    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            try:
                fm_meta = yaml.safe_load(parts[1]) or {}
            except Exception:
                fm_meta = {}
            body = parts[2].strip()

    # Extract title: frontmatter > first H1 heading > filename stem
    title: str = file_path.stem
    title_match = re.match(r"^#\s+(.+)$", body, re.MULTILINE)
    if title_match:
        title = title_match.group(1).strip()
    if fm_meta.get("title"):
        title = str(fm_meta["title"])

    # Extract authors from frontmatter
    authors: list[str] = []
    raw_authors = fm_meta.get("authors") or fm_meta.get("author")
    if raw_authors:
        if isinstance(raw_authors, list):
            authors = [str(a) for a in raw_authors if a]
        elif isinstance(raw_authors, str) and raw_authors.strip():
            authors = [raw_authors.strip()]

    # Extract year from frontmatter
    year: int | None = None
    if fm_meta.get("year"):
        try:
            year = int(fm_meta["year"])
        except (ValueError, TypeError):
            pass
    if year is None and fm_meta.get("date"):
        year = extract_year_from_string(str(fm_meta["date"]))

    from zettel.assets import extract_markdown_images
    body, images = extract_markdown_images(cfg, body, file_path)

    meta: dict[str, Any] = {
        "title": title, "authors": authors, "year": year, "_images": images,
    }
    # Pass through known bibliographic frontmatter fields for ABNT inference.
    _BIBLIO_KEYS = (
        "document_type", "subtitle", "edition", "place", "city", "publisher",
        "editora", "translator", "traducao", "isbn", "journal", "periodico",
        "volume", "issue", "number", "pages", "paginas", "doi", "url",
        "accessed_at", "access_date", "site_name", "published_at",
        "institution", "instituicao", "course", "curso", "discipline",
        "disciplina", "degree", "advisor", "orientador", "event_name",
        "report_number", "chapter_title", "book_title", "chapter_authors",
        "book_editors", "editors",
    )
    for key in _BIBLIO_KEYS:
        if key in fm_meta and fm_meta[key] not in (None, ""):
            meta[key] = fm_meta[key]

    return body, meta
