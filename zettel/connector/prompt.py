"""Prompt 2 (``permanent_note.md``): payload, cached call, parsing and the PT-BR guard."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from zettel.config import AppConfig
from zettel.llm import (
    LLMUnavailableError,
    PromptParts,
    cached_call_llm,
    call_llm,
    fill_template,
    is_llm_unavailable,
    load_prompt_parts,
    parse_llm_json,
)
from zettel.schemas import (
    NoteWriteOutput,
    PermanentNoteCandidate,
    PermanentNoteLLMOutput,
    RelationshipResult,
    RelationType,
)
from zettel.state import StateDB

logger = logging.getLogger(__name__)


class ConnectRejected(RuntimeError):
    """Prompt 2 declined to write a permanent note. ``reason`` is the model text."""

    def __init__(self, message: str, *, reason: str = "", category: str = "") -> None:
        super().__init__(message)
        self.reason = (reason or message).strip()
        self.category = (category or "").strip()


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


def note_write_messages(
    cfg: AppConfig,
    prompt_parts: PromptParts,
    cand: PermanentNoteCandidate,
    *,
    anchor_quote: str,
    source_excerpt: str,
) -> tuple[str, str]:
    """Fill ``note_write.md``. The approved candidate, the anchor and the excerpt."""
    mapping = {
        "language": cfg.language,
        "domain": cfg.domain.name,
        "thesis": cand.thesis,
        "definition": cand.definition,
        "intuition": cand.intuition or "",
        "limits": cand.limits or "",
        "anchor_quote": anchor_quote,
        "source_excerpt": source_excerpt,
    }
    system = fill_template(prompt_parts.system, mapping) if prompt_parts.system else ""
    return system, fill_template(prompt_parts.user_template, mapping)


def note_link_messages(
    cfg: AppConfig,
    prompt_parts: PromptParts,
    *,
    thesis: str,
    definition: str,
    rag_context: str,
) -> tuple[str, str]:
    """Fill ``note_link.md`` with the thesis and definition already written."""
    mapping = {
        "language": cfg.language,
        "domain": cfg.domain.name,
        "thesis": thesis,
        "definition": definition,
        "rag_context": rag_context,
    }
    system = fill_template(prompt_parts.system, mapping) if prompt_parts.system else ""
    return system, fill_template(prompt_parts.user_template, mapping)


def thesis_similarity(approved: str, written: str) -> float:
    """Character similarity of the approved thesis and the one about to be stored."""
    from difflib import SequenceMatcher

    from zettel.hashing import fold_for_match

    left = fold_for_match(approved or "")
    right = fold_for_match(written or "")
    if not left and not right:
        return 1.0
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left, right).ratio()


def clip_source_excerpt(text: str, limit: int) -> str:
    """The source passage the write prompt may see, cut on the configured budget."""
    body = (text or "").strip()
    if limit <= 0 or len(body) <= limit:
        return body
    return body[:limit].rstrip() + "..."


# The prompt allows 0-4 connections. A fifth does not fail the note.
MAX_LLM_CONNECTIONS = 4


def coerce_llm_connections(raw: Any) -> list[RelationshipResult]:
    """Keep well-typed connections. A bad item is dropped, not fatal.

    Invalid ``relation_type``, a repeated target and anything past the cap of 4
    are warnings. Whether the target was in the RAG block is decided later, in
    ``resolve_connections``, which sees that block.
    """
    if not isinstance(raw, list):
        if raw:
            logger.warning("connections nao e uma lista; nenhuma conexao foi mantida")
        return []
    from zettel.vault import normalize_note_id

    kept: list[RelationshipResult] = []
    seen: set[str] = set()
    for item in raw:
        if len(kept) >= MAX_LLM_CONNECTIONS:
            logger.warning("Conexao alem do teto %d descartada", MAX_LLM_CONNECTIONS)
            break
        if not isinstance(item, dict):
            logger.warning("Conexao descartada: item nao e objeto")
            continue
        note_id = normalize_note_id(str(item.get("related_note_id") or ""))
        if not note_id or note_id in seen:
            logger.warning(
                "Conexao descartada: alvo ausente ou repetido (%r)",
                item.get("related_note_id"),
            )
            continue
        try:
            relation = RelationType(str(item.get("relation_type") or ""))
        except ValueError:
            logger.warning("Conexao descartada: tipo invalido %r", item.get("relation_type"))
            continue
        seen.add(note_id)
        kept.append(
            RelationshipResult(
                related_note_id=note_id,
                relation_type=relation,
                description=str(item.get("description") or ""),
            )
        )
    return kept


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
    force: bool = False,
    subject_id: str = "",
) -> tuple[PermanentNoteLLMOutput, bool]:
    """Run Prompt 2 through the SQLite response cache. Returns ``(output, cache_hit)``.

    Raises ``ConnectRejected`` when the model declines the concept. A response
    that does not parse is removed from the cache and recorded in ``llm_failures``.
    The PT-BR guard runs on accepted output that slipped into English.
    """
    system, user = prompt2_messages(cfg, prompt_parts, cand, payload)
    if force:
        user += "\n\nEsta chamada foi forcada: nao recuse o conceito. Escreva a nota."
    slot: dict[str, str] = {}
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
        cache_slot=slot,
    )

    try:
        output = parse_permanent_note_output(response_text)
    except Exception as exc:
        checksum = slot.get("checksum") or ""
        if checksum:
            db.delete_llm_cache([checksum])
        db.record_llm_failure(
            "connect",
            str(exc),
            response_text,
            subject_id=subject_id or label,
            call_checksum=checksum,
            model=slot.get("model") or "",
        )
        raise
    if output.status == "rejected":
        logger.warning("%s nao gerou nota permanente valida. Motivo: %s", label, output.reason)
        raise ConnectRejected(
            output.reason or "sem motivo informado",
            reason=output.reason or "",
            category=output.category,
        )

    if needs_ptbr_fix(f"{output.thesis} {output.definition} {output.intuition}"):
        output = apply_ptbr_guard(cfg, db, llm, output)
    return output, cache_hit


def _drop_bad_connect_cache(
    db: StateDB, slot: dict, response_text: str, subject_id: str, exc: Exception
) -> None:
    """A bad JSON answer is not reused. The raw text stays in llm_failures."""
    checksum = slot.get("checksum") or ""
    if checksum:
        db.delete_llm_cache([checksum])
    db.record_llm_failure(
        "connect",
        str(exc),
        response_text,
        subject_id=subject_id,
        call_checksum=checksum,
        model=slot.get("model") or "",
    )


def generate_note_links(
    cfg: AppConfig,
    db: StateDB,
    llm: Any,
    *,
    thesis: str,
    definition: str,
    rag_context: str,
    label: str,
    step: int = 0,
    total: int = 0,
    subject_id: str = "",
) -> tuple[list[RelationshipResult], bool]:
    """One link-prompt call. Reconnect uses this; the write prompt is not repeated."""
    link_parts = load_prompt_parts(cfg.prompts_path / "note_link.md")
    system, user = note_link_messages(
        cfg,
        link_parts,
        thesis=thesis,
        definition=definition,
        rag_context=rag_context,
    )
    link_slot: dict[str, str] = {}
    link_text, link_hit = cached_call_llm(
        cfg,
        db,
        "connect",
        link_parts.full_template,
        system,
        user,
        get_client=lambda: llm,
        call=call_llm,
        label=label,
        step=step,
        total=total,
        cache_slot=link_slot,
    )
    try:
        parsed = parse_llm_json(link_text)
        if not isinstance(parsed, dict):
            raise TypeError("JSON do prompt de ligacao nao e objeto")
        return coerce_llm_connections(parsed.get("connections")), link_hit
    except Exception as exc:
        _drop_bad_connect_cache(db, link_slot, link_text, subject_id, exc)
        raise


def generate_split_note(
    cfg: AppConfig,
    db: StateDB,
    llm: Any,
    cand: PermanentNoteCandidate,
    payload: Prompt2Payload,
    *,
    source_text: str,
    anchor_quote: str,
    step: int = 0,
    total: int = 0,
    subject_id: str = "",
) -> tuple[PermanentNoteLLMOutput, bool, float]:
    """Write the note from the source, then link the written thesis. Two calls.

    The similarity is the approved thesis against the written one. The caller
    decides whether that number is low enough to flag; this function never drops
    the note.
    """
    excerpt = clip_source_excerpt(source_text, cfg.linking.write_source_chars)
    write_parts = load_prompt_parts(cfg.prompts_path / "note_write.md")
    system, user = note_write_messages(
        cfg, write_parts, cand, anchor_quote=anchor_quote, source_excerpt=excerpt
    )
    write_slot: dict[str, str] = {}
    write_text, write_hit = cached_call_llm(
        cfg,
        db,
        "connect",
        write_parts.full_template,
        system,
        user,
        get_client=lambda: llm,
        call=call_llm,
        label=f"connect-write:{subject_id}",
        step=step,
        total=total,
        cache_slot=write_slot,
    )
    try:
        written = NoteWriteOutput.model_validate(parse_llm_json(write_text))
    except Exception as exc:
        _drop_bad_connect_cache(db, write_slot, write_text, subject_id, exc)
        raise
    draft = PermanentNoteLLMOutput(
        status="ok",
        title=written.title,
        thesis=written.thesis,
        definition=written.definition,
        intuition=written.intuition,
        example=written.example,
        limits=written.limits,
        tags=list(cand.tags),
    )
    if needs_ptbr_fix(" ".join(getattr(draft, field) for field in _GUARDED_FIELDS)):
        draft = apply_ptbr_guard(cfg, db, llm, draft)
    draft.connections, link_hit = generate_note_links(
        cfg,
        db,
        llm,
        thesis=draft.thesis,
        definition=draft.definition,
        rag_context=payload.rag_context,
        label=f"connect-link:{subject_id}",
        step=step,
        total=total,
        subject_id=subject_id,
    )
    return draft, write_hit and link_hit, thesis_similarity(cand.thesis, draft.thesis)


def parse_permanent_note_output(text: str) -> PermanentNoteLLMOutput:
    """Parse LLM response into PermanentNoteLLMOutput.

    The body fields are optional in the schema because a rejected concept answers
    with ``status``/``reason``/``category`` only. An *accepted* answer without a
    body is a broken response, not an empty note — reject it here. Connections
    are validated one by one: a bad relation does not reject the note.
    """
    data = parse_llm_json(text)
    if not isinstance(data, dict):
        raise TypeError("resposta do Prompt 2 nao e um objeto JSON")
    raw_connections = data.pop("connections", [])
    output = PermanentNoteLLMOutput(**data)
    output.connections = coerce_llm_connections(raw_connections)
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
    cfg: AppConfig, db: StateDB, llm: Any, output: PermanentNoteLLMOutput
) -> PermanentNoteLLMOutput:
    """Ask the LLM to return the textual fields corrected to PT-BR, as JSON.

    Goes through the SQLite response cache, so a replay of Prompt 2 does not
    pay the guard again. A transport/auth/quota failure stops the phase
    (ADR-059). Any other failure keeps the original text.
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
        response_text, _hit = cached_call_llm(
            cfg,
            db,
            "connect",
            guard_parts.full_template,
            system,
            user,
            get_client=lambda: llm,
            call=call_llm,
            label="connect-ptbr-guard",
        )
        corrected = parse_llm_json(response_text)
        for field in _GUARDED_FIELDS:
            setattr(output, field, corrected.get(field, getattr(output, field)))
        output.ptbr_guard = True
    except LLMUnavailableError:
        raise
    except Exception as e:
        if is_llm_unavailable(e):
            raise LLMUnavailableError(str(e), phase="connect") from e
        logger.warning("Guardrail PT-BR falhou: %s", e)
    return output
