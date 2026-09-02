"""Harvest pipeline orchestration: file processing, chunking, and persistence."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from zettel.config import AppConfig
from zettel.hashing import (
    compute_pipeline_signature,
    file_sha256,
    normalize_text_for_hash,
    sha256_hex,
)
from zettel.index import VectorIndex
from zettel.paging import (
    ContentPaging,
    compute_docling_config_hash,
    resolve_content_paging,
)
from zettel.state import StateDB
from zettel.vault import build_source_note, safe_write_note

from . import chunking, duplicates, extract
from .biblio_hitl import resolve_bibliography
from .citekey import generate_citekey
from .duplicates import HarvestAborted

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".pdf", ".md", ".markdown", ".txt"}


# ── Public API ─────────────────────────────────────────────────────


def run_harvest(
    cfg: AppConfig,
    db: StateDB,
    idx: VectorIndex,
    interactive: bool = True,
    duplicate_action: str | None = None,
    skip_biblio: bool = False,
    content_start_file: int | None = None,
    content_start_book: int | None = None,
    skip_paging: bool = False,
    dump_dir: Path | None = None,
    extraction_dump_dir: Path | None = None,
    selected_file: Path | None = None,
    observer=None,
) -> list[str]:
    """Scan inbox, extract text, create SRC + LIT index, chunk. Returns new source_ids."""
    new_sources: list[str] = []
    inbox = cfg.inbox_path

    signature = compute_pipeline_signature({
        "chunking": cfg.chunking.model_dump(),
        "harvest": cfg.harvest.model_dump(),
        "images": cfg.images.model_dump(),
        "docling_config_hash": compute_docling_config_hash(cfg),
    })
    run_id = db.start_run(signature)
    from zettel.usage import begin_run, finish_pipeline_run
    begin_run(run_id)
    run_status = "completed"

    if not inbox.exists():
        logger.warning("Inbox nao encontrado: %s", inbox)
        finish_pipeline_run(db, run_id, run_status)
        return new_sources

    if selected_file is not None:
        selected_file = selected_file.resolve()
        try:
            selected_file.relative_to(inbox.resolve())
        except ValueError as exc:
            raise ValueError("O arquivo selecionado deve estar dentro do inbox") from exc
        files = [selected_file] if selected_file.is_file() else []
    else:
        files = [
            f for f in inbox.rglob("*")
            if f.suffix.lower() in SUPPORTED_EXTENSIONS and f.is_file()
        ]
    logger.info("Encontrados %d arquivos no inbox", len(files))
    from zettel.progress import report
    report(observer, "harvest", f"{len(files)} arquivo(s) encontrado(s).", total_items=len(files))

    total_stats = {"text_len": 0, "chapters": 0, "chunks": 0}
    try:
        for item_index, file_path in enumerate(files, 1):
            report(
                observer, "harvest", f"Processando {file_path.name}.",
                current_item=file_path.name, current_index=item_index, total_items=len(files),
            )
            sid, stats = _process_file(
                cfg, db, idx, file_path, run_id, interactive, duplicate_action,
                skip_biblio=skip_biblio,
                content_start_file=content_start_file,
                content_start_book=content_start_book,
                skip_paging=skip_paging,
                extraction_dump_dir=extraction_dump_dir,
            )
            if sid:
                new_sources.append(sid)
                total_stats["text_len"] += stats.get("text_len", 0)
                total_stats["chapters"] += stats.get("chapters", 0)
                total_stats["chunks"] += stats.get("chunks", 0)
                _maybe_dump_chunks(cfg, db, sid, dump_dir)
    except HarvestAborted as e:
        logger.warning("Harvest abortado pelo usuario: %s", e)
        run_status = "aborted"

    if new_sources:
        logger.info(
            "Harvest concluido: %d fontes, %d caracteres, %d capitulos, %d chunks",
            len(new_sources), total_stats["text_len"],
            total_stats["chapters"], total_stats["chunks"],
        )

    finish_pipeline_run(db, run_id, run_status)
    return new_sources


def run_rechunk(
    cfg: AppConfig,
    db: StateDB,
    idx: VectorIndex,
    source_id: str | None = None,
    dump_dir: Path | None = None,
) -> dict[str, int]:
    """Re-chunk sources from their persisted extracted_text, without touching files."""
    if source_id:
        sources = [db.get_source(source_id)] if db.get_source(source_id) else []
    else:
        sources = db.list_sources()

    stats = {"sources": 0, "chunks": 0, "skipped": 0}
    for src in sources:
        if not src:
            continue
        sid = src["source_id"]
        text = src.get("extracted_text")
        if not text:
            logger.warning(
                "Fonte %s nao tem texto extraido persistido (anterior a Fase 0). "
                "Reprocesse o arquivo original via harvest.", sid,
            )
            stats["skipped"] += 1
            continue
        chapters = chunking.split_into_chapters(text, src["origin_type"])
        paging = ContentPaging(
            content_start_file_page=int(src.get("content_start_file_page") or 1),
            content_start_book_page=int(src.get("content_start_book_page") or 1),
            confidence=src.get("page_offset_confidence") or "skipped",
        )
        page_map = extract.page_map_for_source(src)
        n = chunking.chunk_and_persist(
            cfg, db, idx, sid, chapters, page_map=page_map, paging=paging,
            origin_type=src.get("origin_type") or "",
        )
        _finalize_source_chunking(db, idx, sid, chapters)
        _maybe_dump_chunks(cfg, db, sid, dump_dir)
        stats["sources"] += 1
        stats["chunks"] += n
        logger.info("Rechunk %s: %d chunks (%d capitulos)", sid, n, len(chapters))
    return stats


def source_chunking_incomplete(db: StateDB, source_id: str) -> bool:
    """True when persisted chapters do not cover the current H1/H2 split of extracted_text."""
    src = db.get_source(source_id)
    if not src or not src.get("extracted_text"):
        return False
    chapters = chunking.split_into_chapters(src["extracted_text"], src["origin_type"])
    return not _chapters_fully_persisted(db, source_id, chapters)


def list_incomplete_sources(db: StateDB) -> list[str]:
    """Return source_ids whose chapter coverage is incomplete vs extracted_text."""
    return [
        src["source_id"]
        for src in db.list_sources()
        if src.get("extracted_text") and source_chunking_incomplete(db, src["source_id"])
    ]


# ── File Processing ────────────────────────────────────────────────────


def _process_file(
    cfg: AppConfig,
    db: StateDB,
    idx: VectorIndex,
    file_path: Path,
    run_id: int,
    interactive: bool = True,
    duplicate_action: str | None = None,
    skip_biblio: bool = False,
    content_start_file: int | None = None,
    content_start_book: int | None = None,
    skip_paging: bool = False,
    extraction_dump_dir: Path | None = None,
) -> tuple[str | None, dict[str, int]]:
    """Process a single file: extract, chunk, persist. Returns (source_id, stats) or (None, {})."""
    empty_stats: dict[str, int] = {}
    checksum = file_sha256(file_path)
    existing = db.get_file(str(file_path))
    config_hash = compute_docling_config_hash(cfg)

    if existing and existing["file_checksum"] == checksum:
        sid = existing.get("source_id")
        if sid:
            src = db.get_source(sid)
            if src and src.get("docling_config_hash") and src["docling_config_hash"] != config_hash:
                logger.warning(
                    "Fonte %s: docling_config_hash mudou (%s -> %s). "
                    "Use `zettel rechunk --source-id %s` para reaplicar chunking.",
                    sid, src["docling_config_hash"], config_hash, sid,
                )
            if source_chunking_incomplete(db, sid):
                return _complete_incomplete_source(cfg, db, idx, sid)
        logger.debug("Arquivo inalterado, pulando: %s", file_path.name)
        return None, empty_stats

    renamed_from = db.get_file_by_checksum(checksum, exclude_path=str(file_path))
    if renamed_from and renamed_from.get("source_id"):
        logger.info(
            "Arquivo '%s' e uma copia identica de '%s' (mesmo hash de arquivo). "
            "Associando ao mesmo source_id (%s) em vez de reprocessar.",
            file_path.name, Path(renamed_from["path"]).name, renamed_from["source_id"],
        )
        sid = renamed_from["source_id"]
        db.upsert_file(str(file_path), checksum, file_path.suffix.lower().lstrip("."), sid)
        db.record_duplicate(run_id, "file")
        if source_chunking_incomplete(db, sid):
            return _complete_incomplete_source(cfg, db, idx, sid)
        return None, empty_stats

    ext = file_path.suffix.lower()
    origin_type = "pdf" if ext == ".pdf" else "md"

    text, metadata = extract.extract_text(cfg, file_path, origin_type)
    if not text.strip():
        logger.warning("Nenhum texto extraido de: %s", file_path.name)
        return None, empty_stats

    extraction_checksum = sha256_hex(normalize_text_for_hash(text))

    cross_format_source = db.get_source_by_extraction_checksum(extraction_checksum)
    if cross_format_source:
        sid = cross_format_source["source_id"]
        logger.info(
            "Conteudo de '%s' e identico (apos normalizacao) a fonte existente %s "
            "(%s). Reaproveitando fonte, sem gerar novo citekey/SRC/LIT/chunks.",
            file_path.name, sid, cross_format_source["citekey"],
        )
        db.upsert_file(str(file_path), checksum, origin_type, sid)
        db.record_duplicate(run_id, "content")
        if source_chunking_incomplete(db, sid):
            return _complete_incomplete_source(cfg, db, idx, sid)
        return None, empty_stats

    from zettel.bibliography import (
        bibliography_dict,
        build_bibliographic_metadata,
        format_abnt,
        frontmatter_biblio_fields,
        primary_authors,
        primary_title,
    )
    biblio = build_bibliographic_metadata(cfg, db, metadata, text, file_path.name)
    biblio = resolve_bibliography(
        file_path, biblio, interactive, skip_biblio, cfg,
    )
    if biblio is None:
        logger.warning(
            "Arquivo '%s' pulado: metadados bibliograficos incompletos "
            "(use --skip-biblio no modo nao-interativo para forcar).",
            file_path.name,
        )
        return None, empty_stats

    title = primary_title(biblio, fallback=metadata.get("title", file_path.stem))
    authors = primary_authors(biblio) or list(metadata.get("authors") or [])
    year = biblio.year if biblio.year is not None else metadata.get("year")
    abnt_reference = format_abnt(biblio) if biblio.document_type else ""
    biblio_json = json.dumps(bibliography_dict(biblio), ensure_ascii=False)
    biblio_fm = frontmatter_biblio_fields(biblio)

    citekey = generate_citekey(db, authors, year, title)
    source_id = f"@{citekey}"

    from zettel.usage import get_tracker, set_source
    set_source(source_id)

    existing_source = db.get_source(source_id)
    if existing_source and existing_source.get("extraction_checksum") == extraction_checksum:
        db.upsert_file(str(file_path), checksum, origin_type, source_id)
        set_source(None)
        if source_chunking_incomplete(db, source_id):
            return _complete_incomplete_source(cfg, db, idx, source_id)
        logger.info("Texto extraido inalterado para %s, pulando rechunking", source_id)
        return None, empty_stats

    chapters = chunking.split_into_chapters(text, origin_type)

    dup_candidates = duplicates.find_semantic_duplicate_candidates(cfg, db, idx, chapters)
    if dup_candidates:
        decision = duplicates.resolve_duplicate_decision(
            file_path, dup_candidates, interactive, duplicate_action, cfg,
        )
        if decision == "abort":
            set_source(None)
            raise HarvestAborted(f"Usuario abortou o harvest ao processar {file_path.name}")
        db.record_duplicate(run_id, "semantic")
        if decision == "skip":
            logger.warning(
                "Arquivo '%s' pulado por suspeita de duplicidade semantica (candidatos: %s).",
                file_path.name, ", ".join(c["citekey"] for c in dup_candidates),
            )
            set_source(None)
            return None, empty_stats
        logger.info(
            "Arquivo '%s' segue como nova fonte apesar da suspeita de duplicidade semantica.",
            file_path.name,
        )

    total_pages_file = metadata.get("total_pages_file")
    page_map = metadata.get("_page_map") or []
    if origin_type == "md":
        page_map = []

    paging = resolve_content_paging(
        page_map,
        interactive=interactive,
        content_start_file=content_start_file,
        content_start_book=content_start_book,
        skip_paging=skip_paging or origin_type == "md",
        printed_by_file_page=metadata.get("_printed_page_hints") or {},
        biblio_pages=getattr(biblio, "pages", None),
    )
    logger.info(
        "[SOURCE=%s] Paginacao: arquivo p.%d = impressa p.%d (offset derivado=%d, %s)",
        source_id,
        paging.content_start_file_page,
        paging.content_start_book_page,
        paging.page_offset,
        paging.confidence,
    )

    db.upsert_file(str(file_path), checksum, origin_type, source_id)
    db.upsert_source(
        source_id=source_id, citekey=citekey, title=title, authors=authors,
        year=year, file_checksum=checksum, origin_path=str(file_path),
        origin_type=origin_type, extraction_checksum=extraction_checksum,
        document_type=biblio.document_type,
        bibliography_json=biblio_json,
        abnt_reference=abnt_reference or None,
        total_pages_file=total_pages_file,
        page_offset=paging.page_offset,
        page_offset_confidence=paging.confidence,
        content_start_file_page=paging.content_start_file_page,
        content_start_book_page=paging.content_start_book_page,
        processing_status="in_progress",
        docling_config_hash=config_hash,
    )
    db.update_source_texts(source_id, extracted_text=text)
    _maybe_dump_extraction(cfg, db, source_id, extraction_dump_dir)

    logger.info(
        "[SOURCE=%s] Gravando nota SRC e indice LIT no vault (antes dos embeddings)",
        source_id,
    )
    _create_vault_notes(
        cfg, source_id, citekey, title, authors, year,
        str(file_path), origin_type, checksum,
        document_type=biblio.document_type,
        biblio_fields=biblio_fm,
        abnt_reference=abnt_reference or None,
        total_pages_file=total_pages_file,
        page_offset=paging.page_offset,
        page_offset_confidence=paging.confidence,
        content_start_file_page=paging.content_start_file_page,
        content_start_book_page=paging.content_start_book_page,
        processing_status="in_progress",
        docling_config_hash=config_hash,
        db=db,
    )
    idx.upsert_source(source_id, f"{title} -- {', '.join(authors)}", {
        "citekey": citekey, "title": title, "origin_type": origin_type,
    })

    images = metadata.get("_images") or []
    if images:
        from zettel.assets import register_assets
        logger.info(
            "[SOURCE=%s] Registrando %d imagens extraidas...",
            source_id, len(images),
        )
        register_assets(db, source_id, chapters, images)
    else:
        logger.info("[SOURCE=%s] Nenhuma imagem para registrar", source_id)

    logger.info(
        "[SOURCE=%s] Iniciando chunking estrutural (%d capitulos) "
        "e indexacao vetorial dos chunks...",
        source_id, len(chapters),
    )
    chunk_count = chunking.chunk_and_persist(
        cfg, db, idx, source_id, chapters,
        page_map=page_map,
        paging=paging,
        origin_type=origin_type,
    )
    _finalize_source_chunking(db, idx, source_id, chapters)

    total_pages_book = None
    if total_pages_file is not None:
        total_pages_book = max(
            1,
            total_pages_file - paging.content_start_file_page + paging.content_start_book_page,
        )

    db.update_source_paging(
        source_id,
        total_pages_file=total_pages_file,
        total_pages_book=total_pages_book,
        page_offset=paging.page_offset,
        page_offset_confidence=paging.confidence,
        content_start_file_page=paging.content_start_file_page,
        content_start_book_page=paging.content_start_book_page,
        processing_status="completed",
        last_chunk_processed=-1,
        total_chunks=chunk_count,
        docling_config_hash=config_hash,
    )

    tracker = get_tracker()
    if tracker:
        delta = tracker.summary_for_source(source_id).as_dict()
        db.add_source_usage(source_id, delta)

    _create_vault_notes(
        cfg, source_id, citekey, title, authors, year,
        str(file_path), origin_type, checksum,
        document_type=biblio.document_type,
        biblio_fields=biblio_fm,
        abnt_reference=abnt_reference or None,
        total_pages_file=total_pages_file,
        total_pages_book=total_pages_book,
        page_offset=paging.page_offset,
        page_offset_confidence=paging.confidence,
        content_start_file_page=paging.content_start_file_page,
        content_start_book_page=paging.content_start_book_page,
        processing_status="completed",
        total_chunks=chunk_count,
        docling_config_hash=config_hash,
        db=db,
    )
    set_source(None)

    stats = {"text_len": len(text), "chapters": len(chapters), "chunks": chunk_count}
    logger.info(
        "Fonte processada: %s (%d capitulos, %d chunks, %d caracteres)",
        source_id, len(chapters), chunk_count, len(text),
    )
    return source_id, stats


# ── Helpers ────────────────────────────────────────────────────────


def _expected_chapter_ids(source_id: str, chapters: list[dict[str, str]]) -> set[str]:
    return {f"{source_id}::ch{i:03d}" for i in range(len(chapters))}


def _chapters_fully_persisted(
    db: StateDB, source_id: str, chapters: list[dict[str, str]]
) -> bool:
    expected = _expected_chapter_ids(source_id, chapters)
    if not expected:
        return True
    actual = {c["chapter_id"] for c in db.get_chapters_for_source(source_id)}
    return expected <= actual


def _maybe_dump_chunks(
    cfg: AppConfig, db: StateDB, source_id: str, dump_dir: Path | None,
) -> None:
    """Write a markdown chunk dump when ``dump_dir`` is set (harvest/rechunk opt-in)."""
    if dump_dir is None:
        return
    from zettel.chunk_dump import dump_source_chunks
    dump_source_chunks(cfg, db, source_id, dump_dir)


def _maybe_dump_extraction(
    cfg: AppConfig, db: StateDB, source_id: str, dump_dir: Path | None,
) -> None:
    """Write extraction Markdown when ``dump_dir`` is set (harvest opt-in)."""
    if dump_dir is None:
        return
    from zettel.extraction_dump import dump_source_extraction
    dump_source_extraction(cfg, db, source_id, dump_dir)


def _finalize_source_chunking(
    db: StateDB,
    idx: VectorIndex,
    source_id: str,
    chapters: list[dict[str, str]],
) -> None:
    """Prune orphan chapters, re-resolve asset chapter_ids after a full chunk pass."""
    keep = _expected_chapter_ids(source_id, chapters)
    _prune_orphan_chapters(db, idx, source_id, keep)
    from zettel.assets import reresolve_asset_chapters
    reresolve_asset_chapters(db, source_id, chapters)


def _prune_orphan_chapters(
    db: StateDB, idx: VectorIndex, source_id: str, keep_ids: set[str]
) -> None:
    removed_chunks: list[str] = []
    for ch in db.get_chapters_for_source(source_id):
        if ch["chapter_id"] not in keep_ids:
            removed_chunks.extend(db.delete_chapter(ch["chapter_id"]))
            logger.info("Capitulo orfao removido: %s", ch["chapter_id"])
    if removed_chunks:
        idx.delete_chunks(removed_chunks)


def _complete_incomplete_source(
    cfg: AppConfig, db: StateDB, idx: VectorIndex, source_id: str,
) -> tuple[str | None, dict[str, int]]:
    """Resume chunking for a source whose extracted_text was not fully persisted."""
    src = db.get_source(source_id)
    if not src or not src.get("extracted_text"):
        return None, {}

    chapters = chunking.split_into_chapters(src["extracted_text"], src["origin_type"])
    paging = ContentPaging(
        content_start_file_page=int(src.get("content_start_file_page") or 1),
        content_start_book_page=int(src.get("content_start_book_page") or 1),
        confidence=src.get("page_offset_confidence") or "skipped",
    )
    page_map = extract.page_map_for_source(src)

    logger.info(
        "[SOURCE=%s] Completando chunking incompleto (%d capitulos)...",
        source_id, len(chapters),
    )
    chunk_count = chunking.chunk_and_persist(
        cfg, db, idx, source_id, chapters, page_map=page_map, paging=paging,
        origin_type=src.get("origin_type") or "",
    )
    _finalize_source_chunking(db, idx, source_id, chapters)

    total_pages_file = src.get("total_pages_file")
    total_pages_book = None
    if total_pages_file is not None:
        total_pages_book = max(
            1,
            total_pages_file - paging.content_start_file_page + paging.content_start_book_page,
        )

    db.update_source_paging(
        source_id,
        total_pages_file=total_pages_file,
        total_pages_book=total_pages_book,
        page_offset=paging.page_offset,
        page_offset_confidence=paging.confidence,
        processing_status="completed",
        total_chunks=chunk_count,
    )
    return source_id, {"text_len": len(src.get("extracted_text") or ""), "chapters": len(chapters), "chunks": chunk_count}


def _create_vault_notes(
    cfg: AppConfig,
    source_id: str,
    citekey: str,
    title: str,
    authors: list[str],
    year: int | None,
    origin_path: str,
    origin_type: str,
    file_checksum: str,
    document_type: str | None = None,
    biblio_fields: dict[str, Any] | None = None,
    abnt_reference: str | None = None,
    total_pages_file: int | None = None,
    total_pages_book: int | None = None,
    page_offset: int | None = None,
    page_offset_confidence: str | None = None,
    content_start_file_page: int | None = None,
    content_start_book_page: int | None = None,
    processing_status: str | None = None,
    total_chunks: int | None = None,
    docling_config_hash: str | None = None,
    db: StateDB | None = None,
) -> None:
    """Write SRC and LIT index notes to the vault."""
    from zettel.vault import (
        build_literature_index_note,
        literature_index_filename,
        source_note_filename,
        sync_source_costs_to_vault,
    )

    # Write SRC note
    src_meta, src_body = build_source_note(
        source_id, citekey, title, authors, year, origin_path, origin_type, file_checksum,
        document_type=document_type,
        biblio_fields=biblio_fields,
        abnt_reference=abnt_reference,
        total_pages_file=total_pages_file,
        total_pages_book=total_pages_book,
        page_offset=page_offset,
        page_offset_confidence=page_offset_confidence,
        content_start_file_page=content_start_file_page,
        content_start_book_page=content_start_book_page,
        processing_status=processing_status,
        total_chunks=total_chunks,
        docling_config_hash=docling_config_hash,
    )
    src_path = cfg.vault_path / "10_Sources" / source_note_filename(citekey, title)
    src_path.parent.mkdir(parents=True, exist_ok=True)
    safe_write_note(src_path, src_meta, src_body)

    # Write LIT index note. Flat, title-slugged path: this is where every generated
    # wikilink points and where review/rebuild/purge look for it.
    lit_meta, lit_body = build_literature_index_note(source_id, citekey, title)
    lit_path = cfg.vault_path / "20_Literature" / literature_index_filename(citekey, title)
    lit_path.parent.mkdir(parents=True, exist_ok=True)
    if not lit_path.exists():
        safe_write_note(lit_path, lit_meta, lit_body)

    # Sync costs accumulated in SQLite onto the SRC frontmatter
    if db:
        sync_source_costs_to_vault(cfg, db, source_id)
