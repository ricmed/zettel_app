"""One Markdown source crosses the real pipeline: the contract between phases (#322).

Every other test seeds the state a phase is supposed to receive. This one lets
each phase receive what the previous one actually wrote: ``zettel run-all`` over
a two-chapter Markdown, with the operational ``config/config.yaml`` (paths moved
to ``tmp_path``, the decision shadow off), a fixed LLM answer per phase and a
deterministic token-hash embedding behind the real ``VectorIndex`` and Chroma.

If a phase renames a status, a ``candidate_json`` field or the link from a
concept to its chunk, the permanent note below stops being written or stops
citing its literature note, and this test fails.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from tests.fakes import token_hash_ef_builder

REPO = Path(__file__).resolve().parents[1]

CHAPTERS = [
    {
        "heading": "Mercadoria",
        "anchor": (
            "a mercadoria e antes de tudo um objeto externo que satisfaz necessidades "
            "humanas de qualquer especie"
        ),
        "thesis": "A mercadoria e definida por satisfazer necessidades humanas como objeto externo",
        "concept": "mercadoria",
    },
    {
        "heading": "Dinheiro",
        "anchor": (
            "o dinheiro funciona como medida dos valores porque todas as mercadorias "
            "expressam nele o seu valor"
        ),
        "thesis": "O dinheiro mede valores porque todas as mercadorias se expressam nele",
        "concept": "dinheiro",
    },
]

FILLER = (
    "Esta passagem desenvolve o argumento com exemplos historicos e comparacoes entre "
    "formas de troca, retomando a analise do capitulo anterior e preparando a seguinte. "
)


def _source_markdown() -> str:
    parts = [
        "---",
        "title: O Capital",
        "author: Karl Marx",
        "year: 2013",
        "document_type: livro",
        "place: Sao Paulo",
        "publisher: Boitempo",
        "edition: 2. ed.",
        "---",
        "",
        "# O Capital",
        "",
    ]
    for chapter in CHAPTERS:
        sentence = chapter["anchor"][0].upper() + chapter["anchor"][1:] + "."
        parts += [f"## {chapter['heading']}", "", FILLER * 2 + sentence + " " + FILLER, ""]
    return "\n".join(parts)


# -- fixed LLM answers per phase --------------------------------------------


def _chapter_in(text: str) -> dict | None:
    return next((c for c in CHAPTERS if c["anchor"] in text.lower()), None)


def _extract_answer(user: str) -> dict:
    chapter = _chapter_in(user)
    if chapter is None:  # a chunk with no idea of its own, e.g. the title preamble
        return {
            "chunk_status": "rejected",
            "rejection_reason": "Trecho sem ideia propria.",
            "rejection_category": "structural",
            "summary": "Preambulo.",
            "candidates": [],
        }
    return {
        "chunk_status": "accepted",
        "rejection_reason": "",
        "rejection_category": "",
        "summary": f"O trecho define {chapter['concept']} na economia politica.",
        "key_concepts": [chapter["concept"]],
        "candidates": [
            {
                "thesis": chapter["thesis"],
                "definition": (
                    f"No vocabulario da economia politica, {chapter['concept']} designa uma "
                    "categoria basica a partir da qual o autor organiza toda a analise seguinte."
                ),
                "intuition": "Funciona como o ponto de partida concreto do raciocinio.",
                "limits": "A definicao vale para a sociedade mercantil analisada pelo autor.",
                "anchor_quote": chapter["anchor"],
                "source_locator": chapter["heading"],
                "tags": [chapter["concept"]],
                "relevance_score": 3,
            }
        ],
    }


def _connect_answer(user: str) -> dict:
    chapter = next(c for c in CHAPTERS if c["thesis"] in user)
    return {
        "status": "accepted",
        "title": chapter["thesis"],
        "thesis": chapter["thesis"],
        "definition": (
            f"Para o autor, {chapter['concept']} e uma categoria que so faz sentido dentro "
            "das relacoes de troca que a sociedade mercantil estabelece entre as pessoas."
        ),
        "intuition": "Pense numa feira em que tudo e trocado por algo equivalente.",
        "example": "Um casaco trocado por dez metros de linho.",
        "limits": "Vale para a producao mercantil, nao para toda economia possivel.",
        "connections": [],
        "tags": [chapter["concept"]],
    }


ANSWERS = {
    "extract": _extract_answer,
    "connect": _connect_answer,
    "summarize": lambda _user: {"summary": "Resumo do trecho.", "key_topics": ["valor"]},
}


class _FakeChat:
    model_name = "fake-llm"

    def __init__(self, phase: str, calls: list[str]):
        self.phase = phase
        self.calls = calls

    def invoke(self, messages: list[Any], **_kwargs: Any) -> Any:
        self.calls.append(self.phase)
        user = str(messages[-1].content)
        if self.phase not in ANSWERS:
            raise AssertionError(f"fase {self.phase} nao deveria chamar o LLM neste acervo")
        return type("Answer", (), {"content": json.dumps(ANSWERS[self.phase](user))})()


@pytest.fixture
def pipeline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A project root in ``tmp_path`` running the operational config offline."""
    data = yaml.safe_load((REPO / "config" / "config.yaml").read_text(encoding="utf-8"))
    data["prompts_path"] = str(REPO / "prompts")
    data["domain"]["examples_path"] = str(REPO / "config" / "domain_examples.yaml")
    data["gardener"]["topics_path"] = str(REPO / "config" / "moc_topics.yaml")
    data["retrieval"]["article"]["personalities_path"] = str(REPO / "config" / "personalities.yaml")
    data["images"]["enabled"] = False
    data["harvest"]["biblio_llm_enabled"] = False
    for site in data["decision"]["sites"]:
        data["decision"]["sites"][site] = "off"
    config = tmp_path / "config" / "config.yaml"
    config.parent.mkdir()
    config.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    inbox = tmp_path / "data" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "capital.md").write_text(_source_markdown(), encoding="utf-8")

    from zettel import index

    # The operational provider name keeps the config valid; its builder is the offline one.
    monkeypatch.setitem(index._EF_BUILDERS, data["embedding"]["provider"], token_hash_ef_builder)
    calls: list[str] = []

    def fake_get_llm(_cfg: Any, phase: str, *_a: Any, **_k: Any) -> _FakeChat:
        return _FakeChat(phase, calls)

    for module in ("zettel.llm", "zettel.extractor", "zettel.connector.run", "zettel.gardener"):
        monkeypatch.setattr(f"{module}.get_llm", fake_get_llm)

    from zettel.cli import app

    def run_all() -> None:
        result = CliRunner().invoke(
            app, ["run-all", "--config", str(config), "--yes", "--auto-approve"]
        )
        assert result.exit_code == 0, result.output

    return tmp_path, run_all, calls


def _vault_bytes(vault: Path) -> dict[str, bytes]:
    return {str(p.relative_to(vault)): p.read_bytes() for p in sorted(vault.rglob("*.md"))}


def test_a_markdown_source_becomes_cited_permanent_notes(pipeline):
    root, run_all, calls = pipeline
    run_all()

    from zettel.state import StateDB
    from zettel.vault import parse_frontmatter, read_managed_block

    vault = root / "vault"
    ztls = sorted((vault / "30_Permanent").glob("ZTL - *.md"))
    assert len(ztls) == len(CHAPTERS), calls

    db = StateDB(root / "data" / "state.db")
    try:
        (source,) = db.list_sources()
        citekey = source["citekey"]
        for path in ztls:
            meta, body = parse_frontmatter(path.read_text(encoding="utf-8"))
            note = db.get_note(meta["note_id"])
            chunk = db.get_chunk(meta["chunk_id"])
            chapter = _chapter_in(chunk["text"])

            # review approved the draft and connect consumed the concept it left
            noted = db.get_concepts_by_status("noted")
            assert [c["note_id"] for c in noted].count(note["note_id"]) == 1
            assert chunk["status"] in ("approved", "persisted")

            # the note cites the approved granular LIT of its own chunk ...
            lit = Path(chunk["literature_note_path"])
            assert lit.is_file() and lit.parent.name == citekey
            assert lit.stem in body.split("## Fonte", 1)[1]

            # ... with a citation computed by code: Markdown has no pages (ADR-013, ADR-051)
            assert "page" not in meta
            provenance = json.loads(note["provenance_json"])
            assert provenance["anchor_quote"] == chapter["anchor"]
            assert provenance["citation"].startswith("(MARX, 2013")
            assert "p." not in provenance["citation"]
            assert chapter["anchor"] in read_managed_block(body, "auto-evidence")

            # the approved LIT lists the note it produced
            lit_block = read_managed_block(lit.read_text(encoding="utf-8"), "auto-lit-permanent")
            assert path.stem in lit_block
    finally:
        db.close()

    index_note = next((vault / "20_Literature").glob("LIT - *.md"))
    chapter_map = read_managed_block(index_note.read_text(encoding="utf-8"), "auto-chapter-map")
    for path in ztls:
        assert path.stem in chapter_map


def test_a_second_run_changes_nothing_and_calls_no_llm(pipeline):
    root, run_all, calls = pipeline
    run_all()
    before = _vault_bytes(root / "vault")
    calls_before = len(calls)

    run_all()

    assert _vault_bytes(root / "vault") == before
    assert len(calls) == calls_before
