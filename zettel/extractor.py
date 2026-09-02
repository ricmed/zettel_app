"""The Extractor — LLM processing of chunks into granular literature drafts.

Processes each pending chunk with Prompt 1, writes a draft LIT note under
``00_Inbox/Review/``, and leaves concepts in ``awaiting_review`` until
``zettel review`` approves them. Deduplication against permanent notes runs
only after approval (in review), not here.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from ulid import ULID

from zettel.config import AppConfig, llm_phase
from zettel.hashing import (
    compute_llm_call_checksum,
    normalize_text_for_hash,
    sha256_hex,
    short_hash,
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
from zettel.paging import format_source_locator
from zettel.schemas import (
    DedupeDecision,
    DedupeResult,
    LiteratureChunkOutput,
    PermanentNoteCandidate,
)
from zettel.state import StateDB
from zettel.vault import (
    build_literature_chunk_note,
    literature_chunk_filename,
    literature_source_dirname,
    safe_write_note,
)

logger = logging.getLogger(__name__)


# ── Public API ─────────────────────────────────────────────────────────


def run_extract(
    cfg: AppConfig,
    db: StateDB,
    idx: VectorIndex,
    *,
    auto_approve: bool = False,
    observer=None,
) -> list[dict]:
    """Process pending chunks into literature drafts. Returns awaiting-review candidates.

    If ``auto_approve`` is True, chunks with ``review_confidence`` >= config limiar
    are immediately approved via ``zettel.review.approve_chunk``.
    """
    from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, MofNCompleteColumn

    from zettel.usage import begin_run, finish_pipeline_run, get_tracker, set_source
    from zettel.vault import sync_source_costs_to_vault

    run_id = db.start_run("extract")
    begin_run(run_id)

    from zettel.assets import describe_pending_assets
    described = describe_pending_assets(cfg, db, observer=observer)
    if described:
        logger.info("Imagens descritas nesta execucao: %d", described)

    llm = get_llm(cfg, "extract")
    prompt_parts = load_prompt_parts(cfg.prompts_path / "literature_note.md")
    prompt_hash = sha256_hex(prompt_parts.full_template)

    pending = db.get_pending_chunks()
    total = len(pending)
    logger.info("Chunks pendentes para extracao: %d", total)
    from zettel.progress import report
    report(observer, "extract", f"{total} chunk(s) pendente(s).", total_items=total)

    all_candidates: list[dict] = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]Extract[/bold blue] {task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        transient=True,
    ) as progress:
        task = progress.add_task("chunks", total=total)
        for i, chunk_row in enumerate(pending, 1):
            chunk_id = chunk_row["chunk_id"]
            source_id = chunk_row["source_id"]
            set_source(source_id)
            progress.update(task, description=f"chunk {i}/{total}", advance=1)
            report(
                observer, "extract", f"Extraindo chunk {i}/{total}.",
                current_item=chunk_id, current_index=i, total_items=total,
            )

            page_file = chunk_row.get("page_in_file")
            page_book = chunk_row.get("page_in_book")
            page_conf = chunk_row.get("page_confidence") or "unknown"
            logger.info(
                "[SOURCE=%s] [CHUNK=%s idx=%s/%d] "
                "[PAGE file=%s book=%s conf=%s] → Iniciando analise LLM",
                source_id, chunk_id, chunk_row.get("chunk_index"), total,
                page_file, page_book, page_conf,
            )

            candidates, _output = _process_chunk(
                cfg, db, idx, llm, chunk_row, prompt_parts, prompt_hash,
                step=i, total=total,
            )
            all_candidates.extend(candidates)
            logger.info(
                "[SOURCE=%s] [CHUNK=%s] → Analise concluida, %d candidatos",
                source_id, chunk_id, len(candidates),
            )

            db.update_source_paging(
                source_id,
                last_chunk_processed=chunk_row.get("chunk_index"),
            )

    set_source(None)
    tracker = get_tracker()
    if tracker:
        for sid in tracker.sources_touched():
            db.add_source_usage(sid, tracker.summary_for_source(sid).as_dict())
            sync_source_costs_to_vault(cfg, db, sid)

    if auto_approve:
        from zettel.review import approve_high_confidence
        n = approve_high_confidence(cfg, db, idx)
        logger.info("Auto-approve: %d chunks persistidos", n)

    logger.info(
        "Extract concluido: %d candidatos aguardando review (status awaiting_review)",
        len(all_candidates),
    )
    finish_pipeline_run(db, run_id)
    return all_candidates


# ── Chunk Processing ──────────────────────────────────────────────────


def _process_chunk(
    cfg: AppConfig,
    db: StateDB,
    idx: VectorIndex,
    llm: Any,
    chunk_row: dict,
    prompt_parts: PromptParts,
    prompt_hash: str,
    *,
    step: int | None = None,
    total: int | None = None,
) -> tuple[list[dict], LiteratureChunkOutput | None]:
    """Process a single chunk with Prompt 1. Writes draft; status=awaiting_review."""
    from zettel.usage import clear_progress, set_progress

    chunk_id = chunk_row["chunk_id"]
    source_id = chunk_row["source_id"]
    chunk_text = chunk_row["text"]
    chunk_checksum = chunk_row["chunk_checksum"]
    t0 = time.perf_counter()

    if step is not None:
        set_progress(step, total, "chunk")

    images_context = _build_images_context(
        db, source_id, chunk_row.get("chapter_id", ""),
        page_in_file=chunk_row.get("page_in_file"),
    )
    images_ctx_checksum = (
        sha256_hex(normalize_text_for_hash(images_context)) if images_context else ""
    )

    spec = llm_phase(cfg, "extract")
    call_checksum = compute_llm_call_checksum(
        prompt_hash, chunk_checksum, spec.model, cfg.llm.temperature, cfg.language,
        rag_context_checksum=images_ctx_checksum,
    )
    cached = db.get_cached_llm_response(call_checksum)
    if cached:
        logger.debug("Cache hit para chunk %s", chunk_id)
        from zettel.usage import record_cache_hit
        record_cache_hit(label=f"extract:{chunk_id}", model=spec.model)
        response_text = cached
    else:
        source = db.get_source(source_id)
        source_title = source["title"] if source else "Desconhecido"
        section_path = chunk_row.get("section_path") or chunk_row.get("locator") or ""
        locator = format_source_locator(
            chunk_row.get("page_in_book"),
            section_path,
            chunk_row.get("page_in_file"),
        ) or chunk_row.get("locator", "")

        mapping = {
            "language": cfg.language,
            "domain": cfg.gardener.domain or "Geral",
            "source_id": source_id,
            "source_title": source_title,
            "section_path": section_path,
            "locator": locator,
            "images_context": images_context,
            "chunk_text": chunk_text,
        }
        system = fill_template(prompt_parts.system, mapping) if prompt_parts.system else ""
        user = fill_template(prompt_parts.user_template, mapping)

        try:
            response_text = call_llm(
                llm,
                user,
                system=system or None,
                label=f"extract:{chunk_id}",
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
            logger.info(
                "[SOURCE=%s] [CHUNK=%s] [LLM_CALL model=%s] → resposta recebida",
                source_id, chunk_id, spec.model,
            )
        except Exception as e:
            logger.error("Erro no LLM para chunk %s: %s", chunk_id, e)
            db.update_chunk_status(chunk_id, "failed")
            clear_progress()
            return [], None

    try:
        output = _parse_literature_output(response_text)
    except Exception as e:
        logger.warning(
            "Falha ao parsear output do chunk %s: %s -- tentando retry", chunk_id, e
        )
        try:
            retry_prompt = (
                f"O JSON abaixo esta malformado. Corrija e retorne APENAS o JSON valido:\n\n"
                f"{response_text}"
            )
            response_text = call_llm(
                llm,
                retry_prompt,
                label=f"extract-retry:{chunk_id}",
                step=step,
                total=total,
                provider=spec.provider,
                prompt_cache=False,
            )
            output = _parse_literature_output(response_text)
        except Exception:
            logger.error("Chunk %s enviado para revisao manual (parse falhou)", chunk_id)
            db.update_chunk_status(chunk_id, "failed")
            clear_progress()
            return [], None

    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    confidence = _score_review_confidence(output, cfg)

    # Structural locator for candidates
    locator = format_source_locator(
        chunk_row.get("page_in_book"),
        chunk_row.get("section_path") or "",
        chunk_row.get("page_in_file"),
    )
    for cand in output.candidates:
        if locator and (not cand.source_locator or cand.source_locator.startswith("p.?")
                        or len(cand.source_locator) < 3):
            cand.source_locator = locator

    approved_cands, rejected_cands = _filter_candidates(output.candidates, cfg)
    if rejected_cands:
        logger.info(
            "Chunk %s: %d candidatos rejeitados pela filtragem de qualidade",
            chunk_id, len(rejected_cands),
        )

    literature_id = str(ULID())
    draft_path = _write_literature_draft(
        cfg, db, chunk_row, output, literature_id, confidence, elapsed_ms,
        candidates=approved_cands,
        llm_model=spec.model,
    )

    summary_payload = {
        "summary": output.summary,
        "key_concepts": output.key_concepts,
        "chunk_status": output.chunk_status,
        "candidates": [c.model_dump() for c in approved_cands],
    }
    db.update_chunk_review(
        chunk_id,
        status="awaiting_review",
        literature_note_path=str(draft_path) if draft_path else None,
        literature_id=literature_id,
        review_confidence=confidence,
        summary_json=json.dumps(summary_payload, ensure_ascii=False),
        llm_prompt1_hash=prompt_hash,
        llm_call_checksum=call_checksum,
    )

    logger.info(
        "[SOURCE=%s] [NOTE=%s] status=AWAITING_REVIEW confidence=%.2f",
        source_id, draft_path, confidence,
    )

    candidates: list[dict] = []
    for cand in approved_cands:
        if not cand.relevant_image_ids:
            from zettel.assets import asset_ids_in_text
            cand.relevant_image_ids = asset_ids_in_text(db, source_id, chunk_text)

        concept_id = _compute_concept_id(source_id, chunk_id, cand)
        candidates.append({
            "concept_id": concept_id,
            "source_id": source_id,
            "chunk_id": chunk_id,
            "candidate": cand,
            "literature_id": literature_id,
        })
        anchor_hash = (
            sha256_hex(normalize_text_for_hash(cand.anchor_quote)) if cand.anchor_quote else ""
        )
        thesis_hash = sha256_hex(normalize_text_for_hash(cand.thesis))
        db.upsert_concept(
            concept_id, source_id, chunk_id, anchor_hash, thesis_hash,
            candidate_json=cand.model_dump_json(), status="awaiting_review",
        )

    clear_progress()
    return candidates, output


def _write_literature_draft(
    cfg: AppConfig,
    db: StateDB,
    chunk_row: dict,
    output: LiteratureChunkOutput,
    literature_id: str,
    confidence: float,
    elapsed_ms: int,
    candidates: list[PermanentNoteCandidate],
    llm_model: str = "",
) -> Path | None:
    source = db.get_source(chunk_row["source_id"])
    if not source:
        return None
    citekey = source["citekey"]
    title = source["title"]
    chunk_index = int(chunk_row.get("chunk_index") or 0)
    section_path = chunk_row.get("section_path") or ""

    images = _images_for_chunk(db, chunk_row)
    meta, body = build_literature_chunk_note(
        source_id=chunk_row["source_id"],
        citekey=citekey,
        title=title,
        chunk_id=chunk_row["chunk_id"],
        chunk_index=chunk_index,
        literature_id=literature_id,
        summary=output.summary,
        key_concepts=output.key_concepts,
        candidates=[c.model_dump() for c in candidates],
        images=images,
        section_path=section_path,
        source_text=chunk_row.get("text") or "",
        page_in_file=chunk_row.get("page_in_file"),
        page_in_book=chunk_row.get("page_in_book"),
        page_confidence=chunk_row.get("page_confidence") or "unknown",
        status="awaiting_review",
        review_confidence=confidence,
        llm_model=llm_model,
        processing_time_ms=elapsed_ms,
    )

    draft_root = cfg.vault_path / cfg.literature_review.drafts_subdir
    draft_dir = draft_root / literature_source_dirname(citekey)
    draft_dir.mkdir(parents=True, exist_ok=True)
    path = draft_dir / literature_chunk_filename(
        citekey,
        chunk_index=chunk_index,
        page_in_book=chunk_row.get("page_in_book"),
        page_in_file=chunk_row.get("page_in_file"),
        section_path=section_path,
        summary=output.summary,
    )
    safe_write_note(path, meta, body)
    return path


def _images_for_chunk(db: StateDB, chunk_row: dict) -> list[dict[str, Any]]:
    """Assets for this chunk: same chapter, optionally same page."""
    assets = db.get_assets_for_source(chunk_row["source_id"])
    chapter_id = chunk_row.get("chapter_id")
    page = chunk_row.get("page_in_file")
    out: list[dict[str, Any]] = []
    for a in assets:
        if chapter_id and a.get("chapter_id") and a["chapter_id"] != chapter_id:
            continue
        if page is not None and a.get("page_in_file") is not None:
            if abs(int(a["page_in_file"]) - int(page)) > 1:
                continue
        out.append({
            "path": a["path"],
            "description": a.get("description") or "",
            "asset_id": a["asset_id"],
        })
    return out


def _score_review_confidence(output: LiteratureChunkOutput, cfg: AppConfig) -> float:
    """Heuristic confidence in [0, 1] for auto-approve decisions."""
    if output.chunk_status == "rejected":
        return 0.1
    score = 0.4
    if output.summary and len(output.summary.split()) >= 20:
        score += 0.15
    if output.key_concepts:
        score += min(0.15, 0.05 * len(output.key_concepts))
    if not output.candidates:
        return min(score, 0.55)
    approved, _ = _filter_candidates(output.candidates, cfg)
    if not approved:
        return min(score, 0.45)
    avg_rel = sum(c.relevance_score for c in approved) / len(approved)
    score += 0.1 * (avg_rel / 5.0)
    with_quote = sum(1 for c in approved if c.anchor_quote.strip())
    score += 0.2 * (with_quote / len(approved))
    return round(min(1.0, score), 3)


def _build_images_context(
    db: StateDB,
    source_id: str,
    chapter_id: str,
    page_in_file: int | None = None,
) -> str:
    assets = db.get_assets_for_source(source_id)
    lines: list[str] = []
    for a in assets:
        if a.get("chapter_id") and chapter_id and a["chapter_id"] != chapter_id:
            continue
        if (
            page_in_file is not None
            and a.get("page_in_file") is not None
            and abs(int(a["page_in_file"]) - int(page_in_file)) > 1
        ):
            continue
        desc = a.get("description")
        if desc:
            lines.append(f"- {a['asset_id']}: {desc}")
    if not lines:
        return ""
    header = (
        "Imagens disponiveis neste trecho (referencie o asset_id em "
        "relevant_image_ids quando a imagem for essencial ao conceito):"
    )
    return header + "\n" + "\n".join(lines)


# ── Candidate Filtering ──────────────────────────────────────────────


def _filter_candidates(
    candidates: list[PermanentNoteCandidate],
    cfg: AppConfig,
) -> tuple[list[PermanentNoteCandidate], list[PermanentNoteCandidate]]:
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


def _check_candidate(cand: PermanentNoteCandidate, ext: Any) -> str | None:
    if cand.chunk_status == "rejected":
        return (
            f"chunk_status={cand.chunk_status}, "
            f"rejection_reason={cand.rejection_reason}, "
            f"rejection_category={cand.rejection_category}"
        )
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


# ── Deduplication (used by review after approval) ─────────────────────


def deduplicate_candidates(
    cfg: AppConfig,
    db: StateDB,
    idx: VectorIndex,
    llm: Any,
    candidates: list[dict],
) -> list[dict]:
    """Semantic deduplication of candidates against existing permanent notes."""
    from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, MofNCompleteColumn

    if not candidates:
        return []

    spec = llm_phase(cfg, "review")
    dedupe_parts = load_prompt_parts(cfg.prompts_path / "dedupe_decision.md")
    approved: list[dict] = []
    total = len(candidates)

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]Dedupe[/bold blue] {task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        transient=True,
    ) as progress:
        task = progress.add_task("candidatos", total=total)
        for i, cand_dict in enumerate(candidates, 1):
            cand: PermanentNoteCandidate = cand_dict["candidate"]
            progress.update(task, description=f"candidato {i}/{total}", advance=1)
            logger.info("Deduplicando candidato %d/%d: %s", i, total, cand.thesis[:50])
            query_text = f"{cand.thesis} {cand.definition}"
            similar = idx.query_similar_notes(query_text, n_results=cfg.linking.topk)

            if not similar:
                approved.append(cand_dict)
                continue

            closest_distance = similar[0].get("distance", 999)
            similarity_threshold_distance = 2 * (1 - cfg.linking.dedupe_threshold)
            if closest_distance > similarity_threshold_distance:
                approved.append(cand_dict)
                continue

            existing_notes_text = _format_existing_notes(similar)
            mapping = {
                "new_thesis": cand.thesis,
                "new_definition": cand.definition,
                "existing_notes": existing_notes_text,
            }
            system = fill_template(dedupe_parts.system, mapping) if dedupe_parts.system else ""
            user = fill_template(dedupe_parts.user_template, mapping)

            try:
                response = call_llm(
                    llm,
                    user,
                    system=system or None,
                    provider=spec.provider,
                    prompt_cache=cfg.llm.prompt_cache,
                )
                result = _parse_dedupe_result(response)
            except Exception as e:
                logger.warning("Erro na deduplicacao, aprovando candidato: %s", e)
                approved.append(cand_dict)
                continue

            if result.decision == DedupeDecision.CREATE_NEW:
                approved.append(cand_dict)
            elif result.decision == DedupeDecision.IGNORE:
                logger.info("Candidato ignorado (duplicata): %s", cand.thesis[:60])
            elif result.decision in (DedupeDecision.REFINE_EXISTING, DedupeDecision.MERGE):
                cand_dict["refines_note_id"] = result.target_note_id
                cand_dict["refine_reason"] = result.reason
                approved.append(cand_dict)

    approved_ids = {c["concept_id"] for c in approved}
    for cand_dict in candidates:
        cid = cand_dict["concept_id"]
        db.update_concept_status(cid, "approved" if cid in approved_ids else "duplicate")

    return approved


# Backwards-compatible alias
_deduplicate_candidates = deduplicate_candidates


def _compute_concept_id(
    source_id: str, chunk_id: str, cand: PermanentNoteCandidate
) -> str:
    if cand.anchor_quote:
        anchor_hash = sha256_hex(normalize_text_for_hash(cand.anchor_quote))
        concept_key = sha256_hex(f"{source_id}|{chunk_id}|{anchor_hash}")
    else:
        thesis_hash = sha256_hex(normalize_text_for_hash(cand.thesis))
        concept_key = sha256_hex(f"{source_id}|{chunk_id}|{thesis_hash}")
    return f"{source_id}::concept::{short_hash(concept_key)}"


def _parse_literature_output(text: str) -> LiteratureChunkOutput:
    json_text = extract_json(text)
    data = json.loads(json_text)
    return LiteratureChunkOutput(**data)


def _parse_dedupe_result(text: str) -> DedupeResult:
    json_text = extract_json(text)
    data = json.loads(json_text)
    return DedupeResult(**data)


def _format_existing_notes(notes: list[dict]) -> str:
    parts: list[str] = []
    for n in notes:
        nid = n.get("id", "?")
        meta = n.get("metadata", {})
        doc = n.get("document", "")[:200]
        title = meta.get("title", "Sem titulo")
        dist = n.get("distance", "?")
        parts.append(f"- **{nid}** ({title}) [dist={dist}]: {doc}")
    return "\n".join(parts)
