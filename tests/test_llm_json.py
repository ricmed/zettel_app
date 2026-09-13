r"""Tests for parsing LLM JSON that carries LaTeX (#178).

With formula enrichment, chunks carry LaTeX and models copy it into JSON answers with a
single backslash. Two failures were observed on a real run: `\mathbf` and friends raised
"Invalid \escape" and lost the answer (6 of 15 concepts in one source), and `\top` /
`\nabla` parsed without error into a tab + "op" and a newline + "abla".

The guard cases matter as much as the repairs: a newline followed by an ordinary word
must stay a newline.
"""

import json

import pytest
from zettel.llm import parse_llm_json, repair_latex_escapes


def _field(raw_json: str) -> str:
    return parse_llm_json(raw_json)["d"]


# -- repairs -------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        # escapes JSON does not define: used to raise Invalid \escape
        (r'{"d": "matriz \mathbf{A} e \sum_i x_i"}', r"matriz \mathbf{A} e \sum_i x_i"),
        (r'{"d": "conjunto \{x\} e \(a\) com \, espaco"}', r"conjunto \{x\} e \(a\) com \, espaco"),
        (r'{"d": "\underline{x} e \Delta"}', r"\underline{x} e \Delta"),
        # escapes JSON defines: used to parse into control characters
        (r'{"d": "\frac{a}{b}"}', r"\frac{a}{b}"),
        (r'{"d": "\beta e \bar{x}"}', r"\beta e \bar{x}"),
        (r'{"d": "\theta + \tau \times 2"}', r"\theta + \tau \times 2"),
        (r'{"d": "\rho e \right)"}', r"\rho e \right)"),
    ],
)
def test_unescaped_latex_is_repaired(raw, expected):
    assert _field(raw) == expected


def test_the_two_corruptions_found_in_the_database():
    """Stored as tab+"op" and newline+"abla" before this fix."""
    assert _field(r'{"d": "x ^ { \top } A x"}') == r"x ^ { \top } A x"
    assert _field(r'{"d": "f ( x ) + \nabla f ( x + t h )"}') == r"f ( x ) + \nabla f ( x + t h )"


# -- guards: legitimate JSON escapes must survive ------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        (r'{"d": "linha um\ne entao dois"}', "linha um\ne entao dois"),  # PT "e" after newline
        (r'{"d": "fim.\nOutra frase"}', "fim.\nOutra frase"),
        (r'{"d": "item\nnot only"}', "item\nnot only"),
        (r'{"d": "coluna\tvalor"}', "coluna\tvalor"),
        (r'{"d": "a\r\nb"}', "a\r\nb"),
        (r'{"d": "ele disse \"oi\""}', 'ele disse "oi"'),
        (r'{"d": "a\/b"}', "a/b"),
        (r'{"d": "café"}', "café"),
        (r'{"d": "barra literal \\ aqui"}', "barra literal \\ aqui"),
    ],
)
def test_legitimate_escapes_are_untouched(raw, expected):
    assert _field(raw) == expected


def test_already_escaped_latex_is_idempotent():
    raw = r'{"d": "\\frac{a}{b} e \\top e \\mathbf{x}"}'
    assert repair_latex_escapes(raw) == raw
    assert _field(raw) == r"\frac{a}{b} e \top e \mathbf{x}"


def test_valid_json_without_backslashes_is_unchanged():
    raw = json.dumps({"thesis": "Uma tese", "tags": ["a", "b"], "score": 4})
    assert repair_latex_escapes(raw) == raw


def test_parse_still_extracts_from_a_fenced_block():
    text = "Resposta:\n```json\n" + r'{"d": "\mathbf{x}"}' + "\n```"
    assert parse_llm_json(text) == {"d": r"\mathbf{x}"}
