"""Set or repair paging metadata on existing sources."""

from __future__ import annotations

import contextlib
import json
import logging
import re
from pathlib import Path

from zettel.citation import load_provenance, resolve_citation
from zettel.config import AppConfig
from zettel.index import VectorIndex
from zettel.paging import (
    ContentPaging,
    assemble_printed_pages,
    compute_page_in_book,
    max_printed_page,
    read_printed_page_candidates,
)
from zettel.state import StateDB
from zettel.vault import (
    frontmatter_json,
    literature_chunk_filename_for_row,
    parse_frontmatter,
    render_evidence_block,
    safe_write_note,
    upsert_managed_block,
)

logger = logging.getLogger(__name__)


def run_set_paging(
    cfg: AppConfig,
    db: StateDB,
    idx: VectorIndex,
    source_id: str,
    *,
    content_start_file: int | None = None,
    content_start_book: int | None = None,
    drop_before_start: bool = False,
    from_pdf: bool = False,
    printed_candidates: list[list[int]] | None = None,
) -> dict[str, int]:
    """Repair paging on an existing source without re-calling the LLM.

    Updates source bounds, recomputes ``page_in_book`` on all chunks, drops
    ``pending`` chunks before ``content_start_file``, optionally drops
    awaiting_review/approved with ``--drop-before-start``, and patches LIT
    frontmatter page fields in the vault.

    ``from_pdf`` reads the printed number from the PDF text layer and stores
    that map. ``printed_candidates`` injects the same reading (tests). Without
    either, the linear offset is recomputed and any stored map is dropped.
    """
    src = db.get_source(source_id)
    if not src:
        raise ValueError(f"Fonte nao encontrada: {source_id}")
    if (src.get("origin_type") or "pdf") != "pdf":
        # Native Markdown has no pages (ADR-013): its locator is the section path.
        raise ValueError(
            f"{source_id} e {src['origin_type']}: sem paginas para corrigir (ADR-013)."
        )

    printed_pages: list[int | None] | None = None
    if from_pdf or printed_candidates is not None:
        candidates = printed_candidates
        if candidates is None:
            origin = Path(src.get("origin_path") or "")
            if not origin.is_file():
                raise ValueError(f"PDF da fonte nao encontrado: {origin}")
            candidates = read_printed_page_candidates(origin)
        printed_pages = assemble_printed_pages(candidates)
        if max_printed_page(printed_pages) is None:
            raise ValueError(
                f"{source_id}: a camada de texto nao tem uma serie estavel de paginas impressas."
            )
        start_file = max(
            1,
            int(
                content_start_file
                if content_start_file is not None
                else src.get("content_start_file_page") or 1
            ),
        )
        start_book = max(
            1,
            int(
                content_start_book
                if content_start_book is not None
                else src.get("content_start_book_page") or 1
            ),
        )
    else:
        if content_start_file is None:
            raise ValueError("Informe content_start_file ou --from-pdf.")
        start_file = max(1, int(content_start_file))
        start_book = max(1, int(content_start_book or 1))

    paging = ContentPaging(
        start_file,
        start_book,
        "confirmed",
        tuple(printed_pages) if printed_pages else None,
    )

    total_pages_file = src.get("total_pages_file")
    read_max = max_printed_page(printed_pages)
    if read_max is not None:
        total_pages_book = read_max
    elif total_pages_file is not None:
        total_pages_book = max(1, int(total_pages_file) - start_file + start_book)
    else:
        total_pages_book = None

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
    if printed_pages is not None:
        db.save_printed_pages(source_id, printed_pages)
    else:
        db.delete_printed_pages(source_id)
    src = db.get_source(source_id) or src

    stats = {
        "updated": 0,
        "dropped_pending": 0,
        "dropped_other": 0,
        "notes_patched": 0,
        "permanent_patched": 0,
    }

    drop_ids: list[str] = []
    renamed: list[tuple[str, str]] = []
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

        page_book = compute_page_in_book(page_file, start_file, start_book, printed_pages)
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
        old_stem = path.stem
        if new_path != path:
            try:
                new_path.parent.mkdir(parents=True, exist_ok=True)
                path.replace(new_path)
                path = new_path
            except OSError:
                continue
            db.update_chunk_review(chunk["chunk_id"], literature_note_path=str(path))
            renamed.append((old_stem, path.stem))
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

    stats["permanent_patched"] = _retarget_permanent_notes(db, source_id, renamed)
    _refresh_literature_index(cfg, db, source_id)
    logger.info(
        "set-paging %s: updated=%d dropped_pending=%d dropped_other=%d "
        "notes=%d permanent=%d remaining=%d",
        source_id,
        stats["updated"],
        stats["dropped_pending"],
        stats["dropped_other"],
        stats["notes_patched"],
        stats["permanent_patched"],
        remaining,
    )
    return stats


_PAGE_LINE = re.compile(r"^(- Página:\s*)\d+", re.MULTILINE)
_LOCATOR_LINE = re.compile(r"^(- Localizador:\s*)p\.\d+", re.MULTILINE)


def _retarget_permanent_notes(
    db: StateDB,
    source_id: str,
    renamed: list[tuple[str, str]],
) -> int:
    """Point ZTL citations at the new printed page without touching their hashes."""
    changed = 0
    source = db.get_source(source_id)
    for note in db.get_notes_for_source(source_id):
        if not _rewrite_permanent_note(db, source, note, renamed):
            continue
        changed += 1
    return changed


def _rewrite_permanent_note(
    db: StateDB,
    source: dict | None,
    note: dict,
    renamed: list[tuple[str, str]],
) -> bool:
    path = Path(note["path"]) if note.get("path") else None
    if path is not None and path.is_file():
        try:
            meta, body = parse_frontmatter(path.read_text(encoding="utf-8"))
        except OSError:
            return False
    else:
        try:
            meta = json.loads(note.get("frontmatter_json") or "{}")
        except json.JSONDecodeError:
            meta = {}
        body = note.get("body") or ""
        path = None
    if not isinstance(meta, dict):
        meta = {}

    provenance = load_provenance(note)
    anchor = str(provenance.get("anchor_quote") or "")
    chunk_id = meta.get("chunk_id")
    chunk = db.get_chunk(chunk_id) if chunk_id else None
    citation = resolve_citation(source, chunk, anchor)
    old_page = meta.get("page")
    new_body = body
    for old_stem, new_stem in renamed:
        if old_stem and old_stem != new_stem:
            new_body = new_body.replace(old_stem, new_stem)
    if citation.page is not None:
        new_body = _PAGE_LINE.sub(rf"\g<1>{citation.page}", new_body, count=1)
        new_body = _LOCATOR_LINE.sub(rf"\g<1>p.{citation.page}", new_body, count=1)
        if isinstance(old_page, int) and old_page != citation.page:
            new_body = new_body.replace(f"|p. {old_page}", f"|p. {citation.page}")
        meta["page"] = citation.page
        meta["citation_page_confidence"] = citation.page_confidence
    if citation.cite and anchor:
        evidence = render_evidence_block(citation.cite, anchor)
        if evidence:
            new_body = upsert_managed_block(new_body, "auto-evidence", evidence)
            provenance["citation"] = citation.cite
    if new_body == body and meta.get("page") == old_page:
        return False

    if path is not None:
        safe_write_note(path, meta, new_body)
        new_body = parse_frontmatter(path.read_text(encoding="utf-8"))[1]
    db.update_note_citation_text(
        note["note_id"],
        body=new_body,
        frontmatter_json=frontmatter_json(meta),
        provenance_json=json.dumps(provenance, ensure_ascii=False),
    )
    return True
