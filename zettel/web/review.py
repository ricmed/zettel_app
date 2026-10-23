"""Literature review queue."""

from __future__ import annotations

import contextlib
import json

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from zettel.web.enqueue import operation_lock, post_job
from zettel.web.rendering import error_page, render, service
from zettel.web.security import authenticated, redirect_login

router = APIRouter()

_DRAFT_PAGE_SIZE = 20
_REJECTED_PAGE_SIZE = 10
_LINK_RELATIONS = ("supports", "contradicts", "extends", "depends_on", "exemplifies", "related")
_LINK_SCOPES = ("intra", "inter")


def _summary(chunk: dict) -> dict:
    summary = {}
    with contextlib.suppress(json.JSONDecodeError, TypeError):
        summary = json.loads(chunk.get("summary_json") or "{}")
    return summary if isinstance(summary, dict) else {}


def _bounds(total: int, page: int, size: int) -> tuple[int, int, int]:
    """Clamp ``page`` to the queue and return it with the page count and offset."""
    page_count = max(1, (total + size - 1) // size)
    page = min(max(1, page), page_count)
    return page, page_count, (page - 1) * size


def _edge_note_detail(
    note_id: str,
    title: str | None,
    body: str | None,
    source_id: str | None,
    provenance_json: str | None,
) -> dict:
    from zettel.citation import load_provenance
    from zettel.markdown_fences import h2_section
    from zettel.note_content import note_content, note_sections

    raw_title = (title or "").strip()
    raw_body = body or ""
    sections = note_sections(raw_title, raw_body)
    thesis = sections["thesis"]
    if thesis == raw_title:
        h2_thesis = h2_section(raw_body, "Tese")
        if h2_thesis:
            thesis = h2_thesis
    has_structured = any(sections[k] for k in ("definition", "limits"))
    excerpt = ""
    if not has_structured and not raw_body.lstrip().startswith("> **Tese**:"):
        free = note_content(raw_title, raw_body)
        if free and free != thesis:
            excerpt = free
    prov = load_provenance({"provenance_json": provenance_json})
    return {
        "note_id": note_id,
        "title": raw_title or note_id,
        "source_id": source_id or "",
        "thesis": thesis,
        "definition": sections["definition"],
        "limits": sections["limits"],
        "excerpt": excerpt,
        "citation": str(prov.get("citation") or ""),
        "source_locator": str(prov.get("source_locator") or ""),
        "anchor_quote": str(prov.get("anchor_quote") or ""),
    }


def _link_page(db, source_id, relation_type, scope, page):
    filters = {"source_id": source_id or None, "relation_type": relation_type, "scope": scope}
    _, total = db.proposed_connections_page(**filters, limit=0)
    page, page_count, offset = _bounds(total, page, _DRAFT_PAGE_SIZE)
    rows, _ = db.proposed_connections_page(**filters, limit=_DRAFT_PAGE_SIZE, offset=offset)
    items = []
    for row in rows:
        source_note = _edge_note_detail(
            row["source_note_id"],
            row.get("source_title"),
            row.get("source_body"),
            row.get("source_source_id"),
            row.get("source_provenance_json"),
        )
        target_note = _edge_note_detail(
            row["target_note_id"],
            row.get("target_title"),
            row.get("target_body"),
            row.get("target_source_id"),
            row.get("target_provenance_json"),
        )
        items.append(
            {
                **row,
                "source_thesis": source_note["thesis"],
                "target_thesis": target_note["thesis"],
                "source_note": source_note,
                "target_note": target_note,
            }
        )
    return items, total, page, page_count


def _duplicate_page(db, source_id, page):
    from zettel.review import format_dedupe_item, pending_dedupe_concepts

    concepts = pending_dedupe_concepts(db, source_id or None)
    page, page_count, offset = _bounds(len(concepts), page, _DRAFT_PAGE_SIZE)
    items = [
        {"concept_id": concept["concept_id"], "card": format_dedupe_item(db, concept)}
        for concept in concepts[offset : offset + _DRAFT_PAGE_SIZE]
    ]
    return items, len(concepts), page, page_count


def _rejected_page(db, source_id, page):
    from zettel.review import extract_rejection_origin, is_extract_rejected

    queue = [
        c for c in db.list_chunk_queue("rejected", source_id or None) if is_extract_rejected(c)
    ]
    page, page_count, offset = _bounds(len(queue), page, _REJECTED_PAGE_SIZE)
    items = []
    for light in queue[offset : offset + _REJECTED_PAGE_SIZE]:
        chunk = db.get_chunk(light["chunk_id"]) or light
        summary = _summary(chunk)
        items.append(
            {
                **chunk,
                "rejection_origin": extract_rejection_origin(chunk),
                "rejection_category": summary.get("rejection_category") or "",
                "rejection_reason": summary.get("rejection_reason") or "",
                "rejected_candidates": summary.get("rejected_candidates") or [],
            }
        )
    return items, len(queue), page, page_count


def _draft_page(db, cfg, source_id, confidence, page):
    """Order and filter on the light rows; load text and concepts for one page only."""
    from zettel.review import (
        BAND_HIGH,
        BAND_MEDIUM,
        BAND_VERY_LOW,
        filter_chunks_by_band,
        highlight_spans,
        ordered_review_concepts,
        review_queue_key,
    )

    queue = db.list_chunk_queue("awaiting_review", source_id or None)
    band = {"low": BAND_VERY_LOW, "medium": BAND_MEDIUM, "high": BAND_HIGH}.get(confidence)
    if band:
        queue = filter_chunks_by_band(
            queue, band, cfg.literature_review.auto_approve_min_confidence
        )
    # The same priority as `zettel review`: defects first, then low confidence (#268).
    queue.sort(key=review_queue_key)
    page, page_count, offset = _bounds(len(queue), page, _DRAFT_PAGE_SIZE)
    items = []
    for light in queue[offset : offset + _DRAFT_PAGE_SIZE]:
        chunk = db.get_chunk(light["chunk_id"]) or light
        summary = _summary(chunk)
        concepts = []
        for concept in ordered_review_concepts(db, chunk):
            cand = _summary({"summary_json": concept.get("candidate_json")})
            concepts.append(
                {
                    "concept_id": concept["concept_id"],
                    "thesis": cand.get("thesis") or "",
                    "definition": cand.get("definition") or "",
                    "limits": cand.get("limits") or "",
                    "relevance_score": cand.get("relevance_score"),
                    "anchor_quote": cand.get("anchor_quote") or "",
                }
            )
        items.append(
            {
                **chunk,
                "summary": summary.get("summary", ""),
                "concepts": concepts,
                "rejected_candidates": summary.get("rejected_candidates") or [],
                "excerpt_parts": highlight_spans(
                    chunk.get("text") or "", [item["anchor_quote"] for item in concepts]
                ),
            }
        )
    return items, len(queue), page, page_count


@router.get("/review", response_class=HTMLResponse)
def review(
    request: Request,
    source_id: str = "",
    confidence: str = "",
    page: int = 1,
    queue: str = "",
    relation_type: str = "",
    scope: str = "",
):
    """Each queue is filtered and ordered on light rows and enriched one page at a
    time, so the cost of a page does not grow with the queue (#309)."""
    if not authenticated(request):
        return redirect_login()
    queue = queue if queue in {"rejected", "duplicates", "links"} else "drafts"
    relation_type = relation_type if relation_type in _LINK_RELATIONS else ""
    scope = scope if scope in _LINK_SCOPES else ""
    cfg = service(request).cfg
    db = service(request).db()
    try:
        from zettel.review import pending_dedupe_concepts

        sources = db.list_sources()
        duplicate_count = len(pending_dedupe_concepts(db, source_id or None))
        _, link_count = db.proposed_connections_page(limit=0)
        if queue == "links":
            items, total, page, page_count = _link_page(db, source_id, relation_type, scope, page)
            size = _DRAFT_PAGE_SIZE
        elif queue == "duplicates":
            items, total, page, page_count = _duplicate_page(db, source_id, page)
            size = _DRAFT_PAGE_SIZE
        elif queue == "rejected":
            items, total, page, page_count = _rejected_page(db, source_id, page)
            size = _REJECTED_PAGE_SIZE
        else:
            items, total, page, page_count = _draft_page(db, cfg, source_id, confidence, page)
            size = _DRAFT_PAGE_SIZE
    finally:
        db.close()
    return render(
        request,
        "review.html",
        page="review",
        chunks=items,
        sources=sources,
        selected_source=source_id,
        selected_confidence=confidence,
        selected_relation=relation_type,
        selected_scope=scope,
        relations=_LINK_RELATIONS,
        review_page=page,
        page_count=page_count,
        has_next=page * size < total,
        queue=queue,
        duplicate_count=duplicate_count,
        link_count=link_count,
    )


@router.post("/review/action")
def review_action(
    request: Request,
    action: str = Form(...),
    csrf: str = Form(""),
    chunk_ids: list[str] = Form(default=[]),
    concept_ids: list[str] = Form(default=[]),
):
    from zettel.web_app import REVIEW_ACTIONS

    if action not in REVIEW_ACTIONS:
        return error_page(request, 400, "Ação inválida")
    return post_job(
        request,
        "review",
        {"action": action, "chunk_ids": chunk_ids, "concept_ids": concept_ids},
        csrf,
    )


@router.post("/review/links/batch")
def review_links_batch(
    request: Request,
    action: str = Form(...),
    csrf: str = Form(""),
    edges: list[str] = Form(default=[]),
):
    """Accept or reject the selected edges as one job (#309).

    Each edge is ``source|target|relation``; a malformed one is dropped here and
    an edge that is no longer proposed is skipped by the job.
    """
    if action not in {"accept", "reject"}:
        return error_page(request, 400, "Ação inválida")
    parsed = []
    for raw in edges[:500]:
        parts = raw.split("|")
        if len(parts) == 3 and all(parts):
            parsed.append(parts)
    if not parsed:
        return error_page(request, 400, "Selecione ao menos uma aresta.")
    return post_job(request, "review_links", {"action": action, "edges": parsed}, csrf)


@router.post("/review/links")
def review_links(
    request: Request,
    source_note_id: str = Form(...),
    target_note_id: str = Form(...),
    relation_type: str = Form(...),
    action: str = Form(...),
    new_type: str = Form(""),
    csrf: str = Form(""),
):
    from fastapi.responses import RedirectResponse

    from zettel.web.security import csrf_ok

    if not authenticated(request):
        return redirect_login()
    if not csrf_ok(request, csrf):
        return error_page(request, 403, "CSRF inválido")
    if action not in {"accept", "reject", "retype"}:
        return error_page(request, 400, "Ação inválida")
    from zettel.connector.links import decide_link
    from zettel.oplock import LockBusy

    # One edge is fast enough for the request, but it changes the graph that
    # connect and garden walk: it takes the operation lock like a job (#308).
    try:
        lock = operation_lock(request, "review_links")
    except LockBusy as busy:
        return error_page(request, 409, str(busy))
    db = service(request).db()
    try:
        decide_link(
            service(request).cfg,
            db,
            source_note_id,
            target_note_id,
            relation_type,
            action,
            new_type=new_type or None,
            surface="web",
        )
    finally:
        db.close()
        lock.release()
    return RedirectResponse("/review?queue=links", status_code=303)
