"""The Connector — RAG-based linking, permanent note generation, backlinking.

Takes approved candidates from the Extractor, generates full permanent notes
using Prompt 2, and creates/updates vault files with managed backlink blocks.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from ulid import ULID

from zettel.config import AppConfig
from zettel.hashing import (
    extract_embeddable_text,
    normalize_text_for_hash,
    sha256_hex,
)
from zettel.index import VectorIndex
from zettel.schemas import PermanentNoteCandidate, PermanentNoteLLMOutput, RelationshipResult
from zettel.state import StateDB
from zettel.vault import (
    build_permanent_note_body,
    note_filename,
    safe_update_managed_blocks,
    safe_write_note,
    _slug,
)

logger = logging.getLogger(__name__)


# ── Public API ─────────────────────────────────────────────────────────


def run_connect(cfg: AppConfig, db: StateDB, idx: VectorIndex, candidates: list[dict]) -> list[str]:
    """Generate permanent notes from approved candidates. Returns created note_ids."""
    llm = _get_llm(cfg)
    prompt_template = _load_prompt(cfg.prompts_path / "permanent_note.md")

    created_ids: list[str] = []
    total = len(candidates)

    for i, cand_dict in enumerate(candidates, 1):
        cand: PermanentNoteCandidate = cand_dict["candidate"]
        logger.info("Gerando nota %d/%d: %s", i, total, cand.thesis[:50])

        note_id = _process_candidate(cfg, db, idx, llm, cand_dict, prompt_template)
        if note_id:
            created_ids.append(note_id)
            logger.info("Nota %d/%d OK (id=%s)", i, total, note_id)

    logger.info("Notas permanentes criadas/atualizadas: %d", len(created_ids))
    return created_ids


# ── Candidate Processing ──────────────────────────────────────────────


def _process_candidate(
    cfg: AppConfig, db: StateDB, idx: VectorIndex,
    llm: Any, cand_dict: dict, prompt_template: str,
) -> str | None:
    """Process a single candidate into a permanent note."""
    cand: PermanentNoteCandidate = cand_dict["candidate"]
    source_id = cand_dict["source_id"]
    concept_id = cand_dict["concept_id"]
    merge_target = cand_dict.get("merge_target")

    # Check if concept already has a note
    existing_concept = db.get_concept(concept_id)
    if existing_concept and existing_concept.get("note_id"):
        note_id = existing_concept["note_id"]
        logger.debug("Conceito %s já tem nota %s, atualizando", concept_id, note_id)
    elif merge_target:
        note_id = merge_target
        logger.info("Merge do conceito %s na nota %s", concept_id, note_id)
    else:
        note_id = str(ULID())

    # Build literature reference
    source = db.get_source(source_id)
    citekey = source["citekey"] if source else "unknown"
    title_src = source["title"] if source else ""
    literature_ref = f"[[LIT - @{citekey} - {_slug(title_src)}]]"

    # RAG: find related notes for connections
    query_text = f"{cand.thesis} {cand.definition}"
    similar = idx.query_similar_notes(query_text, n_results=cfg.linking.topk, exclude_id=note_id)
    rag_context = _build_rag_context(similar)

    # Fill prompt
    filled = prompt_template.replace("{thesis}", cand.thesis)
    filled = filled.replace("{definition}", cand.definition)
    filled = filled.replace("{intuition}", cand.intuition or "")
    filled = filled.replace("{limits}", cand.limits or "")
    filled = filled.replace("{source_id}", source_id)
    filled = filled.replace("{source_locator}", cand.source_locator or "")
    filled = filled.replace("{literature_ref}", literature_ref)
    filled = filled.replace("{rag_context}", rag_context)

    # Call LLM
    try:
        response_text = _call_llm(llm, filled)
        note_output = _parse_permanent_note_output(response_text)
    except Exception as e:
        logger.error("Erro ao gerar nota permanente para conceito %s: %s", concept_id, e)
        return None

    # PT-BR guard check
    note_body_text = f"{note_output.thesis} {note_output.definition} {note_output.intuition}"
    if _needs_ptbr_fix(note_body_text):
        note_output = _apply_ptbr_guard(cfg, llm, note_output)

    # Build note body
    body = build_permanent_note_body(
        thesis=note_output.thesis,
        definition=note_output.definition,
        intuition=note_output.intuition,
        example=note_output.example,
        limits=note_output.limits,
        connections_text=note_output.connections_text,
        literature_ref=literature_ref,
        source_locator=cand.source_locator or "",
    )

    # Build frontmatter
    from datetime import datetime
    now = datetime.now().isoformat()
    tags = note_output.tags or cand.tags
    meta = {
        "type": "permanent",
        "note_id": note_id,
        "source_id": source_id,
        "literature_ref": literature_ref,
        "source_locator": cand.source_locator or "",
        "tags": tags,
        "created_at": now,
        "updated_at": now,
    }

    # Write to vault
    title = note_output.title or cand.thesis[:60]
    filename = note_filename("ZTL", note_id, title)
    note_path = cfg.vault_path / "30_Permanent" / filename
    safe_write_note(note_path, meta, body)

    # Compute semantic checksum and update index
    embeddable = extract_embeddable_text(body)
    semantic_checksum = sha256_hex(normalize_text_for_hash(embeddable))

    db.upsert_note(
        note_id=note_id, source_id=source_id, path=str(note_path),
        title=title, note_semantic_checksum=semantic_checksum,
        embedding_model=cfg.embedding.model,
    )
    db.upsert_concept(concept_id, source_id, cand_dict["chunk_id"], note_id=note_id)

    idx.upsert_permanent_note(note_id, embeddable, {
        "title": title, "source_id": source_id, "tags": ", ".join(tags),
        "note_semantic_checksum": semantic_checksum,
    })

    # Backlinking: update related notes' managed blocks
    _update_backlinks(cfg, db, note_id, title, similar)

    return note_id


# ── RAG Context ───────────────────────────────────────────────────────


def _build_rag_context(similar_notes: list[dict]) -> str:
    """Build RAG context string from similar notes."""
    if not similar_notes:
        return "Nenhuma nota existente encontrada."

    parts: list[str] = []
    for n in similar_notes:
        nid = n.get("id", "?")
        meta = n.get("metadata", {})
        title = meta.get("title", "Sem título")
        doc = n.get("document", "")[:150]
        tags = meta.get("tags", "")
        parts.append(f"- **[[ZTL - {nid} - {_slug(title)}]]**: {doc}... (tags: {tags})")
    return "\n".join(parts)


# ── Backlinking ───────────────────────────────────────────────────────


def _update_backlinks(
    cfg: AppConfig, db: StateDB, new_note_id: str, new_title: str,
    similar_notes: list[dict],
) -> None:
    """Add backlinks to related notes' managed blocks."""
    for n in similar_notes:
        target_id = n.get("id")
        if not target_id:
            continue
        target_record = db.get_note(target_id)
        if not target_record or not target_record.get("path"):
            continue

        target_path = Path(target_record["path"])
        if not target_path.exists():
            continue

        new_link = f"- [[ZTL - {new_note_id} - {_slug(new_title)}]]"
        safe_update_managed_blocks(target_path, {
            "auto-backlinks": _merge_backlink(target_path, new_link),
        })


def _merge_backlink(path: Path, new_link: str) -> str:
    """Merge a new backlink into existing backlinks block."""
    from zettel.vault import read_managed_block
    content = path.read_text(encoding="utf-8")
    existing = read_managed_block(content, "auto-backlinks")
    if existing and new_link.strip() in existing:
        return existing
    if existing:
        return existing + "\n" + new_link
    return new_link


# ── PT-BR Guard ───────────────────────────────────────────────────────


def _needs_ptbr_fix(text: str) -> bool:
    """Simple heuristic: check if text has too many English words."""
    english_markers = ["the ", "and ", "this ", "that ", "with ", "from ", "which ", "where "]
    count = sum(1 for m in english_markers if m.lower() in text.lower())
    return count >= 3


def _apply_ptbr_guard(cfg: AppConfig, llm: Any, output: PermanentNoteLLMOutput) -> PermanentNoteLLMOutput:
    """Run the PT-BR guard on the note output."""
    try:
        guard_prompt = _load_prompt(cfg.prompts_path / "ptbr_guard.md")
        full_text = f"{output.thesis}\n{output.definition}\n{output.intuition}\n{output.example}\n{output.limits}"
        filled = guard_prompt.replace("{text}", full_text)
        corrected = _call_llm(llm, filled)
        # Re-parse corrected text into parts (best effort)
        lines = corrected.strip().split("\n")
        if len(lines) >= 2:
            output.thesis = lines[0]
            output.definition = "\n".join(lines[1:])
    except Exception as e:
        logger.warning("Guardrail PT-BR falhou: %s", e)
    return output


# ── LLM Helpers ───────────────────────────────────────────────────────


def _get_llm(cfg: AppConfig) -> Any:
    """Instantiate the configured LLM."""
    if cfg.llm.provider == "openai":
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(
            model=cfg.llm.model,
            temperature=cfg.llm.temperature,
            max_retries=cfg.llm.max_retries,
        )
    elif cfg.llm.provider == "anthropic":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(
            model=cfg.llm.model,
            temperature=cfg.llm.temperature,
            max_retries=cfg.llm.max_retries,
        )
    elif cfg.llm.provider == "ollama":
        from langchain_ollama import ChatOllama
        return ChatOllama(
            model=cfg.llm.model,
            temperature=cfg.llm.temperature,
        )
    else:
        raise ValueError(f"LLM provider não suportado: {cfg.llm.provider}")


def _call_llm(llm: Any, prompt: str) -> str:
    """Call the LLM and return the response text."""
    from langchain_core.messages import HumanMessage
    response = llm.invoke([HumanMessage(content=prompt)])
    return response.content


def _load_prompt(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"Prompt não encontrado: {path}")
    return path.read_text(encoding="utf-8")


def _parse_permanent_note_output(text: str) -> PermanentNoteLLMOutput:
    """Parse LLM response into PermanentNoteLLMOutput."""
    json_text = _extract_json(text)
    data = json.loads(json_text)
    return PermanentNoteLLMOutput(**data)


def _extract_json(text: str) -> str:
    match = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    text = text.strip()
    if text.startswith("{") or text.startswith("["):
        return text
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1:
        return text[start : end + 1]
    raise ValueError("Nenhum JSON encontrado na resposta do LLM")
