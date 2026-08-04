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
    compute_embedding_input_hash,
    compute_llm_call_checksum,
    extract_embeddable_text,
    normalize_text_for_hash,
    sha256_hex,
)
from zettel.index import VectorIndex
from zettel.llm import call_llm, extract_json, get_llm, load_prompt
from zettel.retrieval import RetrievedNote, Retriever
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


def _relation_type_value(relation_type: Any) -> str:
    """Normalize RelationType / str to the canonical string value.

    ``RelationType`` is a ``str, Enum`` hybrid, so ``isinstance(x, str)`` is True
    for members — but ``f"{RelationType.SUPPORTS}"`` renders as
    ``RelationType.SUPPORTS``, not ``supports``. Always prefer ``.value``.
    """
    from enum import Enum
    if isinstance(relation_type, Enum):
        return str(relation_type.value)
    return str(relation_type or "related")


# ── Public API ─────────────────────────────────────────────────────────


def run_connect(
    cfg: AppConfig, db: StateDB, idx: VectorIndex, candidates: list[dict]
) -> list[str]:
    """Generate permanent notes from approved candidates. Returns created note_ids."""
    from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, MofNCompleteColumn

    llm = get_llm(cfg)
    prompt_template = load_prompt(cfg.prompts_path / "permanent_note.md")
    retriever = Retriever(cfg, db, idx)

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

            note_id = _process_candidate(
                cfg, db, idx, llm, cand_dict, prompt_template, retriever
            )
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
    retriever: Retriever,
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

    # Prefer LLM-provided image ids; fall back to paths embedded in the source chunk.
    image_ids = list(getattr(cand, "relevant_image_ids", None) or [])
    if not image_ids:
        image_ids = _fallback_image_ids(db, cand_dict)
        if image_ids:
            cand.relevant_image_ids = image_ids

    query_text = f"{cand.thesis} {cand.definition}"
    similar = retriever.search_notes(
        query_text, topk=cfg.linking.topk, exclude_id=note_id
    ).hits
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
    images_context = _build_candidate_images_context(db, cand)
    filled = filled.replace("{images_context}", images_context)

    # Cache do Prompt 2 (a chamada mais cara do pipeline). A chave cobre todo o prompt
    # preenchido (tese/definicao/RAG/etc.), entao um re-connect apos falha nao paga de novo.
    prompt_hash = sha256_hex(prompt_template)
    filled_hash = sha256_hex(normalize_text_for_hash(filled))
    call_checksum = compute_llm_call_checksum(
        prompt_hash, filled_hash, cfg.llm.model, cfg.llm.temperature, cfg.language,
    )
    try:
        cached = db.get_cached_llm_response(call_checksum)
        if cached is not None:
            logger.debug("Cache hit (Prompt 2) para conceito %s", concept_id)
            response_text = cached
        else:
            response_text = call_llm(llm, filled)
            db.cache_llm_response(
                call_checksum, json.dumps({"prompt": filled}, ensure_ascii=False), response_text
            )
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
    images = _resolve_images(db, image_ids)

    body = build_permanent_note_body(
        thesis=note_output.thesis,
        definition=note_output.definition,
        intuition=note_output.intuition,
        example=note_output.example,
        limits=note_output.limits,
        connections=resolved_connections,
        literature_ref=literature_ref,
        source_locator=cand.source_locator or "",
        images=images,
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
        "origin": "pipeline",
        "created_at": now,
        "updated_at": now,
    }

    title = note_output.title or cand.thesis[:60]
    filename = note_filename("ZTL", note_id, title)
    note_path = cfg.vault_path / "30_Permanent" / filename
    safe_write_note(note_path, meta, body)

    embeddable = extract_embeddable_text(body)
    semantic_checksum = sha256_hex(normalize_text_for_hash(embeddable))

    # Retencao: persiste o corpo completo e o frontmatter da ZTL no SQLite, permitindo
    # reconstruir o arquivo .md byte-a-byte sem reprocessar o LLM (ver `zettel rebuild`).
    db.upsert_note(
        note_id=note_id, source_id=source_id, path=str(note_path),
        title=title, note_semantic_checksum=semantic_checksum,
        embedding_model=cfg.embedding.model,
        body=body, frontmatter_json=json.dumps(meta, ensure_ascii=False),
        origin="pipeline",
    )
    db.upsert_concept(
        concept_id, source_id, cand_dict["chunk_id"], note_id=note_id, status="noted",
    )

    # Skip re-embedding when the note's semantic content and embedding model are unchanged.
    emb_hash = compute_embedding_input_hash(
        semantic_checksum, cfg.embedding.provider, cfg.embedding.model
    )
    existing_note = db.get_note(note_id)
    if not existing_note or existing_note.get("embedding_input_hash") != emb_hash:
        idx.upsert_permanent_note(note_id, embeddable, {
            "title": title, "source_id": source_id, "tags": ", ".join(tags),
            "note_semantic_checksum": semantic_checksum,
        })
        db.update_note_embedding(note_id, emb_hash, cfg.embedding.model)

    _persist_and_backlink(cfg, db, note_id, title, connections)

    return note_id


# ── Connection Resolution ────────────────────────────────────────────


def _resolve_images(db: StateDB, image_ids: list[str] | None) -> list[dict]:
    """Resolve candidate.relevant_image_ids into {path, description} for the ZTL note."""
    if not image_ids:
        return []
    resolved: list[dict] = []
    for aid in image_ids:
        asset = db.get_asset(aid)
        if asset and asset.get("path"):
            resolved.append({"path": asset["path"], "description": asset.get("description") or ""})
    return resolved


def _fallback_image_ids(db: StateDB, cand_dict: dict) -> list[str]:
    """When the LLM left relevant_image_ids empty, use image paths present in the chunk text."""
    from zettel.assets import asset_ids_in_text

    chunk_id = cand_dict.get("chunk_id") or ""
    source_id = cand_dict.get("source_id") or ""
    chunk = db.get_chunk(chunk_id) if chunk_id else None
    if not chunk:
        return []
    return asset_ids_in_text(db, source_id, chunk.get("text") or "")


def _build_candidate_images_context(db: StateDB, cand: PermanentNoteCandidate) -> str:
    """Describe relevant images for Prompt 2 (empty string when none)."""
    ids = list(getattr(cand, "relevant_image_ids", None) or [])
    if not ids:
        return ""
    lines: list[str] = []
    for aid in ids:
        asset = db.get_asset(aid)
        if not asset:
            continue
        desc = asset.get("description") or "(sem descricao)"
        lines.append(f"- {aid}: {desc}")
    if not lines:
        return ""
    return (
        "Figuras essenciais ao conceito (ja serao embutidas na nota; use-as na "
        "definicao/exemplo quando iluminarem o mecanismo):\n" + "\n".join(lines)
    )


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
            "relation_type": _relation_type_value(conn.relation_type),
            "description": conn.description,
        })
    return resolved


# ── RAG Context ───────────────────────────────────────────────────────


def _build_rag_context(similar_notes: list[RetrievedNote]) -> str:
    """Build RAG context from retrieved notes, split into two provenance groups.

    Search seeds (hop 0) and graph neighbours (hop >= 1) are rendered under
    separate headings so the LLM can weigh a typed connection (e.g. contradicts)
    differently from a plain embedding match when proposing connections.
    """
    if not similar_notes:
        return "Nenhuma nota existente encontrada."

    embedding_hits = [n for n in similar_notes if n.hop == 0]
    graph_hits = [n for n in similar_notes if n.hop >= 1]

    parts: list[str] = []

    if embedding_hits:
        parts.append("### Similares por embedding")
        for n in embedding_hits:
            title = n.title or n.metadata.get("title", "Sem titulo")
            doc = (n.document or "")[:150]
            tags = n.metadata.get("tags", "")
            parts.append(
                f"- **[[ZTL - {n.note_id} - {_slug(title)}]]**: {doc}... (tags: {tags})"
            )

    if graph_hits:
        parts.append("")
        parts.append("### Vizinhas por conexao no grafo")
        for n in graph_hits:
            title = n.title or n.metadata.get("title", "Sem titulo")
            doc = (n.document or "")[:150]
            rel = "related"
            anchor = ""
            if n.via:
                rel = n.via[-1].get("relation_type", "related")
                anchor = n.via[-1].get("from", "")
            anchor_txt = f" a partir de [[ZTL - {anchor}]]" if anchor else ""
            parts.append(
                f"- **[[ZTL - {n.note_id} - {_slug(title)}]]** "
                f"(relacao: {rel}{anchor_txt}): {doc}..."
            )

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
        relation_type = _relation_type_value(conn.relation_type)

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
