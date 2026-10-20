"""Error pages with the layout and the "next action" banner (#316)."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from zettel.web import create_app
from zettel.web.next_action import next_action

_WEB = Path(__file__).resolve().parent.parent / "zettel" / "web"


@pytest.fixture
def web_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
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
                "images:",
                "  enabled: false",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("SESSION_SECRET", "web-test-secret")
    with TestClient(create_app(config), raise_server_exceptions=False) as client:
        yield client, tmp_path


def _login(client: TestClient) -> str:
    page = client.get("/login")
    token = re.search(r'name="login_csrf" value="([^"]+)"', page.text).group(1)
    client.post(
        "/login",
        data={"instance_secret": "web-test-secret", "login_csrf": token},
        follow_redirects=False,
    )
    return re.search(r'name="csrf" value="([^"]+)"', client.get("/").text).group(1)


def _is_error_page(response, status: int) -> bool:
    return (
        response.status_code == status
        and 'id="error-page"' in response.text
        and 'class="shell"' in response.text
        and 'href="/"' in response.text
    )


def test_not_found_pages_have_the_layout(web_client):
    client, _ = web_client
    _login(client)
    assert _is_error_page(client.get("/notes/nao-existe"), 404)
    assert _is_error_page(client.get("/rota-que-nao-existe"), 404)
    assert client.get("/api/jobs/nao-existe").headers["content-type"].startswith("application/json")


def test_forbidden_and_incomplete_forms_have_the_layout(web_client):
    client, _ = web_client
    _login(client)
    assert _is_error_page(client.post("/pipeline/extract", data={"csrf": "errado"}), 403)
    # A missing required field used to answer FastAPI's 422 JSON.
    assert _is_error_page(client.post("/review/action", data={}), 400)


def test_an_unhandled_error_shows_a_reference_the_log_has(web_client, monkeypatch, caplog):
    client, _ = web_client
    _login(client)

    def broken(self):
        raise RuntimeError(r"falhou em D:\projetos\segredo.db")

    monkeypatch.setattr("zettel.web_app.WebApplication.jobs", broken)
    with caplog.at_level("ERROR"):
        response = client.get("/runs")
    assert _is_error_page(response, 500)
    assert "segredo" not in response.text
    ref = re.search(r"ref\. ([0-9a-f]{8})", response.text).group(1)
    assert any(ref in record.getMessage() for record in caplog.records)


def test_a_busy_slot_links_to_the_job_that_holds_it(web_client):
    client, _ = web_client
    csrf = _login(client)
    db = client.app.state.service.db()
    try:
        db.upsert_source("@A2024", "A2024", "Fonte", ["A"], 2024, "h", "/a.md", "md")
        db.upsert_asset("a1", "@A2024", "90_Assets/a.png", "img", status="failed")
        assert db.create_web_job("ocupante", "garden", {})
    finally:
        db.close()
    client.app.state.service.worker.stop()  # keep the job queued
    response = client.post("/pipeline/retry_assets", data={"csrf": csrf})
    assert _is_error_page(response, 409)
    assert 'href="/jobs/ocupante"' in response.text
    assert "garden" in response.text


def test_route_modules_return_no_plain_text_error():
    """Every HTML error goes through error_page, never HTMLResponse("...", status_code=4xx)."""
    offenders = []
    for path in _WEB.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and getattr(node.func, "id", None) == "HTMLResponse"
                and any(k.arg == "status_code" for k in node.keywords)
            ):
                offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == []


_EMPTY = {
    "sources": 3,
    "chunks_pending": 0,
    "chunks_awaiting_review": 0,
    "concepts_dedupe_pending": 0,
    "connect_queue": 0,
    "links_proposed": 0,
    "notes": 5,
    "notes_since_garden": 0,
}


@pytest.mark.parametrize(
    ("counts", "href", "word"),
    [
        ({"sources": 0, "notes": 0}, "/documents", "documento"),
        ({"chunks_pending": 4, "chunks_awaiting_review": 2}, "/pipeline", "extract"),
        ({"chunks_awaiting_review": 2, "connect_queue": 1}, "/review", "drafts"),
        (
            {"concepts_dedupe_pending": 1, "connect_queue": 1},
            "/review?queue=duplicates",
            "duplicat",
        ),
        ({"connect_queue": 3, "links_proposed": 9}, "/pipeline", "connect"),
        ({"links_proposed": 9, "notes_since_garden": 4}, "/review?queue=links", "arestas"),
        ({"notes_since_garden": 4}, "/pipeline", "garden"),
        ({}, None, None),
    ],
)
def test_next_action_points_at_the_earliest_unfinished_phase(counts, href, word):
    action = next_action({**_EMPTY, **counts})
    if href is None:
        assert action is None
        return
    assert action.href == href
    assert word in (action.label + action.detail).lower()


def test_next_action_names_a_job_in_progress_first():
    action = next_action(
        {**_EMPTY, "chunks_pending": 4}, active_job={"job_id": "j1", "operation": "extract"}
    )
    assert action.href == "/jobs/j1"
    assert "extract" in action.label


def test_next_action_says_when_the_phase_is_blocked():
    action = next_action(
        {**_EMPTY, "chunks_pending": 4},
        blocker=lambda operation: "A fase extract não tem credencial de LLM.",
    )
    assert action.href == "/settings"
    assert "credencial" in action.detail


def test_authenticated_pages_show_the_banner(web_client):
    client, _ = web_client
    _login(client)
    page = client.get("/").text
    assert 'id="next-action"' in page
    assert 'href="/documents"' in page  # an empty vault starts with a document
    assert 'id="next-action"' not in client.get("/notes/nao-existe").text
