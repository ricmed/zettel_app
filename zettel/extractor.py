"""The Extractor — LLM processing of chunks, LIT aggregation, deduplication.

Processes each pending chunk with Prompt 1 (literature extraction),
then runs semantic deduplication against existing permanent notes.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from ulid import ULID

from zettel.config import AppConfig
from zettel.hashing import (
    compute_llm_call_checksum,
    normalize_text_for_hash,
    sha256_hex,
    short_hash,
)
from zettel.index import VectorIndex
from zettel.schemas import (
    DedupeDecision,
    DedupeResult,
    LiteratureChunkOutput,
    PermanentNoteCandidate,
)
from zettel.state import StateDB
from zettel.vault import (
    note_filename,
    parse_frontmatter,
    read_managed_block,
    safe_update_managed_blocks,
    upsert_managed_block,
)

logger = logging.getLogger(__name__)


# ── Public API ─────────────────────────────────────────────────────────


def run_extract(cfg: AppConfig, db: StateDB, idx: VectorIndex) -> list[dict]:
    """Process all pending chunks. Returns list of approved candidates."""
    llm = _get_llm(cfg)
    prompt_template = _load_prompt(cfg.prompts_path / "literature_note.md")
    prompt_hash = sha256_hex(prompt_template)

    pending = db.get_pending_chunks()
    total = len(pending)
    logger.info("Chunks pendentes para extracao: %d", total)

    all_candidates: list[dict] = []
    outputs_by_source: dict[str, list[LiteratureChunkOutput]] = {}

    for i, chunk_row in enumerate(pending, 1):
        chunk_id = chunk_row["chunk_id"]
        source_id = chunk_row["source_id"]
        logger.info("Extraindo chunk %d/%d (%s)", i, total, chunk_id)

        candidates, output = _process_chunk(cfg, db, idx, llm, chunk_row, prompt_template, prompt_hash)
        all_candidates.extend(candidates)
        logger.info("Chunk %d/%d OK - %d candidatos", i, total, len(candidates))

        if output:
            outputs_by_source.setdefault(source_id, []).append(output)

    # Aggregate LIT notes per source
    _aggregate_literature_notes(cfg, db, outputs_by_source)

    # Deduplicate candidates
    approved = _deduplicate_candidates(cfg, db, idx, llm, all_candidates)
    logger.info("Candidatos aprovados apos deduplicacao: %d / %d", len(approved), len(all_candidates))

    return approved


# ── Chunk Processing ──────────────────────────────────────────────────


def _process_chunk(
    cfg: AppConfig, db: StateDB, idx: VectorIndex,
    llm: Any, chunk_row: dict, prompt_template: str, prompt_hash: str,
) -> tuple[list[dict], LiteratureChunkOutput | None]:
    """Process a single chunk with Prompt 1. Returns (candidate dicts, parsed output)."""
    chunk_id = chunk_row["chunk_id"]
    source_id = chunk_row["source_id"]
    chunk_text = chunk_row["text"]
    chunk_checksum = chunk_row["chunk_checksum"]

    # Check LLM call cache
    call_checksum = compute_llm_call_checksum(
        prompt_hash, chunk_checksum, cfg.llm.model, cfg.llm.temperature, cfg.language,
    )
    cached = db.get_cached_llm_response(call_checksum)
    if cached:
        logger.debug("Cache hit para chunk %s", chunk_id)
        response_text = cached
    else:
        # Get source info
        source = db.get_source(source_id)
        source_title = source["title"] if source else "Desconhecido"

        # Fill prompt
        filled = prompt_template.replace("{source_id}", source_id)
        filled = filled.replace("{source_title}", source_title)
        filled = filled.replace("{chapter_title}", chunk_row.get("locator", ""))
        filled = filled.replace("{locator}", chunk_row.get("locator", ""))
        filled = filled.replace("{chunk_text}", chunk_text)

        # Call LLM
        try:
            response_text = _call_llm(llm, filled)
            db.cache_llm_response(call_checksum, json.dumps({"prompt": filled[:200]}), response_text)
        except Exception as e:
            logger.error("Erro no LLM para chunk %s: %s", chunk_id, e)
            db.update_chunk_status(chunk_id, "failed")
            return [], None

    # Parse response
    try:
        output = _parse_literature_output(response_text)
    except Exception as e:
        logger.warning("Falha ao parsear output do chunk %s: %s -- tentando retry", chunk_id, e)
        try:
            retry_prompt = f"O JSON abaixo esta malformado. Corrija e retorne APENAS o JSON valido:\n\n{response_text}"
            response_text = _call_llm(llm, retry_prompt)
            output = _parse_literature_output(response_text)
        except Exception:
            logger.error("Chunk %s enviado para revisao manual", chunk_id)
            db.update_chunk_status(chunk_id, "failed")
            return [], None

    # Update LIT note
    _append_to_literature_note(cfg, db, source_id, chunk_id, output)

    # Mark chunk as extracted
    db.update_chunk_status(chunk_id, "extracted", prompt_hash, call_checksum)

    # Filter candidates by quality
    approved_cands, rejected_cands = _filter_candidates(output.candidates, cfg)
    if rejected_cands:
        logger.info(
            "Chunk %s: %d candidatos rejeitados pela filtragem de qualidade",
            chunk_id, len(rejected_cands),
        )

    # Build candidate dicts (only approved)
    candidates: list[dict] = []
    for cand in approved_cands:
        concept_id = _compute_concept_id(source_id, chunk_id, cand)
        candidates.append({
            "concept_id": concept_id,
            "source_id": source_id,
            "chunk_id": chunk_id,
            "candidate": cand,
        })
        # Persist concept
        anchor_hash = sha256_hex(normalize_text_for_hash(cand.anchor_quote)) if cand.anchor_quote else ""
        thesis_hash = sha256_hex(normalize_text_for_hash(cand.thesis))
        db.upsert_concept(concept_id, source_id, chunk_id, anchor_hash, thesis_hash)

    return candidates, output


# ── Candidate Filtering ──────────────────────────────────────────────


def _filter_candidates(
    candidates: list[PermanentNoteCandidate],
    cfg: AppConfig,
) -> tuple[list[PermanentNoteCandidate], list[PermanentNoteCandidate]]:
    """Filter candidates by structural quality rules.

    Returns (approved, rejected).
    """
    ext = cfg.extraction
    approved: list[PermanentNoteCandidate] = []
    rejected: list[PermanentNoteCandidate] = []

    for cand in candidates:
        reason = _check_candidate(cand, ext)
        if reason:
            logger.debug("Candidato rejeitado (%s): %s", reason, cand.thesis[:60])
            rejected.append(cand)
        else:
            approved.append(cand)

    return approved, rejected


def _check_candidate(
    cand: PermanentNoteCandidate,
    ext: Any,
) -> str | None:
    """Return rejection reason or None if candidate passes all checks."""
    if cand.chunk_status == "rejected":
        return f"chunk_status={cand.chunk_status}, rejection_reason={cand.rejection_reason}, rejection_category={cand.rejection_category}"
    if cand.relevance_score < ext.min_relevance_score:
        return f"relevance_score={cand.relevance_score} < {ext.min_relevance_score}"

    thesis_words = len(cand.thesis.split())
    if thesis_words < ext.min_thesis_words:
        return f"thesis_words={thesis_words} < {ext.min_thesis_words}"

    definition_words = len(cand.definition.split())
    if definition_words < ext.min_definition_words:
        return f"definition_words={definition_words} < {ext.min_definition_words}"

    if ext.require_anchor_quote and not cand.anchor_quote.strip():
        return "anchor_quote vazio"

    return None


# ── Deduplication ─────────────────────────────────────────────────────


def _deduplicate_candidates(
    cfg: AppConfig, db: StateDB, idx: VectorIndex,
    llm: Any, candidates: list[dict],
) -> list[dict]:
    """Semantic deduplication of candidates against existing notes."""
    if not candidates:
        return []

    dedupe_prompt = _load_prompt(cfg.prompts_path / "dedupe_decision.md")
    approved: list[dict] = []
    total = len(candidates)

    for i, cand_dict in enumerate(candidates, 1):
        cand: PermanentNoteCandidate = cand_dict["candidate"]
        logger.info("Deduplicando candidato %d/%d: %s", i, total, cand.thesis[:50])
        query_text = f"{cand.thesis} {cand.definition}"

        # Query existing notes
        similar = idx.query_similar_notes(query_text, n_results=cfg.linking.topk)

        if not similar:
            approved.append(cand_dict)
            continue

        # Check if any is above threshold (ChromaDB uses L2 distance, lower = more similar)
        # For cosine distance: threshold of 0.85 similarity = distance < 0.15
        closest_distance = similar[0].get("distance", 999)
        similarity_threshold_distance = 2 * (1 - cfg.linking.dedupe_threshold)

        if closest_distance > similarity_threshold_distance:
            # Not similar enough — create new
            approved.append(cand_dict)
            continue

        # Ask LLM to decide
        existing_notes_text = _format_existing_notes(similar)
        filled = dedupe_prompt.replace("{new_thesis}", cand.thesis)
        filled = filled.replace("{new_definition}", cand.definition)
        filled = filled.replace("{existing_notes}", existing_notes_text)

        try:
            response = _call_llm(llm, filled)
            result = _parse_dedupe_result(response)
        except Exception as e:
            logger.warning("Erro na deduplicação, aprovando candidato: %s", e)
            approved.append(cand_dict)
            continue

        if result.decision == DedupeDecision.CREATE_NEW:
            approved.append(cand_dict)
        elif result.decision == DedupeDecision.IGNORE:
            logger.info("Candidato ignorado (duplicata): %s", cand.thesis[:60])
        elif result.decision in (DedupeDecision.REFINE_EXISTING, DedupeDecision.MERGE):
            cand_dict["merge_target"] = result.target_note_id
            cand_dict["merge_reason"] = result.reason
            approved.append(cand_dict)

    return approved


# ── LIT Note Aggregation ─────────────────────────────────────────────


def _append_to_literature_note(
    cfg: AppConfig, db: StateDB, source_id: str, chunk_id: str,
    output: LiteratureChunkOutput,
) -> None:
    """Append chunk extraction results to the LIT master note."""
    source = db.get_source(source_id)
    if not source:
        return

    citekey = source["citekey"]
    title = source["title"]

    # Find LIT file
    lit_dir = cfg.vault_path / "20_Literature"
    lit_files = list(lit_dir.glob(f"LIT - @{citekey}*"))
    if not lit_files:
        logger.warning("Nota LIT não encontrada para %s", source_id)
        return

    lit_path = lit_files[0]
    content = lit_path.read_text(encoding="utf-8")

    # Build chunk section
    chunk_section = f"\n### Chunk: {chunk_id}\n\n"
    chunk_section += f"**Resumo**: {output.summary}\n\n"
    if output.key_concepts:
        chunk_section += "**Conceitos**: " + ", ".join(output.key_concepts) + "\n\n"
    if output.candidates:
        chunk_section += "**Candidatos a notas permanentes**:\n"
        for c in output.candidates:
            chunk_section += f"- {c.thesis}\n"
    chunk_section += "\n"

    # Append to the "Log de chunks processados" section using managed block
    block_name = "auto-chunks-log"
    existing_log = ""
    existing = read_managed_block(content, block_name)
    if existing:
        existing_log = existing + "\n"

    content = upsert_managed_block(content, block_name, existing_log + chunk_section)
    lit_path.write_text(content, encoding="utf-8")


def _aggregate_literature_notes(
    cfg: AppConfig, db: StateDB,
    outputs_by_source: dict[str, list[LiteratureChunkOutput]],
) -> None:
    """Aggregate all chunk outputs per source into the LIT note managed blocks.

    Updates: auto-resumo (concatenated summaries), auto-conceitos (deduplicated),
    auto-candidatos (all candidate theses).
    """
    for source_id, outputs in outputs_by_source.items():
        source = db.get_source(source_id)
        if not source:
            continue

        citekey = source["citekey"]
        title = source["title"]

        # Find LIT file
        lit_dir = cfg.vault_path / "20_Literature"
        lit_files = list(lit_dir.glob(f"LIT - @{citekey}*"))
        if not lit_files:
            logger.warning("Nota LIT nao encontrada para agregacao: %s", source_id)
            continue

        lit_path = lit_files[0]

        # Aggregate summaries
        summaries = [o.summary for o in outputs if o.summary]
        resumo_text = "\n\n".join(summaries) if summaries else "_Nenhum resumo disponivel._"

        # Aggregate and deduplicate key concepts
        all_concepts: list[str] = []
        seen_concepts: set[str] = set()
        for o in outputs:
            for concept in o.key_concepts:
                lower = concept.lower().strip()
                if lower not in seen_concepts:
                    seen_concepts.add(lower)
                    all_concepts.append(concept)
        conceitos_text = "\n".join(f"- {c}" for c in all_concepts) if all_concepts else ""

        # Aggregate candidate theses
        all_theses: list[str] = []
        for o in outputs:
            for cand in o.candidates:
                all_theses.append(cand.thesis)
        candidatos_text = "\n".join(f"- {t}" for t in all_theses) if all_theses else ""

        # Update managed blocks
        safe_update_managed_blocks(lit_path, {
            "auto-resumo": resumo_text,
            "auto-conceitos": conceitos_text,
            "auto-candidatos": candidatos_text,
        })
        logger.info("LIT agregada para %s: %d resumos, %d conceitos, %d candidatos",
                     source_id, len(summaries), len(all_concepts), len(all_theses))


# ── Concept ID ────────────────────────────────────────────────────────


def _compute_concept_id(source_id: str, chunk_id: str, cand: PermanentNoteCandidate) -> str:
    """Compute a stable concept_id based on source text anchors."""
    if cand.anchor_quote:
        anchor_hash = sha256_hex(normalize_text_for_hash(cand.anchor_quote))
        concept_key = sha256_hex(f"{source_id}|{chunk_id}|{anchor_hash}")
    else:
        thesis_hash = sha256_hex(normalize_text_for_hash(cand.thesis))
        concept_key = sha256_hex(f"{source_id}|{chunk_id}|{thesis_hash}")
    return f"{source_id}::concept::{short_hash(concept_key)}"


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
    """Load a prompt template from file."""
    if not path.exists():
        raise FileNotFoundError(f"Prompt não encontrado: {path}")
    return path.read_text(encoding="utf-8")


def _parse_literature_output(text: str) -> LiteratureChunkOutput:
    """Parse LLM response into structured LiteratureChunkOutput."""
    # Extract JSON from possible markdown code blocks
    json_text = _extract_json(text)
    data = json.loads(json_text)
    return LiteratureChunkOutput(**data)


def _parse_dedupe_result(text: str) -> DedupeResult:
    """Parse LLM response into DedupeResult."""
    json_text = _extract_json(text)
    data = json.loads(json_text)
    return DedupeResult(**data)


def _extract_json(text: str) -> str:
    """Extract JSON from text that may include markdown code blocks."""
    # Try to find JSON in code blocks
    import re
    match = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    # Try raw JSON
    text = text.strip()
    if text.startswith("{") or text.startswith("["):
        return text
    # Last resort: find first { to last }
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1:
        return text[start : end + 1]
    raise ValueError("Nenhum JSON encontrado na resposta do LLM")


def _format_existing_notes(notes: list[dict]) -> str:
    """Format existing notes for the deduplication prompt."""
    parts: list[str] = []
    for n in notes:
        nid = n.get("id", "?")
        meta = n.get("metadata", {})
        doc = n.get("document", "")[:200]
        title = meta.get("title", "Sem título")
        dist = n.get("distance", "?")
        parts.append(f"- **{nid}** ({title}) [dist={dist}]: {doc}")
    return "\n".join(parts)
