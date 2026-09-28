"""One approved candidate -> one permanent (ZTL) note.

``process_candidate`` is the orchestrator; each step below it does one thing:
decide the target note, gather context, run Prompt 2, assemble connections,
write the note (vault + SQLite + Chroma), link it into the graph.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from ulid import ULID

from zettel.citation import NoteCitation, citation_frontmatter, note_provenance, resolve_citation
from zettel.config import AppConfig
from zettel.connector import context, links
from zettel.connector.prompt import (
    ConnectRejected,
    Prompt2Payload,
    generate_permanent_note,
)
from zettel.hashing import (
    compute_embedding_input_hash,
    extract_embeddable_text,
    normalize_text_for_hash,
    sha256_hex,
)
from zettel.index import VectorIndex
from zettel.llm import LLMUnavailableError, PromptParts
from zettel.retrieval import Retriever
from zettel.schemas import PermanentNoteCandidate, PermanentNoteLLMOutput
from zettel.state import StateDB
from zettel.time import now_vault_iso
from zettel.usage import clear_progress, get_tracker, set_progress
from zettel.vault import (
    build_permanent_note_body,
    format_suggestion_line,
    judgement_frontmatter,
    literature_chunk_wikilink_for_row,
    literature_index_stem,
    note_filename,
    parse_frontmatter,
    safe_write_note,
    write_auto_connections,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ConnectSession:
    """What stays fixed across every candidate of one connect run."""

    cfg: AppConfig
    db: StateDB
    idx: VectorIndex
    llm: Any
    prompt_parts: PromptParts
    retriever: Retriever
    example_fields: dict[str, str]
    taxonomy: context.Taxonomy
    origin: str = "pipeline"


def literature_ref_for_chunk(citekey: str, title_src: str, chunk: dict[str, Any] | None) -> str:
    """Wikilink to the approved granular LIT for this chunk (fallback: index).

    The alias carries the printed page, so the reader sees ``p. 42 — Topico``
    instead of a bare filename.
    """
    if chunk and chunk.get("status") in ("approved", "persisted"):
        return literature_chunk_wikilink_for_row(citekey, chunk, with_alias=True)
    return f"[[{literature_index_stem(citekey, title_src)}]]"


def process_candidate(
    session: ConnectSession,
    cand_dict: dict,
    *,
    step: int | None = None,
    total: int | None = None,
) -> str | None:
    """Write the permanent note for one candidate. Returns its note_id, or None.

    ``None`` means nothing was written: the concept is already covered by another
    note, or Prompt 2 failed with a recoverable error (logged; the concept stays
    ``approved`` for the next run). ``ConnectRejected`` and ``LLMUnavailableError``
    propagate to ``run_connect``.
    """
    if step is not None:
        set_progress(step, total, "nota")
    try:
        return _process(session, cand_dict, step=step, total=total)
    finally:
        clear_progress()


def _process(
    session: ConnectSession, cand_dict: dict, *, step: int | None, total: int | None
) -> str | None:
    cfg, db = session.cfg, session.db
    cand: PermanentNoteCandidate = cand_dict["candidate"]
    source_id = cand_dict["source_id"]
    concept_id = cand_dict["concept_id"]
    chunk_id = cand_dict.get("chunk_id") or ""

    note_id = _target_note_id(db, cand_dict)
    if note_id is None:
        return None

    source = db.get_source(source_id)
    chunk_row = db.get_chunk(chunk_id) if chunk_id else None
    literature_ref = literature_ref_for_chunk(
        source["citekey"] if source else "unknown",
        source["title"] if source else "",
        chunk_row,
    )
    # Structural page and citation, derived by code from the chunk row and the
    # grounded anchor quote — never from the LLM-authored locator (ADR-051).
    citation = resolve_citation(source, chunk_row, cand.anchor_quote)

    # Prefer LLM-provided image ids; fall back to paths embedded in the source chunk.
    if not cand.relevant_image_ids:
        cand.relevant_image_ids = context.fallback_image_ids(db, source_id, chunk_row)

    query_text = f"{cand.thesis} {cand.definition}"
    similar = session.retriever.search_notes(
        query_text, topk=cfg.linking.topk, exclude_id=note_id
    ).hits
    distant = context.search_distant_analogies(
        cfg, session.idx, session.retriever, query_text, note_id, similar, session.taxonomy
    )
    payload = Prompt2Payload(
        source_id=source_id,
        literature_ref=literature_ref,
        rag_context=context.build_rag_context(db, similar, distant),
        images_context=context.images_context(db, cand.relevant_image_ids),
        examples=session.example_fields,
    )

    tracker = get_tracker()
    usage_before = tracker.summary().as_dict() if tracker else {}
    try:
        note_output, cache_hit = generate_permanent_note(
            cfg,
            db,
            session.llm,
            session.prompt_parts,
            cand,
            payload,
            label=f"connect:{concept_id}",
            step=step,
            total=total,
        )
    except (ConnectRejected, LLMUnavailableError):
        raise
    except Exception as e:
        logger.error("Erro ao gerar nota permanente para conceito %s: %s", concept_id, e)
        return None
    usage_after = tracker.summary().as_dict() if tracker else {}

    connections = links.assemble_connections(
        cfg,
        db,
        note_output.connections,
        similar=similar,
        source_id=source_id,
        note_id=note_id,
        refines_note_id=cand_dict.get("refines_note_id"),
        refine_reason=cand_dict.get("refine_reason") or "",
    )
    # Distant analogies are suggestions (auto-connections), never graph edges (ADR-043).
    distant_ids = {n.note_id for n in distant}
    resolved = links.resolve_connections(db, connections)
    edges = [c for c in resolved if c["related_note_id"] not in distant_ids]
    suggestions = [c for c in resolved if c["related_note_id"] in distant_ids]

    _write_note(
        session,
        cand_dict,
        note_id=note_id,
        note_output=note_output,
        edges=edges,
        suggestions=suggestions,
        literature_ref=literature_ref,
        citation=citation,
        provenance_extra={
            **_usage_delta(usage_before, usage_after),
            "llm_cache_hit": cache_hit,
        },
    )
    links.persist_and_backlink(cfg, db, note_id, edges)
    return note_id


def _target_note_id(db: StateDB, cand_dict: dict) -> str | None:
    """The note this concept writes to, or None when another note already covers it.

    A concept that already has a note is rewritten in place — unless that note is
    manual, which the pipeline never overwrites. A concept without one first checks
    whether a note written from the same chunk/thesis already covers it
    (``find_covering_note_id``); either covered case marks the concept ``noted``.
    """
    concept_id = cand_dict["concept_id"]
    existing_note_id = (db.get_concept(concept_id) or {}).get("note_id")
    if existing_note_id:
        note = db.get_note(existing_note_id)
        if not note or str(note.get("origin") or "") != "manual":
            logger.debug("Conceito %s ja tem nota %s, atualizando", concept_id, existing_note_id)
            return existing_note_id
        covering = existing_note_id
        logger.info("Conceito %s ja coberto pela nota manual %s, pulando", concept_id, covering)
    else:
        from zettel.manual_lit import find_covering_note_id

        covering = find_covering_note_id(
            db,
            source_id=cand_dict["source_id"],
            chunk_id=cand_dict.get("chunk_id") or "",
            thesis=cand_dict["candidate"].thesis,
        )
        if not covering:
            return str(ULID())
        logger.info("Conceito %s ja coberto pela nota %s, pulando geracao", concept_id, covering)

    db.upsert_concept(
        concept_id,
        cand_dict["source_id"],
        cand_dict["chunk_id"],
        note_id=covering,
        status="noted",
    )
    return None


def _usage_delta(before: dict, after: dict) -> dict[str, Any]:
    """LLM cost and tokens spent between two ``CostTracker`` snapshots."""

    def diff(key: str) -> float:
        return float(after.get(key) or 0) - float(before.get(key) or 0)

    return {
        "llm_cost_usd": round(diff("cost_usd_llm"), 6),
        "llm_tokens_prompt": int(diff("tokens_prompt")),
        "llm_tokens_completion": int(diff("tokens_completion")),
    }


def _write_note(
    session: ConnectSession,
    cand_dict: dict,
    *,
    note_id: str,
    note_output: PermanentNoteLLMOutput,
    edges: list[dict],
    suggestions: list[dict],
    literature_ref: str,
    citation: NoteCitation,
    provenance_extra: dict[str, Any],
) -> None:
    """Write the ZTL file, persist it in SQLite and (re-)embed it when it changed."""
    cfg, db = session.cfg, session.db
    cand: PermanentNoteCandidate = cand_dict["candidate"]
    source_id = cand_dict["source_id"]

    body = build_permanent_note_body(
        thesis=note_output.thesis,
        definition=note_output.definition,
        intuition=note_output.intuition,
        example=note_output.example,
        limits=note_output.limits,
        connections=edges,
        literature_ref=literature_ref,
        source_locator=cand.source_locator or "",
        page=citation.page,
        images=context.resolve_images(db, cand.relevant_image_ids),
        citation=citation.cite,
        anchor_quote=cand.anchor_quote,
    )

    now = now_vault_iso(cfg.vault_timezone)
    tags = note_output.tags or cand.tags
    title = note_output.title[:100] or cand.thesis[:60]
    meta = {
        "type": "permanent",
        "note_id": note_id,
        "title": title,
        "source_id": source_id,
        "chunk_id": cand_dict.get("chunk_id") or "",
        "tags": tags,
        "origin": session.origin,
        "created_at": now,
        "updated_at": now,
    }
    # The page comes from code, not the LLM (ADR-051); omitted rather than null
    # for native Markdown.
    meta.update(citation_frontmatter(citation))
    # The author's judgement travels verbatim from the candidate, not through the
    # LLM: the export (`zettel skill`) reads it from here instead of re-parsing the
    # LIT draft. Absent keys mean the chunk stated none — noise-free by default.
    meta.update(judgement_frontmatter(cand))

    note_path = cfg.vault_path / "30_Permanent" / note_filename("ZTL", note_id, title)
    safe_write_note(note_path, meta, body)
    if suggestions:
        write_auto_connections(
            note_path,
            [
                format_suggestion_line(
                    c.get("wiki_link") or c["related_note_id"],
                    relation_type=c.get("relation_type") or "",
                    description=c.get("description") or "",
                )
                for c in suggestions
            ],
            vault_timezone=cfg.vault_timezone,
        )
        # Mirror the file as it now sits on disk: suggestions block and bumped
        # `updated_at` included, so SQLite is written once and stays faithful.
        meta, body = parse_frontmatter(note_path.read_text(encoding="utf-8"))

    # Managed blocks are stripped here, so the suggestions never reach the embedding.
    embeddable = extract_embeddable_text(body)
    semantic_checksum = sha256_hex(normalize_text_for_hash(embeddable))

    # Retencao: persiste o corpo completo e o frontmatter da ZTL no SQLite, permitindo
    # reconstruir o arquivo .md byte-a-byte sem reprocessar o LLM (ver `zettel rebuild`).
    db.upsert_note(
        note_id=note_id,
        source_id=source_id,
        path=str(note_path),
        title=title,
        note_semantic_checksum=semantic_checksum,
        embedding_model=cfg.embedding.model,
        body=body,
        frontmatter_json=json.dumps(meta, ensure_ascii=False),
        origin=session.origin,
        provenance_json=json.dumps(
            note_provenance(
                citation,
                anchor_quote=cand.anchor_quote,
                literature_ref=literature_ref,
                source_locator=cand.source_locator or "",
                **provenance_extra,
            ),
            ensure_ascii=False,
        ),
    )
    db.upsert_concept(
        cand_dict["concept_id"],
        source_id,
        cand_dict["chunk_id"],
        note_id=note_id,
        status="noted",
    )

    # Skip re-embedding when the note's semantic content and embedding model are unchanged.
    emb_hash = compute_embedding_input_hash(
        semantic_checksum, cfg.embedding.provider, cfg.embedding.model
    )
    if (db.get_note(note_id) or {}).get("embedding_input_hash") != emb_hash:
        session.idx.upsert_permanent_note(
            note_id,
            embeddable,
            {
                "title": title,
                "source_id": source_id,
                "tags": ", ".join(tags),
                "note_semantic_checksum": semantic_checksum,
            },
        )
        db.update_note_embedding(note_id, emb_hash, cfg.embedding.model)
