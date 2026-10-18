"""Fence-aware Markdown readers shared by harvest, dumps and note parsers (ADR-014)."""

import re

from zettel.markdown_fences import (
    collapse_table_padding,
    h2_section,
    headings_outside_fences,
    iter_table_spans,
)

PY_FENCE = (
    "```python\n"
    "# utils.py — exibe prompt e resposta\n"
    "import os\n"
    "## nao e uma secao\n"
    "print(os.getcwd())  # comentario no fim da linha\n"
    "```"
)

_ATX = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)


def test_headings_outside_fences_skips_python_comments():
    text = f"# Titulo\n\nprosa\n\n{PY_FENCE}\n\n## Secao real\n"
    titles = [m.group(2) for m in headings_outside_fences(_ATX, text)]
    assert titles == ["Titulo", "Secao real"]


def test_headings_outside_fences_without_fences_is_plain_finditer():
    text = "# A\n## B\n"
    assert [m.group(2) for m in headings_outside_fences(_ATX, text)] == ["A", "B"]


def test_h2_section_is_not_cut_by_a_heading_inside_a_fence():
    body = f"## Resumo\n\nantes do codigo\n\n{PY_FENCE}\n\ndepois do codigo\n\n## Limites\n\nfim\n"
    section = h2_section(body, "Resumo")
    assert section.startswith("antes do codigo")
    assert PY_FENCE in section
    assert section.endswith("depois do codigo")
    assert h2_section(body, "Limites") == "fim"


def test_h2_section_ignores_a_matching_heading_inside_a_fence():
    body = "```markdown\n## Resumo\n\nexemplo\n```\n\n## Resumo\n\nreal\n"
    assert h2_section(body, "Resumo") == "real"


def test_h2_section_missing_heading_is_empty():
    assert h2_section("## Outra\n\ntexto\n", "Resumo") == ""


def test_table_span_stops_at_a_blank_line_and_ignores_fences():
    table = "| a | b |\n| --- | --- |\n| 1 | 2 |\n"
    text = f"antes\n\n{table}\ndepois\n"
    spans = iter_table_spans(text)
    assert len(spans) == 1
    assert text[spans[0][0] : spans[0][1]] == table
    fenced = "```\n| a | b |\n| --- | --- |\n| 1 | 2 |\n```\n"
    assert iter_table_spans(fenced) == []
    assert iter_table_spans(text + table.rstrip("\n"))[-1][1] == len(text + table.rstrip("\n"))


def test_collapse_table_padding_keeps_cells_and_skips_fences():
    raw = "| a    | b  |\n| --- | --- |\n| 1     | 2 |\n"
    assert collapse_table_padding(raw) == "| a | b |\n| --- | --- |\n| 1 | 2 |\n"
    fenced = "```\n| a    | b |\n| --- | --- |\n```\n"
    assert collapse_table_padding(fenced) == fenced


def test_h2_section_only_h2_closes_the_section():
    body = "## Resumo\n\nparte 1\n\n### Sub\n\nparte 2\n\n# H1\n\nparte 3\n\n## Proxima\n"
    section = h2_section(body, "Resumo")
    assert "parte 3" in section
    assert "Proxima" not in section
