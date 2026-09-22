"""Citation provenance of a permanent note (ADR-051): page, ABNT cite, evidence block."""

from zettel.citation import (
    NoteCitation,
    citation_frontmatter,
    load_provenance,
    note_provenance,
    page_label,
    resolve_citation,
    stored_authors,
)
from zettel.hashing import extract_embeddable_text
from zettel.paging import PAGE_BREAK_MARKER
from zettel.vault import build_permanent_note_body, read_managed_block

QUOTE = "O Sistema 1 opera de forma automatica e rapida, com pouco ou nenhum esforco"

SOURCE = {
    "authors": '["Daniel Kahneman"]',
    "year": 2011,
    # PDF file pages 1-2 are front matter; printed p.1 is file p.3.
    "content_start_file_page": 3,
    "content_start_book_page": 1,
    "extracted_text": f"\n\n{PAGE_BREAK_MARKER}\n\n".join(
        [
            "capa",
            "sumario",
            "Capitulo 1. Sem a citacao nesta pagina.",
            f"Adiante o autor afirma: {QUOTE}. E segue.",
        ]
    ),
}
# The chunk starts on file p.3 (printed p.1); the anchor sits on file p.4.
CHUNK = {"page_in_file": 3, "page_in_book": 1}


def test_page_label():
    assert page_label(None) == ""
    assert page_label(42) == "p. 42"
    assert page_label(42, 42) == "p. 42"
    assert page_label(42, 43) == "p. 42-43"


def test_stored_authors_accepts_json_text_or_list():
    assert stored_authors('["A B", "C D"]') == ["A B", "C D"]
    assert stored_authors(["A B"]) == ["A B"]
    assert stored_authors(None) == []
    assert stored_authors("nao-json") == []
    assert stored_authors('{"a": 1}') == []


def test_citation_uses_the_page_where_the_anchor_sits():
    """The chunk's first page is p.1, but the quote is on printed p.2."""
    citation = resolve_citation(SOURCE, CHUNK, QUOTE)
    assert citation == NoteCitation(
        page=2, pages="p. 2", cite="(KAHNEMAN, 2011, p. 2)", page_confidence="quote"
    )


def test_citation_falls_back_to_chunk_page_when_anchor_not_found():
    citation = resolve_citation(SOURCE, CHUNK, "frase que nao existe em lugar nenhum do texto")
    assert citation.page == 1
    assert citation.cite == "(KAHNEMAN, 2011, p. 1)"
    assert citation.page_confidence == "chunk"


def test_citation_without_pages_for_native_markdown():
    """Native Markdown has no pages (ADR-013): author and year only."""
    source = {**SOURCE, "extracted_text": f"# Doc\n\n{QUOTE}."}
    citation = resolve_citation(source, {"page_in_file": None, "page_in_book": None}, QUOTE)
    assert citation == NoteCitation(
        page=None, pages="", cite="(KAHNEMAN, 2011)", page_confidence="none"
    )


def test_citation_without_source_has_no_cite():
    citation = resolve_citation(None, CHUNK, QUOTE)
    assert citation.cite == ""
    assert citation.page == 1


def test_citation_frontmatter_keeps_only_the_page():
    full = citation_frontmatter(resolve_citation(SOURCE, CHUNK, QUOTE))
    assert full == {"page": 2, "citation_page_confidence": "quote"}
    assert citation_frontmatter(NoteCitation(None, "", "", "none")) == {}


def test_note_provenance_carries_citation_anchor_and_extras():
    citation = resolve_citation(SOURCE, CHUNK, QUOTE)
    record = note_provenance(citation, anchor_quote=f"  {QUOTE} ", llm_cache_hit=False)
    assert record == {
        "citation": "(KAHNEMAN, 2011, p. 2)",
        "anchor_quote": QUOTE,
        "llm_cache_hit": False,
    }
    empty = note_provenance(NoteCitation(None, "", "", "none"), anchor_quote="", locator="")
    assert empty == {}


def test_load_provenance_tolerates_missing_or_bad_json():
    assert load_provenance(None) == {}
    assert load_provenance({"provenance_json": "nao e json"}) == {}
    assert load_provenance({"provenance_json": '{"citation": "x"}'}) == {"citation": "x"}


def _body(**kwargs) -> str:
    return build_permanent_note_body(
        thesis="Tese",
        definition="Definicao conceitual.",
        intuition="",
        example="",
        limits="",
        connections=[],
        literature_ref="[[LIT - x]]",
        **kwargs,
    )


def test_evidence_block_carries_citation_and_verbatim_quote():
    body = _body(page=2, citation="(KAHNEMAN, 2011, p. 2)", anchor_quote=QUOTE)
    block = read_managed_block(body, "auto-evidence")
    assert block is not None
    assert "- Citação: (KAHNEMAN, 2011, p. 2)" in block
    assert f'> "{QUOTE}" (KAHNEMAN, 2011, p. 2)' in block
    # Lives under ## Fonte, before any connections.
    assert body.index("## Fonte") < body.index("zettel:auto-evidence:start")


def test_evidence_block_stays_out_of_the_embedding():
    """The note stays conceptual for retrieval: the literal passage is not embedded."""
    body = _body(page=2, citation="(KAHNEMAN, 2011, p. 2)", anchor_quote=QUOTE)
    embeddable = extract_embeddable_text(body)
    assert "Sistema 1" not in embeddable
    assert "KAHNEMAN" not in embeddable
    assert "Definicao conceitual." in embeddable


def test_no_evidence_block_when_nothing_to_cite():
    assert "auto-evidence" not in _body()
