"""Tests for vault I/O operations."""

from zettel.hashing import extract_embeddable_text
from zettel.vault import (
    author_year_label,
    best_candidate_thesis,
    build_literature_chunk_note,
    compose_note,
    literature_chunk_filename,
    literature_chunk_filename_for_row,
    literature_chunk_topic,
    literature_chunk_wikilink,
    literature_index_filename,
    literature_index_link_label,
    literature_source_dirname,
    normalize_note_id,
    note_filename,
    parse_frontmatter,
    permanent_wikilink,
    read_managed_block,
    rewrite_bare_permanent_wikilinks,
    safe_update_managed_blocks,
    source_note_filename,
    upsert_managed_block,
    _slug,
)


def test_parse_frontmatter_basic():
    content = "---\ntype: permanent\nnote_id: abc123\n---\n\n# Title\n\nBody"
    meta, body = parse_frontmatter(content)
    assert meta["type"] == "permanent"
    assert meta["note_id"] == "abc123"
    assert "# Title" in body


def test_parse_frontmatter_no_frontmatter():
    content = "# Just a heading\n\nSome body text"
    meta, body = parse_frontmatter(content)
    assert meta == {}
    assert body == content


def test_compose_note():
    meta = {"type": "permanent", "note_id": "abc"}
    body = "# Title\n\nBody"
    result = compose_note(meta, body)
    assert result.startswith("---\n")
    assert "type: permanent" in result
    assert "# Title" in result


def test_read_managed_block():
    content = (
        "Some text\n"
        "<!-- zettel:auto-backlinks:start -->\n"
        "- link1\n"
        "- link2\n"
        "<!-- zettel:auto-backlinks:end -->\n"
        "More text"
    )
    block = read_managed_block(content, "auto-backlinks")
    assert block is not None
    assert "link1" in block
    assert "link2" in block


def test_read_managed_block_not_found():
    content = "No blocks here"
    result = read_managed_block(content, "auto-backlinks")
    assert result is None


def test_upsert_managed_block_insert():
    content = "# Title\n\nBody text"
    result = upsert_managed_block(content, "auto-backlinks", "- new link")
    assert "<!-- zettel:auto-backlinks:start -->" in result
    assert "- new link" in result
    assert "<!-- zettel:auto-backlinks:end -->" in result


def test_upsert_managed_block_replace():
    content = (
        "# Title\n\n"
        "<!-- zettel:auto-backlinks:start -->\n"
        "- old link\n"
        "<!-- zettel:auto-backlinks:end -->\n"
    )
    result = upsert_managed_block(content, "auto-backlinks", "- new link")
    assert "old link" not in result
    assert "- new link" in result


def test_safe_update_managed_blocks_bumps_updated_at(tmp_path):
    path = tmp_path / "note.md"
    path.write_text(
        compose_note(
            {
                "type": "permanent",
                "note_id": "abc",
                "updated_at": "2020-01-01T00:00:00",
            },
            "# Title\n\nBody\n",
        ),
        encoding="utf-8",
    )
    safe_update_managed_blocks(path, {"auto-connections": "- [[Other]]"})
    meta, body = parse_frontmatter(path.read_text(encoding="utf-8"))
    assert meta["updated_at"] > "2020-01-01T00:00:00"
    assert "[[Other]]" in body
    assert "<!-- zettel:auto-connections:start -->" in body


def test_safe_update_managed_blocks_idempotent_keeps_updated_at(tmp_path):
    path = tmp_path / "note.md"
    initial = compose_note(
        {
            "type": "permanent",
            "note_id": "abc",
            "updated_at": "2020-01-01T00:00:00",
        },
        (
            "# Title\n\n"
            "<!-- zettel:auto-connections:start -->\n"
            "- [[Other]]\n"
            "<!-- zettel:auto-connections:end -->\n"
        ),
    )
    path.write_text(initial, encoding="utf-8")
    safe_update_managed_blocks(path, {"auto-connections": "- [[Other]]"})
    meta, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
    assert meta["updated_at"] == "2020-01-01T00:00:00"


def test_slug():
    assert _slug("Hello World! Test 123") == "hello-world-test-123"
    assert len(_slug("a" * 200)) <= 100
    assert _slug("a" * 100) == "a" * 100


def test_note_filename():
    name = note_filename("ZTL", "ABC123", "My Great Note")
    assert name == "ZTL - ABC123 - my-great-note.md"


def test_source_note_filename_uses_author_year():
    name = source_note_filename("Negro2026KnowledgeGraphs", "Knowledge Graphs and LLMs in Action")
    assert name == "SRC - Negro2026 - knowledge-graphs-and-llms-in-action.md"


def test_author_year_label():
    assert author_year_label("Negro2026KnowledgeGraphs") == "Negro2026"
    assert author_year_label("@Negro2026KnowledgeGraphs") == "Negro2026"
    assert author_year_label("Book2024") == "Book2024"
    assert author_year_label("UntitledOnly") == "UntitledOnly"


def test_literature_source_dirname_strips_at():
    assert literature_source_dirname("@Negro2026KnowledgeGraphs") == "Negro2026KnowledgeGraphs"
    assert literature_source_dirname("Negro2026KnowledgeGraphs") == "Negro2026KnowledgeGraphs"


def test_literature_index_filename_no_at_no_index_suffix():
    name = literature_index_filename(
        "Negro2026KnowledgeGraphs", "Knowledge Graphs and LLMs in Action"
    )
    assert name == "LIT - Negro2026 - knowledge-graphs-and-llms-in-action.md"


def test_literature_chunk_filename_page_and_section():
    name = literature_chunk_filename(
        "Negro2026KnowledgeGraphs",
        chunk_index=7,
        page_in_book=42,
        section_path="Cap 2 > Sistema 1 > Intuicao",
    )
    assert name == "LIT - Negro2026 - p042 - intuicao-0007.md"


def test_literature_chunk_filename_same_section_differs_by_index():
    kwargs = dict(
        citekey="Negro2026KnowledgeGraphs",
        page_in_book=42,
        section_path="Cap 2 > Sistema 1",
    )
    a = literature_chunk_filename(chunk_index=7, **kwargs)
    b = literature_chunk_filename(chunk_index=8, **kwargs)
    assert a != b
    assert a.endswith("-0007.md")
    assert b.endswith("-0008.md")


def test_literature_chunk_filename_falls_back_to_summary_slug():
    name = literature_chunk_filename(
        "Book2024",
        chunk_index=3,
        page_in_book=10,
        section_path="Documento completo",
        summary="Um resumo sobre vies de confirmacao",
    )
    assert name == "LIT - Book2024 - p010 - um-resumo-sobre-vies-de-confirmacao-0003.md"


# ── #57: thesis-based title/slug ────────────────────────────────────────


def test_best_candidate_thesis_picks_highest_relevance():
    candidates = [
        {"thesis": "Tese fraca", "relevance_score": 2},
        {"thesis": "Tese forte e declarativa sobre o tema", "relevance_score": 5},
        {"thesis": "Tese media", "relevance_score": 3},
    ]
    assert best_candidate_thesis(candidates) == "Tese forte e declarativa sobre o tema"


def test_best_candidate_thesis_tie_keeps_first_in_list():
    candidates = [
        {"thesis": "Primeira tese, mesma relevancia", "relevance_score": 4},
        {"thesis": "Segunda tese, mesma relevancia", "relevance_score": 4},
    ]
    assert best_candidate_thesis(candidates) == "Primeira tese, mesma relevancia"


def test_best_candidate_thesis_empty_list():
    assert best_candidate_thesis([]) == ""
    assert best_candidate_thesis(None) == ""


def test_literature_chunk_topic_precedence_thesis_over_summary_over_section():
    # thesis wins even when summary and section_path are both present
    assert literature_chunk_topic(
        section_path="Cap 2 > Sistema 1", summary="um resumo qualquer",
        thesis="L1 induz esparsidade nos pesos",
    ) == "L1 induz esparsidade nos pesos"
    # no thesis: summary wins over section_path
    assert literature_chunk_topic(
        section_path="Cap 2 > Sistema 1", summary="um resumo qualquer",
    ) == "um resumo qualquer"
    # no thesis, no summary: section_path
    assert literature_chunk_topic(section_path="Cap 2 > Sistema 1") == "Sistema 1"
    # nothing at all
    assert literature_chunk_topic() == "nota"


def test_literature_chunk_filename_two_theses_in_same_section_differ():
    """Two LIT notes from the same section but different theses get different slugs."""
    a = literature_chunk_filename(
        "Book2024", chunk_index=7, page_in_book=8,
        section_path="Cap > Pontos de Atencao",
        thesis="Regularizacao L1 induz esparsidade nos pesos do modelo",
    )
    b = literature_chunk_filename(
        "Book2024", chunk_index=9, page_in_book=8,
        section_path="Cap > Pontos de Atencao",
        thesis="Dropout previne overfitting ao desligar neuronios aleatoriamente",
    )
    assert a != b
    assert "regularizacao" in a or "l1" in a
    assert "dropout" in b


def test_literature_chunk_filename_thesis_beats_generic_section_path():
    name = literature_chunk_filename(
        "Book2024", chunk_index=1, page_in_book=8,
        section_path="Cap > 7 Pontos de Atencao e Anti-Padroes",
        thesis="Gradient clipping evita explosao de gradientes em RNNs profundas",
    )
    assert "pontos-de-atencao" not in name
    assert "gradient" in name


def test_literature_chunk_filename_for_row_derives_thesis_from_summary_json():
    import json

    chunk = {
        "chunk_index": 2,
        "page_in_book": 5,
        "section_path": "Cap > Secao",
        "summary_json": json.dumps({
            "summary": "resumo generico",
            "candidates": [
                {"thesis": "tese fraca", "relevance_score": 2},
                {"thesis": "Backpropagation calcula gradientes via regra da cadeia", "relevance_score": 5},
            ],
        }),
    }
    name = literature_chunk_filename_for_row("Book2024", chunk)
    assert "backpropagation" in name


def test_literature_chunk_wikilink_is_path_qualified():
    link = literature_chunk_wikilink(
        "Negro2026KnowledgeGraphs",
        chunk_index=7,
        page_in_book=42,
        section_path="Cap 2 > Sistema 1",
        alias="p. 42 — Sistema 1",
    )
    assert link.startswith("[[Negro2026KnowledgeGraphs/LIT - Negro2026 - p042 - sistema-1-0007|")
    assert "p. 42 — Sistema 1" in link


def test_literature_index_link_label():
    label = literature_index_link_label(
        page_in_book=42,
        section_path="Cap 2 > Sistema 1",
    )
    assert label == "p. 42 — Sistema 1"


def test_literature_chunk_note_includes_source_excerpt():
    source = "Paragrafo integral do chunk sobre Sistema 1 e intuicao."
    _, body = build_literature_chunk_note(
        source_id="@S",
        citekey="Book2024",
        title="Livro",
        chunk_id="@S::ch::abc",
        chunk_index=1,
        literature_id="lit1",
        summary="Resumo gerado pelo LLM.",
        key_concepts=["intuicao"],
        candidates=[],
        section_path="Cap > Sistema 1",
        source_text=source,
        page_in_book=20,
    )
    assert "## Trecho da fonte" in body
    assert "zettel:auto-source-excerpt:start" in body
    assert source in body
    embeddable = extract_embeddable_text(compose_note({"type": "literature"}, body))
    assert source not in embeddable
    assert "Resumo gerado pelo LLM." in embeddable


# ── #58: render anchor_quote + relevance in the LIT note ─────────────────


def test_literature_chunk_note_renders_thesis_relevance_and_anchor_quote():
    _, body = build_literature_chunk_note(
        source_id="@S",
        citekey="Book2024",
        title="Livro",
        chunk_id="@S::ch::abc",
        chunk_index=1,
        literature_id="lit1",
        summary="Resumo gerado pelo LLM.",
        key_concepts=["intuicao"],
        candidates=[{
            "thesis": "L1 induz esparsidade nos pesos do modelo",
            "definition": "Definicao completa aqui",
            "anchor_quote": "a penalidade L1 empurra pesos irrelevantes para exatamente zero",
            "relevance_score": 4,
            "source_locator": "p.42",
        }],
        section_path="Cap > Sistema 1",
        source_text="Paragrafo integral do chunk.",
        page_in_book=20,
    )
    assert "L1 induz esparsidade nos pesos do modelo" in body
    assert "relevancia 4/5" in body
    assert "p.42" in body
    assert "a penalidade L1 empurra pesos irrelevantes para exatamente zero" in body
    assert "zettel:auto-candidate-quotes:start" in body


def test_literature_chunk_note_anchor_quote_excluded_from_embedding():
    _, body = build_literature_chunk_note(
        source_id="@S",
        citekey="Book2024",
        title="Livro",
        chunk_id="@S::ch::abc",
        chunk_index=1,
        literature_id="lit1",
        summary="Resumo gerado pelo LLM.",
        key_concepts=[],
        candidates=[{
            "thesis": "L1 induz esparsidade nos pesos do modelo",
            "anchor_quote": "a penalidade L1 empurra pesos irrelevantes para exatamente zero",
            "relevance_score": 4,
        }],
        source_text="",
    )
    embeddable = extract_embeddable_text(compose_note({"type": "literature"}, body))
    # The thesis (real signal) stays; the raw quote (duplicated source text) doesn't.
    assert "L1 induz esparsidade nos pesos do modelo" in embeddable
    assert "a penalidade L1 empurra pesos irrelevantes" not in embeddable


def test_literature_chunk_note_candidate_without_anchor_quote_does_not_break_layout():
    _, body = build_literature_chunk_note(
        source_id="@S",
        citekey="Book2024",
        title="Livro",
        chunk_id="@S::ch::abc",
        chunk_index=1,
        literature_id="lit1",
        summary="Resumo.",
        key_concepts=[],
        candidates=[{
            "thesis": "Uma tese sem citacao ancora disponivel",
            "anchor_quote": "",
            "relevance_score": 3,
        }],
        source_text="",
    )
    assert "Uma tese sem citacao ancora disponivel" in body
    assert "_(sem citação-âncora)_" in body
    assert "## Trecho da fonte" in body  # rest of the note still renders


def test_literature_chunk_note_empty_source_placeholder():
    _, body = build_literature_chunk_note(
        source_id="@S",
        citekey="Book2024",
        title="Livro",
        chunk_id="@S::ch::abc",
        chunk_index=1,
        literature_id="lit1",
        summary="Resumo.",
        key_concepts=[],
        candidates=[],
        source_text="",
    )
    assert "_Trecho nao disponivel._" in body


def test_permanent_wikilink_prefers_path_stem():
    path = "/vault/30_Permanent/ZTL - 01ABC - titulo-curto.md"
    link = permanent_wikilink(
        "01ABC",
        "Titulo longo diferente no frontmatter",
        path=path,
    )
    assert link == "[[ZTL - 01ABC - titulo-curto]]"


def test_permanent_wikilink_falls_back_to_title():
    link = permanent_wikilink("01ABC", "Hello World")
    assert link == "[[ZTL - 01ABC - hello-world]]"


def test_normalize_note_id_bare_ulid():
    ulid = "01HAAAAAAAAAAAAAAAAAAAAAAA"
    assert normalize_note_id(ulid) == ulid


def test_normalize_note_id_strips_ztl_prefix_and_wikilink():
    ulid = "01HAAAAAAAAAAAAAAAAAAAAAAA"
    assert normalize_note_id(f"ZTL - {ulid}") == ulid
    assert normalize_note_id(f"ZTL - ZTL - {ulid}") == ulid
    assert normalize_note_id(f"[[ZTL - {ulid} - algum-slug]]") == ulid
    assert normalize_note_id(f"[[ZTL - ZTL - {ulid}]]") == ulid


def test_normalize_note_id_rejects_prose():
    assert normalize_note_id("análise de séries temporais") is None
    assert normalize_note_id("") is None
    assert normalize_note_id("   ") is None


def test_normalize_note_id_legacy_short_id():
    assert normalize_note_id("ABC123") == "ABC123"
    assert normalize_note_id("ZTL - ABC123 - um-slug") == "ABC123"


def test_rewrite_bare_permanent_wikilinks_uses_file_stem():
    ulid = "01HAAAAAAAAAAAAAAAAAAAAAAA"
    path = "/vault/30_Permanent/ZTL - 01HAAAAAAAAAAAAAAAAAAAAAAA - analise.md"

    def lookup(note_id: str):
        return path if note_id == ulid else None

    text = (
        f"- [[ZTL - ZTL - {ulid}]] (extends) -- desc\n"
        f"- [[ZTL - {ulid}]] (related)\n"
        f"- [[ZTL - {ulid} - analise]] (keeps slug form)\n"
    )
    out = rewrite_bare_permanent_wikilinks(text, lookup)
    assert f"[[ZTL - {ulid} - analise]] (extends)" in out
    assert f"[[ZTL - {ulid} - analise]] (related)" in out
    # Already-slugged wikilink is not a "bare" target, so it stays as written.
    assert f"[[ZTL - {ulid} - analise]] (keeps slug form)" in out
    assert "ZTL - ZTL" not in out
