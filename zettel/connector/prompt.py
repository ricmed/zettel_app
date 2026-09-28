"""Prompt 2 (``permanent_note.md``): payload, cached call, parsing and the PT-BR guard."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from zettel.config import AppConfig, llm_phase
from zettel.llm import (
    PromptParts,
    cached_call_llm,
    call_llm,
    fill_template,
    load_prompt_parts,
    parse_llm_json,
)
from zettel.schemas import PermanentNoteCandidate, PermanentNoteLLMOutput
from zettel.state import StateDB

logger = logging.getLogger(__name__)


class ConnectRejected(RuntimeError):
    """Prompt 2 declined to write a permanent note. ``reason`` is the model text."""

    def __init__(self, message: str, *, reason: str = ""):
        super().__init__(message)
        self.reason = (reason or message).strip()


@dataclass(frozen=True)
class Prompt2Payload:
    """Everything Prompt 2 receives besides the candidate itself."""

    source_id: str
    literature_ref: str
    rag_context: str
    images_context: str
    examples: dict[str, str]


def format_judgement(items: list[str]) -> str:
    """Render an author-judgement list for the Prompt 2 payload."""
    return "; ".join(items) if items else "(nenhuma)"


def prompt2_messages(
    cfg: AppConfig,
    prompt_parts: PromptParts,
    cand: PermanentNoteCandidate,
    payload: Prompt2Payload,
) -> tuple[str, str]:
    """Fill ``permanent_note.md``. Returns ``(system, user)``.

    SECURITY NOTE: candidate fields originate from LLM output derived from
    user-supplied files. Sanitize prompt delimiters before interpolation if
    untrusted input is expected, to reduce prompt-injection risk.
    """
    mapping = {
        "language": cfg.language,
        "domain": cfg.domain.name,
        "thesis": cand.thesis,
        "definition": cand.definition,
        "intuition": cand.intuition or "",
        "limits": cand.limits or "",
        "decision_rules": format_judgement(cand.decision_rules),
        "anti_patterns": format_judgement(cand.anti_patterns),
        "named_frameworks": format_judgement(cand.named_frameworks),
        "source_id": payload.source_id,
        "source_locator": cand.source_locator or "",
        "literature_ref": payload.literature_ref,
        "rag_context": payload.rag_context,
        "images_context": payload.images_context,
        "thesis_examples": payload.examples.get("thesis_examples", ""),
        "decision_examples": payload.examples.get("decision_examples", ""),
    }
    system = fill_template(prompt_parts.system, mapping) if prompt_parts.system else ""
    return system, fill_template(prompt_parts.user_template, mapping)


def generate_permanent_note(
    cfg: AppConfig,
    db: StateDB,
    llm: Any,
    prompt_parts: PromptParts,
    cand: PermanentNoteCandidate,
    payload: Prompt2Payload,
    *,
    label: str,
    step: int | None = None,
    total: int | None = None,
) -> tuple[PermanentNoteLLMOutput, bool]:
    """Run Prompt 2 through the SQLite response cache. Returns ``(output, cache_hit)``.

    Raises ``ConnectRejected`` when the model declines the concept. The PT-BR
    guard runs on accepted output that slipped into English.
    """
    system, user = prompt2_messages(cfg, prompt_parts, cand, payload)
    response_text, cache_hit = cached_call_llm(
        cfg,
        db,
        "connect",
        prompt_parts.full_template,
        system,
        user,
        get_client=lambda: llm,
        call=call_llm,
        label=label,
        step=step,
        total=total,
    )

    output = parse_permanent_note_output(response_text)
    if output.status == "rejected":
        logger.warning("%s nao gerou nota permanente valida. Motivo: %s", label, output.reason)
        raise ConnectRejected(output.reason or "sem motivo informado", reason=output.reason or "")

    if needs_ptbr_fix(f"{output.thesis} {output.definition} {output.intuition}"):
        output = apply_ptbr_guard(cfg, llm, output)
    return output, cache_hit


def parse_permanent_note_output(text: str) -> PermanentNoteLLMOutput:
    """Parse LLM response into PermanentNoteLLMOutput.

    The body fields are optional in the schema because a rejected concept answers
    with ``status``/``reason``/``category`` only. An *accepted* answer without a
    body is a broken response, not an empty note — reject it here.
    """
    output = PermanentNoteLLMOutput(**parse_llm_json(text))
    if output.status != "rejected":
        missing = [f for f in ("title", "thesis", "definition") if not getattr(output, f).strip()]
        if missing:
            raise ValueError(f"Nota aceita sem campos obrigatorios: {', '.join(missing)}")
    return output


# ── PT-BR guard ───────────────────────────────────────────────────────

_ENGLISH_MARKERS = re.compile(r"\b(the|and|this|that|with|from|which|where)\b")
_PTBR_FIX_MIN_MARKERS = 3
_GUARDED_FIELDS = ("thesis", "definition", "intuition", "example", "limits")


def needs_ptbr_fix(text: str) -> bool:
    """Heuristic: at least three distinct English function words in the text."""
    return len(set(_ENGLISH_MARKERS.findall(text.lower()))) >= _PTBR_FIX_MIN_MARKERS


def apply_ptbr_guard(
    cfg: AppConfig, llm: Any, output: PermanentNoteLLMOutput
) -> PermanentNoteLLMOutput:
    """Ask the LLM to return the textual fields corrected to PT-BR, as JSON.

    Sends the fields as a JSON object and expects the same keys back, preserving
    the structure of PermanentNoteLLMOutput. A failure keeps the original text.
    """
    try:
        guard_parts = load_prompt_parts(cfg.prompts_path / "ptbr_guard.md")
        note_json = json.dumps(
            {field: getattr(output, field) for field in _GUARDED_FIELDS},
            ensure_ascii=False,
            indent=2,
        )
        mapping = {"text": note_json}
        system = fill_template(guard_parts.system, mapping) if guard_parts.system else ""
        user = fill_template(guard_parts.user_template, mapping)
        corrected = parse_llm_json(
            call_llm(
                llm,
                user,
                system=system or None,
                provider=llm_phase(cfg, "connect").provider,
                prompt_cache=cfg.llm.prompt_cache,
            )
        )
        for field in _GUARDED_FIELDS:
            setattr(output, field, corrected.get(field, getattr(output, field)))
    except Exception as e:
        logger.warning("Guardrail PT-BR falhou: %s", e)
    return output
