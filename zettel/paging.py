"""Page inference: file page vs book page, content-start paging, interpolation.

Three layers for ``page_in_file``:
1. Explicit metadata (Docling page-break map) — preferred; uses the **first**
   page of the chunk when content spans multiple pages
2. Regex on chunk head/tail (fallback only when no page map exists; never for
   native Markdown sources, which have no pages)
3. Interpolation between neighbouring explicit pages

Book/journal pages use content-start bounds:
  ``page_in_book = page_in_file - content_start_file_page + content_start_book_page``

Docling ``prov.page_no`` and page-break markers are the PDF *file* index
(1-based), not the printed number. Printed numbers (book p.1 after front
matter, journal article starting at p.200) come from the content-start offset,
seeded by HITL (``--content-start-*``) or a bibliographic page range (``pages:``
frontmatter) — there is no automated header/footer scan (ADR-012, no PyMuPDF).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from zettel.config import AppConfig

logger = logging.getLogger(__name__)

# Inserted by Docling export_to_markdown(page_break_placeholder=...). Sequential
# splits are 1-based PDF file pages — the same index as ProvenanceItem.page_no.
PAGE_BREAK_MARKER = "<!-- zettel:page-break -->"
PAGE_BREAK_RE = re.compile(r"\n*" + re.escape(PAGE_BREAK_MARKER) + r"\n*")

PAGE_PATTERNS = [
    re.compile(r"^\s*(\d{1,4})\s*$", re.MULTILINE),
    re.compile(r"\n\s*(\d{1,4})\s*\n"),
    re.compile(r"(?i)P[aá]gina\s+(\d+)"),
    re.compile(r"^\s*(\d{1,4})\s+\w", re.MULTILINE),
]

# Prefer markdown headings (Docling page map) so TOC list entries are ignored.
MARKDOWN_CHAPTER_START_PATTERNS = [
    re.compile(r"(?i)^\s*#+\s*cap[ií]tulo\s+1\b", re.MULTILINE),
    re.compile(r"(?i)^\s*#+\s*chapter\s+1\b", re.MULTILINE),
    re.compile(r"(?i)^\s*#+\s*1[\.\)]\s+\w", re.MULTILINE),
    re.compile(r"(?i)^\s*#+\s*introduction\b", re.MULTILINE),
    re.compile(r"(?i)^\s*#+\s*introdu[cç][aã]o\b", re.MULTILINE),
]

PLAIN_CHAPTER_START_PATTERNS = [
    re.compile(r"(?i)^\s*cap[ií]tulo\s+1\b", re.MULTILINE),
    re.compile(r"(?i)^\s*chapter\s+1\b", re.MULTILINE),
    re.compile(r"(?i)^\s*introduction\b", re.MULTILINE),
    re.compile(r"(?i)^\s*introdu[cç][aã]o\b", re.MULTILINE),
]

CHAPTER_START_PATTERNS = MARKDOWN_CHAPTER_START_PATTERNS + PLAIN_CHAPTER_START_PATTERNS

_BIBLIO_PAGE_RANGE = re.compile(r"(?<!\d)(\d{1,4})\s*[-\u2013\u2014]\s*(\d{1,4})(?!\d)")


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
        """Derived offset: file - book when book numbering starts at start_book."""
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

    if (
        prev_page is not None
        and next_page is not None
        and next_idx is not None
        and prev_idx is not None
    ):
        span = next_idx - prev_idx
        if span <= 0:
            return prev_page
        progress = (chunk_index - prev_idx) / span
        return round(prev_page + (next_page - prev_page) * progress)

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


def strip_page_break_markers(text: str) -> str:
    """Remove Docling page-break comments inserted during harvest."""
    if not text or PAGE_BREAK_MARKER not in text:
        return text
    cleaned = PAGE_BREAK_RE.sub("\n\n", text)
    return cleaned.strip()


def page_map_from_marked_markdown(text: str) -> list[tuple[int, str]]:
    """Split markdown that contains ``PAGE_BREAK_MARKER`` into a file-page map.

    Empty (marker-less) input returns ``[]`` so callers fall back to the
    regex/interpolation layers in this module.
    """
    if not text or PAGE_BREAK_MARKER not in text:
        return []
    parts = text.split(PAGE_BREAK_MARKER)
    return [(i + 1, part.strip()) for i, part in enumerate(parts)]


def parse_biblio_start_page(pages: str | None) -> int | None:
    """First page of a bibliographic range (``200-210``), or None.

    A lone number (``320`` / ``320 p.``) is treated as *total* length of a
    book, not a starting page — using it as ``content_start_book_page`` would
    silently shift every citation.
    """
    if not pages or not str(pages).strip():
        return None
    match = _BIBLIO_PAGE_RANGE.search(str(pages))
    if not match:
        return None
    start = int(match.group(1))
    return start if start >= 1 else None


def _looks_like_toc(head: str) -> bool:
    """True when the page looks like a table of contents, not chapter 1 itself."""
    lines = [ln.strip() for ln in (head or "").splitlines() if ln.strip()]
    if len(lines) < 5:
        return False
    leaders = sum(1 for ln in lines if "...." in ln or "……" in ln or ". . ." in ln)
    trailing_num = sum(1 for ln in lines if re.search(r"\d+\s*$", ln) and len(ln) < 80)
    return leaders >= 3 or trailing_num >= 6


def _page_has_chapter_start(head: str, patterns: Sequence[re.Pattern[str]]) -> bool:
    return any(p.search(head) for p in patterns)


def suggest_content_start(
    page_map: Sequence[tuple[int, str]],
    *,
    biblio_pages: str | None = None,
) -> dict[str, Any]:
    """Heuristic content-start: book front matter vs journal vs 1=1 document.

    * Book: first non-TOC page matching Capítulo 1 / Introduction; printed
      page number is always 1 (no header/footer OCR -- ADR-012 ruled that out
      along with PyMuPDF).
    * Journal article: no chapter marker, bibliographic ``pages: 200-210``.
    * Handout / article starting at 1: file 1 = printed 1, confidence none.
    """
    biblio_start = parse_biblio_start_page(biblio_pages)

    # Markdown headings first (Docling map); skip TOC-style pages.
    for patterns in (MARKDOWN_CHAPTER_START_PATTERNS, PLAIN_CHAPTER_START_PATTERNS):
        for page_no, text in page_map:
            head = (text or "")[:1200]
            if _looks_like_toc(head):
                continue
            if _page_has_chapter_start(head, patterns):
                start_file = int(page_no)
                return {
                    "content_start_file_page": start_file,
                    "content_start_book_page": 1,
                    "confidence": "heuristic",
                    "needs_confirmation": True,
                    "anchor_page_in_file": start_file,
                }

    start_file = 1
    if biblio_start is not None and biblio_start > 1:
        return {
            "content_start_file_page": start_file,
            "content_start_book_page": biblio_start,
            "confidence": "heuristic",
            "needs_confirmation": True,
            "anchor_page_in_file": start_file,
        }
    return {
        "content_start_file_page": 1,
        "content_start_book_page": 1,
        "confidence": "none",
        "needs_confirmation": True,
        "anchor_page_in_file": None,
    }


def resolve_content_paging(
    page_map: Sequence[tuple[int, str]],
    *,
    interactive: bool,
    content_start_file: int | None,
    content_start_book: int | None,
    skip_paging: bool,
    biblio_pages: str | None = None,
) -> ContentPaging:
    """Resolve content-start file/book pages before chunking.

    Precedence: explicit CLI/web flags > ``--skip-paging`` > interactive HITL >
    non-interactive heuristic (chapter-1 / biblio range) > file page 1 =
    printed page 1.
    """
    if skip_paging and content_start_file is None:
        return ContentPaging(1, 1, "skipped")

    suggested = suggest_content_start(page_map, biblio_pages=biblio_pages)
    sug_file = int(suggested.get("content_start_file_page") or 1)
    sug_book = int(suggested.get("content_start_book_page") or 1)

    if content_start_file is not None:
        start_file = int(content_start_file)
        start_book = int(content_start_book) if content_start_book is not None else sug_book
        return ContentPaging(start_file, start_book, "confirmed")

    if not interactive:
        if suggested.get("confidence") == "heuristic":
            logger.info(
                "Paginacao heuristica (nao-interativo): arquivo p.%d = impressa p.%d",
                sug_file,
                sug_book,
            )
            return ContentPaging(sug_file, sug_book, "heuristic")
        return ContentPaging(1, 1, "skipped")

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
            "Padrao: arquivo p.1. Se for artigo de revista, o numero impresso "
            "pode ser 200 mesmo com o PDF comecando em 1.[/cyan]"
        )
    console.print(
        "[dim]Livro: p.arquivo do cap. 1 + numero impresso nessa pagina "
        "(geralmente 1). "
        "Artigo de revista: arquivo p.1 = primeira pagina impressa na revista. "
        "Apostila/tutorial que comeca em 1: aceite os padroes. "
        "Markdown nativo nao tem pagina. "
        "Paginas do arquivo anteriores ao inicio sao ignoradas. "
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


def _normalize_for_page_lookup(text: str) -> str:
    """Collapse whitespace and heading hashes so Docling MD matches page slices."""
    stripped = strip_page_break_markers(text or "")
    stripped = re.sub(r"^#{1,6}\s+", "", stripped, flags=re.MULTILINE)
    return re.sub(r"\s+", " ", stripped).strip()


def lookup_page_for_chunk(
    chunk_text: str,
    page_map: Sequence[tuple[int, str]],
    min_overlap: int = 40,
) -> int | None:
    """Return the **first** file page that matches the chunk start (not a span).

    Uses only the beginning of the chunk (~120 chars) so a chunk that crosses a
    page boundary is attributed to the page where it begins. Both sides are
    normalized (heading hashes stripped, whitespace collapsed) so Docling
    markdown chunks match the Docling per-page map.
    """
    needle = _normalize_for_page_lookup(chunk_text[:400])
    if len(needle) < 20 or not page_map:
        return None
    best_page, best_score = None, 0
    probe = needle[:120]
    for page_no, page_text in page_map:
        normalized = _normalize_for_page_lookup(page_text)
        if probe in normalized:
            return page_no
        words = set(probe.lower().split())
        page_words = set(normalized[:2000].lower().split())
        score = len(words & page_words)
        if score > best_score and score >= max(3, min_overlap // 10):
            best_score, best_page = score, page_no
    return best_page


def compute_docling_config_hash(cfg: AppConfig) -> str:
    """Stable hash of ingestion knobs that invalidate mid-flight resume."""
    payload = {
        "chunk_size": cfg.chunking.chunk_size,
        "chunk_overlap": cfg.chunking.chunk_overlap,
        "min_section_chars": cfg.chunking.min_section_chars,
        "min_chunk_chars": cfg.chunking.min_chunk_chars,
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
