from __future__ import annotations

import re
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from zettel.hashing import file_sha256
from zettel.web import create_app


@pytest.fixture
def web_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config = tmp_path / "config.yaml"
    config.write_text(
        "\n".join([
            f"vault_path: {tmp_path / 'vault'}",
            f"inbox_path: {tmp_path / 'inbox'}",
            f"chroma_path: {tmp_path / 'chroma'}",
            f"state_db_path: {tmp_path / 'state.db'}",
            f"cache_path: {tmp_path / 'cache'}",
            f"prompts_path: {Path('prompts').resolve()}",
            "images:",
            "  enabled: false",
        ]),
        encoding="utf-8",
    )
    monkeypatch.setenv("SESSION_SECRET", "web-test-secret")
    with TestClient(create_app(config)) as client:
        yield client, tmp_path


def _login(client: TestClient) -> str:
    login_page = client.get("/login")
    login_csrf = re.search(r'name="login_csrf" value="([^"]+)"', login_page.text).group(1)
    response = client.post(
        "/login", data={"instance_secret": "web-test-secret", "login_csrf": login_csrf},
        follow_redirects=False,
    )
    assert response.status_code == 303
    page = client.get("/")
    assert page.status_code == 200
    match = re.search(r'name="csrf" value="([^"]+)"', page.text)
    assert match
    return match.group(1)


def test_authentication_and_csrf_protect_mutations(web_client):
    client, _ = web_client
    assert client.get("/", follow_redirects=False).status_code == 303
    login_page = client.get("/login")
    token = re.search(r'name="login_csrf" value="([^"]+)"', login_page.text).group(1)
    assert client.post(
        "/login", data={"instance_secret": "wrong", "login_csrf": token},
    ).status_code == 401
    assert client.post(
        "/login", data={"instance_secret": "web-test-secret", "login_csrf": "wrong"},
    ).status_code == 403
    csrf = _login(client)
    assert client.post("/pipeline/retry_assets", data={"csrf": "wrong"}).status_code == 403
    assert client.post(
        "/pipeline/retry_assets", data={"csrf": csrf}, follow_redirects=False,
    ).status_code == 303


def test_upload_rejects_traversal_and_collisions(web_client):
    client, tmp_path = web_client
    csrf = _login(client)
    traversal = client.post(
        "/documents/upload",
        data={"csrf": csrf},
        files={"file": ("../escape.txt", b"unsafe", "text/plain")},
    )
    assert traversal.status_code == 400
    assert not (tmp_path / "escape.txt").exists()
    markup = client.post(
        "/documents/upload",
        data={"csrf": csrf},
        files={"file": ("<img onerror=x>.md", b"unsafe", "text/markdown")},
    )
    assert markup.status_code == 400

    uploaded = client.post(
        "/documents/upload",
        data={"csrf": csrf},
        files={"file": ("paper.md", b"# Paper", "text/markdown")},
        follow_redirects=False,
    )
    assert uploaded.status_code == 303
    assert (tmp_path / "inbox" / "paper.md").read_bytes() == b"# Paper"
    collision = client.post(
        "/documents/upload",
        data={"csrf": csrf},
        files={"file": ("paper.md", b"other", "text/markdown")},
    )
    assert collision.status_code == 409


def test_navigation_and_retry_job_flow(web_client):
    client, _ = web_client
    csrf = _login(client)
    for path, text in [
        ("/", "Bom trabalho"),
        ("/documents", "Documentos"),
        ("/pipeline", "Pré-requisitos"),
        ("/review", "Revisão humana"),
        ("/notes", "Notas / MOCs"),
        ("/runs", "Execuções"),
        ("/settings", "SQLite FTS5"),
    ]:
        response = client.get(path)
        assert response.status_code == 200
        assert text in response.text

    response = client.post(
        "/pipeline/retry_assets", data={"csrf": csrf}, follow_redirects=False,
    )
    job_id = response.headers["location"].rsplit("/", 1)[-1]
    for _ in range(30):
        payload = client.get(f"/api/jobs/{job_id}").json()
        if payload["job"]["state"] not in {"queued", "running"}:
            break
        time.sleep(0.05)
    assert payload["job"]["state"] == "succeeded"
    assert payload["job"]["result"] == {"assets_reset": 0}
    assert payload["events"]
    detail = client.get(f"/jobs/{job_id}")
    assert 'id="job-state"' in detail.text
    assert "succeeded" in detail.text
    assert 'id="job-result"' in detail.text
    assert "Concluído" in detail.text


def test_pipeline_blocks_extract_without_harvest_output(web_client):
    client, _ = web_client
    csrf = _login(client)
    response = client.post("/pipeline/extract", data={"csrf": csrf})
    assert response.status_code == 409
    assert "Não há chunks pendentes" in response.text


def test_unknown_details_do_not_expose_arbitrary_files(web_client):
    client, _ = web_client
    _login(client)
    assert client.get("/sources/not-found").status_code == 404
    assert client.get("/notes/not-found").status_code == 404
    assert client.get("/mocs/not-found").status_code == 404


def test_nested_inbox_file_can_be_selected_for_harvest(web_client, monkeypatch):
    client, tmp_path = web_client
    csrf = _login(client)
    nested = tmp_path / "inbox" / "folder" / "nested.txt"
    nested.parent.mkdir(parents=True)
    nested.write_text("text", encoding="utf-8")

    captured = {}
    service = client.app.state.service

    def fake_submit(operation, payload):
        captured.update(operation=operation, payload=payload)
        return "job-id"

    monkeypatch.setattr(service, "submit", fake_submit)
    response = client.post(
        "/documents/harvest",
        data={
            "csrf": csrf,
            "selected_file": "folder/nested.txt",
            "duplicate_action": "skip",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert captured["operation"] == "harvest"
    assert captured["payload"]["selected_file"] == str(nested.resolve())


def test_harvest_rejects_absolute_and_parent_paths(web_client):
    client, tmp_path = web_client
    csrf = _login(client)
    invalid = [str((tmp_path / "outside.txt").resolve()), "../outside.txt"]
    for selected in invalid:
        response = client.post(
            "/documents/harvest",
            data={"csrf": csrf, "selected_file": selected, "duplicate_action": "skip"},
        )
        assert response.status_code == 400


def test_documents_hide_completed_file_but_show_changed_copy(web_client, monkeypatch):
    client, tmp_path = web_client
    csrf = _login(client)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    completed = inbox / "completed.md"
    incomplete = inbox / "incomplete.md"
    pending = inbox / "pending.md"
    completed.write_text("# Processado", encoding="utf-8")
    incomplete.write_text("# Interrompido", encoding="utf-8")
    pending.write_text("# Novo", encoding="utf-8")

    checksum = file_sha256(completed)
    incomplete_checksum = file_sha256(incomplete)
    db = client.app.state.service.db()
    try:
        db.upsert_source(
            source_id="@completed",
            citekey="completed",
            title="Documento processado",
            authors=[],
            year=None,
            file_checksum=checksum,
            origin_path=str(completed.resolve()),
            origin_type="md",
            processing_status="completed",
        )
        db.upsert_file(
            str(completed.resolve()), checksum, "md", source_id="@completed",
        )
        db.upsert_source(
            source_id="@incomplete",
            citekey="incomplete",
            title="Documento interrompido",
            authors=[],
            year=None,
            file_checksum=incomplete_checksum,
            origin_path=str(incomplete.resolve()),
            origin_type="md",
            processing_status="completed",
        )
        db.update_source_texts(
            "@incomplete",
            extracted_text="# Capítulo incompleto\n\nConteúdo que ainda precisa de chunks.",
        )
        db.upsert_file(
            str(incomplete.resolve()), incomplete_checksum, "md",
            source_id="@incomplete",
        )
    finally:
        db.close()

    page = client.get("/documents")
    assert 'value="pending.md"' in page.text
    assert 'value="incomplete.md"' in page.text
    assert 'value="completed.md"' not in page.text
    response = client.post(
        "/documents/harvest",
        data={"csrf": csrf, "selected_file": "completed.md"},
    )
    assert response.status_code == 409
    assert "já foi processado" in response.text

    captured = {}

    def fake_submit(operation, payload):
        captured.update(operation=operation, payload=payload)
        return "resume-job"

    monkeypatch.setattr(client.app.state.service, "submit", fake_submit)
    resumed = client.post(
        "/documents/harvest",
        data={"csrf": csrf, "selected_file": "incomplete.md"},
        follow_redirects=False,
    )
    assert resumed.status_code == 303
    assert captured["operation"] == "harvest"
    assert captured["payload"]["selected_file"] == str(incomplete.resolve())

    completed.write_text("# Conteúdo alterado", encoding="utf-8")
    refreshed = client.get("/documents")
    assert 'value="completed.md"' in refreshed.text


def test_note_and_moc_details_render_sanitized_markdown(web_client):
    client, _ = web_client
    _login(client)
    markdown = (
        "# Cabeçalho\n\n"
        "- primeiro\n"
        "- segundo\n\n"
        "> citação\n\n"
        "[seguro](https://example.com)\n\n"
        "URL direta: https://docs.example.com/guia\n\n"
        "<script>alert('xss')</script>\n\n"
        "[perigoso](javascript:alert('xss'))"
    )
    db = client.app.state.service.db()
    try:
        db.upsert_note(
            "note-markdown", None, None, title="Nota formatada", body=markdown,
        )
        db.upsert_note(
            "note-target", None, None, title="Nota relacionada", body="# Destino",
        )
        db.upsert_note_connection(
            "note-markdown", "note-target", "extends", "Amplia o assunto",
        )
        db.upsert_moc(
            "moc-markdown", "Mapa formatado", body="## Seção\n\n`código`",
        )
    finally:
        db.close()

    note = client.get("/notes/note-markdown")
    assert note.status_code == 200
    assert "<h1>Cabeçalho</h1>" in note.text
    assert "<li>primeiro</li>" in note.text
    assert "<blockquote>" in note.text
    assert 'href="https://example.com"' in note.text
    assert 'href="https://docs.example.com/guia"' in note.text
    assert "<script>" not in note.text
    assert 'href="javascript:' not in note.text
    assert "Nota relacionada" in note.text
    assert 'href="/notes/note-target"' in note.text
    assert "note-target" in note.text
    assert "Amplia o assunto" in note.text

    moc = client.get("/mocs/moc-markdown")
    assert moc.status_code == 200
    assert "<h2>Seção</h2>" in moc.text
    assert "<code>código</code>" in moc.text


def test_documents_can_queue_full_pipeline(web_client, monkeypatch):
    client, _ = web_client
    csrf = _login(client)
    captured = {}
    service = client.app.state.service

    monkeypatch.setattr("zettel.web._llm_ready", lambda cfg: True)

    def fake_submit(operation, payload):
        captured.update(operation=operation, payload=payload)
        return "run-all-job"

    monkeypatch.setattr(service, "submit", fake_submit)
    page = client.get("/documents")
    assert "Executar pipeline completo" in page.text

    response = client.post(
        "/documents/run-all", data={"csrf": csrf}, follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/jobs/run-all-job"
    assert captured == {
        "operation": "run_all",
        "payload": {
            "duplicate_action": "skip",
            "skip_biblio": False,
            "skip_paging": True,
        },
    }