"""Login, logout, favicon."""

from __future__ import annotations

import hmac
import secrets

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from zettel.web.rendering import render
from zettel.web.security import authenticated, csrf_ok, secret, session_value, sign

router = APIRouter()


@router.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(status_code=204)


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if not secret():
        return render(
            request,
            "login.html",
            error="SESSION_SECRET não está configurado.",
            login_csrf="",
        )
    if authenticated(request):
        return RedirectResponse("/", status_code=303)
    return render(request, "login.html", login_csrf=sign("login"))


@router.post("/login", response_class=HTMLResponse)
async def login(request: Request, instance_secret: str = Form(...), login_csrf: str = Form("")):
    valid_login_csrf = bool(
        secret() and login_csrf and hmac.compare_digest(login_csrf, sign("login"))
    )
    if not valid_login_csrf:
        return HTMLResponse("CSRF inválido", status_code=403)
    if not hmac.compare_digest(instance_secret, secret()):
        return render(
            request,
            "login.html",
            error="Segredo inválido.",
            status_code=401,
            login_csrf=sign("login"),
        )
    response = RedirectResponse("/", status_code=303)
    forwarded_proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    response.set_cookie(
        "zettel_session",
        session_value(secrets.token_urlsafe(24)),
        httponly=True,
        samesite="lax",
        secure=forwarded_proto == "https",
        max_age=86400,
    )
    return response


@router.post("/logout")
async def logout(request: Request, csrf: str = Form("")):
    if not csrf_ok(request, csrf):
        return HTMLResponse("CSRF inválido", status_code=403)
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie("zettel_session")
    return response
