"""Set or repair paging metadata on existing sources."""

from __future__ import annotations

import contextlib
import json
import logging
from pathlib import Path

from zettel.config import AppConfig
from zettel.index import VectorIndex
from zettel.paging import ContentPaging, compute_page_in_book
from zettel.state import StateDB
from zettel.vault import (
    literature_chunk_filename_for_row,
    parse_frontmatter,
    safe_write_note,
)

logger = logging.getLogger(__name__)


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
            db.update_chunk_review(chunk["chunk_id"], literature_note_path=str(path))
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
        for cid in drop_ids:
            ch = db.get_chunk(cid)
            if not ch:
                continue
            if ch.get("status") == "pending":
                stats["dropped_pending"] += 1
            lit = ch.get("literature_note_path")
            if lit:
                with contextlib.suppress(OSError):
                    Path(lit).unlink(missing_ok=True)
        db.delete_chunks(drop_ids)
        idx.delete_chunks(drop_ids)

    remaining = len(db.get_chunks_for_source(source_id))
    db.update_source_paging(source_id, total_chunks=remaining)

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
                k: v
                for k, v in raw.items()
                if k not in ("document_type", "title", "authors", "year", "confidence")
            }
        except (json.JSONDecodeError, TypeError):
            biblio_fm = None

    from .pipeline import _create_vault_notes

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
