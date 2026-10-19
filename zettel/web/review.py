"""Literature review queue."""

from __future__ import annotations

import contextlib
import json

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from zettel.web.enqueue import post_job
from zettel.web.rendering import render, service
from zettel.web.security import authenticated, redirect_login

router = APIRouter()

_DRAFT_PAGE_SIZE = 20
_REJECTED_PAGE_SIZE = 10


def _summary(chunk: dict) -> dict:
    summary = {}
    with contextlib.suppress(json.JSONDecodeError, TypeError):
        summary = json.loads(chunk.get("summary_json") or "{}")
    return summary if isinstance(summary, dict) else {}


@router.get("/review", response_class=HTMLResponse)
async def review(
    request: Request,
    source_id: str = "",
    confidence: str = "",
    page: int = 1,
    queue: str = "",
):
    if not authenticated(request):
        return redirect_login()
    queue = queue if queue in {"rejected", "duplicates"} else "drafts"
    db = service(request).db()
    try:
        from zettel.review import format_dedupe_item, pending_dedupe_concepts

        sources = db.list_sources()
        duplicate_count = len(pending_dedupe_concepts(db, source_id or None))
        if queue == "duplicates":
            enriched = [
                {
                    "concept_id": concept["concept_id"],
                    "card": format_dedupe_item(db, concept),
                }
                for concept in pending_dedupe_concepts(db, source_id or None)
            ]
            page_size = _DRAFT_PAGE_SIZE
        elif queue == "rejected":
            from zettel.review import extract_rejected_chunks

            enriched = []
            for chunk in extract_rejected_chunks(db, source_id or None):
                summary = _summary(chunk)
                from zettel.review import extract_rejection_origin

                enriched.append(
                    {
                        **chunk,
                        "rejection_origin": extract_rejection_origin(chunk),
                        "rejection_category": summary.get("rejection_category") or "",
                        "rejection_reason": summary.get("rejection_reason") or "",
                        "rejected_candidates": summary.get("rejected_candidates") or [],
                    }
                )
            page_size = _REJECTED_PAGE_SIZE
        else:
            from zettel.review import highlight_spans, ordered_review_concepts

            chunks = db.get_chunks_by_status("awaiting_review", source_id or None)
            enriched = []
            for chunk in chunks:
                summary = _summary(chunk)
                concepts = []
                for concept in ordered_review_concepts(db, chunk):
                    cand = _summary({"summary_json": concept.get("candidate_json")})
                    concepts.append(
                        {
                            "concept_id": concept["concept_id"],
                            "thesis": cand.get("thesis") or "",
                            "definition": cand.get("definition") or "",
                            "intuition": cand.get("intuition") or "",
                            "limits": cand.get("limits") or "",
                            "relevance_score": cand.get("relevance_score"),
                            "anchor_quote": cand.get("anchor_quote") or "",
                        }
                    )
                text = chunk.get("text") or ""
                enriched.append(
                    {
                        **chunk,
                        "summary": summary.get("summary", ""),
                        "concepts": concepts,
                        "rejected_candidates": summary.get("rejected_candidates") or [],
                        "excerpt_parts": highlight_spans(
                            text, [item["anchor_quote"] for item in concepts]
                        ),
                    }
                )
            from zettel.review import BAND_HIGH, BAND_MEDIUM, BAND_VERY_LOW, filter_chunks_by_band

            band = {"low": BAND_VERY_LOW, "medium": BAND_MEDIUM, "high": BAND_HIGH}.get(confidence)
            if band:
                threshold = service(request).cfg.literature_review.auto_approve_min_confidence
                enriched = filter_chunks_by_band(enriched, band, threshold)
            page_size = _DRAFT_PAGE_SIZE
    finally:
        db.close()
    total = len(enriched)
    page_count = max(1, (total + page_size - 1) // page_size) if total else 1
    page = min(max(1, page), page_count)
    enriched = enriched[(page - 1) * page_size : page * page_size]
    return render(
        request,
        "review.html",
        page="review",
        chunks=enriched,
        sources=sources,
        selected_source=source_id,
        selected_confidence=confidence,
        review_page=page,
        page_count=page_count,
        has_next=page * page_size < total,
        queue=queue,
        duplicate_count=duplicate_count,
    )


@router.post("/review/action")
async def review_action(
    request: Request,
    action: str = Form(...),
    csrf: str = Form(""),
    chunk_ids: list[str] = Form(default=[]),
    concept_ids: list[str] = Form(default=[]),
):
    if action not in {
        "approve",
        "reject",
        "requeue",
        "discard",
        "force",
        "keep_duplicate",
        "discard_duplicate",
    }:
        return HTMLResponse("Ação inválida", status_code=400)
    return post_job(
        request,
        "review",
        {"action": action, "chunk_ids": chunk_ids, "concept_ids": concept_ids},
        csrf,
    )
