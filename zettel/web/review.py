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
    queue = "rejected" if queue == "rejected" else "drafts"
    db = service(request).db()
    try:
        sources = db.list_sources()
        if queue == "rejected":
            from zettel.review import extract_rejected_chunks

            enriched = []
            for chunk in extract_rejected_chunks(db, source_id or None):
                summary = _summary(chunk)
                enriched.append(
                    {
                        **chunk,
                        "rejection_category": summary.get("rejection_category") or "",
                        "rejection_reason": summary.get("rejection_reason") or "",
                    }
                )
            page_size = _REJECTED_PAGE_SIZE
        else:
            chunks = db.get_chunks_by_status("awaiting_review", source_id or None)
            enriched = []
            for chunk in chunks:
                summary = _summary(chunk)
                enriched.append(
                    {
                        **chunk,
                        "summary": summary.get("summary", ""),
                        "candidates": summary.get("candidates", []),
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
    )


@router.post("/review/action")
async def review_action(
    request: Request,
    action: str = Form(...),
    csrf: str = Form(""),
    chunk_ids: list[str] = Form(default=[]),
):
    if action not in {"approve", "reject", "requeue", "discard", "force"}:
        return HTMLResponse("Ação inválida", status_code=400)
    return post_job(request, "review", {"action": action, "chunk_ids": chunk_ids}, csrf)
