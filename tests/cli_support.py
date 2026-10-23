"""Shared helpers for tests that run real ``zettel`` commands (#327, #330).

A helper module, not a test file. ``write_operational_config`` moves the
operational ``config/config.yaml`` into a throwaway project root: every path
under it, the prompts and taxonomy read from the repository, the decision shadow
off, no images and no bibliographic LLM. ``CliInstance`` invokes commands
against it through Typer's ``CliRunner``, so ``load_deps``, the operation lock,
``get_db`` and ``get_idx`` all run for real.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[1]
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def write_operational_config(root: Path) -> Path:
    """``root/config/config.yaml``: the operational config with every path inside ``root``."""
    data = yaml.safe_load((REPO / "config" / "config.yaml").read_text(encoding="utf-8"))
    data["prompts_path"] = str(REPO / "prompts")
    data["domain"]["examples_path"] = str(REPO / "config" / "domain_examples.yaml")
    data["gardener"]["topics_path"] = str(REPO / "config" / "moc_topics.yaml")
    data["retrieval"]["article"]["personalities_path"] = str(REPO / "config" / "personalities.yaml")
    data["images"]["enabled"] = False
    data["harvest"]["biblio_llm_enabled"] = False
    for site in data["decision"]["sites"]:
        data["decision"]["sites"][site] = "off"
    config = root / "config" / "config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    (root / "data" / "inbox").mkdir(parents=True, exist_ok=True)
    return config


@dataclass
class CliInstance:
    """A throwaway project root and a way to run ``zettel`` commands against it."""

    root: Path
    config: Path

    @property
    def vault(self) -> Path:
        return self.root / "vault"

    @property
    def inbox(self) -> Path:
        return self.root / "data" / "inbox"

    def run(self, command: str, *args: str, input: str | None = None, code: int | None = 0) -> str:
        """Run ``zettel <command> --config <config> <args>``; return its plain output.

        ``code`` is the expected exit code; ``None`` accepts any.
        """
        from typer.testing import CliRunner
        from zettel.cli import app

        result = CliRunner().invoke(
            app, [command, "--config", str(self.config), *args], input=input
        )
        output = _ANSI.sub("", result.output)
        if code is not None:
            assert result.exit_code == code, (
                f"exit {result.exit_code}\n{output}\n{result.exception!r}"
            )
        return output

    def db(self) -> Any:
        from zettel.state import StateDB

        return StateDB(self.root / "data" / "state.db")


# -- a two-chapter source and fixed LLM answers per phase (#322) --------------

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


def source_markdown() -> str:
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


def chapter_in(text: str) -> dict | None:
    return next((c for c in CHAPTERS if c["anchor"] in text.lower()), None)


def _extract_answer(user: str) -> dict:
    chapter = chapter_in(user)
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
        "limits": "Vale para a producao mercantil, nao para toda economia possivel.",
        "connections": [],
        "tags": [chapter["concept"]],
    }


ANSWERS = {
    "extract": _extract_answer,
    "connect": _connect_answer,
    "summarize": lambda _user: {"summary": "Resumo do trecho.", "key_topics": ["valor"]},
    # ask answers in prose, not JSON
    "ask": lambda _user: "A mercadoria satisfaz necessidades humanas.",
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
        answer = ANSWERS[self.phase](user)
        content = answer if isinstance(answer, str) else json.dumps(answer)
        return type("Answer", (), {"content": content})()


def install_fake_llm(monkeypatch: Any) -> list[str]:
    """Answer every LLM phase of the pipeline with ``ANSWERS``; returns the phases called."""
    calls: list[str] = []

    def fake_get_llm(_cfg: Any, phase: str, *_a: Any, **_k: Any) -> _FakeChat:
        return _FakeChat(phase, calls)

    for module in (
        "zettel.llm",
        "zettel.ask",
        "zettel.extractor",
        "zettel.connector.run",
        "zettel.gardener",
    ):
        monkeypatch.setattr(f"{module}.get_llm", fake_get_llm)
    return calls
