"""Settings and health."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from zettel.web.health import llm_phase_rows
from zettel.web.health import llm_ready as _llm_ready
from zettel.web.rendering import render, service
from zettel.web.security import authenticated, redirect_login

router = APIRouter()


@router.get("/settings", response_class=HTMLResponse)
async def settings(request: Request):
    if not authenticated(request):
        return redirect_login()
    svc = service(request)
    cfg = svc.cfg
    db = svc.db()
    try:
        health = {
            "fts5": db.fts_enabled,
            "state_db": cfg.state_db_path.exists(),
            "vault": cfg.vault_path.exists(),
            "inbox": cfg.inbox_path.exists(),
            "llm": _llm_ready(cfg),
        }
    finally:
        db.close()
    from zettel.index import _format_space_id, peek_stored_embedding_identity

    stored_p, stored_m, stored_d = peek_stored_embedding_identity(cfg.chroma_path)
    cfg_p, cfg_m, cfg_d = (
        cfg.embedding.provider,
        cfg.embedding.model,
        cfg.embedding.dimensions,
    )
    has_stored = stored_p is not None or stored_m is not None or stored_d is not None
    embedding = {
        "stored": (
            _format_space_id(stored_p, stored_m, stored_d) if has_stored else "ainda não gravado"
        ),
        "configured": _format_space_id(cfg_p, cfg_m, cfg_d),
        "drift": has_stored and (stored_p != cfg_p or stored_m != cfg_m or stored_d != cfg_d),
    }
    return render(
        request,
        "settings.html",
        page="settings",
        cfg=cfg,
        health=health,
        embedding=embedding,
        llm_phases=llm_phase_rows(cfg),
    )
