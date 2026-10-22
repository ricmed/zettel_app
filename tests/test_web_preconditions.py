"""One precondition per web operation, shared by the button and the handler (#314)."""

from __future__ import annotations

import re

import pytest
from fastapi.responses import RedirectResponse
from zettel.config import LLM_PHASES
from zettel.web.preconditions import PIPELINE_OPERATIONS, blocker, llm_phases

from tests.web_support import login

_ALL = frozenset(LLM_PHASES)


def _seed_work(db):
    """One item of work for every pipeline operation."""
    db.upsert_source("@Ana2024", "Ana2024", "Livro", ["Ana"], 2024, "h", "/x.md", "md")
    db.upsert_chapter("@Ana2024::ch000", "@Ana2024", "Cap", "chh")
    for chunk_id, status in (("pend", "pending"), ("fail", "failed"), ("ok", "approved")):
        db.upsert_chunk(
            f"@Ana2024::ch000::{chunk_id}",
            "@Ana2024",
            "@Ana2024::ch000",
            "texto",
            f"ck-{chunk_id}",
            status=status,
        )
    db.upsert_concept(
        "c1",
        "@Ana2024",
        "@Ana2024::ch000::ok",
        candidate_json='{"thesis": "Uma tese.", "definition": "Uma definição."}',
        status="approved",
    )
    db.upsert_note("n1", "@Ana2024", None, title="Nota", body="corpo")
    db.upsert_asset("a1", "@Ana2024", "90_Assets/a.png", "img", status="failed")


def _credentials(monkeypatch, phases, embedding):
    monkeypatch.setattr(
        "zettel.web.preconditions.llm_phase_ready", lambda cfg, phase: phase in phases
    )
    monkeypatch.setattr("zettel.web.preconditions.embedding_ready", lambda cfg: embedding)


def _buttons(html: str) -> dict[str, bool]:
    """Operation -> whether its Executar button is enabled on /pipeline."""
    found = re.findall(
        r'action="/pipeline/([a-z_]+)">.*?<button class="button"( disabled)?', html, re.S
    )
    return {op: not disabled for op, disabled in found}


@pytest.fixture
def accepted_jobs(monkeypatch):
    """Accept a submit without running it: only the gate is under test."""
    submitted = []

    def fake_post_job(request, operation, payload, csrf):
        submitted.append(operation)
        return RedirectResponse("/runs", status_code=303)

    monkeypatch.setattr("zettel.web.pipeline.post_job", fake_post_job)
    return submitted


_CASES = [
    (frozenset(), False),
    (_ALL, False),
    (_ALL, True),
    (_ALL - {"article"}, True),
    (_ALL - {"extract"}, True),
    (_ALL - {"connect"}, True),
    (_ALL - {"garden"}, True),
    (_ALL - {"images"}, True),
]


@pytest.mark.parametrize(("phases", "embedding"), _CASES)
@pytest.mark.parametrize("seeded", [False, True])
def test_pipeline_button_enabled_iff_the_server_accepts(
    web_client, monkeypatch, accepted_jobs, phases, embedding, seeded
):
    client, _ = web_client
    csrf = login(client)
    if seeded:
        db = client.app.state.service.db()
        try:
            _seed_work(db)
        finally:
            db.close()
    _credentials(monkeypatch, phases, embedding)

    enabled = _buttons(client.get("/pipeline").text)
    assert set(enabled) == set(PIPELINE_OPERATIONS)
    for operation in PIPELINE_OPERATIONS:
        response = client.post(
            f"/pipeline/{operation}", data={"csrf": csrf}, follow_redirects=False
        )
        accepted = response.status_code == 303
        assert accepted == enabled[operation], (operation, response.text)
        assert accepted or response.status_code == 409


def test_extract_does_not_need_the_article_credential(web_client, monkeypatch, accepted_jobs):
    client, _ = web_client
    csrf = login(client)
    db = client.app.state.service.db()
    try:
        _seed_work(db)
    finally:
        db.close()
    _credentials(monkeypatch, _ALL - {"article", "ask", "summarize"}, embedding=False)

    assert _buttons(client.get("/pipeline").text)["extract"]
    response = client.post("/pipeline/extract", data={"csrf": csrf}, follow_redirects=False)
    assert response.status_code == 303
    assert accepted_jobs == ["extract"]


def test_without_embedding_connect_and_ask_say_why(web_client, monkeypatch, accepted_jobs):
    client, _ = web_client
    csrf = login(client)
    db = client.app.state.service.db()
    try:
        _seed_work(db)
    finally:
        db.close()
    _credentials(monkeypatch, _ALL, embedding=False)

    page = client.get("/pipeline").text
    assert "Sem credencial de embedding" in page
    connect = client.post("/pipeline/connect", data={"csrf": csrf})
    assert connect.status_code == 409
    assert "Sem credencial de embedding" in connect.text

    studio = client.get("/studio").text
    assert "Sem credencial de embedding" in studio
    ask = client.post("/studio/ask", data={"csrf": csrf, "question": "O que é RAG?"})
    assert ask.status_code == 409
    assert "Sem credencial de embedding" in ask.text


def test_retry_assets_refuses_when_nothing_failed(web_client, monkeypatch, accepted_jobs):
    client, _ = web_client
    csrf = login(client)
    response = client.post("/pipeline/retry_assets", data={"csrf": csrf})
    assert response.status_code == 409
    assert "assets com falha" in response.text
    assert accepted_jobs == []


@pytest.mark.parametrize(
    ("operation", "expected"),
    [
        ("extract", {"extract"}),
        ("connect", {"connect"}),
        ("garden", {"garden"}),
        ("garden_hubs", {"garden"}),
        ("sync", set()),
        ("retry_chunks", set()),
        ("retry_assets", set()),
        ("run_all", {"harvest", "extract", "review", "connect", "garden"}),
        ("ask", {"ask"}),
        ("article", {"article"}),
        ("summarize", {"summarize"}),
        ("catalog", set()),
    ],
)
def test_each_operation_names_only_the_phases_it_calls(web_client, operation, expected):
    client, _ = web_client
    cfg = client.app.state.service.cfg
    assert set(llm_phases(cfg, operation)) == expected


def test_extract_needs_the_images_phase_only_when_images_are_on(web_client):
    client, _ = web_client
    cfg = client.app.state.service.cfg.model_copy(deep=True)
    cfg.images.enabled = True
    assert set(llm_phases(cfg, "extract")) == {"extract", "images"}
    assert "images" in llm_phases(cfg, "run_all")


def test_blocker_reports_state_before_credentials(web_client, monkeypatch):
    """Nothing to do is the more useful answer than a missing key."""
    client, _ = web_client
    _credentials(monkeypatch, frozenset(), embedding=False)
    cfg = client.app.state.service.cfg
    empty = dict.fromkeys(
        ("chunks_pending", "connect_queue", "notes", "chunks_failed", "assets_failed"), 0
    )
    assert "chunks pendentes" in blocker(cfg, "extract", empty)
    assert "credencial" in blocker(cfg, "ask")
