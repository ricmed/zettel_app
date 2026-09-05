"""Auth + CSRF + 409 wrapper around ``WebApplication.submit``."""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse

from zettel.web.rendering import render, service
from zettel.web.security import authenticated, csrf_ok, redirect_login


def post_job(request: Request, operation: str, payload: dict[str, Any], csrf: str):
    if not authenticated(request):
        return redirect_login()
    if not csrf_ok(request, csrf):
        return HTMLResponse("CSRF inválido", status_code=403)
    job_id = service(request).submit(operation, payload)
    if not job_id:
        return render(
            request,
            "jobs.html",
            page="jobs",
            jobs=service(request).jobs(),
            error="Outra operação mutante já está em andamento.",
            status_code=409,
        )
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)
