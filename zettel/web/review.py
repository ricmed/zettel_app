"""Literature review queue."""

from __future__ import annotations

import json

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from zettel.web.enqueue import post_job
from zettel.web.rendering import render, service
from zettel.web.security import authenticated, redirect_login

router = APIRouter()


@router.get("/review", response_class=HTMLResponse)
async def review(request: Request, source_id: str = "", confidence: str = "", page: int = 1):
    if not authenticated(request):
        return redirect_login()
    db = service(request).db()
    try:
        chunks = db.get_chunks_by_status("awaiting_review", source_id or None)
        sources = db.list_sources()
        enriched = []
        for chunk in chunks:
            summary = {}
            try:
                summary = json.loads(chunk.get("summary_json") or "{}")
            except json.JSONDecodeError:
                pass
            enriched.append({
                **chunk,
                "summary": summary.get("summary", ""),
                "candidates": summary.get("candidates", []),
            })
    finally:
        db.close()
    if confidence in {"low", "medium", "high"}:
        threshold = service(request).cfg.literature_review.auto_approve_min_confidence
        enriched = [
            c for c in enriched
            if (
                "low" if (c.get("review_confidence") or 0) < .4 else
                "medium" if (c.get("review_confidence") or 0) < threshold else "high"
            ) == confidence
        ]
    page_size = 20
    total = len(enriched)
    page = max(1, page)
    enriched = enriched[(page - 1) * page_size:page * page_size]
    return render(
        request, "review.html", page="review", chunks=enriched, sources=sources,
        selected_source=source_id, selected_confidence=confidence,
        review_page=page, has_next=page * page_size < total,
    )


@router.post("/review/action")
async def review_action(
    request: Request, action: str = Form(...), csrf: str = Form(""),
    chunk_ids: list[str] = Form(default=[]),
):
    if action not in {"approve", "reject"}:
        return HTMLResponse("Ação inválida", status_code=400)
    return post_job(request, "review", {"action": action, "chunk_ids": chunk_ids}, csrf)
