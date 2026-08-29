"""The Harvester — file detection, text extraction, chunking, SRC/LIT creation.

Supports: PDF (Docling / PyMuPDF), Markdown.
Audio support is stub-only (requires faster-whisper).
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from zettel.config import AppConfig
from zettel.hashing import (
    file_sha256,
    normalize_text_for_hash,
    sha256_hex,
    short_hash,
)
from zettel.index import VectorIndex
from zettel.paging import (
    ContentPaging,
    apply_page_inference,
    build_page_map_from_texts,
    compute_docling_config_hash,
    compute_page_in_book,
    extract_page_hint,
    lookup_page_for_chunk,
    suggest_content_start,
)
from zettel.state import StateDB
from zettel.vault import (
    build_literature_index_note,
    build_source_note,
    literature_index_filename,
    source_note_filename,
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
    skip_biblio: bool = False,
    content_start_file: int | None = None,
    content_start_book: int | None = None,
    skip_paging: bool = False,
    dump_dir: Path | None = None,
    extraction_dump_dir: Path | None = None,
) -> list[str]:
    """Scan inbox, extract text, create SRC + LIT index, chunk. Returns new source_ids.

    Args:
        interactive: if True and a probable duplicate is detected, prompt the user
            (skip / continue / abort). If False, `duplicate_action` (or the config
            default `harvest.non_interactive_duplicate_action`) decides automatically.
        duplicate_action: overrides the configured non-interactive default
            ("skip" | "continue" | "abort"). Ignored when `interactive` is True.
        skip_biblio: if True, allow incomplete bibliographic metadata in non-interactive
            mode; otherwise incomplete biblio skips the file when not interactive.
        content_start_file: PDF/file page (1-based) where content processing starts.
        content_start_book: printed page number on that first content page (default 1).
        skip_paging: skip HITL; process from file page 1 with book page = file page.
        dump_dir: if set, write a markdown dump of persisted chunks per new source.
        extraction_dump_dir: if set, write the persisted extraction Markdown
            (Docling/MD) as soon as ``extracted_text`` is saved.
    """
    new_sources: list[str] = []
    inbox = cfg.inbox_path

    from zettel.hashing import compute_pipeline_signature
    signature = compute_pipeline_signature({
        "chunking": cfg.chunking.model_dump(),
        "harvest": cfg.harvest.model_dump(),
        "images": cfg.images.model_dump(),
        "pdf_extractor": cfg.pdf_extractor,
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
    """Re-chunk sources from their persisted extracted_text, without touching files.

    Applies the current chunking config (e.g. new structural sections). Sources whose
    extracted_text predates Fase 0 (NULL) are skipped with a warning — they need a
    re-harvest of the original file. Returns {"sources": N, "chunks": M, "skipped": K}.
    """
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
        chapters = _split_into_chapters(text, src["origin_type"])
        paging = ContentPaging(
            content_start_file_page=int(src.get("content_start_file_page") or 1),
            content_start_book_page=int(src.get("content_start_book_page") or 1),
            confidence=src.get("page_offset_confidence") or "skipped",
        )
        # Prefer rebuilding page_map from origin PDF when available
        page_map: list[tuple[int, str]] = []
        origin = src.get("origin_path")
        if origin and Path(origin).suffix.lower() == ".pdf" and Path(origin).exists():
            try:
                page_map = _pymupdf_page_map(Path(origin))
            except Exception as e:
                logger.debug("Page map indisponivel no rechunk de %s: %s", sid, e)
        n = _chunk_and_persist(
            cfg, db, idx, sid, chapters, page_map=page_map, paging=paging,
        )
        _finalize_source_chunking(db, idx, sid, chapters)
        _maybe_dump_chunks(cfg, db, sid, dump_dir)
        stats["sources"] += 1
        stats["chunks"] += n
        logger.info("Rechunk %s: %d chunks (%d capitulos)", sid, n, len(chapters))
    return stats


def run_set_paging(
    cfg: AppConfig,
    db: StateDB,
    idx: VectorIndex,
    source_id: str,
    *,
    content_start_file: int,
    content_start_book: int = 1,
    drop_before_start: bool = False,
) -> dict[str, int]:
    """Repair paging on an existing source without re-calling the LLM.

    Updates source bounds, recomputes ``page_in_book`` on all chunks, drops
    ``pending`` chunks before ``content_start_file``, optionally drops
    awaiting_review/approved with ``--drop-before-start``, and patches LIT
    frontmatter page fields in the vault.
    """
    from zettel.vault import (
        literature_chunk_filename_for_row,
        parse_frontmatter,
        safe_write_note,
    )

    src = db.get_source(source_id)
    if not src:
        raise ValueError(f"Fonte nao encontrada: {source_id}")

    start_file = max(1, int(content_start_file))
    start_book = max(1, int(content_start_book))
    paging = ContentPaging(start_file, start_book, "confirmed")

    total_pages_file = src.get("total_pages_file")
    total_pages_book = None
    if total_pages_file is not None:
        total_pages_book = max(1, int(total_pages_file) - start_file + start_book)

    db.update_source_paging(
        source_id,
        total_pages_file=total_pages_file,
        total_pages_book=total_pages_book,
        page_offset=paging.page_offset,
        page_offset_confidence=paging.confidence,
        content_start_file_page=start_file,
        content_start_book_page=start_book,
        total_chunks=None,
    )

    stats = {
        "updated": 0,
        "dropped_pending": 0,
        "dropped_other": 0,
        "notes_patched": 0,
    }

    drop_ids: list[str] = []
    for chunk in db.get_chunks_for_source(source_id):
        page_file = chunk.get("page_in_file")
        status = chunk.get("status") or "pending"
        before_start = page_file is not None and int(page_file) < start_file

        if before_start and status == "pending":
            drop_ids.append(chunk["chunk_id"])
            continue
        if before_start and drop_before_start:
            drop_ids.append(chunk["chunk_id"])
            if status != "pending":
                stats["dropped_other"] += 1
            continue

        page_book = compute_page_in_book(page_file, start_file, start_book)
        db.update_chunk_pages(
            chunk["chunk_id"],
            page_in_book=page_book,
            page_confidence=chunk.get("page_confidence") or "unknown",
        )
        stats["updated"] += 1

        lit_path_str = chunk.get("literature_note_path")
        if not lit_path_str:
            continue
        path = Path(lit_path_str)
        if not path.exists():
            continue
        citekey = src["citekey"]
        updated_row = dict(chunk)
        updated_row["page_in_book"] = page_book
        new_path = path.parent / literature_chunk_filename_for_row(citekey, updated_row)
        if new_path != path:
            try:
                new_path.parent.mkdir(parents=True, exist_ok=True)
                path.replace(new_path)
                path = new_path
            except OSError:
                continue
            db.update_chunk_review(
                chunk["chunk_id"], literature_note_path=str(path)
            )
        try:
            meta, body = parse_frontmatter(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        meta["page_in_file"] = page_file
        meta["page_in_book"] = page_book
        if chunk.get("page_confidence"):
            meta["page_confidence"] = chunk["page_confidence"]
        safe_write_note(path, meta, body)
        stats["notes_patched"] += 1

    if drop_ids:
        # Remove draft files for dropped chunks when present
        for cid in drop_ids:
            ch = db.get_chunk(cid)
            if not ch:
                continue
            if ch.get("status") == "pending":
                stats["dropped_pending"] += 1
            lit = ch.get("literature_note_path")
            if lit:
                try:
                    Path(lit).unlink(missing_ok=True)
                except OSError:
                    pass
        db.delete_chunks(drop_ids)
        idx.delete_chunks(drop_ids)

    remaining = len(db.get_chunks_for_source(source_id))
    db.update_source_paging(source_id, total_chunks=remaining)

    # Refresh SRC note paging fields
    authors = []
    try:
        authors = json.loads(src.get("authors") or "[]")
    except (json.JSONDecodeError, TypeError):
        authors = []
    biblio_fm = None
    if src.get("bibliography_json"):
        try:
            raw = json.loads(src["bibliography_json"])
            biblio_fm = {
                k: v for k, v in raw.items()
                if k not in ("document_type", "title", "authors", "year", "confidence")
            }
        except (json.JSONDecodeError, TypeError):
            biblio_fm = None
    _create_vault_notes(
        cfg,
        source_id,
        src["citekey"],
        src["title"],
        authors,
        src.get("year"),
        src.get("origin_path") or "",
        src.get("origin_type") or "pdf",
        src.get("file_checksum") or "",
        document_type=src.get("document_type"),
        biblio_fields=biblio_fm,
        abnt_reference=src.get("abnt_reference"),
        total_pages_file=total_pages_file,
        total_pages_book=total_pages_book,
        page_offset=paging.page_offset,
        page_offset_confidence=paging.confidence,
        content_start_file_page=start_file,
        content_start_book_page=start_book,
        processing_status=src.get("processing_status"),
        total_chunks=remaining,
        docling_config_hash=src.get("docling_config_hash"),
        db=db,
        cost_usd_total=src.get("cost_usd_total"),
        cost_usd_llm=src.get("cost_usd_llm"),
        cost_usd_embedding=src.get("cost_usd_embedding"),
        tokens_prompt=src.get("tokens_prompt"),
        tokens_completion=src.get("tokens_completion"),
        tokens_embedding=src.get("tokens_embedding"),
    )
    from zettel.review import _refresh_literature_index
    _refresh_literature_index(cfg, db, source_id)
    logger.info(
        "set-paging %s: updated=%d dropped_pending=%d dropped_other=%d notes=%d remaining=%d",
        source_id,
        stats["updated"],
        stats["dropped_pending"],
        stats["dropped_other"],
        stats["notes_patched"],
        remaining,
    )
    return stats


def source_chunking_incomplete(db: StateDB, source_id: str) -> bool:
    """True when persisted chapters do not cover the current H1/H2 split of extracted_text.

    Detects interrupted harvests that registered assets/partial chapters but never
    finished `_chunk_and_persist` for later sections.
    """
    src = db.get_source(source_id)
    if not src or not src.get("extracted_text"):
        return False
    chapters = _split_into_chapters(src["extracted_text"], src["origin_type"])
    return not _chapters_fully_persisted(db, source_id, chapters)


def list_incomplete_sources(db: StateDB) -> list[str]:
    """Return source_ids whose chapter coverage is incomplete vs extracted_text."""
    return [
        src["source_id"]
        for src in db.list_sources()
        if src.get("extracted_text") and source_chunking_incomplete(db, src["source_id"])
    ]


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
    cfg: AppConfig, db: StateDB, idx: VectorIndex, source_id: str
) -> tuple[str | None, dict[str, int]]:
    """Finish chunking for a source that has extracted_text but incomplete chapters."""
    empty: dict[str, int] = {}
    src = db.get_source(source_id)
    if not src or not src.get("extracted_text"):
        return None, empty
    text = src["extracted_text"]
    chapters = _split_into_chapters(text, src["origin_type"])
    logger.warning(
        "Fonte %s com chunking incompleto (%d capitulos esperados, %d no DB). "
        "Completando via rechunk. Use `zettel rechunk` se o harvest pular de novo.",
        source_id,
        len(chapters),
        len(db.get_chapters_for_source(source_id)),
    )
    paging = ContentPaging(
        content_start_file_page=int(src.get("content_start_file_page") or 1),
        content_start_book_page=int(src.get("content_start_book_page") or 1),
        confidence=src.get("page_offset_confidence") or "skipped",
    )
    page_map: list[tuple[int, str]] = []
    origin = src.get("origin_path")
    if origin and Path(origin).suffix.lower() == ".pdf" and Path(origin).exists():
        try:
            page_map = _pymupdf_page_map(Path(origin))
        except Exception as e:
            logger.debug("Page map indisponivel ao completar %s: %s", source_id, e)
    n = _chunk_and_persist(
        cfg, db, idx, source_id, chapters, page_map=page_map, paging=paging,
    )
    _finalize_source_chunking(db, idx, source_id, chapters)
    return source_id, {
        "text_len": len(text),
        "chapters": len(chapters),
        "chunks": n,
    }


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

    # ── Camada 1: duplicidade por hash de arquivo (copia renomeada) ────
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

    text, metadata = _extract_text(cfg, file_path, origin_type)
    if not text.strip():
        logger.warning("Nenhum texto extraido de: %s", file_path.name)
        return None, empty_stats

    extraction_checksum = sha256_hex(normalize_text_for_hash(text))

    # ── Camada 2: duplicidade por hash de conteudo extraido (cross-formato) ──
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

    # ── Metadados bibliograficos (ABNT) ────────────────────────────────
    from zettel.bibliography import (
        bibliography_dict,
        build_bibliographic_metadata,
        format_abnt,
        frontmatter_biblio_fields,
        primary_authors,
        primary_title,
    )
    biblio = build_bibliographic_metadata(cfg, db, metadata, text, file_path.name)
    biblio = _resolve_bibliography(
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

    citekey = _generate_citekey(db, authors, year, title)
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

    chapters = _split_into_chapters(text, origin_type)

    # ── Camada 3: quase-duplicata semantica via ChromaDB ───────────────
    dup_candidates = _find_semantic_duplicate_candidates(cfg, db, idx, chapters)
    if dup_candidates:
        decision = _resolve_duplicate_decision(
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

    paging = _resolve_content_paging(
        page_map,
        interactive=interactive,
        content_start_file=content_start_file,
        content_start_book=content_start_book,
        skip_paging=skip_paging,
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

    # Grava SRC + indice LIT ANTES dos embeddings (podem demorar minutos).
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
    chunk_count = _chunk_and_persist(
        cfg, db, idx, source_id, chapters,
        page_map=page_map,
        paging=paging,
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

    cost_kwargs: dict = {}
    tracker = get_tracker()
    if tracker:
        delta = tracker.summary_for_source(source_id).as_dict()
        db.add_source_usage(source_id, delta)
        row = db.get_source(source_id) or {}
        cost_kwargs = {
            "cost_usd_total": row.get("cost_usd_total"),
            "cost_usd_llm": row.get("cost_usd_llm"),
            "cost_usd_embedding": row.get("cost_usd_embedding"),
            "tokens_prompt": row.get("tokens_prompt"),
            "tokens_completion": row.get("tokens_completion"),
            "tokens_embedding": row.get("tokens_embedding"),
        }

    # Atualiza SRC com offset/paginas/total_chunks/custos finais.
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
        **cost_kwargs,
    )
    set_source(None)

    stats = {"text_len": len(text), "chapters": len(chapters), "chunks": chunk_count}
    logger.info(
        "Fonte processada: %s (%d capitulos, %d chunks, %d caracteres)",
        source_id, len(chapters), chunk_count, len(text),
    )
    return source_id, stats


# ── Semantic Duplicate Detection ───────────────────────────────────────


def _sample_chunk_texts(cfg: AppConfig, chapters: list[dict[str, str]], sample_size: int) -> list[str]:
    """Split chapters into chunks (without persisting) and return an evenly distributed sample.

    Reuses the same structural chunker as `_chunk_and_persist`, so the semantic
    duplicate check samples exactly the chunks that would be persisted.
    """
    all_chunks: list[str] = []
    for chapter in chapters:
        all_chunks.extend(text for _, text in _split_chapter_into_chunks(cfg, chapter))

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

    logger.info(
        "Deduplicacao semantica: consultando Chroma com %d amostras de chunks "
        "(threshold=%.2f) — gera embeddings das amostras",
        len(sample_texts), threshold,
    )
    matches = idx.find_similar_chunks(sample_texts, n_results=3)
    logger.info(
        "Deduplicacao semantica: %d hits retornados pelo indice de chunks",
        len(matches),
    )
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
    from rich.prompt import Prompt
    from rich.table import Table

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


def _resolve_bibliography(
    file_path: Path,
    biblio: Any,
    interactive: bool,
    skip_biblio: bool,
    cfg: AppConfig,
) -> Any | None:
    """Confirm/complete bibliographic metadata. Returns meta or None to skip file.

    In interactive mode always shows a preview (even when already complete) so the
    user can confirm or edit before SRC is written and embeddings start.
    """
    from zettel.bibliography import (
        BIBLIO_FRONTMATTER_FIELDS,
        DOCUMENT_TYPE_LABELS,
        DOCUMENT_TYPES,
        FIELD_LABELS,
        REQUIRED_FIELDS,
        BibliographicMetadata,
        format_abnt,
        is_complete,
        missing_required,
        required_fields,
    )

    threshold = cfg.harvest.biblio_confidence_threshold
    complete = is_complete(biblio, threshold)

    if not interactive:
        if complete:
            logger.info(
                "Biblio completa para '%s' (tipo=%s, confidence=%.2f) — "
                "aceita sem prompt (modo nao-interativo)",
                file_path.name, biblio.document_type, biblio.confidence,
            )
            return biblio
        if skip_biblio:
            logger.warning(
                "Metadados bibliograficos incompletos para '%s' "
                "(faltando: %s; confidence=%.2f). Seguindo por --skip-biblio.",
                file_path.name,
                ", ".join(missing_required(biblio)) or "tipo incerto",
                biblio.confidence,
            )
            return biblio
        return None

    from rich.console import Console
    from rich.prompt import Confirm, Prompt
    from rich.table import Table

    console = Console(stderr=True)
    meta = biblio.model_copy(deep=True)

    def _show_preview(m: Any, title: str = "Campos inferidos") -> None:
        console.print(f"\n[bold]Metadados bibliograficos: {file_path.name}[/bold]")
        table = Table(title=title)
        table.add_column("Campo")
        table.add_column("Valor")
        table.add_row("document_type", m.document_type or "(ausente)")
        table.add_row("confidence", f"{m.confidence:.2f}")
        preview_fields = (
            required_fields(m.document_type)
            if m.document_type
            else ["title", "authors", "year"]
        )
        for field in preview_fields:
            if field == "document_type":
                continue
            value = getattr(m, field, None)
            if isinstance(value, list):
                display = ", ".join(value) if value else "(vazio)"
            else:
                display = str(value) if value not in (None, "") else "(vazio)"
            table.add_row(FIELD_LABELS.get(field, field), display)
        console.print(table)
        if m.document_type:
            abnt = format_abnt(m)
            if abnt:
                console.print(f"\n[bold]Referencia ABNT:[/bold]\n{abnt}")

    _show_preview(meta)

    # Complete: still ask confirmation; decline -> edit path below.
    force_edit = False
    if complete:
        if Confirm.ask(
            "Metadados completos. Confirmar e gravar SRC?",
            default=True,
            console=console,
        ):
            meta.confidence = max(meta.confidence, threshold)
            return BibliographicMetadata.model_validate(meta.model_dump())
        console.print("[cyan]Edicao dos metadados:[/cyan]")
        force_edit = True

    low_confidence = not meta.document_type or meta.confidence < threshold
    if low_confidence or not meta.document_type or not complete or force_edit:
        ask_type = (not meta.document_type) or low_confidence or Confirm.ask(
            "Alterar tipo documental?",
            default=not bool(meta.document_type),
            console=console,
        )
        if ask_type:
            console.print("Tipos disponiveis:")
            for i, dtype in enumerate(DOCUMENT_TYPES, 1):
                console.print(f"  {i}. {dtype} — {DOCUMENT_TYPE_LABELS[dtype]}")
            default_idx = (
                str(DOCUMENT_TYPES.index(meta.document_type) + 1)
                if meta.document_type in DOCUMENT_TYPES
                else "1"
            )
            choice = Prompt.ask(
                "Tipo documental",
                choices=[str(i) for i in range(1, len(DOCUMENT_TYPES) + 1)],
                default=default_idx,
                console=console,
            )
            meta.document_type = DOCUMENT_TYPES[int(choice) - 1]
            meta.confidence = max(meta.confidence, threshold)

    to_fill = [f for f in missing_required(meta) if f != "document_type"]
    if to_fill:
        console.print(
            "[cyan]Preencha os campos obrigatorios faltantes "
            "(Enter deixa vazio):[/cyan]"
        )
    elif Confirm.ask(
        "Revisar campos obrigatorios ja preenchidos?", default=False, console=console,
    ):
        to_fill = [
            f for f in required_fields(meta.document_type) if f != "document_type"
        ]

    for field in to_fill:
        current = getattr(meta, field, None)
        if isinstance(current, list):
            default = ", ".join(current) if current else ""
        elif current is None:
            default = ""
        else:
            default = str(current)

        label = FIELD_LABELS.get(field, field)
        answer = Prompt.ask(label, default=default or "", console=console)
        answer = answer.strip()
        if field in ("authors", "chapter_authors", "book_editors"):
            setattr(
                meta, field,
                [a.strip() for a in answer.split(",") if a.strip()] if answer else [],
            )
        elif field == "year":
            try:
                setattr(meta, field, int(answer) if answer else None)
            except ValueError:
                setattr(meta, field, None)
        else:
            setattr(meta, field, answer or None)

    still_missing = missing_required(meta)
    if still_missing:
        console.print(
            f"[yellow]Ainda faltam campos obrigatorios: {', '.join(still_missing)}[/yellow]"
        )
        if Confirm.ask("Continuar mesmo assim?", default=False, console=console):
            meta.confidence = max(meta.confidence, threshold)
        else:
            return None

    if Confirm.ask("Preencher campos opcionais?", default=False, console=console):
        optional = [
            f for f in BIBLIO_FRONTMATTER_FIELDS
            if f not in REQUIRED_FIELDS.get(meta.document_type, ())
        ]
        for field in optional:
            current = getattr(meta, field, None)
            if isinstance(current, list) and current:
                continue
            if isinstance(current, str) and current.strip():
                continue
            if current not in (None, "", []):
                continue
            label = FIELD_LABELS.get(field, field)
            answer = Prompt.ask(f"{label} (opcional)", default="", console=console)
            answer = answer.strip()
            if not answer:
                continue
            if field in ("authors", "chapter_authors", "book_editors"):
                setattr(meta, field, [a.strip() for a in answer.split(",") if a.strip()])
            else:
                setattr(meta, field, answer)

    _show_preview(meta, title="Metadados finais")
    if not Confirm.ask("Confirmar e gravar SRC?", default=True, console=console):
        return None

    meta.confidence = max(meta.confidence, threshold)
    return BibliographicMetadata.model_validate(meta.model_dump())


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
    """Extract text and basic metadata from a file.

    Extracted images (when images.enabled) are stashed in metadata["_images"] as a
    list of {checksum, path, context_snippet} for later DB registration.
    """
    if origin_type == "pdf":
        return _extract_pdf(cfg, file_path)
    return _extract_markdown(cfg, file_path)


def _extract_pdf(cfg: AppConfig, file_path: Path) -> tuple[str, dict[str, Any]]:
    """Extract text from PDF using configured extractor."""
    if cfg.pdf_extractor == "docling":
        return _extract_pdf_docling(cfg, file_path)
    return _extract_pdf_pymupdf(file_path)


def _extract_pdf_docling(cfg: AppConfig, file_path: Path) -> tuple[str, dict[str, Any]]:
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
        text = result.document.export_to_markdown()

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
                    metadata["year"] = _extract_year_from_string(origin.date)
        except Exception:
            pass

        if not metadata["authors"] or not metadata["year"]:
            _enrich_metadata_from_pymupdf(file_path, metadata)

        num_pages = getattr(result.document, "num_pages", None)
        if isinstance(num_pages, int):
            metadata["total_pages_file"] = num_pages
        # Page map from PyMuPDF (Docling markdown loses page boundaries).
        try:
            logger.info("Montando mapa de paginas via PyMuPDF para inferencia de pagina...")
            page_map = _pymupdf_page_map(file_path)
            if page_map:
                metadata["_page_map"] = page_map
                if "total_pages_file" not in metadata:
                    metadata["total_pages_file"] = len(page_map)
                logger.info("Mapa de paginas: %d paginas do arquivo", len(page_map))
        except Exception as e:
            logger.debug("Page map PyMuPDF indisponivel: %s", e)

        logger.info(
            "Docling: Conversao concluida - %s (%d caracteres, %s paginas)",
            file_path.name, len(text), metadata.get("total_pages_file", "?"),
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
            "total_pages_file": doc.page_count,
            "_page_map": build_page_map_from_texts(pages),
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


def _pymupdf_page_map(file_path: Path) -> list[tuple[int, str]]:
    """Build a (page_no, text) map via PyMuPDF for page inference."""
    import pymupdf
    doc = pymupdf.open(str(file_path))
    try:
        pages = [page.get_text() for page in doc]
    finally:
        doc.close()
    return build_page_map_from_texts(pages)


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


def _extract_markdown(cfg: AppConfig, file_path: Path) -> tuple[str, dict[str, Any]]:
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


# ── Section Splitting (structural, H3-H6) ─────────────────────────────


def _split_chapter_into_sections(
    chapter_title: str, chapter_text: str, min_section_chars: int
) -> list[dict[str, str]]:
    """Split a chapter's text into sub-sections by H3-H6 headings.

    Returns [{"section_path": "Cap > Sub > Subsub", "text": ...}]. Text before the
    first sub-heading (and chapters without any H3+) yields a single section whose
    path is the chapter title. Sections shorter than `min_section_chars` are merged
    forward to avoid crumb-sized chunks.
    """
    heading_re = re.compile(r"^(#{3,6})\s+(.+)$", re.MULTILINE)
    matches = list(heading_re.finditer(chapter_text))
    if not matches:
        return [{"section_path": chapter_title, "text": chapter_text.strip()}]

    raw: list[dict[str, str]] = []
    if matches[0].start() > 0:
        preamble = chapter_text[: matches[0].start()].strip()
        if preamble:
            raw.append({"section_path": chapter_title, "text": preamble})

    stack: list[tuple[int, str]] = []  # (heading level, title)
    for i, m in enumerate(matches):
        level = len(m.group(1))
        title = m.group(2).strip()
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
        section_path = " > ".join([chapter_title] + [t for _, t in stack])

        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(chapter_text)
        text = chapter_text[start:end].strip()
        if text:
            raw.append({"section_path": section_path, "text": text})

    return _merge_small_sections(raw, min_section_chars)


def _merge_small_sections(
    sections: list[dict[str, str]], min_section_chars: int
) -> list[dict[str, str]]:
    """Merge sections shorter than min_section_chars into the following one.

    A trailing small section is appended to the previous kept section instead.
    """
    if not sections:
        return sections

    merged: list[dict[str, str]] = []
    carry: dict[str, str] | None = None
    for sec in sections:
        if carry:
            sec = {
                "section_path": sec["section_path"],
                "text": carry["text"] + "\n\n" + sec["text"],
            }
            carry = None
        if len(sec["text"]) < min_section_chars:
            carry = sec
        else:
            merged.append(sec)
    if carry:
        if merged:
            merged[-1]["text"] += "\n\n" + carry["text"]
        else:
            merged.append(carry)
    return merged


def _split_chapter_into_chunks(
    cfg: AppConfig, chapter: dict[str, str]
) -> list[tuple[str, str]]:
    """Return (section_path, chunk_text) pairs for one chapter.

    Sections that fit in chunk_size become a single chunk; larger ones are further
    split by the generic RecursiveCharacterTextSplitter (fallback within a section).
    """
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=cfg.chunking.chunk_size,
        chunk_overlap=cfg.chunking.chunk_overlap,
        separators=["\n\n", "\n", ". ", " ", ""],
    )

    pairs: list[tuple[str, str]] = []
    sections = _split_chapter_into_sections(
        chapter["title"], chapter["text"], cfg.chunking.min_section_chars
    )
    for sec in sections:
        text = sec["text"]
        if not text:
            continue
        pieces = [text] if len(text) <= cfg.chunking.chunk_size else splitter.split_text(text)
        for piece in pieces:
            if piece.strip():
                pairs.append((sec["section_path"], piece))
    return pairs


# ── Chunking ──────────────────────────────────────────────────────────


def _chunk_and_persist(
    cfg: AppConfig, db: StateDB, idx: VectorIndex,
    source_id: str, chapters: list[dict[str, str]],
    page_map: list[tuple[int, str]] | None = None,
    paging: ContentPaging | None = None,
) -> int:
    """Split chapters into structural chunks and persist to state + index.

    Chunks carry a hierarchical `section_path` locator plus inferred page numbers.
    Chunks with ``page_in_file < content_start_file_page`` are discarded (not
    persisted). Unchanged chapters are skipped (by checksum); when a chapter
    changes, its stale chunks are removed and only genuinely new chunk ids are
    re-embedded.
    Returns total chunk count for the source after this pass (all chapters).
    """
    page_map = page_map or []
    paging = paging or ContentPaging()
    allow_regex = not bool(page_map)
    start_file = paging.content_start_file_page
    start_book = paging.content_start_book_page

    # Global chunk index across chapters for stable ordering / filenames.
    existing_all = db.get_chunks_for_source(source_id)
    next_index = 0
    if existing_all:
        idxs = [c.get("chunk_index") for c in existing_all if c.get("chunk_index") is not None]
        if idxs:
            next_index = max(idxs) + 1

    # First pass: collect all new specs with provisional pages, then infer.
    pending_specs: list[dict[str, Any]] = []

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

        chunk_pairs = _split_chapter_into_chunks(cfg, chapter)
        keep_ids: set[str] = set()
        chapter_specs: list[dict[str, Any]] = []
        for section_path, chunk_text in chunk_pairs:
            chunk_norm = normalize_text_for_hash(chunk_text)
            chunk_checksum = sha256_hex(chunk_norm)
            chunk_id = f"{source_id}::{chapter_id}::{short_hash(chunk_checksum)}"
            if chunk_id in keep_ids:
                logger.debug(
                    "Chunk duplicado por conteudo ignorado: %s (%s)",
                    chunk_id, section_path,
                )
                continue
            keep_ids.add(chunk_id)
            meta_page = lookup_page_for_chunk(chunk_text, page_map) if page_map else None
            hint = extract_page_hint(
                chunk_text, page_from_meta=meta_page, allow_regex=allow_regex,
            )
            chapter_specs.append({
                "chunk_id": chunk_id,
                "chapter_id": chapter_id,
                "section_path": section_path,
                "text": chunk_text,
                "chunk_checksum": chunk_checksum,
                "page_hint": hint,
            })

        removed = db.delete_chunks_for_chapter(chapter_id, keep_ids)
        if removed:
            idx.delete_chunks(removed)

        # Assign sequential indices for new/changed chapter chunks
        for spec in chapter_specs:
            # Reuse index if chunk already existed
            old = db.get_chunk(spec["chunk_id"])
            if old and old.get("chunk_index") is not None:
                spec["chunk_index"] = old["chunk_index"]
            else:
                spec["chunk_index"] = next_index
                next_index += 1
            pending_specs.append(spec)

    # Infer missing pages across the newly written batch using only this batch's
    # hints plus already-persisted pages for the source.
    if pending_specs:
        hints = [s["page_hint"] for s in pending_specs]
        inferred = apply_page_inference(hints)
        for spec, hint in zip(pending_specs, inferred):
            spec["page_in_file"] = hint.page_in_file
            spec["page_confidence"] = hint.confidence

        before = len(pending_specs)
        kept: list[dict[str, Any]] = []
        skipped_ids: list[str] = []
        for spec in pending_specs:
            page_file = spec.get("page_in_file")
            # Unknown page: keep (cannot prove it is before content start).
            if page_file is not None and page_file < start_file:
                skipped_ids.append(spec["chunk_id"])
                continue
            page_book = compute_page_in_book(page_file, start_file, start_book)
            spec["page_in_book"] = page_book
            kept.append(spec)
        pending_specs = kept
        if skipped_ids:
            db.delete_chunks(skipped_ids)
            idx.delete_chunks(skipped_ids)
            logger.info(
                "[SOURCE=%s] %d chunk(s) antes da p.%d do arquivo ignorados "
                "(%d permanecem de %d)",
                source_id, len(skipped_ids), start_file, len(pending_specs), before,
            )

        already = idx.existing_ids("chunks", [s["chunk_id"] for s in pending_specs]) if pending_specs else set()
        to_embed = [s for s in pending_specs if s["chunk_id"] not in already]
        logger.info(
            "[SOURCE=%s] Persistindo %d chunks no SQLite; "
            "gerando embeddings no Chroma para %d novos "
            "(%d ja indexados, pulados)",
            source_id, len(pending_specs), len(to_embed), len(already),
        )
        embed_i = 0
        for spec in pending_specs:
            db.upsert_chunk(
                chunk_id=spec["chunk_id"],
                source_id=source_id,
                chapter_id=spec["chapter_id"],
                text=spec["text"],
                chunk_checksum=spec["chunk_checksum"],
                locator=spec["section_path"],
                section_path=spec["section_path"],
                chunk_index=spec["chunk_index"],
                page_in_file=spec.get("page_in_file"),
                page_in_book=spec.get("page_in_book"),
                page_confidence=spec.get("page_confidence", "unknown"),
            )
            if spec["chunk_id"] not in already:
                embed_i += 1
                idx.upsert_chunk(
                    spec["chunk_id"],
                    spec["text"],
                    {
                        "source_id": source_id,
                        "chapter_id": spec["chapter_id"],
                        "locator": spec["section_path"],
                        "section_path": spec["section_path"],
                        "chunk_index": spec["chunk_index"],
                        "page_in_file": spec.get("page_in_file") or -1,
                        "page_in_book": spec.get("page_in_book") or -1,
                    },
                    progress=(embed_i, len(to_embed)),
                )
        if to_embed:
            logger.info(
                "[SOURCE=%s] Embeddings de chunks concluidos: %d/%d",
                source_id, len(to_embed), len(to_embed),
            )

    # Return total chunks currently stored for the source
    return len(db.get_chunks_for_source(source_id))


def _resolve_content_paging(
    page_map: list[tuple[int, str]],
    *,
    interactive: bool,
    content_start_file: int | None,
    content_start_book: int | None,
    skip_paging: bool,
) -> ContentPaging:
    """Resolve content-start file/book pages before chunking."""
    if skip_paging and content_start_file is None:
        return ContentPaging(1, 1, "skipped")

    suggested = suggest_content_start(page_map)
    sug_file = int(suggested.get("content_start_file_page") or 1)
    sug_book = int(suggested.get("content_start_book_page") or 1)

    if content_start_file is not None:
        start_file = int(content_start_file)
        start_book = int(content_start_book) if content_start_book is not None else 1
        return ContentPaging(start_file, start_book, "confirmed")

    if skip_paging or not interactive:
        conf = "skipped" if skip_paging else (
            "heuristic" if suggested.get("confidence") == "heuristic" else "skipped"
        )
        if not interactive and not skip_paging:
            # Non-interactive without flags: process all pages (file==book).
            return ContentPaging(1, 1, "skipped")
        return ContentPaging(sug_file if conf == "heuristic" else 1, sug_book if conf == "heuristic" else 1, conf)

    from rich.console import Console
    from rich.prompt import Prompt

    console = Console(stderr=True)
    anchor = suggested.get("anchor_page_in_file")
    if anchor is not None:
        console.print(
            f"[cyan]Detectei inicio de conteudo na pagina {anchor} do arquivo. "
            f"Sugestao: arquivo p.{sug_file} = impressa p.{sug_book}.[/cyan]"
        )
    else:
        console.print(
            "[cyan]Nao detectei Capitulo 1 / Introduction no mapa de paginas. "
            "Padrao: processar desde p.1 do arquivo (numeracao = pagina do arquivo).[/cyan]"
        )
    console.print(
        "[dim]Paginas do arquivo anteriores ao inicio serao ignoradas no chunking/extract. "
        "Chunk que cruza paginas usa a pagina do inicio do trecho.[/dim]"
    )

    file_answer = Prompt.ask(
        "Pagina do arquivo (PDF) onde o conteudo comeca",
        default=str(sug_file),
        console=console,
    )
    book_answer = Prompt.ask(
        "Numero impresso nessa primeira pagina de conteudo",
        default=str(sug_book),
        console=console,
    )
    try:
        start_file = max(1, int(file_answer.strip()))
    except ValueError:
        logger.warning("Inicio de arquivo invalido '%s'; usando %d", file_answer, sug_file)
        start_file = sug_file
    try:
        start_book = max(1, int(book_answer.strip()))
    except ValueError:
        logger.warning("Inicio impresso invalido '%s'; usando %d", book_answer, sug_book)
        start_book = sug_book
    return ContentPaging(start_file, start_book, "confirmed")


# ── Vault Note Creation ───────────────────────────────────────────────


def _create_vault_notes(
    cfg: AppConfig, source_id: str, citekey: str, title: str,
    authors: list[str], year: int | None, origin_path: str,
    origin_type: str, checksum: str,
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
    cost_usd_total: float | None = None,
    cost_usd_llm: float | None = None,
    cost_usd_embedding: float | None = None,
    tokens_prompt: int | None = None,
    tokens_completion: int | None = None,
    tokens_embedding: int | None = None,
) -> None:
    """Create SRC note and empty literature index in the vault."""
    vault = cfg.vault_path

    src_meta, src_body = build_source_note(
        source_id, citekey, title, authors, year, origin_path, origin_type, checksum,
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
        cost_usd_total=cost_usd_total,
        cost_usd_llm=cost_usd_llm,
        cost_usd_embedding=cost_usd_embedding,
        tokens_prompt=tokens_prompt,
        tokens_completion=tokens_completion,
        tokens_embedding=tokens_embedding,
    )
    src_filename = source_note_filename(citekey, title)
    safe_write_note(vault / "10_Sources" / src_filename, src_meta, src_body)

    lit_meta, lit_body = build_literature_index_note(source_id, citekey, title)
    lit_filename = literature_index_filename(citekey, title)
    lit_path = vault / "20_Literature" / lit_filename
    safe_write_note(lit_path, lit_meta, lit_body)
    if db is not None:
        from zettel.vault import compose_note
        db.update_source_texts(source_id, lit_body=compose_note(lit_meta, lit_body))
