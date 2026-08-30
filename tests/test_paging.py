"""Tests for page inference helpers."""

from zettel.config import AppConfig
from zettel.harvester import _resolve_content_paging
from zettel.paging import (
    PAGE_BREAK_MARKER,
    ContentPaging,
    PageHint,
    apply_page_inference,
    compute_docling_config_hash,
    compute_page_in_book,
    detect_printed_page_from_regions,
    extract_page_hint,
    format_source_locator,
    infer_missing_page,
    lookup_page_for_chunk,
    page_map_from_marked_markdown,
    parse_biblio_start_page,
    strip_page_break_markers,
    suggest_content_start,
)


def test_extract_page_hint_from_meta():
    hint = extract_page_hint("texto sem numero", page_from_meta=12)
    assert hint.page_in_file == 12
    assert hint.confidence == "explicit"


def test_extract_page_hint_from_regex():
    text = "Conteudo do paragrafo.\n\n42\n\nMais texto."
    hint = extract_page_hint(text)
    assert hint.page_in_file == 42
    assert hint.confidence == "explicit"


def test_extract_page_hint_skips_regex_when_disallowed():
    text = "Conteudo do paragrafo.\n\n42\n\nMais texto."
    hint = extract_page_hint(text, allow_regex=False)
    assert hint.page_in_file is None
    assert hint.confidence == "unknown"


def test_infer_missing_page_interpolation():
    pages = [10, None, None, 16]
    assert infer_missing_page(1, pages) == 12
    assert infer_missing_page(2, pages) == 14


def test_apply_page_inference():
    hints = [
        PageHint(10, "explicit"),
        PageHint(None, "unknown"),
        PageHint(14, "explicit"),
    ]
    out = apply_page_inference(hints)
    assert out[0].confidence == "explicit"
    assert out[1].page_in_file == 12
    assert out[1].confidence == "inferred"


def test_compute_page_in_book():
    # file 35 = book 10 => file 40 = book 15
    assert compute_page_in_book(40, 35, 10) == 15
    assert compute_page_in_book(35, 35, 10) == 10
    assert compute_page_in_book(34, 35, 10) is None
    assert compute_page_in_book(None, 35, 10) is None
    # start_book=1 => classic offset = start_file - 1
    assert compute_page_in_book(50, 33, 1) == 18


def test_content_paging_derived_offset():
    p = ContentPaging(35, 10, "confirmed")
    assert p.page_offset == 25
    p2 = ContentPaging(32, 1, "confirmed")
    assert p2.page_offset == 31


def test_suggest_content_start_from_page_map():
    page_map = [
        (1, "Cover"),
        (10, "Preface"),
        (35, "Chapter 1\nGetting started with graphs"),
    ]
    result = suggest_content_start(page_map)
    assert result["content_start_file_page"] == 35
    assert result["content_start_book_page"] == 1
    assert result["confidence"] == "heuristic"


def test_lookup_page_uses_chunk_start_only():
    """Multi-page chunk is attributed to the first matching page (start of text)."""
    page_map = [
        (10, "AAA unique start of the section about knowledge graphs here."),
        (11, "BBB continuation material that appears later in the chunk body."),
    ]
    chunk = (
        "AAA unique start of the section about knowledge graphs here. "
        "BBB continuation material that appears later in the chunk body."
    )
    assert lookup_page_for_chunk(chunk, page_map) == 10


def test_format_source_locator():
    assert "p.10" in format_source_locator(10, "Cap > Sec")
    assert "Cap > Sec" in format_source_locator(10, "Cap > Sec")


def test_compute_docling_config_hash_stable():
    cfg = AppConfig()
    h1 = compute_docling_config_hash(cfg)
    h2 = compute_docling_config_hash(cfg)
    assert h1 == h2
    assert len(h1) == 16


def test_page_map_from_marked_markdown():
    marked = (
        f"# Cover\n\n{PAGE_BREAK_MARKER}\n\n"
        f"# Chapter 1\n\nOnce upon a unique graph story."
    )
    page_map = page_map_from_marked_markdown(marked)
    assert [p for p, _ in page_map] == [1, 2]
    assert "Cover" in page_map[0][1]
    assert "Chapter 1" in page_map[1][1]
    assert PAGE_BREAK_MARKER not in strip_page_break_markers(marked)
    assert page_map_from_marked_markdown("sem marcador") == []


def test_lookup_page_normalizes_markdown_headings():
    page_map = [
        (7, "# Unique heading about latent spaces\n\nParagraph on embeddings."),
    ]
    chunk = "## Unique heading about latent spaces\n\nParagraph on embeddings."
    assert lookup_page_for_chunk(chunk, page_map) == 7


def test_suggest_content_start_skips_toc_then_finds_chapter():
    toc = "\n".join(
        f"Chapter {i} ........ {i * 10}" for i in range(1, 12)
    )
    page_map = [
        (3, toc),
        (21, "# Chapter 1\n\nThe real beginning of the argument with enough text."),
    ]
    result = suggest_content_start(page_map)
    assert result["content_start_file_page"] == 21
    assert result["content_start_book_page"] == 1


def test_suggest_content_start_journal_from_printed_page():
    page_map = [
        (1, "# Abstract\n\nThis paper discusses unique methods."),
        (2, "Continuation of the article body."),
    ]
    result = suggest_content_start(page_map, printed_by_file_page={1: 200})
    assert result["content_start_file_page"] == 1
    assert result["content_start_book_page"] == 200
    assert result["confidence"] == "heuristic"


def test_suggest_content_start_journal_from_biblio_range():
    page_map = [(1, "Abstract of a specialized journal article about widgets.")]
    result = suggest_content_start(page_map, biblio_pages="200-210")
    assert result["content_start_book_page"] == 200
    assert result["confidence"] == "heuristic"


def test_suggest_content_start_book_uses_printed_number_on_chapter_page():
    page_map = [
        (1, "Cover"),
        (35, "# Chapter 1\n\nGetting started with graphs and knowledge."),
    ]
    result = suggest_content_start(page_map, printed_by_file_page={35: 1})
    assert result["content_start_file_page"] == 35
    assert result["content_start_book_page"] == 1


def test_parse_biblio_start_page():
    assert parse_biblio_start_page("200-210") == 200
    assert parse_biblio_start_page("p. 45–60") == 45
    assert parse_biblio_start_page("320") is None
    assert parse_biblio_start_page("320 p.") is None
    assert parse_biblio_start_page(None) is None


def test_detect_printed_page_from_header_footer():
    assert detect_printed_page_from_regions("Journal Name\n200\n", "", 1) == 200
    assert detect_printed_page_from_regions("", "1", 35) == 1
    assert detect_printed_page_from_regions("2024", "", 1) is None
    # Prefer printed number that differs from the file index
    assert detect_printed_page_from_regions("35\n1", "", 35) == 1


def test_resolve_content_paging_noninteractive_uses_heuristic():
    page_map = [(35, "# Chapter 1\n\nGetting started with graphs.")]
    paging = _resolve_content_paging(
        page_map,
        interactive=False,
        content_start_file=None,
        content_start_book=None,
        skip_paging=False,
    )
    assert paging.content_start_file_page == 35
    assert paging.content_start_book_page == 1
    assert paging.confidence == "heuristic"


def test_resolve_content_paging_skip_paging_ignores_heuristic():
    page_map = [(35, "# Chapter 1\n\nGetting started with graphs.")]
    paging = _resolve_content_paging(
        page_map,
        interactive=False,
        content_start_file=None,
        content_start_book=None,
        skip_paging=True,
    )
    assert paging.content_start_file_page == 1
    assert paging.content_start_book_page == 1
    assert paging.confidence == "skipped"


def test_resolve_content_paging_explicit_flags_win():
    paging = _resolve_content_paging(
        [(1, "x")],
        interactive=False,
        content_start_file=1,
        content_start_book=200,
        skip_paging=True,
        printed_by_file_page={1: 199},
    )
    assert paging == ContentPaging(1, 200, "confirmed")
