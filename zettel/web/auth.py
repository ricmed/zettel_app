"""Login, logout, favicon."""

from __future__ import annotations

import logging
import secrets
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from zettel.web.rendering import error_page, render, service
from zettel.web.security import (
    LOGIN_COOKIE,
    LOGIN_TOKEN_MAX_AGE,
    SESSION_COOKIE,
    SESSION_MAX_AGE,
    authenticated,
    csrf_ok,
    is_https,
    login_throttle,
    login_token_ok,
    new_login_token,
    password_ok,
    secret,
    session_value,
)

logger = logging.getLogger(__name__)

router = APIRouter()

_FAVICON = Path(__file__).resolve().parent.parent / "static" / "favicon.svg"


@router.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(_FAVICON, media_type="image/svg+xml")


def _login_page(request: Request, *, error: str | None = None, status_code: int = 200):
    """The form with a fresh random token, mirrored in a short-lived cookie."""
    token = new_login_token()
    response = render(request, "login.html", error=error, status_code=status_code, login_csrf=token)
    response.set_cookie(
        LOGIN_COOKIE,
        token,
        httponly=True,
        samesite="strict",
        secure=is_https(request),
        max_age=LOGIN_TOKEN_MAX_AGE,
        path="/login",
    )
    return response


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if not secret():
        return render(
            request,
            "login.html",
            error="SESSION_SECRET não está configurado.",
            login_csrf="",
        )
    if authenticated(request):
        return RedirectResponse("/", status_code=303)
    return _login_page(request)


@router.post("/login", response_class=HTMLResponse)
def login(request: Request, instance_secret: str = Form(...), login_csrf: str = Form("")):
    if not secret() or not login_token_ok(request, login_csrf):
        return error_page(request, 403, "CSRF inválido")
    client = request.client.host if request.client else ""
    throttle = login_throttle(request)
    wait = throttle.retry_after(client)
    if wait:
        page = error_page(request, 429, f"Muitas tentativas. Tente de novo em {wait} s.")
        page.headers["Retry-After"] = str(wait)
        return page
    if not password_ok(instance_secret):
        throttle.failure(client)
        logger.warning("Web: login recusado para %s", client or "cliente desconhecido")
        return _login_page(request, error="Segredo inválido.", status_code=401)
    throttle.success(client)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        session_value(request, secrets.token_urlsafe(24)),
        httponly=True,
        samesite="lax",
        secure=is_https(request),
        max_age=SESSION_MAX_AGE,
    )
    response.delete_cookie(LOGIN_COOKIE, path="/login")
    return response


@router.post("/logout")
def logout(request: Request, csrf: str = Form("")):
    if not csrf_ok(request, csrf):
        return error_page(request, 403, "CSRF inválido")
    # A new signing key ends every session, including a cookie copied elsewhere.
    service(request).rotate_session_key()
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response
