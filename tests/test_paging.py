"""Tests for page inference helpers."""

from zettel.config import AppConfig
from zettel.paging import (
    PAGE_BREAK_MARKER,
    ContentPaging,
    PageHint,
    PageIndex,
    apply_page_inference,
    compute_docling_config_hash,
    compute_page_in_book,
    content_start_from_page_labels,
    extract_page_hint,
    format_source_locator,
    infer_missing_page,
    locate_quote_pages,
    lookup_page_for_chunk,
    page_map_from_marked_markdown,
    page_map_is_complete,
    page_marker,
    parse_biblio_start_page,
    read_pdf_page_labels,
    render_page_marked_markdown,
    strip_page_break_markers,
    suggest_content_start,
    suggested_page_excerpt,
)
from zettel.paging import (
    resolve_content_paging as _resolve_content_paging,
)


def test_extract_page_hint_from_meta():
    hint = extract_page_hint("texto sem numero", page_from_meta=12)
    assert hint.page_in_file == 12
    assert hint.confidence == "exact"


def test_extract_page_hint_from_regex():
    text = "Conteudo do paragrafo.\n\n42\n\nMais texto."
    hint = extract_page_hint(text)
    assert hint.page_in_file == 42
    assert hint.confidence == "fuzzy"


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
        PageHint(10, "exact"),
        PageHint(None, "unknown"),
        PageHint(14, "fuzzy"),
    ]
    out = apply_page_inference(hints)
    assert out[0].confidence == "exact"
    assert out[2].confidence == "fuzzy"
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
    hint = lookup_page_for_chunk(chunk, page_map)
    assert hint.page_in_file == 10
    assert hint.confidence == "exact"


def test_format_source_locator():
    assert "p.10" in format_source_locator(10, "Cap > Sec")
    assert "Cap > Sec" in format_source_locator(10, "Cap > Sec")


def test_compute_docling_config_hash_stable():
    cfg = AppConfig()
    h1 = compute_docling_config_hash(cfg)
    h2 = compute_docling_config_hash(cfg)
    assert h1 == h2
    assert len(h1) == 16


def test_compute_docling_config_hash_changes_with_min_chunk_chars():
    cfg = AppConfig()
    before = compute_docling_config_hash(cfg)
    cfg.chunking.min_chunk_chars = cfg.chunking.min_chunk_chars + 1
    after = compute_docling_config_hash(cfg)
    assert before != after


def test_compute_docling_config_hash_changes_with_docling_version(monkeypatch):
    """A Docling upgrade must not resume a source extracted by the previous library."""
    monkeypatch.setattr(
        "zettel.paging._docling_versions",
        lambda: {"docling": "1.0.0", "docling_core": "1.0.0"},
    )
    before = compute_docling_config_hash(AppConfig())
    monkeypatch.setattr(
        "zettel.paging._docling_versions",
        lambda: {"docling": "2.0.0", "docling_core": "1.0.0"},
    )
    after = compute_docling_config_hash(AppConfig())
    assert before != after


def test_compute_docling_config_hash_changes_with_formula_and_code_enrichment():
    """Both change the text Docling emits; a resume across a flip must not reuse pages."""
    base = compute_docling_config_hash(AppConfig())
    formulas = compute_docling_config_hash(AppConfig(formulas={"enabled": True}))
    code = compute_docling_config_hash(AppConfig(code={"enabled": True}))
    assert len({base, formulas, code}) == 3


def test_page_map_from_marked_markdown():
    marked = render_page_marked_markdown(
        [
            (1, "# Cover"),
            (2, ""),
            (3, "# Chapter 1\n\nOnce upon a unique graph story."),
        ]
    )
    page_map = page_map_from_marked_markdown(marked)
    assert page_map == [
        (1, "# Cover"),
        (2, ""),
        (3, "# Chapter 1\n\nOnce upon a unique graph story."),
    ]
    assert page_map_is_complete(page_map, 3)
    assert page_marker(2) not in strip_page_break_markers(marked)
    assert page_map_from_marked_markdown("sem marcador") == []


def test_unnumbered_page_break_is_not_a_page_map():
    """The old positional split slid every page after a blank one."""
    marked = f"# Cover\n\n{PAGE_BREAK_MARKER}\n\n# Chapter 1"
    assert page_map_from_marked_markdown(marked) == []
    assert PAGE_BREAK_MARKER not in strip_page_break_markers(marked)
    assert not page_map_is_complete([(1, "a"), (3, "c")], 3)


def test_lookup_page_normalizes_markdown_headings():
    page_map = [
        (7, "# Unique heading about latent spaces\n\nParagraph on embeddings."),
    ]
    chunk = "## Unique heading about latent spaces\n\nParagraph on embeddings."
    assert lookup_page_for_chunk(chunk, page_map).page_in_file == 7


def test_lookup_page_spanning_chunk_keeps_the_start_page():
    """A probe that crosses the break is labeled with the page where it begins."""
    page_map = [
        (4, "introducao curta FIMUNICO"),
        (5, "COMECO da pagina seguinte com texto bastante para a sonda."),
    ]
    chunk = "FIMUNICO COMECO da pagina seguinte com texto bastante para a sonda."
    hint = lookup_page_for_chunk(chunk, page_map)
    assert hint.page_in_file == 4
    assert hint.confidence == "exact"


def test_lookup_page_repeated_header_is_not_exact():
    header = " ".join(f"tok{i:03d}" for i in range(30)) + " "
    page_map = [
        (1, header + " primeira ocorrencia exclusiva."),
        (9, header + " segunda ocorrencia exclusiva."),
    ]
    index = PageIndex.build(page_map)
    first, cursor = index.locate(page_map[0][1])
    assert first.page_in_file == 1
    assert first.confidence == "fuzzy"
    second, _cursor = index.locate(page_map[1][1], cursor)
    assert second.page_in_file == 9
    assert second.confidence == "fuzzy"


def test_lookup_page_fuzzy_ignores_stopwords():
    """Shared closed-class words do not pick a page; content words do."""
    shared = "de que um uma para com os as "
    page_map = [
        (2, shared + "conceito isolado sem relacao com o trecho buscado."),
        (3, "zettel literatura permanente citacao evidencias ancora no capitulo."),
    ]
    chunk = "ancora evidencias citacao permanente literatura zettel no capitulo."
    hint = lookup_page_for_chunk(chunk, page_map)
    assert hint.page_in_file == 3
    assert hint.confidence == "fuzzy"


def test_page_lookup_stays_under_two_seconds_for_a_long_book():
    """1 000 pages x 1 500 chunks. The old scan was quadratic and took minutes."""
    import time

    filler = "texto de enchimento do paragrafo " * 20
    pages = [(i, f"pagina{i:04d} marcador unico {filler}") for i in range(1, 1001)]
    index = PageIndex.build(pages)
    chunks: list[str] = []
    for _page_no, text in pages:
        chunks.append(text)
        chunks.append(text[len(text) // 2 :])
    assert len(chunks) == 2000
    started = time.perf_counter()
    cursor = 0
    hints = []
    for text in chunks[:1500]:
        hint, cursor = index.locate(text, cursor)
        hints.append(hint)
    elapsed = time.perf_counter() - started
    assert elapsed < 2.0
    assert hints[0].page_in_file == 1
    assert hints[0].confidence == "exact"
    assert all(h.page_in_file is not None for h in hints)


def test_suggest_content_start_skips_toc_then_finds_chapter():
    toc = "\n".join(f"Chapter {i} ........ {i * 10}" for i in range(1, 12))
    page_map = [
        (3, toc),
        (21, "# Chapter 1\n\nThe real beginning of the argument with enough text."),
    ]
    result = suggest_content_start(page_map)
    assert result["content_start_file_page"] == 21
    assert result["content_start_book_page"] == 1


def test_suggest_content_start_journal_from_biblio_range():
    page_map = [(1, "Abstract of a specialized journal article about widgets.")]
    result = suggest_content_start(page_map, biblio_pages="200-210")
    assert result["content_start_book_page"] == 200
    assert result["confidence"] == "heuristic"


def test_parse_biblio_start_page():
    assert parse_biblio_start_page("200-210") == 200
    assert parse_biblio_start_page("p. 45\u201360") == 45
    assert parse_biblio_start_page("320") is None
    assert parse_biblio_start_page("320 p.") is None
    assert parse_biblio_start_page(None) is None


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


def _write_labeled_pdf(path, n_pages: int, nums: str) -> None:
    kids = " ".join(f"{3 + i} 0 R" for i in range(n_pages))
    objects = {
        1: f"<< /Type /Catalog /Pages 2 0 R /PageLabels << /Nums [ {nums} ] >> >>".encode(),
        2: f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>".encode(),
    }
    for index in range(n_pages):
        objects[3 + index] = b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >>"
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number in range(1, 3 + n_pages):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + objects[number] + b"\nendobj\n"
    xref = len(out)
    size = len(offsets)
    out += f"xref\n0 {size}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets[1:]:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer << /Size {size} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    path.write_bytes(out)


def test_page_labels_after_roman_front_matter_start_at_printed_one(tmp_path):
    path = tmp_path / "livro.pdf"
    _write_labeled_pdf(path, 4, "0 << /S /r >> 2 << /S /D >>")
    labels = read_pdf_page_labels(path)
    assert labels == ["i", "ii", "1", "2"]
    start = content_start_from_page_labels(labels)
    assert start["content_start_file_page"] == 3
    assert start["content_start_book_page"] == 1
    assert start["confidence"] == "labels"
    paging = _resolve_content_paging(
        [(3, "# Capitulo 1\n\nO argumento comeca aqui.")],
        interactive=False,
        content_start_file=None,
        content_start_book=None,
        skip_paging=False,
        page_labels=labels,
    )
    assert paging == ContentPaging(3, 1, "labels")


def test_page_labels_starting_at_200_are_the_printed_number(tmp_path):
    path = tmp_path / "artigo.pdf"
    _write_labeled_pdf(path, 3, "0 << /S /D /St 200 >>")
    labels = read_pdf_page_labels(path)
    assert labels == ["200", "201", "202"]
    start = content_start_from_page_labels(labels)
    assert start["content_start_file_page"] == 1
    assert start["content_start_book_page"] == 200


def test_default_page_labels_do_not_override_the_chapter_heuristic(tmp_path):
    path = tmp_path / "sem-rotulo.pdf"
    _write_labeled_pdf(path, 2, "")
    labels = read_pdf_page_labels(path)
    assert content_start_from_page_labels(labels) is None
    page_map = [(2, "# Chapter 1\n\nGetting started with graphs.")]
    paging = _resolve_content_paging(
        page_map,
        interactive=False,
        content_start_file=None,
        content_start_book=None,
        skip_paging=False,
        page_labels=labels,
    )
    assert paging.content_start_file_page == 2
    assert paging.content_start_book_page == 1
    assert paging.confidence == "heuristic"


def test_interactive_prompt_shows_the_suggested_page_excerpt(monkeypatch):
    printed: list[str] = []

    class _Console:
        def __init__(self, *args, **kwargs):
            pass

        def print(self, *args, **kwargs):
            printed.append(" ".join(str(arg) for arg in args))

    monkeypatch.setattr("rich.console.Console", _Console)
    monkeypatch.setattr(
        "rich.prompt.Prompt.ask", lambda *args, **kwargs: kwargs.get("default", "1")
    )
    page_map = [(4, "O argumento comeca nesta linha.\n\ncontinua.")]
    paging = _resolve_content_paging(
        page_map,
        interactive=True,
        content_start_file=None,
        content_start_book=None,
        skip_paging=False,
        page_labels=["i", "ii", "iii", "1"],
    )
    assert paging == ContentPaging(4, 1, "confirmed")
    assert any("O argumento comeca nesta linha." in line for line in printed)
    assert suggested_page_excerpt(page_map, 4) == "O argumento comeca nesta linha."


def test_resolve_content_paging_explicit_flags_win():
    paging = _resolve_content_paging(
        [(1, "x")],
        interactive=False,
        content_start_file=1,
        content_start_book=200,
        skip_paging=True,
    )
    assert paging == ContentPaging(1, 200, "confirmed")


# -- Quote location (ADR-051) ---------------------------------------------

_QUOTE = "o sistema um opera de forma automatica e rapida com pouco esforco"


def _marked(*pages: str) -> str:
    return render_page_marked_markdown([(i, page) for i, page in enumerate(pages, start=1)])


def test_locate_quote_on_first_page_of_chunk():
    text = _marked(
        "capa", "Texto. O Sistema Um opera de forma automatica e rapida, com pouco esforco. Fim."
    )
    assert locate_quote_pages(text, _QUOTE, 2) == (2, 2)


def test_locate_quote_on_next_page_of_chunk():
    """A chunk's page is its first page; the anchor may sit on the following one."""
    text = _marked("capa", "Inicio do capitulo sem a citacao.", f"Adiante: {_QUOTE}.")
    assert locate_quote_pages(text, _QUOTE, 2) == (3, 3)


def test_locate_quote_crossing_a_page_break():
    text = _marked(
        "capa", "Aqui: o sistema um opera de forma", "automatica e rapida com pouco esforco. Fim."
    )
    assert locate_quote_pages(text, _QUOTE, 2) == (2, 3)


def test_locate_quote_tolerates_an_editorial_ellipsis():
    quote = "o sistema um opera de forma [...] automatica e rapida com pouco esforco"
    text = _marked("capa", f"Texto: {_QUOTE}.")
    assert locate_quote_pages(text, quote, 2) == (2, 2)


def test_locate_quote_does_not_look_before_the_chunk_page():
    text = _marked(f"Prefacio cita: {_QUOTE}.", "Pagina do chunk, sem a citacao.")
    assert locate_quote_pages(text, _QUOTE, 2) is None


def test_locate_quote_across_an_empty_page():
    """A blank file page between the chunk start and the anchor keeps its number."""
    text = _marked(
        "Inicio do chunk, sem a citacao nesta primeira pagina.",
        "",
        f"Adiante: {_QUOTE}.",
    )
    assert locate_quote_pages(text, _QUOTE, 1) == (3, 3)


def test_locate_quote_without_page_map_or_match():
    assert locate_quote_pages(f"Markdown nativo: {_QUOTE}.", _QUOTE, 1) is None
    assert locate_quote_pages(_marked("a", "b"), _QUOTE, 1) is None
    assert locate_quote_pages(_marked("a", _QUOTE), "", 1) is None
    assert locate_quote_pages(_marked("a", _QUOTE), _QUOTE, None) is None
