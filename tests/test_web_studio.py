"""Acervo page: ask, catalog, summarize, article and skill."""

from __future__ import annotations

import re
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from zettel.article import ArticleResult
from zettel.article_graph.graph import ArticleStep
from zettel.ask import AskResult, AskSource
from zettel.catalog import CatalogResult, ChapterMatch, SourceMatch
from zettel.summarize import SummarizeOutcome
from zettel.web import create_app


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
    (tmp_path / "vault").mkdir()
    monkeypatch.setenv("SESSION_SECRET", "web-test-secret")
    with TestClient(create_app(config)) as client:
        yield client, tmp_path


def _login(client: TestClient) -> str:
    login_page = client.get("/login")
    token = re.search(r'name="login_csrf" value="([^"]+)"', login_page.text).group(1)
    client.post(
        "/login",
        data={"instance_secret": "web-test-secret", "login_csrf": token},
        follow_redirects=False,
    )
    page = client.get("/studio")
    return re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)


def _wait(client: TestClient, job_id: str, states: set[str]) -> dict:
    payload = {}
    for _ in range(50):
        payload = client.get(f"/api/jobs/{job_id}").json()
        if payload["job"]["state"] in states:
            return payload
        time.sleep(0.05)
    return payload


def test_studio_page_groups_the_five_operations(web_client):
    client, _ = web_client
    assert client.get("/studio", follow_redirects=False).status_code in {302, 303}
    _login(client)
    page = client.get("/studio")
    assert page.status_code == 200
    for label in (
        "Consultar",
        "Preparar",
        "Produzir",
        "Perguntar",
        "Catálogo",
        "Resumir",
        "Artigo",
        "Skill",
    ):
        assert label in page.text
    assert 'name="outline_only"' in page.text
    assert 'data-review="outline"' in page.text
    assert 'href="/studio"' in client.get("/").text


def test_studio_rejects_invalid_forms(web_client, monkeypatch):
    client, _ = web_client
    csrf = _login(client)
    monkeypatch.setattr("zettel.web.health.llm_phase_ready", lambda cfg, phase: False)
    assert client.post("/studio/ask", data={"csrf": "wrong", "question": "x"}).status_code == 403
    empty = client.post("/studio/ask", data={"csrf": csrf, "question": "  "})
    assert empty.status_code == 400
    assert "Escreva a pergunta" in empty.text
    blocked = client.post("/studio/ask", data={"csrf": csrf, "question": "O que é RAG?"})
    assert blocked.status_code == 409
    skill = client.post("/studio/skill", data={"csrf": csrf, "source_id": "@A", "topic": "Redes"})
    assert skill.status_code == 400
    assert "exatamente um recorte" in skill.text
    none = client.post("/studio/skill", data={"csrf": csrf})
    assert none.status_code == 400


def test_catalog_renders_on_the_page_and_refuses_a_busy_index(web_client, monkeypatch):
    client, _ = web_client
    _login(client)
    monkeypatch.setattr("zettel.web.health.embedding_ready", lambda cfg: False)
    missing = client.get("/studio", params={"subject": "rag", "show_context": "1"})
    assert missing.status_code == 409
    assert "embedding" in missing.text

    monkeypatch.setattr("zettel.web.health.embedding_ready", lambda cfg: True)

    def fake_catalog(cfg, db, idx, subject):
        return CatalogResult(
            query=subject,
            sources=[
                SourceMatch(
                    source_id="@A",
                    citekey="Autor2020",
                    title="Livro de RAG",
                    chapters=[
                        ChapterMatch(
                            chapter_id="c1",
                            source_id="@A",
                            chapter_title="Capítulo 1",
                            score=1.0,
                            note_count=3,
                            via_notes=["N1"],
                            matched_notes=1,
                        )
                    ],
                )
            ],
            retrieval_params={"mode": "hybrid"},
        )

    monkeypatch.setattr("zettel.catalog.run_catalog", fake_catalog)
    monkeypatch.setattr("zettel.index.VectorIndex", lambda **kwargs: object())
    page = client.get("/studio", params={"subject": "rag", "show_context": "1"})
    assert page.status_code == 200
    assert "Livro de RAG" in page.text
    assert "## Livro de RAG" in page.text
    assert 'id="catalog-copy"' in page.text
    assert "Capítulo 1" in page.text
    assert "3" in page.text

    monkeypatch.setattr(
        "zettel.state.web.WebMixin.has_active_web_job",
        lambda self: True,
    )
    busy = client.get("/studio", params={"subject": "rag"})
    assert busy.status_code == 409
    assert "índice está em uso" in busy.text


def test_ask_summarize_and_skill_show_a_readable_result(web_client, monkeypatch, tmp_path):
    client, vault_root = web_client
    csrf = _login(client)
    monkeypatch.setattr("zettel.web.health.llm_phase_ready", lambda cfg, phase: True)
    monkeypatch.setattr("zettel.index.VectorIndex", lambda **kwargs: object())

    def fake_ask(cfg, db, idx, question, **kwargs):
        return AskResult(
            question=question,
            answer="Resposta citada do acervo.",
            candidates=[
                AskSource(
                    note_id="N1",
                    title="Nota de RAG",
                    wiki_link="[[ZTL - N1]]",
                    rrf_score=0.2,
                    hop=0,
                    origin="busca",
                    passed_floor=True,
                    floor_reason="similaridade 0.80",
                    vector_similarity=0.8,
                )
            ],
            retrieval_params={"mode": "hybrid", "topk": 8},
        )

    monkeypatch.setattr("zettel.ask.run_ask", fake_ask)
    response = client.post(
        "/studio/ask",
        data={"csrf": csrf, "question": "O que é RAG?", "show_context": "1"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    job_id = response.headers["location"].rsplit("/", 1)[-1]
    assert _wait(client, job_id, {"succeeded"})["job"]["state"] == "succeeded"
    detail = client.get(f"/jobs/{job_id}")
    assert "Resposta citada do acervo." in detail.text
    assert 'id="result-copy"' in detail.text
    assert "Copiar JSON" in detail.text
    assert "Nota de RAG" in detail.text
    assert "topk" in detail.text

    def fake_summarize(cfg, db, idx, source_id=None):
        return SummarizeOutcome(
            chapters_summarized=2,
            chapters_skipped=1,
            sources_summarized=1,
            llm_calls=3,
            cache_hits=0,
        )

    monkeypatch.setattr("zettel.summarize.generate_summaries", fake_summarize)
    response = client.post(
        "/studio/summarize",
        data={"csrf": csrf, "source_id": ""},
        follow_redirects=False,
    )
    job_id = response.headers["location"].rsplit("/", 1)[-1]
    assert _wait(client, job_id, {"succeeded"})["job"]["result"]["chapters_summarized"] == 2
    assert "Capítulos resumidos: 2" in client.get(f"/jobs/{job_id}").text

    pack_dir = vault_root / "vault" / ".claude" / "skills" / "demo"
    pack_dir.mkdir(parents=True)
    (pack_dir / "SKILL.md").write_text("# demo\n", encoding="utf-8")

    class Pack:
        def __init__(self):
            self.slug = "demo"
            self.notes = ["n"]
            self.contradictions = []
            self.include_excerpts = False

    monkeypatch.setattr(
        "zettel.skill_export.run_skill_export",
        lambda *args, **kwargs: (pack_dir, Pack()),
    )
    response = client.post(
        "/studio/skill",
        data={"csrf": csrf, "source_id": "@Autor2020"},
        follow_redirects=False,
    )
    job_id = response.headers["location"].rsplit("/", 1)[-1]
    payload = _wait(client, job_id, {"succeeded", "failed"})
    assert payload["job"]["state"] == "succeeded"
    assert payload["job"]["result"]["notes"] == 1
    assert "demo" in client.get(f"/jobs/{job_id}").text


def test_article_pauses_for_context_and_resumes(web_client, monkeypatch):
    client, _ = web_client
    csrf = _login(client)
    monkeypatch.setattr("zettel.web.health.llm_phase_ready", lambda cfg, phase: True)
    monkeypatch.setattr("zettel.index.VectorIndex", lambda **kwargs: object())

    class FakeDrive:
        def __init__(self, cfg, db, idx, topic, **kwargs):
            self.db = db
            self.topic = topic

        def start(self):
            return ArticleStep(
                interrupt={
                    "type": "context_review",
                    "notes": [
                        {
                            "title": "Nota de contexto",
                            "note_id": "N1",
                            "score": 0.91,
                            "hop": 0,
                            "metadata": {"source_id": "@A"},
                        }
                    ],
                    "executed_queries": ["tema do artigo"],
                }
            )

        def resume(self, value):
            assert value["context_decision"] == "approve"
            return ArticleStep(
                result=ArticleResult(
                    topic=self.topic,
                    style="blog",
                    title="Título gerado",
                    body="Corpo do artigo gerado.",
                )
            )

        def abandon(self):
            return None

    monkeypatch.setattr("zettel.article_graph.graph.ArticleDrive", FakeDrive)
    response = client.post(
        "/studio/article",
        data={
            "csrf": csrf,
            "topic": "tema do artigo",
            "style": "blog",
            "review_context": "1",
            "review_outline": "1",
            "save": "",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    job_id = response.headers["location"].rsplit("/", 1)[-1]
    assert _wait(client, job_id, {"awaiting_input", "failed"})["job"]["state"] == "awaiting_input"
    paused = client.get(f"/jobs/{job_id}")
    assert "Nota de contexto" in paused.text
    assert 'data-copy="result-copy"' in paused.text
    assert "Aprovar" in paused.text
    resumed = client.post(
        f"/jobs/{job_id}/resume",
        data={"csrf": csrf, "decision": "approve"},
        follow_redirects=False,
    )
    assert resumed.status_code == 303
    assert _wait(client, job_id, {"succeeded", "failed"})["job"]["state"] == "succeeded"
    done = client.get(f"/jobs/{job_id}")
    assert "Corpo do artigo gerado." in done.text
    assert "# Título gerado" in done.text
    assert "Título gerado" in done.text
