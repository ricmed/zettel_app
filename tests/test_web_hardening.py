"""Security headers, the request body limit and CSP-safe templates (#315)."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from zettel.web import create_app
from zettel.web.hardening import MAX_BODY_BYTES, BodyLimit

_TEMPLATES = Path(__file__).resolve().parent.parent / "zettel" / "templates"


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config = tmp_path / "config.yaml"
    config.write_text(
        "\n".join(
            [
                f"vault_path: {tmp_path / 'vault'}",
                f"inbox_path: {tmp_path / 'inbox'}",
                f"chroma_path: {tmp_path / 'chroma'}",
                f"state_db_path: {tmp_path / 'state.db'}",
                f"cache_path: {tmp_path / 'cache'}",
                f"prompts_path: {Path('prompts').resolve()}",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("SESSION_SECRET", "web-test-secret")
    with TestClient(create_app(config)) as test_client:
        yield test_client


@pytest.mark.parametrize("path", ["/login", "/", "/static/app.css", "/nao-existe"])
def test_every_response_carries_the_security_headers(client, path):
    response = client.get(path, follow_redirects=False)
    csp = response.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp
    assert "script-src 'self'" in csp
    assert "unsafe-inline" not in csp
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "same-origin"


@pytest.mark.parametrize("template", sorted(p.name for p in _TEMPLATES.glob("*.html")))
def test_templates_need_no_unsafe_inline(template):
    """CSP without 'unsafe-inline' blocks inline scripts, handlers and style attributes."""
    text = (_TEMPLATES / template).read_text(encoding="utf-8")
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", text), "inline <script>"
    assert not re.search(r"\son[a-z]+\s*=", text), "inline event handler"
    assert not re.search(r"\sstyle\s*=", text), "style attribute"
    assert "<style" not in text


def _call(app, headers, body=b""):
    """Drive one ASGI request; return the status sent and how often body was read."""
    reads = []
    sent = {}

    async def receive():
        reads.append(1)
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            sent["status"] = message["status"]

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/documents/upload",
        "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
    }
    asyncio.run(app(scope, receive, send))
    return sent.get("status"), len(reads)


def test_body_over_the_limit_is_refused_before_it_is_read():
    async def app(scope, receive, send):  # would read the body if reached
        await receive()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    limited = BodyLimit(app)
    status, reads = _call(limited, {"content-length": str(MAX_BODY_BYTES + 1)})
    assert (status, reads) == (413, 0)
    assert _call(limited, {"content-length": "10"}, b"0123456789") == (200, 1)
    assert _call(limited, {"transfer-encoding": "chunked"})[0] == 411


def test_upload_over_the_limit_never_reaches_the_inbox(client, tmp_path):
    page = client.get("/login")
    token = re.search(r'name="login_csrf" value="([^"]+)"', page.text).group(1)
    client.post(
        "/login",
        data={"instance_secret": "web-test-secret", "login_csrf": token},
        follow_redirects=False,
    )
    csrf = re.search(r'name="csrf" value="([^"]+)"', client.get("/").text).group(1)
    big = b"x" * (MAX_BODY_BYTES + 1)
    response = client.post(
        "/documents/upload",
        data={"csrf": csrf},
        files={"file": ("grande.md", big, "text/markdown")},
    )
    assert response.status_code == 413
    assert not (tmp_path / "inbox" / "grande.md").exists()
