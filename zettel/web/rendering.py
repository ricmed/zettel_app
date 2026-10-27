"""Jinja2 templates and the request context shared by every HTML route."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from zettel.markdown import render_text_with_math
from zettel.web.security import session
from zettel.web_app import WebApplication

templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates"),
)


def _local_dt_filter(iso_text: str, style: str = "datetime") -> str:
    if not iso_text:
        return ""
    from zettel.time import format_local_datetime

    tz = templates.env.globals.get("vault_timezone", "America/Sao_Paulo")
    return format_local_datetime(iso_text, tz, style=style)


templates.env.filters["local_dt"] = _local_dt_filter
templates.env.filters["with_math"] = render_text_with_math


def context(request: Request, **extra: Any) -> dict[str, Any]:
    current = session(request)
    return {
        "request": request,
        "authenticated": current is not None,
        "csrf": current.get("csrf") if current else "",
        **extra,
    }


def _next_action(service_obj: WebApplication):
    """The banner of an authenticated page: the earliest phase with work (#316)."""
    from zettel.web.next_action import guidance_counts, next_action
    from zettel.web.preconditions import blocker

    db = service_obj.db()
    try:
        counts = guidance_counts(db)
        active = db.active_web_job()
    finally:
        db.close()
    cfg = service_obj.cfg
    return next_action(
        counts, active_job=active, blocker=lambda operation: blocker(cfg, operation, counts)
    )


def render(
    request: Request,
    name: str,
    *,
    status_code: int = 200,
    **extra: Any,
) -> HTMLResponse:
    service_obj = getattr(request.app.state, "service", None)
    page = context(request, **extra)
    if service_obj is not None:
        templates.env.globals["vault_timezone"] = service_obj.cfg.vault_timezone
        if page["authenticated"] and status_code == 200 and "next_action" not in extra:
            page["next_action"] = _next_action(service_obj)
    return templates.TemplateResponse(
        request=request, name=name, context=page, status_code=status_code
    )


_HEADINGS = {
    400: "Pedido inválido",
    403: "Acesso negado",
    404: "Não encontrado",
    405: "Método não permitido",
    409: "Não é possível agora",
    413: "Grande demais",
    429: "Muitas tentativas",
    500: "Erro interno",
}


def error_page(
    request: Request, status_code: int, detail: str, *, busy_job: dict | None = None
) -> HTMLResponse:
    """An error inside the layout, with a way back, never a bare line of text (#316).

    ``busy_job`` is the job holding the queue, linked from a 409.
    """
    referer = urlsplit(request.headers.get("referer") or "")
    same_page = referer.path == request.url.path
    back = None
    if referer.netloc == request.url.netloc and referer.path.startswith("/") and not same_page:
        back = referer.path + (f"?{referer.query}" if referer.query else "")
    return render(
        request,
        "error.html",
        status_code=status_code,
        heading=_HEADINGS.get(status_code, "Erro"),
        detail=detail,
        busy_job=busy_job,
        back=back,
    )


def service(request: Request) -> WebApplication:
    return request.app.state.service
