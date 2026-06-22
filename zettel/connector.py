"""The Connector — RAG-based linking, permanent note generation, backlinking.

Takes approved candidates from the Extractor, generates full permanent notes
using Prompt 2, and creates/updates vault files with managed backlink blocks.
Connections are typed (supports, contradicts, extends, etc.) and backlinks
show the inverse relation in PT-BR.
"""

from __future__ import annotations

import json
import logging
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
from zettel.llm import call_llm, extract_json, get_llm, load_prompt
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


# ── Inverse relation map (PT-BR) ─────────────────────────────────────

_INVERSE_RELATION: dict[str, str] = {
    "supports": "suportado por",
    "contradicts": "contradiz",
    "extends": "estendido por",
    "depends_on": "base para",
    "exemplifies": "exemplificado por",
    "related": "relacionado",
}


def _inverse_relation(relation_type: str) -> str:
    """Return the inverse relation label in PT-BR."""
    return _INVERSE_RELATION.get(relation_type, "relacionado")


# ── Public API ─────────────────────────────────────────────────────────


def run_connect(
    cfg: AppConfig, db: StateDB, idx: VectorIndex, candidates: list[dict]
) -> list[str]:
    """Generate permanent notes from approved candidates. Returns created note_ids."""
    from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, MofNCompleteColumn

    llm = get_llm(cfg)
    prompt_template = load_prompt(cfg.prompts_path / "permanent_note.md")

    created_ids: list[str] = []
    total = len(candidates)

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]Connect[/bold blue] {task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        transient=True,
    ) as progress:
        task = progress.add_task("notas", total=total)
        for i, cand_dict in enumerate(candidates, 1):
            cand: PermanentNoteCandidate = cand_dict["candidate"]
            progress.update(task, description=f"nota {i}/{total}", advance=1)
            logger.info("Gerando nota %d/%d: %s", i, total, cand.thesis[:50])

            note_id = _process_candidate(cfg, db, idx, llm, cand_dict, prompt_template)
            if note_id:
                created_ids.append(note_id)
                logger.info("Nota %d/%d OK (id=%s)", i, total, note_id)

    logger.info("Notas permanentes criadas/atualizadas: %d", len(created_ids))
    return created_ids


# ── Candidate Processing ──────────────────────────────────────────────


def _process_candidate(
    cfg: AppConfig,
    db: StateDB,
    idx: VectorIndex,
    llm: Any,
    cand_dict: dict,
    prompt_template: str,
) -> str | None:
    """Process a single candidate into a permanent note."""
    cand: PermanentNoteCandidate = cand_dict["candidate"]
    source_id = cand_dict["source_id"]
    concept_id = cand_dict["concept_id"]
    refines_note_id = cand_dict.get("refines_note_id")

    existing_concept = db.get_concept(concept_id)
    if existing_concept and existing_concept.get("note_id"):
        note_id = existing_concept["note_id"]
        logger.debug("Conceito %s ja tem nota %s, atualizando", concept_id, note_id)
    else:
        note_id = str(ULID())

    source = db.get_source(source_id)
    citekey = source["citekey"] if source else "unknown"
    title_src = source["title"] if source else ""
    literature_ref = f"[[LIT - @{citekey} - {_slug(title_src)}]]"

    query_text = f"{cand.thesis} {cand.definition}"
    similar = idx.query_similar_notes(query_text, n_results=cfg.linking.topk, exclude_id=note_id)
    rag_context = _build_rag_context(similar)

    # SECURITY NOTE: cand.thesis, cand.definition, and other candidate fields originate
    # from LLM output derived from user-supplied files. Sanitize prompt delimiters
    # (e.g. strip "---", "</s>", "###SYSTEM") before interpolation if untrusted input
    # is expected, to reduce prompt-injection risk.
    filled = prompt_template.replace("{thesis}", cand.thesis)
    filled = filled.replace("{definition}", cand.definition)
    filled = filled.replace("{intuition}", cand.intuition or "")
    filled = filled.replace("{limits}", cand.limits or "")
    filled = filled.replace("{source_id}", source_id)
    filled = filled.replace("{source_locator}", cand.source_locator or "")
    filled = filled.replace("{literature_ref}", literature_ref)
    filled = filled.replace("{rag_context}", rag_context)

    try:
        response_text = call_llm(llm, filled)
        note_output = _parse_permanent_note_output(response_text)
        if note_output.status == "rejected":
            logger.warning(
                "Conceito %s não gerou nota permanente válida. Motivo: %s",
                concept_id, note_output.reason,
            )
            return None
    except Exception as e:
        logger.error("Erro ao gerar nota permanente para conceito %s: %s", concept_id, e)
        return None

    # PT-BR guard: re-translate fields that slipped into English
    note_body_text = f"{note_output.thesis} {note_output.definition} {note_output.intuition}"
    if _needs_ptbr_fix(note_body_text):
        note_output = _apply_ptbr_guard(cfg, llm, note_output)

    connections = list(note_output.connections)

    # If this is a refine_existing candidate, inject an "extends" connection
    if refines_note_id:
        already_connected = any(c.related_note_id == refines_note_id for c in connections)
        if not already_connected:
            connections.append(RelationshipResult(
                related_note_id=refines_note_id,
                relation_type="extends",
                description=cand_dict.get("refine_reason", "Refina nota existente"),
            ))

    resolved_connections = _resolve_connections(db, connections)

    body = build_permanent_note_body(
        thesis=note_output.thesis,
        definition=note_output.definition,
        intuition=note_output.intuition,
        example=note_output.example,
        limits=note_output.limits,
        connections=resolved_connections,
        literature_ref=literature_ref,
        source_locator=cand.source_locator or "",
    )

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

    title = note_output.title or cand.thesis[:60]
    filename = note_filename("ZTL", note_id, title)
    note_path = cfg.vault_path / "30_Permanent" / filename
    safe_write_note(note_path, meta, body)

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

    _persist_and_backlink(cfg, db, note_id, title, connections)

    return note_id


# ── Connection Resolution ────────────────────────────────────────────


def _resolve_connections(db: StateDB, connections: list[RelationshipResult]) -> list[dict]:
    """Resolve note_ids into wiki-links for vault rendering."""
    resolved: list[dict] = []
    for conn in connections:
        note_record = db.get_note(conn.related_note_id)
        if note_record and note_record.get("title"):
            wiki_link = f"[[ZTL - {conn.related_note_id} - {_slug(note_record['title'])}]]"
        else:
            wiki_link = f"[[ZTL - {conn.related_note_id}]]"
        resolved.append({
            "related_note_id": conn.related_note_id,
            "wiki_link": wiki_link,
            "relation_type": (
                conn.relation_type
                if isinstance(conn.relation_type, str)
                else conn.relation_type.value
            ),
            "description": conn.description,
        })
    return resolved


# ── RAG Context ───────────────────────────────────────────────────────


def _build_rag_context(similar_notes: list[dict]) -> str:
    """Build RAG context string from similar notes."""
    if not similar_notes:
        return "Nenhuma nota existente encontrada."

    parts: list[str] = []
    for n in similar_notes:
        nid = n.get("id", "?")
        meta = n.get("metadata", {})
        title = meta.get("title", "Sem titulo")
        doc = n.get("document", "")[:150]
        tags = meta.get("tags", "")
        parts.append(f"- **[[ZTL - {nid} - {_slug(title)}]]**: {doc}... (tags: {tags})")
    return "\n".join(parts)


# ── Backlinking with typed relations ─────────────────────────────────


def _persist_and_backlink(
    cfg: AppConfig,
    db: StateDB,
    new_note_id: str,
    new_title: str,
    connections: list[RelationshipResult],
) -> None:
    """Persist connections to DB and update backlinks on related notes."""
    for conn in connections:
        target_id = conn.related_note_id
        relation_type = (
            conn.relation_type
            if isinstance(conn.relation_type, str)
            else conn.relation_type.value
        )

        db.upsert_note_connection(
            source_note_id=new_note_id,
            target_note_id=target_id,
            relation_type=relation_type,
            description=conn.description,
        )

        target_record = db.get_note(target_id)
        if not target_record or not target_record.get("path"):
            continue

        target_path = Path(target_record["path"])
        if not target_path.exists():
            continue

        inverse = _inverse_relation(relation_type)
        new_link = f"- [[ZTL - {new_note_id} - {_slug(new_title)}]] ({inverse})"
        if conn.description:
            new_link += f" -- {conn.description}"

        safe_update_managed_blocks(target_path, {
            "auto-backlinks": _merge_backlink(target_path, new_link),
        })


def _merge_backlink(path: Path, new_link: str) -> str:
    """Merge a new backlink into the existing backlinks block."""
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


def _apply_ptbr_guard(
    cfg: AppConfig, llm: Any, output: PermanentNoteLLMOutput
) -> PermanentNoteLLMOutput:
    """Run the PT-BR guard: ask the LLM to return corrected fields as JSON.

    Sends the textual fields as a JSON object and expects back a JSON object
    with the same keys corrected to PT-BR — preserving the full structure of
    PermanentNoteLLMOutput instead of destructuring free-text lines.
    """
    try:
        guard_prompt = load_prompt(cfg.prompts_path / "ptbr_guard.md")
        note_json = json.dumps(
            {
                "thesis": output.thesis,
                "definition": output.definition,
                "intuition": output.intuition,
                "example": output.example,
                "limits": output.limits,
            },
            ensure_ascii=False,
            indent=2,
        )
        filled = guard_prompt.replace("{text}", note_json)
        corrected_raw = call_llm(llm, filled)
        corrected_data = json.loads(extract_json(corrected_raw))
        output.thesis = corrected_data.get("thesis", output.thesis)
        output.definition = corrected_data.get("definition", output.definition)
        output.intuition = corrected_data.get("intuition", output.intuition)
        output.example = corrected_data.get("example", output.example)
        output.limits = corrected_data.get("limits", output.limits)
    except Exception as e:
        logger.warning("Guardrail PT-BR falhou: %s", e)
    return output


# ── Parsers ───────────────────────────────────────────────────────────


def _parse_permanent_note_output(text: str) -> PermanentNoteLLMOutput:
    """Parse LLM response into PermanentNoteLLMOutput."""
    json_text = extract_json(text)
    data = json.loads(json_text)
    return PermanentNoteLLMOutput(**data)
