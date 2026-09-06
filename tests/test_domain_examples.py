"""Loader of domain few-shots: missing file, missing section, accepted_example schema."""

from pathlib import Path

import pytest
from zettel.domain_examples import (
    DomainExamplesLoadError,
    load_domain_examples,
    render_for_prompt,
)
from zettel.llm import fill_template, load_prompt_parts

_REPO = Path(__file__).resolve().parents[1]


def test_load_shipped_domain_examples():
    examples = load_domain_examples(_REPO / "config" / "domain_examples.yaml")
    rendered = render_for_prompt(examples, "literature_note")
    assert set(rendered) == {
        "relevance_examples",
        "thesis_examples",
        "judgement_examples",
        "tag_examples",
        "rejection_examples",
        "accepted_example",
    }
    assert rendered["relevance_examples"].strip()
    assert rendered["accepted_example"].strip()
    perm = render_for_prompt(examples, "permanent_note")
    assert set(perm) == {"thesis_examples", "decision_examples"}
    assert perm["thesis_examples"].strip()


def test_accepted_example_covers_fiction_with_extractable_principle():
    """Issue: 'narrative' rejection had no positive counter-example — ficcao

    sem principio e ficcao com principio pareciam identicas ao modelo. O
    arquivo shippado precisa ensinar as duas faces.
    """
    examples = load_domain_examples(_REPO / "config" / "domain_examples.yaml")
    accepted = examples.literature_note.accepted_example
    rejection = examples.literature_note.rejection_examples
    assert "ficcao" in accepted.lower() or "ficção" in accepted.lower()
    assert '"chunk_status": "accepted"' in accepted
    # o par negativo continua existindo: narrativa SEM principio ainda rejeita
    assert "ficcao sem principio" in rejection.lower()


def test_missing_file_raises(tmp_path: Path):
    with pytest.raises(DomainExamplesLoadError, match="nao encontrado"):
        load_domain_examples(tmp_path / "ausente.yaml")


def test_missing_section_renders_empty_string(tmp_path: Path):
    path = tmp_path / "partial.yaml"
    path.write_text("literature_note:\n  thesis_examples: so tese\n", encoding="utf-8")
    examples = load_domain_examples(path)
    rendered = render_for_prompt(examples, "literature_note")
    assert rendered["thesis_examples"] == "so tese"
    assert rendered["relevance_examples"] == ""
    assert rendered["accepted_example"] == ""
    assert "{relevance_examples}" not in rendered["relevance_examples"]


def test_render_never_leaves_raw_placeholder_in_prompt():
    examples = load_domain_examples(_REPO / "config" / "domain_examples.yaml")
    mapping = {
        "language": "pt-BR",
        "domain": "Teste",
        "source_id": "@X",
        "source_title": "T",
        "section_path": "S",
        "locator": "p.1",
        "images_context": "",
        "chunk_text": "texto",
        **render_for_prompt(examples, "literature_note"),
    }
    parts = load_prompt_parts(_REPO / "prompts" / "literature_note.md")
    filled = fill_template(parts.system, mapping) + fill_template(parts.user_template, mapping)
    assert "{relevance_examples}" not in filled
    assert "{accepted_example}" not in filled
    assert "{tag_examples}" not in filled


def test_unknown_prompt_returns_empty_dict():
    examples = load_domain_examples(_REPO / "config" / "domain_examples.yaml")
    assert render_for_prompt(examples, "ask") == {}


def test_query_from_permanent_body():
    from zettel.manual_lit import query_from_permanent_body

    body = "> **Tese**: a mente nao e o corpo\n\n## Definição\n\nSubstâncias distintas.\n"
    assert "mente" in query_from_permanent_body(body)
    assert "Substâncias" in query_from_permanent_body(body)
