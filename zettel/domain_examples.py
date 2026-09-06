"""Few-shots de dominio para extract/connect — YAML + Pydantic, modulo folha.

Espelha ``zettel/taxonomy.py``: modelos, loader tipado e renderizadores markdown.
So ``yaml`` + ``pydantic``. Nao importar chromadb nem o pipeline.

``fill_template`` deixa chave desconhecida LITERAL no prompt. O renderizador
devolve **todas** as chaves do prompt sempre (string vazia se a secao faltar).
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

_JSON_FENCE = re.compile(r"```json\s*\n(.*?)```", re.DOTALL)

LITERATURE_NOTE_KEYS: tuple[str, ...] = (
    "relevance_examples",
    "thesis_examples",
    "judgement_examples",
    "tag_examples",
    "rejection_examples",
    "accepted_example",
)

PERMANENT_NOTE_KEYS: tuple[str, ...] = (
    "thesis_examples",
    "decision_examples",
)

_PROMPT_KEYS: dict[str, tuple[str, ...]] = {
    "literature_note": LITERATURE_NOTE_KEYS,
    "permanent_note": PERMANENT_NOTE_KEYS,
}


class DomainExamplesLoadError(Exception):
    """Arquivo de few-shots ausente ou invalido."""


class LiteratureNoteExamples(BaseModel):
    relevance_examples: str = ""
    thesis_examples: str = ""
    judgement_examples: str = ""
    tag_examples: str = ""
    rejection_examples: str = ""
    accepted_example: str = ""


class PermanentNoteExamples(BaseModel):
    thesis_examples: str = ""
    decision_examples: str = ""


class DomainExamplesFile(BaseModel):
    literature_note: LiteratureNoteExamples = Field(default_factory=LiteratureNoteExamples)
    permanent_note: PermanentNoteExamples = Field(default_factory=PermanentNoteExamples)


def load_domain_examples(path: Path | str | None) -> DomainExamplesFile:
    """Load and validate ``config/domain_examples.yaml`` (or equivalent)."""
    if path is None:
        raise DomainExamplesLoadError("domain.examples_path nao configurado")
    p = Path(path)
    if not p.exists():
        raise DomainExamplesLoadError(f"Arquivo de exemplos de dominio nao encontrado: {p}")
    with open(p, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    if not isinstance(raw, dict):
        raise DomainExamplesLoadError(f"Exemplos de dominio invalidos (esperado mapping YAML): {p}")
    try:
        examples = DomainExamplesFile.model_validate(raw)
    except Exception as e:
        raise DomainExamplesLoadError(f"Exemplos de dominio invalidos em {p}: {e}") from e
    _validate_accepted_example(examples.literature_note.accepted_example, source=p)
    return examples


def render_for_prompt(examples: DomainExamplesFile, prompt_name: str) -> dict[str, str]:
    """Return every placeholder key for ``prompt_name``, never a missing key.

    Unknown ``prompt_name`` returns an empty dict. A missing YAML section
    becomes ``""`` so ``fill_template`` cannot leak ``{relevance_examples}``.
    """
    keys = _PROMPT_KEYS.get(prompt_name)
    if keys is None:
        return {}
    section: Any
    if prompt_name == "literature_note":
        section = examples.literature_note
    else:
        section = examples.permanent_note
    data = section.model_dump()
    return {key: str(data.get(key) or "") for key in keys}


def _validate_accepted_example(text: str, *, source: Path) -> None:
    """Each fenced JSON in ``accepted_example`` must be a ``LiteratureChunkOutput``."""
    if not text.strip():
        return
    from zettel.schemas import LiteratureChunkOutput

    blocks = _JSON_FENCE.findall(text)
    if not blocks:
        raise DomainExamplesLoadError(
            f"{source}: literature_note.accepted_example precisa de um bloco ```json"
        )
    for block in blocks:
        try:
            payload = json.loads(block)
        except json.JSONDecodeError as e:
            raise DomainExamplesLoadError(
                f"{source}: accepted_example nao e JSON valido: {e}"
            ) from e
        try:
            LiteratureChunkOutput.model_validate(payload)
        except Exception as e:
            raise DomainExamplesLoadError(
                f"{source}: accepted_example nao valida contra LiteratureChunkOutput: {e}"
            ) from e
