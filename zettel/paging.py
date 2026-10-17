"""Page inference: file page vs book page, content-start paging, interpolation.

Three layers for ``page_in_file``:
1. Docling page map, located by character offset (bisect) — preferred; uses
   the **first** page of the chunk when content spans multiple pages.
   Confidence is ``exact`` when that offset's probe is unique, ``fuzzy`` when
   the same probe sits on more than one page or only a word-overlap remains.
2. Regex on chunk head/tail (fallback only when no page map exists; never for
   native Markdown sources, which have no pages) — confidence ``fuzzy``
3. Interpolation between neighbouring anchored pages — confidence ``inferred``

Book/journal pages use content-start bounds:
  ``page_in_book = page_in_file - content_start_file_page + content_start_book_page``

Docling ``prov.page_no`` is the PDF *file* index (1-based), not the printed
number. The persisted map is one numbered marker per file page, including
pages with no exportable text (``<!-- zettel:page N -->``). A positional
split of an unnumbered break is not a page map: empty pages used to slide
every later chunk. Printed numbers (book p.1 after front matter, journal
article starting at p.200) come from the content-start offset, seeded by
HITL (``--content-start-*``) or a bibliographic page range (``pages:``
frontmatter), or from the PDF ``/PageLabels`` when the file defines them
(pypdfium2, not a header/footer scan — ADR-012). There is still no automated
header/footer OCR.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zettel.config import AppConfig
from zettel.hashing import fold_for_match
from zettel.search_terms import PT_STOPWORDS

logger = logging.getLogger(__name__)

# Numbered file-page marker persisted in ``extracted_text``. The number is
# Docling's ``prov.page_no`` (1-based PDF index), not the position of the
# marker and not the printed page. Empty pages still get a marker so a later
# page cannot slide left.
PAGE_BREAK_MARKER = "<!-- zettel:page-break -->"
PAGE_NUMBERED_RE = re.compile(r"<!-- zettel:page (\d+) -->")
PAGE_BREAK_RE = re.compile(r"\n*(?:<!-- zettel:page-break -->|<!-- zettel:page \d+ -->)\n*")

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
    confidence: str  # exact | fuzzy | inferred | unknown


@dataclass
class ContentPaging:
    """Where processing starts in the PDF and how printed numbers map."""

    content_start_file_page: int = 1
    content_start_book_page: int = 1
    confidence: str = "skipped"  # labels | confirmed | heuristic | skipped

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
        return PageHint(page_in_file=int(page_from_meta), confidence="exact")

    if not allow_regex:
        return PageHint(page_in_file=None, confidence="unknown")

    head, tail = chunk_text[:200], chunk_text[-200:]
    for pattern in PAGE_PATTERNS:
        for region in (head, tail):
            match = pattern.search(region)
            if match:
                try:
                    # A digit in the body is a guess, not a unique map hit.
                    return PageHint(page_in_file=int(match.group(1)), confidence="fuzzy")
                except (TypeError, ValueError):
                    continue
    return PageHint(page_in_file=None, confidence="unknown")


def infer_missing_page(
    chunk_index: int,
    pages: Sequence[int | None],
) -> int | None:
    """Interpolate page_in_file between nearest anchored neighbours."""
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
        if hint.page_in_file is not None and hint.confidence in {"exact", "fuzzy"}:
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


def page_marker(page_no: int) -> str:
    """HTML comment that names one PDF file page inside ``extracted_text``."""
    return f"<!-- zettel:page {int(page_no)} -->"


def strip_page_break_markers(text: str) -> str:
    """Remove page-map comments so they never become chunk text."""
    if not text or "<!-- zettel:page" not in text:
        return text
    cleaned = PAGE_BREAK_RE.sub("\n\n", text)
    return cleaned.strip()


def render_page_marked_markdown(page_map: Sequence[tuple[int, str]]) -> str:
    """Serialize a file-page map with one numbered marker per page.

    Empty pages are markers with no body. The marker number, not its position,
    is the page. Sources harvested with the unnumbered ``PAGE_BREAK_MARKER``
    are not rewritten here — they need a re-harvest.
    """
    blocks: list[str] = []
    for page_no, body in page_map:
        text = (body or "").strip()
        marker = page_marker(page_no)
        blocks.append(f"{marker}\n\n{text}" if text else marker)
    return "\n\n".join(blocks).strip()


def page_map_from_marked_markdown(text: str) -> list[tuple[int, str]]:
    """Rebuild a file-page map from numbered ``<!-- zettel:page N -->`` markers.

    The page number is the marker's ``N``. A gap is left missing (not filled
    by shifting later pages). Duplicate numbers are an error and yield ``[]``.
    Unnumbered ``<!-- zettel:page-break -->`` markers are not a map: that
    format dropped empty pages. Marker-less input returns ``[]`` so callers
    fall back to the regex/interpolation layers in this module.
    """
    if not text:
        return []
    matches = list(PAGE_NUMBERED_RE.finditer(text))
    if not matches:
        if PAGE_BREAK_MARKER in text:
            logger.error(
                "Marcador de pagina sem numero (%s) ignorado; o mapa posicional "
                "desloca paginas vazias. Re-harvest necessario.",
                PAGE_BREAK_MARKER,
            )
        return []
    spans: dict[int, str] = {}
    for i, match in enumerate(matches):
        page_no = int(match.group(1))
        if page_no in spans:
            logger.error(
                "Marcador de pagina %d repetido; mapa descartado para nao rotular pagina exata.",
                page_no,
            )
            return []
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        spans[page_no] = text[start:end].strip()
    if not spans or min(spans) < 1:
        return []
    return [(page_no, spans[page_no]) for page_no in sorted(spans)]


def page_map_is_complete(
    page_map: Sequence[tuple[int, str]],
    total_pages: int | None,
) -> bool:
    """True when the map is exactly file pages ``1..total_pages``, empties included."""
    if not total_pages or int(total_pages) < 1 or not page_map:
        return False
    return [page_no for page_no, _ in page_map] == list(range(1, int(total_pages) + 1))


_QUOTE_EDGE_WORDS = 5
_QUOTE_PAGE_WINDOW = 4


def locate_quote_pages(
    extracted_text: str | None,
    quote: str,
    first_page_in_file: int | None,
) -> tuple[int, int] | None:
    """File pages (first, last) on which ``quote`` sits, or None when unknown.

    A chunk's ``page_in_file`` is the **first** page it touches, so a quote in a
    chunk that crosses a page break may sit on the next one. The search runs over
    the Docling page map in ``extracted_text`` (the only surface that still carries
    the markers), from the chunk's page through the next few. The quote's head and
    tail are located separately, which both yields a range when the quote crosses
    a break and tolerates an editorial ellipsis in its middle.

    None when there is no page map (native Markdown, marker-less export) or the
    quote cannot be found — the caller falls back to the chunk's page.
    """
    words = fold_for_match(quote).split()
    if not words or first_page_in_file is None:
        return None
    page_map = page_map_from_marked_markdown(extracted_text or "")
    if not page_map:
        return None
    window = [
        (page, f" {fold_for_match(text)} ")
        for page, text in page_map
        if first_page_in_file <= page < first_page_in_file + _QUOTE_PAGE_WINDOW
    ]
    head = f" {' '.join(words[:_QUOTE_EDGE_WORDS])} "
    tail = f" {' '.join(words[-_QUOTE_EDGE_WORDS:])} "
    first = next((page for page, text in window if head in text), None)
    if first is None:
        return None
    last = next((page for page, text in window if page >= first and tail in text), None)
    if last is None:
        return None
    return first, last


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


_ARABIC_LABEL = re.compile(r"\d{1,6}")


def page_labels_are_custom(labels: Sequence[str]) -> bool:
    """True when the labels are not pdfium's default of ``1..N``.

    ``get_page_label`` returns ``\"1\"``, ``\"2\"``, … even when the PDF has no
    ``/PageLabels``. That default is the file index, not a printed number, so
    it must not override the chapter heuristic.
    """
    if not labels:
        return False
    return any((label or "").strip() != str(index) for index, label in enumerate(labels, start=1))


def content_start_from_page_labels(labels: Sequence[str]) -> dict[str, Any] | None:
    """File page and printed number from numeric ``/PageLabels``.

    The first arabic ``1`` is the start of the body after roman front matter.
    When no label is ``1``, the first arabic label is the printed start (a
    journal article whose PDF starts at page 200).
    """
    if not page_labels_are_custom(labels):
        return None
    arabic: list[tuple[int, int]] = []
    for index, label in enumerate(labels, start=1):
        text = (label or "").strip()
        if _ARABIC_LABEL.fullmatch(text):
            arabic.append((index, int(text)))
    if not arabic:
        return None
    for file_page, number in arabic:
        if number == 1:
            chosen = (file_page, 1)
            break
    else:
        chosen = arabic[0]
    file_page, book_page = chosen
    return {
        "content_start_file_page": file_page,
        "content_start_book_page": book_page,
        "confidence": "labels",
        "needs_confirmation": False,
        "anchor_page_in_file": file_page,
    }


def read_pdf_page_labels(file_path: Path) -> list[str]:
    """One label per PDF page. ``[]`` when pdfium cannot open the file."""
    try:
        import pypdfium2 as pdfium
    except ImportError:
        logger.debug("pdfium indisponivel; rotulos de pagina ignorados.")
        return []
    try:
        document = pdfium.PdfDocument(str(file_path))
    except Exception as exc:
        logger.debug("pdfium nao abriu %s para rotulos (%s).", file_path.name, exc)
        return []
    try:
        return [str(document.get_page_label(index) or "") for index in range(len(document))]
    finally:
        document.close()


def suggested_page_excerpt(page_map: Sequence[tuple[int, str]], page_no: int | None) -> str:
    """First non-empty line of the file page the suggestion points at."""
    if page_no is None:
        return ""
    for number, text in page_map:
        if int(number) != int(page_no):
            continue
        for line in (text or "").splitlines():
            stripped = line.strip().lstrip("#").strip()
            if stripped:
                return stripped[:200]
    return ""


def suggest_content_start(
    page_map: Sequence[tuple[int, str]],
    *,
    biblio_pages: str | None = None,
    page_labels: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Where content starts: PDF page labels, then the chapter/journal heuristic.

    * Labels: numeric ``/PageLabels`` (roman front matter, or an article that
      starts at printed page 200). pdfium's default ``1..N`` is not a label.
    * Book: first non-TOC page matching Capítulo 1 / Introduction; printed
      page number is 1. There is no header/footer OCR (ADR-012).
    * Journal article: no chapter marker, bibliographic ``pages: 200-210``.
    * Handout / article starting at 1: file 1 = printed 1, confidence none.
    """
    labeled = content_start_from_page_labels(page_labels or [])
    if labeled is not None:
        return labeled

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
    page_labels: Sequence[str] | None = None,
) -> ContentPaging:
    """Resolve content-start file/book pages before chunking.

    Precedence: explicit CLI/web flags > ``--skip-paging`` > interactive HITL >
    non-interactive heuristic (chapter-1 / biblio range) > file page 1 =
    printed page 1.
    """
    if skip_paging and content_start_file is None:
        return ContentPaging(1, 1, "skipped")

    suggested = suggest_content_start(page_map, biblio_pages=biblio_pages, page_labels=page_labels)
    sug_file = int(suggested.get("content_start_file_page") or 1)
    sug_book = int(suggested.get("content_start_book_page") or 1)

    if content_start_file is not None:
        start_file = int(content_start_file)
        start_book = int(content_start_book) if content_start_book is not None else sug_book
        return ContentPaging(start_file, start_book, "confirmed")

    if not interactive:
        if suggested.get("confidence") in {"labels", "heuristic"}:
            logger.info(
                "Paginacao %s (nao-interativo): arquivo p.%d = impressa p.%d",
                suggested.get("confidence"),
                sug_file,
                sug_book,
            )
            return ContentPaging(sug_file, sug_book, str(suggested.get("confidence")))
        return ContentPaging(1, 1, "skipped")

    from rich.console import Console
    from rich.prompt import Prompt

    console = Console(stderr=True)
    anchor = suggested.get("anchor_page_in_file")
    excerpt = suggested_page_excerpt(page_map, anchor if isinstance(anchor, int) else None)
    if anchor is not None:
        console.print(
            f"[cyan]Detectei inicio de conteudo na pagina {anchor} do arquivo. "
            f"Sugestao: arquivo p.{sug_file} = impressa p.{sug_book}.[/cyan]"
        )
        if excerpt:
            console.print(f"[dim]Trecho nessa pagina: {excerpt}[/dim]")
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


_PROBE_CHARS = 120
_MIN_PROBE_CHARS = 20
_FUZZY_MIN_FRACTION = 0.5
_FUZZY_MIN_HITS = 4
_FUZZY_NEIGHBORS = 2


def _normalize_for_page_lookup(text: str) -> str:
    """Collapse whitespace and heading hashes so Docling MD matches page slices."""
    stripped = strip_page_break_markers(text or "")
    stripped = re.sub(r"^#{1,6}\s+", "", stripped, flags=re.MULTILINE)
    return re.sub(r"\s+", " ", stripped).strip()


def _content_tokens(text: str) -> set[str]:
    """Words that can identify a page. Stopwords and one-letter tokens cannot."""
    return {tok for tok in text.lower().split() if len(tok) >= 3 and tok not in PT_STOPWORDS}


class PageIndex:
    """One normalization of a document's pages, plus offsets for bisect.

    Built once per harvest. Each chunk is then a ``str.find`` from the previous
    hit and a ``bisect`` onto the page starts, so a long book stays linear in
    the text instead of quadratic in pages times chunks.
    """

    def __init__(
        self,
        page_nos: list[int],
        starts: list[int],
        document: str,
        token_sets: list[set[str]],
    ) -> None:
        self.page_nos = page_nos
        self.starts = starts
        self.document = document
        self.token_sets = token_sets

    @classmethod
    def build(cls, page_map: Sequence[tuple[int, str]]) -> PageIndex:
        page_nos: list[int] = []
        starts: list[int] = []
        token_sets: list[set[str]] = []
        parts: list[str] = []
        cursor = 0
        have_text = False
        for page_no, raw in page_map:
            text = _normalize_for_page_lookup(raw)
            page_nos.append(int(page_no))
            starts.append(cursor)
            token_sets.append(_content_tokens(text))
            if not text:
                continue
            if have_text:
                parts.append(" ")
                cursor += 1
                starts[-1] = cursor
            parts.append(text)
            cursor += len(text)
            have_text = True
        return cls(page_nos, starts, "".join(parts), token_sets)

    def page_at(self, offset: int) -> int | None:
        if not self.starts:
            return None
        index = bisect.bisect_right(self.starts, offset) - 1
        if index < 0:
            return None
        return self.page_nos[index]

    def _spans_other_page(self, probe: str, pos: int) -> bool:
        """True when the same probe also occurs on a different file page.

        Repeating inside one page (a sentence copied down the page) is still
        one page. A running header that shows up on two pages is not ``exact``.
        """
        home = self.page_at(pos)
        cursor = 0
        while True:
            found = self.document.find(probe, cursor)
            if found < 0:
                return False
            if found != pos and self.page_at(found) != home:
                return True
            cursor = found + 1

    def _fuzzy_page(self, probe: str, around: int | None) -> int | None:
        tokens = _content_tokens(probe)
        if len(tokens) < _FUZZY_MIN_HITS:
            return None
        if around is None:
            indexes = range(len(self.page_nos))
        else:
            indexes = range(
                max(0, around - _FUZZY_NEIGHBORS),
                min(len(self.page_nos), around + _FUZZY_NEIGHBORS + 1),
            )
        best_i: int | None = None
        best_hits = 0
        for i in indexes:
            hits = len(tokens & self.token_sets[i])
            if hits > best_hits:
                best_hits = hits
                best_i = i
        if best_i is None:
            return None
        if best_hits < _FUZZY_MIN_HITS or best_hits / len(tokens) < _FUZZY_MIN_FRACTION:
            if around is not None:
                return self._fuzzy_page(probe, None)
            return None
        return self.page_nos[best_i]

    def locate(self, chunk_text: str, search_from: int = 0) -> tuple[PageHint, int]:
        """Page where the chunk starts, and the cursor for the next chunk.

        The probe is the first 120 normalized characters. A hit in the joined
        document is ``exact`` only when that probe occurs once. A probe that
        crosses a page boundary still matches, and ``page_at`` reports the page
        that holds its first character. Word overlap is the fallback, and it
        never earns ``exact``.
        """
        needle = _normalize_for_page_lookup(chunk_text[:400])
        if len(needle) < _MIN_PROBE_CHARS or not self.document:
            return PageHint(page_in_file=None, confidence="unknown"), search_from
        probe = needle[:_PROBE_CHARS]
        pos = self.document.find(probe, max(0, search_from))
        if pos < 0 and search_from > 0:
            pos = self.document.find(probe)
        if pos >= 0:
            page = self.page_at(pos)
            confidence = "fuzzy" if self._spans_other_page(probe, pos) else "exact"
            return PageHint(page_in_file=page, confidence=confidence), pos + 1
        around = None
        if search_from > 0:
            anchor = self.page_at(search_from)
            if anchor is not None and anchor in self.page_nos:
                around = self.page_nos.index(anchor)
        page = self._fuzzy_page(probe, around)
        if page is None:
            return PageHint(page_in_file=None, confidence="unknown"), search_from
        return PageHint(page_in_file=page, confidence="fuzzy"), search_from


def lookup_page_for_chunk(
    chunk_text: str,
    page_map: Sequence[tuple[int, str]],
    min_overlap: int = 40,
    *,
    index: PageIndex | None = None,
    search_from: int = 0,
) -> PageHint:
    """Return the file page where the chunk starts, with an honest confidence.

    ``min_overlap`` is retained so older callers keep working; the fuzzy gate
    is a fraction of the probe's content words, not a raw count of four tokens.
    Pass ``index`` (and thread ``search_from``) when locating many chunks of
    one document.
    """
    del min_overlap
    if not page_map and index is None:
        return PageHint(page_in_file=None, confidence="unknown")
    built = index if index is not None else PageIndex.build(page_map)
    hint, _cursor = built.locate(chunk_text, search_from)
    return hint


def _docling_versions() -> dict[str, str]:
    """Installed Docling versions. An upgrade changes the Markdown, so it belongs in the hash."""
    versions: dict[str, str] = {}
    for name in ("docling", "docling_core"):
        try:
            module = __import__(name)
        except ImportError:
            versions[name] = "missing"
            continue
        versions[name] = str(getattr(module, "__version__", "unknown"))
    return versions


def compute_docling_config_hash(cfg: AppConfig) -> str:
    """Stable hash of ingestion knobs that invalidate mid-flight resume.

    Includes the installed Docling versions: a library upgrade is what most
    changes the extracted Markdown, and every existing source then warns on
    the next harvest (rechunk or re-harvest), rather than mixing two dialects.
    """
    versions = _docling_versions()
    payload = {
        "docling_version": versions["docling"],
        "docling_core_version": versions["docling_core"],
        "chunk_size": cfg.chunking.chunk_size,
        "chunk_overlap": cfg.chunking.chunk_overlap,
        "min_section_chars": cfg.chunking.min_section_chars,
        "min_chunk_chars": cfg.chunking.min_chunk_chars,
        "fence_section_slack": cfg.chunking.fence_section_slack,
        "images_enabled": cfg.images.enabled,
        "images_scale": cfg.images.scale,
        "images_min_width": cfg.images.min_width,
        "images_min_height": cfg.images.min_height,
        # Both change the text Docling emits, so a resume across a flip would mix pages
        # converted with and without the decoded formulas or code in one source.
        "formulas_enabled": cfg.formulas.enabled,
        "code_enabled": cfg.code.enabled,
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
