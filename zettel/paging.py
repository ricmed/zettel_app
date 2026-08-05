"""Page inference: file page vs book page, content-start paging, interpolation.

Three layers for ``page_in_file``:
1. Explicit metadata (PyMuPDF page map) — preferred; uses the **first** page
   of the chunk when content spans multiple pages
2. Regex on chunk head/tail (fallback only when no page map match)
3. Interpolation between neighbouring explicit pages

Book pages use content-start bounds:
  ``page_in_book = page_in_file - content_start_file_page + content_start_book_page``
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Sequence

from zettel.config import AppConfig

logger = logging.getLogger(__name__)

PAGE_PATTERNS = [
    re.compile(r"^\s*(\d{1,4})\s*$", re.MULTILINE),
    re.compile(r"\n\s*(\d{1,4})\s*\n"),
    re.compile(r"(?i)P[aá]gina\s+(\d+)"),
    re.compile(r"^\s*(\d{1,4})\s+\w", re.MULTILINE),
]

CHAPTER_START_PATTERNS = [
    re.compile(r"(?i)^\s*#+\s*cap[ií]tulo\s+1\b", re.MULTILINE),
    re.compile(r"(?i)^\s*#+\s*chapter\s+1\b", re.MULTILINE),
    re.compile(r"(?i)^\s*#+\s*1[\.\)]\s+\w", re.MULTILINE),
    re.compile(r"(?i)^\s*#+\s*introduction\b", re.MULTILINE),
    re.compile(r"(?i)^\s*#+\s*introdu[cç][aã]o\b", re.MULTILINE),
    # Plain-text page_map (no markdown headings)
    re.compile(r"(?i)^\s*cap[ií]tulo\s+1\b", re.MULTILINE),
    re.compile(r"(?i)^\s*chapter\s+1\b", re.MULTILINE),
    re.compile(r"(?i)^\s*introduction\b", re.MULTILINE),
    re.compile(r"(?i)^\s*introdu[cç][aã]o\b", re.MULTILINE),
]


@dataclass
class PageHint:
    page_in_file: int | None
    confidence: str  # explicit | inferred | unknown


@dataclass
class ContentPaging:
    """Where processing starts in the PDF and how printed numbers map."""

    content_start_file_page: int = 1
    content_start_book_page: int = 1
    confidence: str = "skipped"  # confirmed | heuristic | skipped

    @property
    def page_offset(self) -> int:
        """Derived legacy offset: file - book when book numbering starts at start_book."""
        return int(self.content_start_file_page) - int(self.content_start_book_page)


def extract_page_hint(
    chunk_text: str,
    page_from_meta: int | None = None,
    *,
    allow_regex: bool = True,
) -> PageHint:
    """Resolve page_in_file for a chunk: metadata first, then optional regex.

    When a page map is available, pass ``allow_regex=False`` so stray numbers in
    the body (TOC entries, "2 EPILOGUE", etc.) are not treated as file pages.
    """
    if page_from_meta is not None and page_from_meta > 0:
        return PageHint(page_in_file=int(page_from_meta), confidence="explicit")

    if not allow_regex:
        return PageHint(page_in_file=None, confidence="unknown")

    head, tail = chunk_text[:200], chunk_text[-200:]
    for pattern in PAGE_PATTERNS:
        for region in (head, tail):
            match = pattern.search(region)
            if match:
                try:
                    return PageHint(page_in_file=int(match.group(1)), confidence="explicit")
                except (TypeError, ValueError):
                    continue
    return PageHint(page_in_file=None, confidence="unknown")


def infer_missing_page(
    chunk_index: int,
    pages: Sequence[int | None],
) -> int | None:
    """Interpolate page_in_file between nearest explicit neighbours."""
    if chunk_index < 0 or chunk_index >= len(pages):
        return None
    if pages[chunk_index] is not None:
        return pages[chunk_index]

    prev_idx, prev_page = None, None
    for i in range(chunk_index - 1, -1, -1):
        if pages[i] is not None:
            prev_idx, prev_page = i, pages[i]
            break

    next_idx, next_page = None, None
    for i in range(chunk_index + 1, len(pages)):
        if pages[i] is not None:
            next_idx, next_page = i, pages[i]
            break

    if prev_page is not None and next_page is not None and next_idx is not None and prev_idx is not None:
        span = next_idx - prev_idx
        if span <= 0:
            return prev_page
        progress = (chunk_index - prev_idx) / span
        return int(round(prev_page + (next_page - prev_page) * progress))

    return prev_page


def apply_page_inference(
    page_hints: list[PageHint],
) -> list[PageHint]:
    """Fill unknown pages via interpolation; mark confidence as inferred."""
    raw = [h.page_in_file for h in page_hints]
    out: list[PageHint] = []
    for i, hint in enumerate(page_hints):
        if hint.page_in_file is not None and hint.confidence == "explicit":
            out.append(hint)
            continue
        estimated = infer_missing_page(i, raw)
        if estimated is not None:
            out.append(PageHint(page_in_file=estimated, confidence="inferred"))
        else:
            out.append(PageHint(page_in_file=None, confidence="unknown"))
    return out


def compute_page_in_book(
    page_in_file: int | None,
    content_start_file_page: int | None,
    content_start_book_page: int | None,
) -> int | None:
    """Map file page to printed book page using content-start bounds.

    ``page_in_book = page_in_file - start_file + start_book``
    Pages before ``start_file`` return ``None`` (caller should skip them).
    """
    if page_in_file is None:
        return None
    start_file = int(content_start_file_page or 1)
    start_book = int(content_start_book_page or 1)
    if page_in_file < start_file:
        return None
    return page_in_file - start_file + start_book


def apply_page_offset(
    page_in_file: int | None,
    page_offset: int | None,
) -> int | None:
    """Legacy helper: book = file - offset (kept for older callers/tests)."""
    if page_in_file is None:
        return None
    offset = page_offset or 0
    # Equivalent to start_file=offset+1, start_book=1 when offset >= 0
    return compute_page_in_book(page_in_file, offset + 1, 1)


def suggest_content_start(
    page_map: Sequence[tuple[int, str]],
) -> dict[str, Any]:
    """Heuristic: first page_map entry matching Capítulo 1 / Introduction."""
    for page_no, text in page_map:
        head = (text or "")[:1200]
        for pattern in CHAPTER_START_PATTERNS:
            if pattern.search(head):
                return {
                    "content_start_file_page": int(page_no),
                    "content_start_book_page": 1,
                    "confidence": "heuristic",
                    "needs_confirmation": True,
                    "anchor_page_in_file": int(page_no),
                }
    return {
        "content_start_file_page": 1,
        "content_start_book_page": 1,
        "confidence": "none",
        "needs_confirmation": True,
        "anchor_page_in_file": None,
    }


def detect_page_offset(
    chunk_texts: Sequence[str],
    page_in_files: Sequence[int | None],
) -> dict[str, Any]:
    """Legacy heuristic via chunks; prefer ``suggest_content_start(page_map)``."""
    for i, text in enumerate(chunk_texts):
        head = text[:800]
        for pattern in CHAPTER_START_PATTERNS:
            if pattern.search(head):
                file_page = page_in_files[i] if i < len(page_in_files) else None
                if file_page is not None and file_page >= 1:
                    offset = file_page - 1
                    return {
                        "offset": offset,
                        "confidence": "heuristic",
                        "needs_confirmation": True,
                        "anchor_chunk_index": i,
                        "anchor_page_in_file": file_page,
                    }
    return {
        "offset": 0,
        "confidence": "none",
        "needs_confirmation": True,
        "anchor_chunk_index": None,
        "anchor_page_in_file": None,
    }


def build_page_map_from_texts(page_texts: Sequence[str]) -> list[tuple[int, str]]:
    """Build [(page_no_1based, text), ...] from per-page extraction (PyMuPDF)."""
    return [(i + 1, t) for i, t in enumerate(page_texts)]


def lookup_page_for_chunk(
    chunk_text: str,
    page_map: Sequence[tuple[int, str]],
    min_overlap: int = 40,
) -> int | None:
    """Return the **first** file page that matches the chunk start (not a span).

    Uses only the beginning of the chunk (~120 chars) so a chunk that crosses a
    page boundary is attributed to the page where it begins.
    """
    needle = re.sub(r"\s+", " ", chunk_text[:200]).strip()
    if len(needle) < 20 or not page_map:
        return None
    best_page, best_score = None, 0
    probe = needle[:120]
    for page_no, page_text in page_map:
        normalized = re.sub(r"\s+", " ", page_text)
        if probe in normalized:
            return page_no
        # Fallback: count shared words in the first window
        words = set(probe.lower().split())
        page_words = set(normalized[:2000].lower().split())
        score = len(words & page_words)
        if score > best_score and score >= max(3, min_overlap // 10):
            best_score, best_page = score, page_no
    return best_page


def compute_docling_config_hash(cfg: AppConfig) -> str:
    """Stable hash of ingestion knobs that invalidate mid-flight resume."""
    payload = {
        "pdf_extractor": cfg.pdf_extractor,
        "chunk_size": cfg.chunking.chunk_size,
        "chunk_overlap": cfg.chunking.chunk_overlap,
        "min_section_chars": cfg.chunking.min_section_chars,
        "images_enabled": cfg.images.enabled,
        "images_scale": cfg.images.scale,
        "images_min_width": cfg.images.min_width,
        "images_min_height": cfg.images.min_height,
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def format_source_locator(
    page_in_book: int | None,
    section_path: str = "",
    page_in_file: int | None = None,
) -> str:
    """Build a human-readable locator for candidates / ZTL frontmatter."""
    parts: list[str] = []
    if page_in_book is not None:
        parts.append(f"p.{page_in_book}")
    elif page_in_file is not None:
        parts.append(f"p.arquivo.{page_in_file}")
    if section_path:
        parts.append(section_path)
    return " / ".join(parts) if parts else ""
