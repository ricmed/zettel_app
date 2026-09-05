"""Dashboard."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from zettel.web.rendering import render, service
from zettel.web.security import authenticated, redirect_login

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def overview(request: Request):
    if not authenticated(request):
        return redirect_login()
    svc = service(request)
    return render(
        request,
        "dashboard.html",
        page="overview",
        dashboard=svc.dashboard(),
        jobs=svc.jobs()[:5],
    )
