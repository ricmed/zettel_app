"""The Harvester — file detection, text extraction, chunking, SRC/LIT creation.

Supports: PDF (Docling / PyMuPDF), Markdown.
Audio support is stub-only (requires faster-whisper).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from ulid import ULID

from zettel.config import AppConfig
from zettel.hashing import (
    file_sha256,
    normalize_text_for_hash,
    sha256_hex,
    short_hash,
)
from zettel.index import VectorIndex
from zettel.state import StateDB
from zettel.vault import (
    build_literature_note,
    build_source_note,
    note_filename,
    safe_write_note,
)

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".pdf", ".md", ".markdown", ".txt"}


# ── Public API ─────────────────────────────────────────────────────────


def run_harvest(cfg: AppConfig, db: StateDB, idx: VectorIndex) -> list[str]:
    """Scan inbox, extract text, create SRC/LIT notes, chunk. Returns new source_ids."""
    new_sources: list[str] = []
    inbox = cfg.inbox_path

    if not inbox.exists():
        logger.warning("Inbox nao encontrado: %s", inbox)
        return new_sources

    files = [f for f in inbox.rglob("*") if f.suffix.lower() in SUPPORTED_EXTENSIONS and f.is_file()]
    logger.info("Encontrados %d arquivos no inbox", len(files))

    total_stats = {"text_len": 0, "chapters": 0, "chunks": 0}
    for file_path in files:
        sid, stats = _process_file(cfg, db, idx, file_path)
        if sid:
            new_sources.append(sid)
            total_stats["text_len"] += stats.get("text_len", 0)
            total_stats["chapters"] += stats.get("chapters", 0)
            total_stats["chunks"] += stats.get("chunks", 0)

    if new_sources:
        logger.info(
            "Harvest concluido: %d fontes, %d caracteres, %d capitulos, %d chunks",
            len(new_sources), total_stats["text_len"],
            total_stats["chapters"], total_stats["chunks"],
        )

    return new_sources


# ── File Processing ────────────────────────────────────────────────────


def _process_file(
    cfg: AppConfig, db: StateDB, idx: VectorIndex, file_path: Path,
) -> tuple[str | None, dict[str, int]]:
    """Process a single file: extract, chunk, persist. Returns (source_id, stats) or (None, {})."""
    empty_stats: dict[str, int] = {}
    checksum = file_sha256(file_path)
    existing = db.get_file(str(file_path))

    if existing and existing["file_checksum"] == checksum:
        logger.debug("Arquivo inalterado, pulando: %s", file_path.name)
        return None, empty_stats

    ext = file_path.suffix.lower()
    origin_type = "pdf" if ext == ".pdf" else "md"

    # Extract text
    text, metadata = _extract_text(cfg, file_path, origin_type)
    if not text.strip():
        logger.warning("Nenhum texto extraido de: %s", file_path.name)
        return None, empty_stats

    extraction_checksum = sha256_hex(normalize_text_for_hash(text))

    # Generate citekey
    title = metadata.get("title", file_path.stem)
    authors = metadata.get("authors", [])
    year = metadata.get("year")
    citekey = _generate_citekey(db, authors, year, title)
    source_id = f"@{citekey}"

    # Check if extraction_checksum changed (for re-processing)
    existing_source = db.get_source(source_id)
    if existing_source and existing_source.get("extraction_checksum") == extraction_checksum:
        logger.info("Texto extraido inalterado para %s, pulando rechunking", source_id)
        db.upsert_file(str(file_path), checksum, origin_type, source_id)
        return None, empty_stats

    # Persist file and source
    db.upsert_file(str(file_path), checksum, origin_type, source_id)
    db.upsert_source(
        source_id=source_id, citekey=citekey, title=title, authors=authors,
        year=year, file_checksum=checksum, origin_path=str(file_path),
        origin_type=origin_type, extraction_checksum=extraction_checksum,
    )

    # Create SRC and LIT notes in vault
    _create_vault_notes(cfg, source_id, citekey, title, authors, year,
                        str(file_path), origin_type, checksum)

    # Index source
    idx.upsert_source(source_id, f"{title} -- {', '.join(authors)}", {
        "citekey": citekey, "title": title, "origin_type": origin_type,
    })

    # Chunk
    chapters = _split_into_chapters(text, origin_type)
    chunk_count = _chunk_and_persist(cfg, db, idx, source_id, chapters)

    stats = {"text_len": len(text), "chapters": len(chapters), "chunks": chunk_count}
    logger.info(
        "Fonte processada: %s (%d capitulos, %d chunks, %d caracteres)",
        source_id, len(chapters), chunk_count, len(text),
    )
    return source_id, stats


# ── Year Extraction Helpers ────────────────────────────────────────────


def _extract_year_from_pdf_date(pdf_date: str | None) -> int | None:
    """Extract year from PDF date strings like 'D:20230415...' or '2023-04-15'."""
    if not pdf_date:
        return None
    # PDF date format: D:YYYYMMDDHHmmSS
    m = re.match(r"D:(\d{4})", pdf_date)
    if m:
        return int(m.group(1))
    return _extract_year_from_string(pdf_date)


def _extract_year_from_string(s: str | None) -> int | None:
    """Extract a plausible 4-digit year (1900-2099) from any string."""
    if not s:
        return None
    m = re.search(r"\b(19\d{2}|20\d{2})\b", s)
    return int(m.group(1)) if m else None


# ── Text Extraction ───────────────────────────────────────────────────


def _extract_text(cfg: AppConfig, file_path: Path, origin_type: str) -> tuple[str, dict[str, Any]]:
    """Extract text and basic metadata from a file."""
    if origin_type == "pdf":
        return _extract_pdf(cfg, file_path)
    return _extract_markdown(file_path)


def _extract_pdf(cfg: AppConfig, file_path: Path) -> tuple[str, dict[str, Any]]:
    """Extract text from PDF using configured extractor."""
    if cfg.pdf_extractor == "docling":
        return _extract_pdf_docling(cfg, file_path)
    return _extract_pdf_pymupdf(file_path)


def _extract_pdf_docling(cfg: AppConfig, file_path: Path) -> tuple[str, dict[str, Any]]:
    """Extract text from PDF using Docling, with GPU acceleration when available."""
    try:
        from docling.document_converter import DocumentConverter
        from docling.datamodel.pipeline_options import PdfPipelineOptions, AcceleratorDevice, AcceleratorOptions
        from docling.datamodel.base_models import InputFormat
        from docling.document_converter import PdfFormatOption

        from zettel.config import detect_device
        device = detect_device(cfg.device)

        accel_device = AcceleratorDevice.CUDA if device == "cuda" else AcceleratorDevice.CPU
        pipeline_options = PdfPipelineOptions()
        pipeline_options.accelerator_options = AcceleratorOptions(
            num_threads=4,
            device=accel_device,
        )

        converter = DocumentConverter(
            format_options={
                InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options),
            }
        )
        logger.info("Docling: Iniciando conversao de %s (dispositivo: %s)", file_path.name, device.upper())

        result = converter.convert(str(file_path))
        text = result.document.export_to_markdown()

        # Try to extract metadata from Docling document origin
        metadata: dict[str, Any] = {"title": file_path.stem, "authors": [], "year": None}
        try:
            origin = getattr(result.document, "origin", None)
            if origin:
                if getattr(origin, "title", None):
                    metadata["title"] = origin.title
                if getattr(origin, "author", None):
                    metadata["authors"] = [a.strip() for a in origin.author.split(",") if a.strip()]
                if getattr(origin, "date", None):
                    metadata["year"] = _extract_year_from_string(origin.date)
        except Exception:
            pass

        # Fallback: use PyMuPDF only for metadata if Docling didn't get author/year
        if not metadata["authors"] or not metadata["year"]:
            _enrich_metadata_from_pymupdf(file_path, metadata)

        num_pages = getattr(result.document, "num_pages", None) or "?"
        logger.info(
            "Docling: Conversao concluida - %s (%d caracteres, %s paginas)",
            file_path.name, len(text), num_pages,
        )
        return text, metadata
    except ImportError:
        logger.warning("Docling nao instalado, tentando PyMuPDF como fallback")
        return _extract_pdf_pymupdf(file_path)
    except Exception as e:
        logger.error("Erro na extracao Docling: %s -- tentando PyMuPDF", e)
        return _extract_pdf_pymupdf(file_path)


def _extract_pdf_pymupdf(file_path: Path) -> tuple[str, dict[str, Any]]:
    """Extract text from PDF using PyMuPDF (fallback)."""
    try:
        import pymupdf
        logger.info("PyMuPDF: Iniciando extracao de %s", file_path.name)
        doc = pymupdf.open(str(file_path))
        pages: list[str] = []
        for page in doc:
            pages.append(page.get_text())
        text = "\n\n".join(pages)

        # Extract metadata including year
        raw_meta = doc.metadata or {}
        year = (
            _extract_year_from_pdf_date(raw_meta.get("creationDate"))
            or _extract_year_from_pdf_date(raw_meta.get("modDate"))
        )

        metadata: dict[str, Any] = {
            "title": raw_meta.get("title", file_path.stem) or file_path.stem,
            "authors": [raw_meta.get("author", "")] if raw_meta.get("author") else [],
            "year": year,
        }
        num_pages = doc.page_count
        doc.close()

        logger.info(
            "PyMuPDF: Extracao concluida - %s (%d caracteres, %d paginas)",
            file_path.name, len(text), num_pages,
        )
        return text, metadata
    except ImportError:
        logger.error("Nem Docling nem PyMuPDF estao instalados. Instale um deles para processar PDFs.")
        return "", {"title": file_path.stem, "authors": [], "year": None}


def _enrich_metadata_from_pymupdf(file_path: Path, metadata: dict[str, Any]) -> None:
    """Use PyMuPDF only for metadata extraction (fallback for Docling)."""
    try:
        import pymupdf
        doc = pymupdf.open(str(file_path))
        raw_meta = doc.metadata or {}

        if not metadata.get("authors") and raw_meta.get("author"):
            metadata["authors"] = [raw_meta["author"]]
        if not metadata.get("year"):
            metadata["year"] = (
                _extract_year_from_pdf_date(raw_meta.get("creationDate"))
                or _extract_year_from_pdf_date(raw_meta.get("modDate"))
            )
        if metadata.get("title") == file_path.stem and raw_meta.get("title"):
            metadata["title"] = raw_meta["title"]
        doc.close()
    except ImportError:
        pass
    except Exception as e:
        logger.debug("PyMuPDF metadata fallback falhou: %s", e)


def _extract_markdown(file_path: Path) -> tuple[str, dict[str, Any]]:
    """Extract text from Markdown files."""
    content = file_path.read_text(encoding="utf-8")
    # Strip existing frontmatter
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            content = parts[2].strip()
    # Try to extract title from first heading
    title = file_path.stem
    title_match = re.match(r"^#\s+(.+)$", content, re.MULTILINE)
    if title_match:
        title = title_match.group(1).strip()
    return content, {"title": title, "authors": [], "year": None}


# ── Citekey Generation ────────────────────────────────────────────────


def _generate_citekey(db: StateDB, authors: list[str], year: int | None, title: str) -> str:
    """Generate a tiered citekey based on available metadata.

    Strategy:
      Author + Year  -> SurnameYearSlug2      (e.g. Silva2023AprendizadoProfundo)
      Author only    -> SurnameSlug3           (e.g. SilvaAprendizadoProfundoRedes)
      Year only      -> YearSlug3              (e.g. 2023AprendizadoProfundoRedes)
      Neither        -> Slug4                  (e.g. AprendizadoProfundoRedesNeurais)
    """
    surname = ""
    if authors and authors[0]:
        parts = authors[0].strip().split()
        if parts:
            surname = parts[-1]

    has_author = bool(surname)
    has_year = year is not None

    # Determine how many slug words based on what metadata is available
    words = re.sub(r"[^\w\s]", "", title).split()

    if has_author and has_year:
        slug_count = 2
        slug_words = [w.capitalize() for w in words[:slug_count]]
        slug = "".join(slug_words) if slug_words else "Untitled"
        base = f"{surname}{year}{slug}"
    elif has_author:
        slug_count = 3
        slug_words = [w.capitalize() for w in words[:slug_count]]
        slug = "".join(slug_words) if slug_words else "Untitled"
        base = f"{surname}{slug}"
    elif has_year:
        slug_count = 3
        slug_words = [w.capitalize() for w in words[:slug_count]]
        slug = "".join(slug_words) if slug_words else "Untitled"
        base = f"{year}{slug}"
    else:
        slug_count = 4
        slug_words = [w.capitalize() for w in words[:slug_count]]
        slug = "".join(slug_words) if slug_words else "Untitled"
        base = slug

    # Handle collisions
    citekey = base
    suffix_idx = 0
    while db.get_source_by_citekey(citekey):
        suffix_idx += 1
        citekey = f"{base}{chr(96 + suffix_idx)}"  # a, b, c...

    return citekey


# ── Chapter Splitting ─────────────────────────────────────────────────


def _split_into_chapters(text: str, origin_type: str) -> list[dict[str, str]]:
    """Split text into chapters/sections (Level 1 hierarchy)."""
    chapters: list[dict[str, str]] = []

    # Split by markdown headings (# or ##)
    heading_pattern = re.compile(r"^(#{1,2})\s+(.+)$", re.MULTILINE)
    matches = list(heading_pattern.finditer(text))

    if not matches:
        # No headings found — treat entire text as one chapter
        return [{"title": "Documento completo", "text": text.strip(), "locator": ""}]

    # Content before first heading
    if matches[0].start() > 0:
        preamble = text[: matches[0].start()].strip()
        if preamble:
            chapters.append({"title": "Introdução", "text": preamble, "locator": "preâmbulo"})

    for i, m in enumerate(matches):
        title = m.group(2).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        chapter_text = text[start:end].strip()
        if chapter_text:
            chapters.append({"title": title, "text": chapter_text, "locator": title})

    return chapters


# ── Chunking ──────────────────────────────────────────────────────────


def _chunk_and_persist(
    cfg: AppConfig, db: StateDB, idx: VectorIndex,
    source_id: str, chapters: list[dict[str, str]],
) -> int:
    """Split chapters into chunks and persist to state + index. Returns total chunk count."""
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=cfg.chunking.chunk_size,
        chunk_overlap=cfg.chunking.chunk_overlap,
        separators=["\n\n", "\n", ". ", " ", ""],
    )

    total_chunks = 0
    for ch_idx, chapter in enumerate(chapters):
        chapter_text = chapter["text"]
        normalized = normalize_text_for_hash(chapter_text)
        chapter_checksum = sha256_hex(normalized)

        chapter_id = f"{source_id}::ch{ch_idx:03d}"

        # Check if chapter changed BEFORE upserting
        existing_chapters = db.get_chapters_for_source(source_id)
        existing_ch = next((c for c in existing_chapters if c["chapter_id"] == chapter_id), None)
        if existing_ch and existing_ch["chapter_checksum"] == chapter_checksum:
            logger.debug("Capitulo inalterado: %s", chapter_id)
            continue

        db.upsert_chapter(chapter_id, source_id, chapter["title"], chapter_checksum, chapter["locator"])

        # Chunk within this chapter (content island)
        chunks = splitter.split_text(chapter_text)
        for chunk_text in chunks:
            chunk_norm = normalize_text_for_hash(chunk_text)
            chunk_checksum = sha256_hex(chunk_norm)
            chunk_id = f"{source_id}::{chapter_id}::{short_hash(chunk_checksum)}"

            db.upsert_chunk(
                chunk_id=chunk_id, source_id=source_id, chapter_id=chapter_id,
                text=chunk_text, chunk_checksum=chunk_checksum,
                locator=chapter.get("locator", ""),
            )
            idx.upsert_chunk(chunk_id, chunk_text, {
                "source_id": source_id, "chapter_id": chapter_id,
                "locator": chapter.get("locator", ""),
            })

        total_chunks += len(chunks)
        logger.debug("Capitulo %s: %d chunks gerados", chapter_id, len(chunks))

    return total_chunks


# ── Vault Note Creation ───────────────────────────────────────────────


def _create_vault_notes(
    cfg: AppConfig, source_id: str, citekey: str, title: str,
    authors: list[str], year: int | None, origin_path: str,
    origin_type: str, checksum: str,
) -> None:
    """Create SRC and LIT notes in the vault."""
    vault = cfg.vault_path

    # SRC note
    src_meta, src_body = build_source_note(
        source_id, citekey, title, authors, year, origin_path, origin_type, checksum
    )
    src_filename = note_filename("SRC", f"@{citekey}", title)
    safe_write_note(vault / "10_Sources" / src_filename, src_meta, src_body)

    # LIT note
    lit_meta, lit_body = build_literature_note(source_id, citekey, title)
    lit_filename = note_filename("LIT", f"@{citekey}", title)
    safe_write_note(vault / "20_Literature" / lit_filename, lit_meta, lit_body)
