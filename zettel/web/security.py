"""Session cookie, CSRF, and the unauthenticated redirect. No package imports."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time

from fastapi import Request
from fastapi.responses import RedirectResponse


def secret() -> str:
    return os.environ.get("SESSION_SECRET", "")


def sign(payload: str) -> str:
    return hmac.new(secret().encode(), payload.encode(), hashlib.sha256).hexdigest()


def session_value(csrf: str) -> str:
    body = (
        base64.urlsafe_b64encode(json.dumps({"csrf": csrf, "created": int(time.time())}).encode())
        .decode()
        .rstrip("=")
    )
    return f"{body}.{sign(body)}"


def session(request: Request) -> dict[str, str] | None:
    if not secret():
        return None
    raw = request.cookies.get("zettel_session", "")
    try:
        body, signature = raw.rsplit(".", 1)
        if not hmac.compare_digest(signature, sign(body)):
            return None
        body += "=" * (-len(body) % 4)
        data = json.loads(base64.urlsafe_b64decode(body))
        if int(time.time()) - int(data.get("created", 0)) > 86400:
            return None
        return data
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def csrf_ok(request: Request, token: str | None) -> bool:
    current = session(request)
    return bool(current and token and hmac.compare_digest(current.get("csrf", ""), token))


def authenticated(request: Request) -> bool:
    return session(request) is not None


def redirect_login() -> RedirectResponse:
    return RedirectResponse("/login", status_code=303)
