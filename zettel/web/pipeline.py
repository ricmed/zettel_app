"""Pipeline phase buttons."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from zettel.web.enqueue import post_job
from zettel.web.health import llm_ready as _llm_ready
from zettel.web.rendering import render, service
from zettel.web.security import authenticated, csrf_ok, redirect_login

router = APIRouter()

_OPERATIONS = {"extract", "connect", "garden", "garden_hubs", "sync", "retry_chunks", "retry_assets"}


def _safe_next(raw: str) -> str | None:
    if not raw or not raw.startswith("/") or raw.startswith("//"):
        return None
    if "\\" in raw or "://" in raw:
        return None
    return raw


@router.get("/pipeline", response_class=HTMLResponse)
async def pipeline(request: Request):
    if not authenticated(request):
        return redirect_login()
    db = service(request).db()
    try:
        stats = db.get_stats()
    finally:
        db.close()
    return render(
        request,
        "pipeline.html",
        page="pipeline",
        stats=stats,
        llm_ready=_llm_ready(service(request).cfg),
    )


@router.post("/pipeline/{operation}")
async def pipeline_action(
    request: Request, operation: str, csrf: str = Form(""), next: str = Form(""),
):
    if operation not in _OPERATIONS:
        return HTMLResponse("Operação indisponível", status_code=404)
    if not authenticated(request):
        return redirect_login()
    if not csrf_ok(request, csrf):
        return HTMLResponse("CSRF inválido", status_code=403)
    db = service(request).db()
    try:
        stats = db.get_stats()
        if operation == "extract" and not stats.get("chunks_pending"):
            return HTMLResponse(
                "Não há chunks pendentes. Execute um harvest válido primeiro.",
                status_code=409,
            )
        if operation == "connect":
            if not db.get_concepts_by_status("approved", without_notes=True):
                return HTMLResponse("Nenhum candidato aprovado aguardando connect.", status_code=409)
        if operation in {"garden", "garden_hubs"} and not stats.get("notes"):
            return HTMLResponse("Não há notas permanentes para jardinagem.", status_code=409)
        if operation == "retry_chunks" and not stats.get("chunks_failed"):
            return HTMLResponse("Não há chunks com falha para reprocessar.", status_code=409)
        if (
            operation in {"extract", "connect", "garden", "garden_hubs"}
            and not _llm_ready(service(request).cfg)
        ):
            return HTMLResponse(
                "O provedor LLM não possui credencial configurada. "
                "Verifique Configuração / saúde.",
                status_code=409,
            )
    finally:
        db.close()
    payload: dict = {"hubs": operation == "garden_hubs"}
    nxt = _safe_next(next)
    if nxt:
        payload["next"] = nxt
    return post_job(request, "garden" if operation == "garden_hubs" else operation, payload, csrf)
