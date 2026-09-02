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

from zettel.config import AppConfig, llm_phase
from zettel.hashing import (
    compute_embedding_input_hash,
    compute_llm_call_checksum,
    extract_embeddable_text,
    normalize_text_for_hash,
    sha256_hex,
)
from zettel.index import VectorIndex
from zettel.llm import (
    PromptParts,
    call_llm,
    extract_json,
    fill_template,
    get_llm,
    load_prompt_parts,
)
from zettel.retrieval import RetrievedNote, Retriever
from zettel.schemas import PermanentNoteCandidate, PermanentNoteLLMOutput, RelationshipResult
from zettel.state import StateDB
from zettel.vault import (
    build_permanent_note_body,
    note_filename,
    normalize_note_id,
    permanent_wikilink,
    read_managed_block,
    safe_update_managed_blocks,
    safe_write_note,
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


def _literature_ref_for_chunk(
    cfg: AppConfig,
    db: StateDB,
    source_id: str,
    citekey: str,
    title_src: str,
    chunk_id: str | None,
) -> str:
    """Wikilink to the approved granular LIT for this chunk (fallback: index)."""
    from zettel.vault import literature_chunk_wikilink_for_row, literature_index_stem

    if chunk_id:
        chunk = db.get_chunk(chunk_id)
        if chunk and chunk.get("status") in ("approved", "persisted"):
            return literature_chunk_wikilink_for_row(citekey, chunk)
    return f"[[{literature_index_stem(citekey, title_src)}]]"


# ── Public API ─────────────────────────────────────────────────────────


def run_connect(
    cfg: AppConfig, db: StateDB, idx: VectorIndex, candidates: list[dict], *,
    observer=None, origin: str = "pipeline",
) -> list[str]:
    """Generate permanent notes from approved candidates. Returns created note_ids.

    ``origin`` is stamped on every note produced: the manual LIT-to-ZTL path reuses
    this same machinery but must stay distinguishable from pipeline output.
    """
    from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, MofNCompleteColumn

    from zettel.usage import begin_run, finish_pipeline_run, get_tracker, set_source
    from zettel.vault import sync_source_costs_to_vault

    run_id = db.start_run("connect")
    begin_run(run_id)

    llm = get_llm(cfg, "connect")
    prompt_parts = load_prompt_parts(cfg.prompts_path / "permanent_note.md")
    retriever = Retriever(cfg, db, idx)

    created_ids: list[str] = []
    total = len(candidates)
    from zettel.progress import report
    report(observer, "connect", f"{total} candidato(s) aprovado(s).", total_items=total)

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
            set_source(cand_dict.get("source_id"))
            progress.update(task, description=f"nota {i}/{total}", advance=1)
            report(
                observer, "connect", f"Gerando nota {i}/{total}.",
                current_item=cand.thesis[:80], current_index=i, total_items=total,
            )
            logger.info("Gerando nota %d/%d: %s", i, total, cand.thesis[:50])

            note_id = _process_candidate(
                cfg, db, idx, llm, cand_dict, prompt_parts, retriever,
                step=i, total=total, origin=origin,
            )
            if note_id:
                created_ids.append(note_id)
                logger.info("Nota %d/%d OK (id=%s)", i, total, note_id)

    set_source(None)
    tracker = get_tracker()
    if tracker:
        for sid in tracker.sources_touched():
            db.add_source_usage(sid, tracker.summary_for_source(sid).as_dict())
            sync_source_costs_to_vault(cfg, db, sid)

    logger.info("Notas permanentes criadas/atualizadas: %d", len(created_ids))
    finish_pipeline_run(db, run_id)
    return created_ids


# ── Candidate Processing ──────────────────────────────────────────────


def _process_candidate(
    cfg: AppConfig,
    db: StateDB,
    idx: VectorIndex,
    llm: Any,
    cand_dict: dict,
    prompt_parts: PromptParts,
    retriever: Retriever,
    *,
    step: int | None = None,
    total: int | None = None,
    origin: str = "pipeline",
) -> str | None:
    """Process a single candidate into a permanent note."""
    from zettel.usage import clear_progress, set_progress

    cand: PermanentNoteCandidate = cand_dict["candidate"]
    source_id = cand_dict["source_id"]
    concept_id = cand_dict["concept_id"]
    refines_note_id = cand_dict.get("refines_note_id")

    if step is not None:
        set_progress(step, total, "nota")

    existing_concept = db.get_concept(concept_id)
    if existing_concept and existing_concept.get("note_id"):
        note_id = existing_concept["note_id"]
        logger.debug("Conceito %s ja tem nota %s, atualizando", concept_id, note_id)
    else:
        note_id = str(ULID())

    source = db.get_source(source_id)
    citekey = source["citekey"] if source else "unknown"
    title_src = source["title"] if source else ""
    literature_ref = _literature_ref_for_chunk(
        cfg, db, source_id, citekey, title_src, cand_dict.get("chunk_id"),
    )

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
    rag_context = _build_rag_context(db, similar)

    # SECURITY NOTE: cand.thesis, cand.definition, and other candidate fields originate
    # from LLM output derived from user-supplied files. Sanitize prompt delimiters
    # (e.g. strip "---", "</s>", "###SYSTEM") before interpolation if untrusted input
    # is expected, to reduce prompt-injection risk.
    images_context = _build_candidate_images_context(db, cand)
    mapping = {
        "language": cfg.language,
        "domain": cfg.gardener.domain or "Geral",
        "thesis": cand.thesis,
        "definition": cand.definition,
        "intuition": cand.intuition or "",
        "limits": cand.limits or "",
        "source_id": source_id,
        "source_locator": cand.source_locator or "",
        "literature_ref": literature_ref,
        "rag_context": rag_context,
        "images_context": images_context,
    }
    system = fill_template(prompt_parts.system, mapping) if prompt_parts.system else ""
    user = fill_template(prompt_parts.user_template, mapping)
    filled_for_hash = f"{system}\n{user}" if system else user

    # Cache do Prompt 2 (a chamada mais cara do pipeline). A chave cobre todo o prompt
    # preenchido (tese/definicao/RAG/etc.), entao um re-connect apos falha nao paga de novo.
    from zettel.usage import get_tracker

    spec = llm_phase(cfg, "connect")
    prompt_hash = sha256_hex(prompt_parts.full_template)
    filled_hash = sha256_hex(normalize_text_for_hash(filled_for_hash))
    call_checksum = compute_llm_call_checksum(
        prompt_hash, filled_hash, spec.model, cfg.llm.temperature, cfg.language,
    )
    tracker = get_tracker()
    snap = tracker.summary().as_dict() if tracker else {}
    cache_hit = False
    try:
        cached = db.get_cached_llm_response(call_checksum)
        if cached is not None:
            logger.debug("Cache hit (Prompt 2) para conceito %s", concept_id)
            from zettel.usage import record_cache_hit
            record_cache_hit(label=f"connect:{concept_id}", model=spec.model)
            response_text = cached
            cache_hit = True
        else:
            response_text = call_llm(
                llm,
                user,
                system=system or None,
                label=f"connect:{concept_id}",
                step=step,
                total=total,
                provider=spec.provider,
                prompt_cache=cfg.llm.prompt_cache,
            )
            db.cache_llm_response(
                call_checksum,
                json.dumps({"system": system, "user": user}, ensure_ascii=False),
                response_text,
            )
        note_output = _parse_permanent_note_output(response_text)
        if note_output.status == "rejected":
            logger.warning(
                "Conceito %s não gerou nota permanente válida. Motivo: %s",
                concept_id, note_output.reason,
            )
            clear_progress()
            return None
    except Exception as e:
        logger.error("Erro ao gerar nota permanente para conceito %s: %s", concept_id, e)
        clear_progress()
        return None

    # PT-BR guard: re-translate fields that slipped into English
    note_body_text = f"{note_output.thesis} {note_output.definition} {note_output.intuition}"
    if _needs_ptbr_fix(note_body_text):
        note_output = _apply_ptbr_guard(cfg, llm, note_output)

    after = tracker.summary().as_dict() if tracker else {}
    note_cost = float(after.get("cost_usd_llm", 0) or 0) - float(snap.get("cost_usd_llm", 0) or 0)
    note_tokens_in = int(after.get("tokens_prompt", 0) or 0) - int(snap.get("tokens_prompt", 0) or 0)
    note_tokens_out = int(after.get("tokens_completion", 0) or 0) - int(
        snap.get("tokens_completion", 0) or 0
    )
    from zettel.usage import format_progress_from_context
    prog = format_progress_from_context()
    if prog:
        logger.info(
            "Nota permanente [%s] conceito=%s custo_usd=%.6f "
            "tokens_in=%d tokens_out=%d cache_hit=%s",
            prog, concept_id, note_cost, note_tokens_in, note_tokens_out, cache_hit,
        )
    else:
        logger.info(
            "Nota permanente conceito=%s custo_usd=%.6f tokens_in=%d tokens_out=%d cache_hit=%s",
            concept_id, note_cost, note_tokens_in, note_tokens_out, cache_hit,
        )

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
    title = note_output.title[:100] or cand.thesis[:60]
    meta = {
        "type": "permanent",
        "note_id": note_id,
        "title": title,
        "source_id": source_id,
        "literature_ref": literature_ref,
        "source_locator": cand.source_locator or "",
        "tags": tags,
        "origin": origin,
        "created_at": now,
        "updated_at": now,
        "llm_cost_usd": round(note_cost, 6),
        "llm_tokens_prompt": note_tokens_in,
        "llm_tokens_completion": note_tokens_out,
        "llm_cache_hit": cache_hit,
    }

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
        origin=origin,
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

    _persist_and_backlink(cfg, db, note_id, title, resolved_connections)

    clear_progress()
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


def _note_on_disk(record: dict | None) -> bool:
    """True when the note row points at a file that still exists."""
    if not record or not record.get("path"):
        return False
    return Path(record["path"]).is_file()


def _resolve_connections(db: StateDB, connections: list[RelationshipResult]) -> list[dict]:
    """Resolve LLM note_ids into wiki-links for vault rendering.

    Drops connections whose target is missing from SQLite or whose file is gone.
    Canonicalizes ``related_note_id`` (strips ``ZTL -`` / wikilink wrappers).
    """
    resolved: list[dict] = []
    seen: set[str] = set()
    for conn in connections:
        note_id = normalize_note_id(conn.related_note_id)
        if not note_id:
            logger.warning(
                "Conexao descartada: related_note_id=%r nao e um id utilizavel",
                conn.related_note_id,
            )
            continue
        if note_id in seen:
            continue
        note_record = db.get_note(note_id)
        if not _note_on_disk(note_record):
            logger.warning(
                "Conexao descartada: related_note_id=%r (canonico=%s) nao existe no vault",
                conn.related_note_id, note_id,
            )
            continue
        seen.add(note_id)
        wiki_link = permanent_wikilink(
            note_id,
            note_record.get("title", ""),
            path=note_record.get("path"),
        )
        resolved.append({
            "related_note_id": note_id,
            "wiki_link": wiki_link,
            "relation_type": _relation_type_value(conn.relation_type),
            "description": conn.description,
        })
    return resolved


# ── RAG Context ───────────────────────────────────────────────────────


def _build_rag_context(db: StateDB, similar_notes: list[RetrievedNote]) -> str:
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
            row = db.get_note(n.note_id)
            wiki = permanent_wikilink(
                n.note_id, title, path=row.get("path") if row else None,
            )
            parts.append(
                f"- note_id: {n.note_id} | **{wiki}**: {doc}... (tags: {tags})"
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
            anchor_txt = f" a partir de note_id: {anchor}" if anchor else ""
            row = db.get_note(n.note_id)
            wiki = permanent_wikilink(
                n.note_id, title, path=row.get("path") if row else None,
            )
            parts.append(
                f"- note_id: {n.note_id} | **{wiki}** "
                f"(relacao: {rel}{anchor_txt}): {doc}..."
            )

    return "\n".join(parts)


# ── Backlinking with typed relations ─────────────────────────────────


def _persist_and_backlink(
    cfg: AppConfig,
    db: StateDB,
    new_note_id: str,
    new_title: str,
    connections: list[dict],
) -> None:
    """Persist resolved connections to DB and rebuild auto-backlinks from the graph."""
    for conn in connections:
        target_id = conn["related_note_id"]
        db.upsert_note_connection(
            source_note_id=new_note_id,
            target_note_id=target_id,
            relation_type=conn.get("relation_type") or "related",
            description=conn.get("description") or "",
        )
        rebuild_auto_backlinks(db, target_id)
    rebuild_auto_backlinks(db, new_note_id)


def rebuild_auto_backlinks(db: StateDB, note_id: str) -> bool:
    """Replace ``auto-backlinks`` with incoming graph edges whose source file exists.

    Returns True when the vault file was written (including clearing a stale block).
    """
    record = db.get_note(note_id)
    if not _note_on_disk(record):
        return False
    path = Path(record["path"])
    incoming = [
        edge for edge in db.get_note_connections(note_id)
        if edge["target_note_id"] == note_id and edge["source_note_id"] != note_id
    ]

    lines: list[str] = []
    for edge in incoming:
        source = db.get_note(edge["source_note_id"])
        if not _note_on_disk(source):
            continue
        wiki = permanent_wikilink(
            edge["source_note_id"],
            source.get("title") or "",
            path=source.get("path"),
        )
        inverse = _inverse_relation(edge.get("relation_type") or "related")
        line = f"- {wiki} ({inverse})"
        description = edge.get("description") or ""
        if description:
            line += f" -- {description}"
        lines.append(line)

    content = path.read_text(encoding="utf-8")
    existing = read_managed_block(content, "auto-backlinks")
    inner = "\n".join(lines)
    if not lines and not existing:
        return False
    if existing is not None and existing.strip() == inner.strip():
        return False
    safe_update_managed_blocks(path, {"auto-backlinks": inner})
    return True


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
        guard_parts = load_prompt_parts(cfg.prompts_path / "ptbr_guard.md")
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
        mapping = {"text": note_json}
        system = fill_template(guard_parts.system, mapping) if guard_parts.system else ""
        user = fill_template(guard_parts.user_template, mapping)
        spec = llm_phase(cfg, "connect")
        corrected_raw = call_llm(
            llm,
            user,
            system=system or None,
            provider=spec.provider,
            prompt_cache=cfg.llm.prompt_cache,
        )
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
    """Parse LLM response into PermanentNoteLLMOutput.

    The body fields are optional in the schema because a rejected concept answers
    with ``status``/``reason``/``category`` only. An *accepted* answer without a
    body is a broken response, not an empty note — reject it here.
    """
    json_text = extract_json(text)
    data = json.loads(json_text)
    output = PermanentNoteLLMOutput(**data)
    if output.status != "rejected":
        missing = [
            f for f in ("title", "thesis", "definition")
            if not getattr(output, f).strip()
        ]
        if missing:
            raise ValueError(
                f"Nota aceita sem campos obrigatorios: {', '.join(missing)}"
            )
    return output
