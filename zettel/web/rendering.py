"""Jinja2 templates and the request context shared by every HTML route."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

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


def context(request: Request, **extra: Any) -> dict[str, Any]:
    current = session(request)
    return {
        "request": request,
        "authenticated": current is not None,
        "csrf": current.get("csrf") if current else "",
        **extra,
    }


def render(
    request: Request,
    name: str,
    *,
    status_code: int = 200,
    **extra: Any,
) -> HTMLResponse:
    service_obj = getattr(request.app.state, "service", None)
    if service_obj is not None:
        templates.env.globals["vault_timezone"] = service_obj.cfg.vault_timezone
    return templates.TemplateResponse(
        request=request,
        name=name,
        context=context(request, **extra),
        status_code=status_code,
    )


def service(request: Request) -> WebApplication:
    return request.app.state.service
