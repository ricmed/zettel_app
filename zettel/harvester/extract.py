"""Text extraction from PDF and Markdown files."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from zettel.config import AppConfig
from zettel.paging import (
    PAGE_BREAK_MARKER,
    build_page_map_from_texts,
    detect_printed_page_from_regions,
    page_map_from_marked_markdown,
)

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


def extract_pdf(cfg: AppConfig, file_path: Path) -> tuple[str, dict[str, Any]]:
    """Extract text from PDF using configured extractor."""
    if cfg.pdf_extractor == "docling":
        return extract_pdf_docling(cfg, file_path)
    return extract_pdf_pymupdf(file_path)


def extract_pdf_docling(cfg: AppConfig, file_path: Path) -> tuple[str, dict[str, Any]]:
    """Extract text from PDF using Docling, with GPU acceleration when available."""
    try:
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import (
            AcceleratorDevice,
            AcceleratorOptions,
            PdfPipelineOptions,
        )
        from docling.document_converter import DocumentConverter, PdfFormatOption

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

        result = converter.convert(str(file_path))
        # Page-break comments keep file-page provenance in the same Markdown
        # dialect as the chunked text. export_to_markdown() without them
        # drops page boundaries; matching that text against PyMuPDF fails.
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

        if not metadata["authors"] or not metadata["year"]:
            enrich_metadata_from_pymupdf(file_path, metadata)

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
            try:
                logger.info("Mapa Docling vazio; fallback PyMuPDF para inferencia de pagina...")
                page_map = pymupdf_page_map(file_path)
                if page_map:
                    metadata["_page_map"] = page_map
                    if "total_pages_file" not in metadata:
                        metadata["total_pages_file"] = len(page_map)
                    logger.info("Mapa de paginas (PyMuPDF): %d paginas do arquivo", len(page_map))
            except Exception as e:
                logger.debug("Page map PyMuPDF indisponivel: %s", e)

        try:
            printed = scan_printed_page_numbers(file_path)
            if printed:
                metadata["_printed_page_hints"] = printed
                logger.info("Numeros impressos detectados em %d pagina(s) de margem", len(printed))
        except Exception as e:
            logger.debug("Varredura de numeros impressos indisponivel: %s", e)

        logger.info(
            "Docling: Conversao concluida - %s (%d caracteres, %s paginas)",
            file_path.name, len(text), metadata.get("total_pages_file", "?"),
        )
        return text, metadata
    except ImportError:
        logger.warning("Docling nao instalado, tentando PyMuPDF como fallback")
        return extract_pdf_pymupdf(file_path)
    except Exception as e:
        logger.error("Erro na extracao Docling: %s -- tentando PyMuPDF", e)
        return extract_pdf_pymupdf(file_path)


def extract_pdf_pymupdf(file_path: Path) -> tuple[str, dict[str, Any]]:
    """Extract text from PDF using PyMuPDF (fallback)."""
    try:
        import pymupdf
        logger.info("PyMuPDF: Iniciando extracao de %s", file_path.name)
        doc = pymupdf.open(str(file_path))
        pages: list[str] = []
        for page in doc:
            pages.append(page.get_text())
        text = "\n\n".join(pages)

        raw_meta = doc.metadata or {}
        year = (
            extract_year_from_pdf_date(raw_meta.get("creationDate"))
            or extract_year_from_pdf_date(raw_meta.get("modDate"))
        )

        metadata: dict[str, Any] = {
            "title": raw_meta.get("title", file_path.stem) or file_path.stem,
            "authors": [raw_meta.get("author", "")] if raw_meta.get("author") else [],
            "year": year,
            "total_pages_file": doc.page_count,
            "_page_map": build_page_map_from_texts(pages),
        }
        num_pages = doc.page_count
        doc.close()
        try:
            printed = scan_printed_page_numbers(file_path)
            if printed:
                metadata["_printed_page_hints"] = printed
        except Exception as e:
            logger.debug("Varredura de numeros impressos indisponivel: %s", e)

        logger.info(
            "PyMuPDF: Extracao concluida - %s (%d caracteres, %d paginas)",
            file_path.name, len(text), num_pages,
        )
        return text, metadata
    except ImportError:
        logger.error(
            "Nem Docling nem PyMuPDF estao instalados. Instale um deles para processar PDFs."
        )
        return "", {"title": file_path.stem, "authors": [], "year": None}


# ── PDF Page Mapping Helpers ───────────────────────────────────────────


def pymupdf_page_map(file_path: Path) -> list[tuple[int, str]]:
    """Build a (page_no, text) map via PyMuPDF for page inference."""
    import pymupdf
    doc = pymupdf.open(str(file_path))
    try:
        pages = [page.get_text() for page in doc]
    finally:
        doc.close()
    return build_page_map_from_texts(pages)


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


def scan_printed_page_numbers(file_path: Path) -> dict[int, int]:
    """Read header/footer bands via PyMuPDF and guess printed page numbers."""
    import pymupdf
    hints: dict[int, int] = {}
    doc = pymupdf.open(str(file_path))
    try:
        for i, page in enumerate(doc):
            rect = page.rect
            band = rect.height * 0.12
            header = page.get_text(
                "text",
                clip=pymupdf.Rect(rect.x0, rect.y0, rect.x1, rect.y0 + band),
            )
            footer = page.get_text(
                "text",
                clip=pymupdf.Rect(rect.x0, rect.y1 - band, rect.x1, rect.y1),
            )
            printed = detect_printed_page_from_regions(header, footer, i + 1)
            if printed is not None:
                hints[i + 1] = printed
    finally:
        doc.close()
    return hints


def page_map_for_source(src: dict[str, Any]) -> list[tuple[int, str]]:
    """Rebuild a page map for rechunk / resume from persisted text or the PDF."""
    text = src.get("extracted_text") or ""
    marked = page_map_from_marked_markdown(text)
    if marked:
        return marked
    origin = src.get("origin_path")
    if origin and Path(origin).suffix.lower() == ".pdf" and Path(origin).exists():
        try:
            return pymupdf_page_map(Path(origin))
        except Exception as e:
            logger.debug("Page map indisponivel para %s: %s", src.get("source_id"), e)
    return []


def enrich_metadata_from_pymupdf(file_path: Path, metadata: dict[str, Any]) -> None:
    """Use PyMuPDF only for metadata extraction (fallback for Docling)."""
    try:
        import pymupdf
        doc = pymupdf.open(str(file_path))
        raw_meta = doc.metadata or {}

        if not metadata.get("authors") and raw_meta.get("author"):
            metadata["authors"] = [raw_meta["author"]]
        if not metadata.get("year"):
            metadata["year"] = (
                extract_year_from_pdf_date(raw_meta.get("creationDate"))
                or extract_year_from_pdf_date(raw_meta.get("modDate"))
            )
        if metadata.get("title") == file_path.stem and raw_meta.get("title"):
            metadata["title"] = raw_meta["title"]
        doc.close()
    except ImportError:
        pass
    except Exception as e:
        logger.debug("PyMuPDF metadata fallback falhou: %s", e)


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
