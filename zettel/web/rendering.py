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


def context(request: Request, **extra: Any) -> dict[str, Any]:
    current = session(request)
    return {
        "request": request,
        "authenticated": current is not None,
        "csrf": current.get("csrf") if current else "",
        **extra,
    }


def render(
    request: Request, name: str, *, status_code: int = 200, **extra: Any,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name=name,
        context=context(request, **extra),
        status_code=status_code,
    )


def service(request: Request) -> WebApplication:
    return request.app.state.service
