"""Response headers and the request body limit, as plain ASGI middleware (#315).

Plain ASGI rather than ``BaseHTTPMiddleware``: the headers are added to the
``http.response.start`` message as it passes, and the body limit answers
before the application reads a single byte, so nothing is buffered.
"""

from __future__ import annotations

from typing import Any

# The largest upload (25 MB, ``documents.MAX_UPLOAD_BYTES``) plus room for the
# multipart envelope and the other form fields.
MAX_BODY_BYTES = 26 * 1024 * 1024

# No 'unsafe-inline': every script is a file under /static and no template
# carries an event handler or a style attribute (tests/test_web_hardening.py).
# frame-ancestors replaces X-Frame-Options.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; "
    "connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; "
    "frame-ancestors 'none'"
)

_HEADERS = [
    (b"content-security-policy", CONTENT_SECURITY_POLICY.encode()),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"same-origin"),
]


class SecurityHeaders:
    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                names = {name.lower() for name, _ in message.get("headers", [])}
                extra = [(name, value) for name, value in _HEADERS if name not in names]
                message = {**message, "headers": [*message.get("headers", []), *extra]}
            await send(message)

        await self.app(scope, receive, send_with_headers)


class BodyLimit:
    """Refuse a body over ``MAX_BODY_BYTES`` by its Content-Length, unread.

    A chunked body has no length to check up front, and no browser sends one
    for a form, so it is refused (411) rather than counted while parsed.
    """

    def __init__(self, app: Any, limit: int = MAX_BODY_BYTES):
        self.app = app
        self.limit = limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        if b"transfer-encoding" in headers:
            await _plain(send, 411, "Envie o corpo com Content-Length.")
            return
        length = headers.get(b"content-length", b"0")
        if not length.isdigit() or int(length) > self.limit:
            mb = self.limit // (1024 * 1024)
            await _plain(send, 413, f"Requisição acima de {mb} MB.")
            return
        await self.app(scope, receive, send)


async def _plain(send, status: int, text: str) -> None:
    body = text.encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", str(len(body)).encode()),
                (b"connection", b"close"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
