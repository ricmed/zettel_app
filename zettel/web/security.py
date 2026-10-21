"""Session cookie, CSRF, login throttle and the unauthenticated redirect. No package imports.

Nothing computed from ``SESSION_SECRET`` leaves the server (#305). The login
form carries a random token that must match a cookie (double submit), and the
session cookie is signed with a random key kept in SQLite
(``WebApplication.session_key``). The secret itself is only compared, online,
behind ``LoginThrottle``: an HMAC of a known message under it, sent to a
visitor, would let anyone test candidate secrets offline.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import secrets
import threading
import time

from fastapi import Request
from fastapi.responses import RedirectResponse

SESSION_COOKIE = "zettel_session"
LOGIN_COOKIE = "zettel_login"
SESSION_MAX_AGE = 86400
LOGIN_TOKEN_MAX_AGE = 600


def secret() -> str:
    return os.environ.get("SESSION_SECRET", "")


def password_ok(candidate: str) -> bool:
    """Bytes, not ``str``: ``compare_digest`` raises on non-ASCII text."""
    expected = secret()
    return bool(expected) and hmac.compare_digest(candidate.encode(), expected.encode())


def _sign(request: Request, payload: str) -> str:
    key = request.app.state.service.session_key()
    return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()


def session_value(request: Request, csrf: str) -> str:
    body = (
        base64.urlsafe_b64encode(json.dumps({"csrf": csrf, "created": int(time.time())}).encode())
        .decode()
        .rstrip("=")
    )
    return f"{body}.{_sign(request, body)}"


def session(request: Request) -> dict[str, str] | None:
    if not secret():
        return None
    raw = request.cookies.get(SESSION_COOKIE, "")
    try:
        body, signature = raw.rsplit(".", 1)
        if not hmac.compare_digest(signature.encode(), _sign(request, body).encode()):
            return None
        body += "=" * (-len(body) % 4)
        data = json.loads(base64.urlsafe_b64decode(body))
        if int(time.time()) - int(data.get("created", 0)) > SESSION_MAX_AGE:
            return None
        return data
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def csrf_ok(request: Request, token: str | None) -> bool:
    current = session(request)
    return bool(
        current and token and hmac.compare_digest(current.get("csrf", "").encode(), token.encode())
    )


def authenticated(request: Request) -> bool:
    return session(request) is not None


def redirect_login() -> RedirectResponse:
    return RedirectResponse("/login", status_code=303)


def new_login_token() -> str:
    return secrets.token_urlsafe(32)


def login_token_ok(request: Request, token: str | None) -> bool:
    """The form token must equal the ``zettel_login`` cookie set with the page."""
    cookie = request.cookies.get(LOGIN_COOKIE, "")
    return bool(cookie and token and hmac.compare_digest(cookie.encode(), token.encode()))


def is_https(request: Request) -> bool:
    return request.headers.get("x-forwarded-proto", request.url.scheme) == "https"


class LoginThrottle:
    """Per-client lockout after repeated failures, doubling up to 15 minutes.

    In memory: the web is one process (ADR-023). Behind a proxy every client
    shares one address, which locks the single user out together with the
    attacker; for a one-person instance that is the safe side.
    """

    FREE_FAILURES = 5
    MAX_LOCK_SECONDS = 900
    _MAX_CLIENTS = 10_000

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._failures: dict[str, tuple[int, float]] = {}

    def retry_after(self, client: str) -> int:
        """Seconds until ``client`` may try again; 0 when it may try now."""
        with self._lock:
            _count, until = self._failures.get(client, (0, 0.0))
            return max(0, math.ceil(until - self._clock()))

    def failure(self, client: str) -> None:
        with self._lock:
            now = self._clock()
            if len(self._failures) >= self._MAX_CLIENTS:
                self._failures = {
                    key: value for key, value in self._failures.items() if value[1] > now
                }
            count = self._failures.get(client, (0, 0.0))[0] + 1
            until = 0.0
            if count >= self.FREE_FAILURES:
                until = now + min(2 ** (count - self.FREE_FAILURES), self.MAX_LOCK_SECONDS)
            self._failures[client] = (count, until)

    def success(self, client: str) -> None:
        with self._lock:
            self._failures.pop(client, None)


_THROTTLE_LOCK = threading.Lock()


def login_throttle(request: Request) -> LoginThrottle:
    """One throttle per app, created on first use."""
    state = request.app.state
    with _THROTTLE_LOCK:
        if not hasattr(state, "login_throttle"):
            state.login_throttle = LoginThrottle()
        return state.login_throttle
