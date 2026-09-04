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
    home = client.get("/")
    assert 'id="theme-toggle"' in home.text
    assert "zettel-theme" in home.text
    for path, text in [
        ("/", "Bom trabalho"),
        ("/documents", "Documentos"),
        ("/pipeline", "Pré-requisitos"),
        ("/review", "Revisão humana"),
        ("/notes", "Notas / MOCs"),
        ("/notes/new", "Criar notas"),
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
            "dump_chunks": "1",
            "dump_extraction": "1",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert captured["operation"] == "harvest"
    assert captured["payload"]["selected_file"] == str(nested.resolve())
    assert captured["payload"]["dump_dir"] == str(tmp_path / "cache" / "chunk-dumps")
    assert captured["payload"]["extraction_dump_dir"] == str(
        tmp_path / "cache" / "extraction-dumps"
    )


def test_manual_source_scaffold_can_be_created_without_overwrite(web_client):
    client, tmp_path = web_client
    csrf = _login(client)
    response = client.post("/notes/new", data={
        "csrf": csrf, "note_type": "SRC", "title": "Thinking Fast",
        "citekey": "Kahneman2011", "authors": "Daniel Kahneman", "year": "2011",
        "document_type": "book",
    })
    assert response.status_code == 201
    assert "Nota criada" in response.text
    created = list((tmp_path / "vault" / "10_Sources").glob("*.md"))
    assert len(created) == 1
    duplicate = client.post("/notes/new", data={
        "csrf": csrf, "note_type": "SRC", "title": "Thinking Fast",
        "citekey": "Kahneman2011",
    })
    assert duplicate.status_code == 400


def test_manual_ztl_uses_single_title_as_thesis(web_client):
    client, tmp_path = web_client
    csrf = _login(client)
    response = client.post("/notes/new", data={
        "csrf": csrf, "note_type": "ZTL",
        "title": "Heurísticas reduzem esforço cognitivo",
    })
    assert response.status_code == 201
    note = next((tmp_path / "vault" / "30_Permanent").glob("*.md"))
    content = note.read_text(encoding="utf-8")
    assert "title: Heurísticas reduzem esforço cognitivo" in content
    assert "> **Tese**: Heurísticas reduzem esforço cognitivo" in content
    page = client.get("/notes/new")
    assert 'name="thesis"' not in page.text
    assert "Tese / título da ideia" in page.text


@pytest.mark.parametrize("source_id", ["../../outside", r"..\\outside", "@not-known"])
def test_manual_lit_rejects_forged_source_ids(web_client, source_id):
    client, tmp_path = web_client
    csrf = _login(client)
    response = client.post("/notes/new", data={
        "csrf": csrf, "note_type": "LIT", "title": "Tentativa",
        "source_id": source_id, "granular": "1",
    })
    assert response.status_code == 400
    assert not (tmp_path / "outside").exists()


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
        "[[ZTL - note-target - nota-relacionada]]\n\n"
        "![[figura-local.png]]\n\n"
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
        db.upsert_note(
            "note-target-2", None, None, title="Outra nota relacionada", body="# Outro destino",
        )
        db.upsert_note(
            "note-target-3", None, None, title="Nota de apoio", body="# Apoio",
        )
        db.upsert_note_connection(
            "note-markdown", "note-target", "extends", "Amplia o assunto",
        )
        db.upsert_note_connection(
            "note-markdown", "note-target-2", "extends", "Amplia outro aspecto",
        )
        db.upsert_note_connection(
            "note-markdown", "note-target-3", "supports", "Sustenta a tese",
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
    assert 'href="/notes/note-target"' in note.text
    assert "![[figura-local.png]]" in note.text
    assert "<script>alert('xss')</script>" not in note.text
    assert 'href="javascript:' not in note.text
    assert "Nota relacionada" in note.text
    assert "note-target" in note.text
    assert "Amplia o assunto" in note.text
    assert "Outra nota relacionada" in note.text
    assert 'href="/notes/note-target-2"' in note.text
    assert "Amplia outro aspecto" in note.text
    assert "Nota de apoio" in note.text
    assert 'href="/notes/note-target-3"' in note.text
    assert note.text.count("connection-row") == 3

    moc = client.get("/mocs/moc-markdown")
    assert moc.status_code == 200
    assert "<h2>Seção</h2>" in moc.text
    assert "<code>código</code>" in moc.text


def test_documents_can_queue_full_pipeline(web_client, monkeypatch):
    client, _ = web_client
    csrf = _login(client)
    captured = {}
    service = client.app.state.service

    monkeypatch.setattr("zettel.web.documents._llm_ready", lambda cfg: True)

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
            "skip_paging": False,
        },
    }


def _seed_source(client, source_id="@Kahneman2011", citekey="Kahneman2011", title="Thinking Fast"):
    db = client.app.state.service.db()
    try:
        db.upsert_source(
            source_id, citekey, title, ["Daniel Kahneman"], 2011, "h", "/p", "md",
        )
        db.conn.execute(
            "UPDATE sources SET extracted_text=?, lit_body=? WHERE source_id=?",
            ("SENTINEL_EXTRACTED_TEXT", "SENTINEL_LIT_BODY", source_id),
        )
        db.conn.commit()
    finally:
        db.close()


def _seed_literature_chunk(
    client, *, source_id="@Kahneman2011", chunk_index=1, section="Sistema 1",
    text="SENTINEL_CHUNK_TEXT", path=None,
):
    db = client.app.state.service.db()
    try:
        db.upsert_chapter(f"{source_id}::ch000", source_id, "Manual", "ch")
        chunk_id = f"{source_id}::manual::{chunk_index:04d}"
        db.upsert_chunk(
            chunk_id, source_id, f"{source_id}::ch000", text, "ck",
            locator="p. 20", section_path=section, chunk_index=chunk_index,
            page_in_book=20, literature_note_path=str(path or f"/vault/{chunk_id}.md"),
        )
        return chunk_id
    finally:
        db.close()


def test_manual_form_markup_contract(web_client):
    """The JS reads data-types / data-required-for; these tests do not execute JS."""
    client, _ = web_client
    _login(client)
    page = client.get("/notes/new")
    assert page.status_code == 200
    assert "for-lit for-ztl" not in page.text
    assert 'data-types="' in page.text
    assert "data-required-for" in page.text
    assert re.search(r"\srequired(\s|=|>)", page.text) is None
    assert 'name="thesis"' not in page.text
    assert "Tese / título da ideia" in page.text


def test_manual_lit_granular_creates_chunk_file(web_client):
    client, tmp_path = web_client
    csrf = _login(client)
    _seed_source(client)
    response = client.post("/notes/new", data={
        "csrf": csrf, "note_type": "LIT", "title": "Sistema 1",
        "source_id": "@Kahneman2011", "granular": "1",
        "chunk_index": "1", "page_number": "20",
    })
    assert response.status_code == 201
    assert "Próximo passo" in response.text
    created = list((tmp_path / "vault" / "20_Literature" / "Kahneman2011").glob("*.md"))
    assert len(created) == 1
    assert "p020" in created[0].name
    content = created[0].read_text(encoding="utf-8")
    assert "chunk_id:" in content
    assert "@Kahneman2011::manual::0001" in content
    assert "Sistema 1" in content


def test_manual_lit_index_collides_without_force(web_client):
    client, tmp_path = web_client
    csrf = _login(client)
    _seed_source(client)
    first = client.post("/notes/new", data={
        "csrf": csrf, "note_type": "SRC", "title": "Thinking Fast",
        "citekey": "Kahneman2011",
    })
    assert first.status_code == 201
    collision = client.post("/notes/new", data={
        "csrf": csrf, "note_type": "LIT", "title": "Thinking Fast",
        "source_id": "@Kahneman2011", "granular": "",
    })
    assert collision.status_code == 400
    forced = client.post("/notes/new", data={
        "csrf": csrf, "note_type": "LIT", "title": "Thinking Fast",
        "source_id": "@Kahneman2011", "granular": "", "force": "1",
    })
    assert forced.status_code == 201


def test_manual_ztl_renders_missing_src_warning(web_client):
    client, _ = web_client
    csrf = _login(client)
    _seed_source(client, source_id="@Ghost2020", citekey="Ghost2020", title="Fantasma")
    response = client.post("/notes/new", data={
        "csrf": csrf, "note_type": "ZTL",
        "title": "Uma tese sem SRC no vault",
        "source_id": "@Ghost2020",
    })
    assert response.status_code == 201
    assert "SRC nao encontrada" in response.text


def test_pickers_require_session_and_omit_payloads(web_client):
    client, tmp_path = web_client
    anonymous = client.get("/api/pickers/sources")
    assert anonymous.status_code == 401
    assert anonymous.json() == {"error": "unauthorized"}
    _login(client)
    _seed_source(client)
    lit_path = tmp_path / "vault" / "20_Literature" / "note.md"
    chunk_id = _seed_literature_chunk(client, path=lit_path)
    sources = client.get("/api/pickers/sources?q=kahneman")
    assert sources.status_code == 200
    body = sources.text
    assert "SENTINEL_EXTRACTED_TEXT" not in body
    assert "SENTINEL_LIT_BODY" not in body
    assert "literature_note_path" not in body
    assert sources.json()["items"][0]["source_id"] == "@Kahneman2011"
    missing = client.get("/api/pickers/literature?q=sistema")
    assert missing.status_code == 400
    assert missing.json() == {"error": "source_id_required"}
    literature = client.get(
        "/api/pickers/literature",
        params={"q": "Sistema", "source_id": "@Kahneman2011"},
    )
    assert literature.status_code == 200
    assert "SENTINEL_CHUNK_TEXT" not in literature.text
    assert "literature_note_path" not in literature.text
    items = literature.json()["items"]
    assert items and items[0]["ref"] == chunk_id
    other = client.get(
        "/api/pickers/literature",
        params={"q": "Sistema", "source_id": "@Other2010"},
    )
    assert other.json()["items"] == []


@pytest.mark.parametrize("query", ["%", "_", '" OR 1=1 --', "NEAR(a b)", "a*", "-x"])
def test_pickers_treat_metacharacters_literally(web_client, query):
    client, _ = web_client
    _login(client)
    _seed_source(client)
    response = client.get("/api/pickers/sources", params={"q": query})
    assert response.status_code == 200
    if query == "%":
        assert response.json()["items"] == []


def test_pickers_are_accent_insensitive_and_clamp_limit(web_client):
    client, _ = web_client
    _login(client)
    _seed_source(client, source_id="@Funcao2020", citekey="Funcao2020", title="Função cognitiva")
    folded = client.get("/api/pickers/sources", params={"q": "funcao"})
    assert folded.json()["items"][0]["source_id"] == "@Funcao2020"
    clamped = client.get("/api/pickers/sources", params={"q": "", "limit": 999})
    assert clamped.status_code == 200
    empty = client.get("/api/pickers/sources", params={"q": ""})
    assert empty.json()["items"][0]["source_id"] == "@Funcao2020"


def test_from_lit_unknown_chunk_is_rejected(web_client):
    client, _ = web_client
    csrf = _login(client)
    response = client.post("/notes/new", data={
        "csrf": csrf, "note_type": "ZTL", "ztl_origin": "from_lit",
        "from_lit": "@nope::manual::0001",
    })
    assert response.status_code == 400


@pytest.mark.parametrize("path", [
    "../../etc/passwd",
    "/etc/passwd",
    r"C:\Windows\win.ini",
    "30_Permanent/other.md",
    "20_Literature/../../secret.md",
])
def test_from_lit_path_cannot_escape_literature_dir(web_client, path):
    client, tmp_path = web_client
    csrf = _login(client)
    response = client.post("/notes/new", data={
        "csrf": csrf, "note_type": "ZTL", "ztl_origin": "from_lit",
        "from_lit_path": path,
    })
    assert response.status_code == 400
    assert not (tmp_path / "secret.md").exists()
    assert not list((tmp_path / "vault" / "30_Permanent").glob("*.md"))


def test_from_lit_index_note_is_rejected(web_client):
    client, tmp_path = web_client
    csrf = _login(client)
    _seed_source(client)
    index = tmp_path / "vault" / "20_Literature" / "LIT - Kahneman2011 - index.md"
    index.parent.mkdir(parents=True, exist_ok=True)
    index.write_text("---\ntype: literature_index\n---\n# Index\n", encoding="utf-8")
    response = client.post("/notes/new", data={
        "csrf": csrf, "note_type": "ZTL", "ztl_origin": "from_lit",
        "from_lit_path": "20_Literature/LIT - Kahneman2011 - index.md",
    })
    assert response.status_code == 400


def test_from_lit_llm_without_sqlite_source_is_conflict(web_client):
    client, tmp_path = web_client
    csrf = _login(client)
    lit_dir = tmp_path / "vault" / "20_Literature" / "Orphan2020"
    lit_dir.mkdir(parents=True)
    lit = lit_dir / "LIT - Orphan2020 - p001 - tema-0001.md"
    lit.write_text(
        "---\ntype: literature\nchunk_id: '@Orphan2020::manual::0001'\n"
        "source_id: '@Orphan2020'\n---\n## Resumo\n\nUma tese real o suficiente.\n",
        encoding="utf-8",
    )
    response = client.post("/notes/new", data={
        "csrf": csrf, "note_type": "ZTL", "ztl_origin": "from_lit",
        "from_lit_path": "20_Literature/Orphan2020/LIT - Orphan2020 - p001 - tema-0001.md",
        "use_llm": "1", "lit_thesis": "Uma tese",
    })
    assert response.status_code == 409
    assert "Pipeline" in response.text


def test_from_lit_scaffold_without_thesis_is_rejected(web_client):
    client, tmp_path = web_client
    csrf = _login(client)
    _seed_source(client)
    lit_dir = tmp_path / "vault" / "20_Literature" / "Kahneman2011"
    lit_dir.mkdir(parents=True)
    lit = lit_dir / "LIT - Kahneman2011 - p001 - tema-0001.md"
    lit.write_text(
        "---\ntype: literature\nchunk_id: '@Kahneman2011::manual::0001'\n"
        "source_id: '@Kahneman2011'\n---\n## Resumo\n\n_Preencha o resumo._\n",
        encoding="utf-8",
    )
    response = client.post("/notes/new", data={
        "csrf": csrf, "note_type": "ZTL", "ztl_origin": "from_lit",
        "from_lit_path": "20_Literature/Kahneman2011/LIT - Kahneman2011 - p001 - tema-0001.md",
    })
    assert response.status_code == 400


def test_from_lit_enqueues_ref_thesis_and_force(web_client, monkeypatch):
    client, tmp_path = web_client
    csrf = _login(client)
    _seed_source(client)
    captured = {}
    service = client.app.state.service

    def fake_submit(operation, payload):
        captured.update(operation=operation, payload=payload)
        return "from-lit-job"

    monkeypatch.setattr(service, "submit", fake_submit)
    lit_dir = tmp_path / "vault" / "20_Literature" / "Kahneman2011"
    lit_dir.mkdir(parents=True)
    rel = "20_Literature/Kahneman2011/LIT - Kahneman2011 - p020 - sistema-1-0001.md"
    (tmp_path / "vault" / rel).write_text(
        "---\ntype: literature\nchunk_id: '@Kahneman2011::manual::0001'\n"
        "source_id: '@Kahneman2011'\n---\n## Resumo\n\nHeurísticas guiam o julgamento.\n",
        encoding="utf-8",
    )
    response = client.post(
        "/notes/new",
        data={
            "csrf": csrf, "note_type": "ZTL", "ztl_origin": "from_lit",
            "from_lit_path": rel, "lit_thesis": "Heurísticas guiam o julgamento.",
            "force": "1",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert captured["operation"] == "manual-ztl-from-lit"
    assert "Kahneman2011" in captured["payload"]["ref"].replace("\\", "/")
    assert captured["payload"]["thesis"] == "Heurísticas guiam o julgamento."
    assert captured["payload"]["force"] is True