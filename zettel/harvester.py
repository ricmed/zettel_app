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


class HarvestAborted(Exception):
    """Raised to stop `run_harvest` early when the user chooses to abort."""


# ── Public API ─────────────────────────────────────────────────────────


def run_harvest(
    cfg: AppConfig,
    db: StateDB,
    idx: VectorIndex,
    interactive: bool = True,
    duplicate_action: str | None = None,
) -> list[str]:
    """Scan inbox, extract text, create SRC/LIT notes, chunk. Returns new source_ids.

    Args:
        interactive: if True and a probable duplicate is detected, prompt the user
            (skip / continue / abort). If False, `duplicate_action` (or the config
            default `harvest.non_interactive_duplicate_action`) decides automatically.
        duplicate_action: overrides the configured non-interactive default
            ("skip" | "continue" | "abort"). Ignored when `interactive` is True.
    """
    new_sources: list[str] = []
    inbox = cfg.inbox_path

    from zettel.hashing import compute_pipeline_signature
    signature = compute_pipeline_signature({
        "chunking": cfg.chunking.model_dump(),
        "harvest": cfg.harvest.model_dump(),
        "pdf_extractor": cfg.pdf_extractor,
    })
    run_id = db.start_run(signature)
    run_status = "completed"

    if not inbox.exists():
        logger.warning("Inbox nao encontrado: %s", inbox)
        db.finish_run(run_id, run_status)
        return new_sources

    files = [
        f for f in inbox.rglob("*")
        if f.suffix.lower() in SUPPORTED_EXTENSIONS and f.is_file()
    ]
    logger.info("Encontrados %d arquivos no inbox", len(files))

    total_stats = {"text_len": 0, "chapters": 0, "chunks": 0}
    try:
        for file_path in files:
            sid, stats = _process_file(
                cfg, db, idx, file_path, run_id, interactive, duplicate_action,
            )
            if sid:
                new_sources.append(sid)
                total_stats["text_len"] += stats.get("text_len", 0)
                total_stats["chapters"] += stats.get("chapters", 0)
                total_stats["chunks"] += stats.get("chunks", 0)
    except HarvestAborted as e:
        logger.warning("Harvest abortado pelo usuario: %s", e)
        run_status = "aborted"

    if new_sources:
        logger.info(
            "Harvest concluido: %d fontes, %d caracteres, %d capitulos, %d chunks",
            len(new_sources), total_stats["text_len"],
            total_stats["chapters"], total_stats["chunks"],
        )

    db.finish_run(run_id, run_status)
    return new_sources


# ── File Processing ────────────────────────────────────────────────────


def _process_file(
    cfg: AppConfig,
    db: StateDB,
    idx: VectorIndex,
    file_path: Path,
    run_id: int,
    interactive: bool = True,
    duplicate_action: str | None = None,
) -> tuple[str | None, dict[str, int]]:
    """Process a single file: extract, chunk, persist. Returns (source_id, stats) or (None, {})."""
    empty_stats: dict[str, int] = {}
    checksum = file_sha256(file_path)
    existing = db.get_file(str(file_path))

    if existing and existing["file_checksum"] == checksum:
        logger.debug("Arquivo inalterado, pulando: %s", file_path.name)
        return None, empty_stats

    # ── Camada 1: duplicidade por hash de arquivo (copia renomeada) ────
    renamed_from = db.get_file_by_checksum(checksum, exclude_path=str(file_path))
    if renamed_from and renamed_from.get("source_id"):
        logger.info(
            "Arquivo '%s' e uma copia identica de '%s' (mesmo hash de arquivo). "
            "Associando ao mesmo source_id (%s) em vez de reprocessar.",
            file_path.name, Path(renamed_from["path"]).name, renamed_from["source_id"],
        )
        db.upsert_file(str(file_path), checksum, file_path.suffix.lower().lstrip("."),
                        renamed_from["source_id"])
        db.record_duplicate(run_id, "file")
        return None, empty_stats

    ext = file_path.suffix.lower()
    origin_type = "pdf" if ext == ".pdf" else "md"

    text, metadata = _extract_text(cfg, file_path, origin_type)
    if not text.strip():
        logger.warning("Nenhum texto extraido de: %s", file_path.name)
        return None, empty_stats

    extraction_checksum = sha256_hex(normalize_text_for_hash(text))

    # ── Camada 2: duplicidade por hash de conteudo extraido (cross-formato) ──
    cross_format_source = db.get_source_by_extraction_checksum(extraction_checksum)
    if cross_format_source:
        logger.info(
            "Conteudo de '%s' e identico (apos normalizacao) a fonte existente %s "
            "(%s). Reaproveitando fonte, sem gerar novo citekey/SRC/LIT/chunks.",
            file_path.name, cross_format_source["source_id"], cross_format_source["citekey"],
        )
        db.upsert_file(str(file_path), checksum, origin_type, cross_format_source["source_id"])
        db.record_duplicate(run_id, "content")
        return None, empty_stats

    title = metadata.get("title", file_path.stem)
    authors = metadata.get("authors", [])
    year = metadata.get("year")
    citekey = _generate_citekey(db, authors, year, title)
    source_id = f"@{citekey}"

    existing_source = db.get_source(source_id)
    if existing_source and existing_source.get("extraction_checksum") == extraction_checksum:
        logger.info("Texto extraido inalterado para %s, pulando rechunking", source_id)
        db.upsert_file(str(file_path), checksum, origin_type, source_id)
        return None, empty_stats

    chapters = _split_into_chapters(text, origin_type)

    # ── Camada 3: quase-duplicata semantica via ChromaDB ───────────────
    dup_candidates = _find_semantic_duplicate_candidates(cfg, db, idx, chapters)
    if dup_candidates:
        decision = _resolve_duplicate_decision(
            file_path, dup_candidates, interactive, duplicate_action, cfg,
        )
        if decision == "abort":
            raise HarvestAborted(f"Usuario abortou o harvest ao processar {file_path.name}")
        db.record_duplicate(run_id, "semantic")
        if decision == "skip":
            logger.warning(
                "Arquivo '%s' pulado por suspeita de duplicidade semantica (candidatos: %s).",
                file_path.name, ", ".join(c["citekey"] for c in dup_candidates),
            )
            return None, empty_stats
        logger.info(
            "Arquivo '%s' segue como nova fonte apesar da suspeita de duplicidade semantica.",
            file_path.name,
        )

    db.upsert_file(str(file_path), checksum, origin_type, source_id)
    db.upsert_source(
        source_id=source_id, citekey=citekey, title=title, authors=authors,
        year=year, file_checksum=checksum, origin_path=str(file_path),
        origin_type=origin_type, extraction_checksum=extraction_checksum,
    )

    _create_vault_notes(cfg, source_id, citekey, title, authors, year,
                        str(file_path), origin_type, checksum)

    idx.upsert_source(source_id, f"{title} -- {', '.join(authors)}", {
        "citekey": citekey, "title": title, "origin_type": origin_type,
    })

    chunk_count = _chunk_and_persist(cfg, db, idx, source_id, chapters)

    stats = {"text_len": len(text), "chapters": len(chapters), "chunks": chunk_count}
    logger.info(
        "Fonte processada: %s (%d capitulos, %d chunks, %d caracteres)",
        source_id, len(chapters), chunk_count, len(text),
    )
    return source_id, stats


# ── Semantic Duplicate Detection ───────────────────────────────────────


def _sample_chunk_texts(cfg: AppConfig, chapters: list[dict[str, str]], sample_size: int) -> list[str]:
    """Split chapters into chunks (without persisting) and return an evenly distributed sample."""
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=cfg.chunking.chunk_size,
        chunk_overlap=cfg.chunking.chunk_overlap,
        separators=["\n\n", "\n", ". ", " ", ""],
    )

    all_chunks: list[str] = []
    for chapter in chapters:
        all_chunks.extend(splitter.split_text(chapter["text"]))

    if not all_chunks:
        return []
    if len(all_chunks) <= sample_size:
        return all_chunks

    step = len(all_chunks) / sample_size
    return [all_chunks[int(i * step)] for i in range(sample_size)]


def _find_semantic_duplicate_candidates(
    cfg: AppConfig, db: StateDB, idx: VectorIndex, chapters: list[dict[str, str]],
) -> list[dict[str, Any]]:
    """Query the chunk index for near-duplicates of a sample of this file's chunks.

    Returns candidate sources (aggregated, best score per source_id) whose
    similarity is at or above `cfg.harvest.duplicate_chunk_threshold`.
    """
    threshold = cfg.harvest.duplicate_chunk_threshold
    sample_size = max(1, cfg.harvest.duplicate_sample_size)
    sample_texts = _sample_chunk_texts(cfg, chapters, sample_size)
    if not sample_texts:
        return []

    matches = idx.find_similar_chunks(sample_texts, n_results=3)
    best_by_source: dict[str, float] = {}
    for m in matches:
        distance = m.get("distance")
        if distance is None:
            continue
        similarity = 1 - (distance / 2)
        meta = m.get("metadata") or {}
        source_id = meta.get("source_id")
        if not source_id or similarity < threshold:
            continue
        if source_id not in best_by_source or similarity > best_by_source[source_id]:
            best_by_source[source_id] = similarity

    candidates: list[dict[str, Any]] = []
    for source_id, similarity in sorted(best_by_source.items(), key=lambda kv: -kv[1]):
        src = db.get_source(source_id)
        candidates.append({
            "source_id": source_id,
            "citekey": src["citekey"] if src else source_id,
            "title": src["title"] if src else "(desconhecido)",
            "similarity": similarity,
        })
    return candidates


def _resolve_duplicate_decision(
    file_path: Path,
    candidates: list[dict[str, Any]],
    interactive: bool,
    duplicate_action: str | None,
    cfg: AppConfig,
) -> str:
    """Decide what to do about a suspected semantic duplicate.

    Returns one of "skip", "continue", "abort".
    """
    if not interactive:
        action = duplicate_action or cfg.harvest.non_interactive_duplicate_action
        logger.warning(
            "Suspeita de duplicidade semantica para '%s' (modo nao-interativo, acao='%s'). "
            "Candidatos: %s",
            file_path.name, action,
            ", ".join(f"{c['citekey']} ({c['similarity']:.2f})" for c in candidates),
        )
        return action

    from rich.console import Console
    from rich.table import Table
    from rich.prompt import Prompt

    console = Console(stderr=True)
    table = Table(title=f"Possivel duplicata: {file_path.name}")
    table.add_column("Citekey", style="bold")
    table.add_column("Titulo")
    table.add_column("Similaridade", justify="right")
    for c in candidates:
        table.add_row(c["citekey"], c["title"], f"{c['similarity']:.0%}")
    console.print(table)

    choice = Prompt.ask(
        "O conteudo parece semelhante a fonte(s) ja existente(s). O que deseja fazer?",
        choices=["pular", "continuar", "abortar"],
        default="pular",
        console=console,
    )
    return {"pular": "skip", "continuar": "continue", "abortar": "abort"}[choice]


# ── Year Extraction Helpers ────────────────────────────────────────────


def _extract_year_from_pdf_date(pdf_date: str | None) -> int | None:
    """Extract year from PDF date strings like 'D:20230415...' or '2023-04-15'."""
    if not pdf_date:
        return None
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


def _extract_text(
    cfg: AppConfig, file_path: Path, origin_type: str
) -> tuple[str, dict[str, Any]]:
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
        from docling.datamodel.pipeline_options import (
            PdfPipelineOptions, AcceleratorDevice, AcceleratorOptions,
        )
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
        logger.info(
            "Docling: Iniciando conversao de %s (dispositivo: %s)", file_path.name, device.upper()
        )

        result = converter.convert(str(file_path))
        text = result.document.export_to_markdown()

        metadata: dict[str, Any] = {"title": file_path.stem, "authors": [], "year": None}
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
                    metadata["year"] = _extract_year_from_string(origin.date)
        except Exception:
            pass

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
        logger.error(
            "Nem Docling nem PyMuPDF estao instalados. Instale um deles para processar PDFs."
        )
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
        year = _extract_year_from_string(str(fm_meta["date"]))

    return body, {"title": title, "authors": authors, "year": year}


# ── Citekey Generation ────────────────────────────────────────────────


def _generate_citekey(db: StateDB, authors: list[str], year: int | None, title: str) -> str:
    """Generate a tiered citekey based on available metadata."""
    surname = ""
    if authors and authors[0]:
        parts = authors[0].strip().split()
        if parts:
            surname = parts[-1]

    has_author = bool(surname)
    has_year = year is not None

    words = re.sub(r"[^\w\s]", "", title).split()

    if has_author and has_year:
        slug_words = [w.capitalize() for w in words[:2]]
        slug = "".join(slug_words) if slug_words else "Untitled"
        base = f"{surname}{year}{slug}"
    elif has_author:
        slug_words = [w.capitalize() for w in words[:3]]
        slug = "".join(slug_words) if slug_words else "Untitled"
        base = f"{surname}{slug}"
    elif has_year:
        slug_words = [w.capitalize() for w in words[:3]]
        slug = "".join(slug_words) if slug_words else "Untitled"
        base = f"{year}{slug}"
    else:
        slug_words = [w.capitalize() for w in words[:4]]
        slug = "".join(slug_words) if slug_words else "Untitled"
        base = slug

    citekey = base
    suffix_idx = 0
    while db.get_source_by_citekey(citekey):
        suffix_idx += 1
        citekey = f"{base}{chr(96 + suffix_idx)}"

    return citekey


# ── Chapter Splitting ─────────────────────────────────────────────────


def _split_into_chapters(text: str, origin_type: str) -> list[dict[str, str]]:
    """Split text into chapters/sections (Level 1 hierarchy)."""
    chapters: list[dict[str, str]] = []

    heading_pattern = re.compile(r"^(#{1,2})\s+(.+)$", re.MULTILINE)
    matches = list(heading_pattern.finditer(text))

    if not matches:
        return [{"title": "Documento completo", "text": text.strip(), "locator": ""}]

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

        existing_chapters = db.get_chapters_for_source(source_id)
        existing_ch = next(
            (c for c in existing_chapters if c["chapter_id"] == chapter_id), None
        )
        if existing_ch and existing_ch["chapter_checksum"] == chapter_checksum:
            logger.debug("Capitulo inalterado: %s", chapter_id)
            continue

        db.upsert_chapter(chapter_id, source_id, chapter["title"], chapter_checksum, chapter["locator"])

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

    src_meta, src_body = build_source_note(
        source_id, citekey, title, authors, year, origin_path, origin_type, checksum
    )
    src_filename = note_filename("SRC", f"@{citekey}", title)
    safe_write_note(vault / "10_Sources" / src_filename, src_meta, src_body)

    lit_meta, lit_body = build_literature_note(source_id, citekey, title)
    lit_filename = note_filename("LIT", f"@{citekey}", title)
    safe_write_note(vault / "20_Literature" / lit_filename, lit_meta, lit_body)
