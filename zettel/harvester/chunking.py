"""Chapter splitting and chunk persistence."""

from __future__ import annotations

import logging
import re
from typing import Any

from zettel.config import AppConfig
from zettel.hashing import normalize_text_for_hash, sha256_hex, short_hash
from zettel.index import VectorIndex
from zettel.paging import (
    ContentPaging,
    apply_page_inference,
    compute_page_in_book,
    extract_page_hint,
    lookup_page_for_chunk,
    strip_page_break_markers,
)
from zettel.state import StateDB

logger = logging.getLogger(__name__)


# ── Chapter Splitting ─────────────────────────────────────────────────


def split_into_chapters(text: str, origin_type: str) -> list[dict[str, str]]:
    """Split text into chapters/sections (Level 1 hierarchy)."""
    text = strip_page_break_markers(text or "")
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


def split_chapter_into_sections(
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

    return merge_small_sections(raw, min_section_chars)


def merge_small_sections(
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


def split_chapter_into_chunks(
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
    sections = split_chapter_into_sections(
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


# ── Chunking and Persistence ──────────────────────────────────────────


def chunk_and_persist(
    cfg: AppConfig, db: StateDB, idx: VectorIndex,
    source_id: str, chapters: list[dict[str, str]],
    page_map: list[tuple[int, str]] | None = None,
    paging: ContentPaging | None = None,
    origin_type: str = "",
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
    # Regex on body digits is a last resort for PDFs with no page map. Native
    # Markdown has no pages — never invent them from stray numbers.
    is_markdown = (origin_type or "").lower() in {"md", "markdown", "txt"}
    allow_regex = not page_map and not is_markdown
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

        chunk_pairs = split_chapter_into_chunks(cfg, chapter)
        keep_ids: set[str] = set()
        chapter_specs: list[dict[str, Any]] = []
        for section_path, chunk_text in chunk_pairs:
            chunk_text = strip_page_break_markers(chunk_text)
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
