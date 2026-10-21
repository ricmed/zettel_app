"""Pipeline phase buttons."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from zettel.web.enqueue import post_job
from zettel.web.health import embedding_ready, llm_phase_ready
from zettel.web.preconditions import PIPELINE_OPERATIONS, blocker, pipeline_state
from zettel.web.rendering import error_page, render, service
from zettel.web.security import authenticated, csrf_ok, redirect_login

logger = logging.getLogger(__name__)

router = APIRouter()


def _safe_next(raw: str) -> str | None:
    if not raw or not raw.startswith("/") or raw.startswith("//"):
        return None
    if "\\" in raw or "://" in raw:
        return None
    return raw


@router.get("/pipeline", response_class=HTMLResponse)
def pipeline(request: Request):
    if not authenticated(request):
        return redirect_login()
    from zettel import preflight
    from zettel.connector import load_approved_candidates
    from zettel.domain_examples import DomainExamplesLoadError

    cfg = service(request).cfg
    db = service(request).db()
    try:
        stats = db.get_stats()
        state = pipeline_state(db)
        # The CLI asks before paying for these two; here submitting the form is
        # the confirmation, so the estimate sits next to the button (#310).
        candidates = load_approved_candidates(db)
        builders = {}
        if stats.get("chunks_pending"):
            builders["extract"] = lambda: preflight.estimate_extract(cfg, db)
        if candidates:
            builders["connect"] = lambda: preflight.estimate_connect(cfg, db, candidates)
        estimates, estimate_errors = {}, {}
        for op, build in builders.items():
            try:
                estimates[op] = build()
            except (OSError, ValueError, DomainExamplesLoadError) as exc:
                # A missing prompt or few-shot file breaks the phase, not the page.
                logger.warning("Pré-voo de %s indisponível: %s", op, exc)
                estimate_errors[op] = str(exc)[:200]
    finally:
        db.close()
    return render(
        request,
        "pipeline.html",
        page="pipeline",
        stats=stats,
        estimates=estimates,
        estimate_errors=estimate_errors,
        state=state,
        blockers={op: blocker(cfg, op, state) for op in PIPELINE_OPERATIONS},
        missing_phases=[
            phase
            for phase in ("extract", "images", "review", "connect", "garden")
            if (phase != "images" or cfg.images.enabled) and not llm_phase_ready(cfg, phase)
        ],
        embedding_ready=embedding_ready(cfg),
    )


@router.post("/pipeline/{operation}")
def pipeline_action(
    request: Request,
    operation: str,
    csrf: str = Form(""),
    next: str = Form(""),
):
    if operation not in PIPELINE_OPERATIONS:
        return error_page(request, 404, "Operação indisponível")
    if not authenticated(request):
        return redirect_login()
    if not csrf_ok(request, csrf):
        return error_page(request, 403, "CSRF inválido")
    db = service(request).db()
    try:
        reason = blocker(service(request).cfg, operation, pipeline_state(db))
    finally:
        db.close()
    if reason:
        return error_page(request, 409, reason)
    payload: dict = {"hubs": operation == "garden_hubs"}
    nxt = _safe_next(next)
    if nxt:
        payload["next"] = nxt
    return post_job(request, "garden" if operation == "garden_hubs" else operation, payload, csrf)
