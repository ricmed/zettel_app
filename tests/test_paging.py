"""Tests for page inference helpers."""

from zettel.paging import (
    ContentPaging,
    apply_page_inference,
    apply_page_offset,
    compute_page_in_book,
    detect_page_offset,
    extract_page_hint,
    format_source_locator,
    infer_missing_page,
    lookup_page_for_chunk,
    suggest_content_start,
    PageHint,
)
from zettel.config import AppConfig
from zettel.paging import compute_docling_config_hash


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


def test_apply_page_offset_legacy():
    assert apply_page_offset(50, 32) == 18
    assert apply_page_offset(None, 5) is None


def test_detect_page_offset_chapter_1():
    texts = ["Prefacio", "# Capitulo 1\nInicio", "mais"]
    pages = [5, 12, 13]
    result = detect_page_offset(texts, pages)
    assert result["offset"] == 11
    assert result["needs_confirmation"] is True


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
