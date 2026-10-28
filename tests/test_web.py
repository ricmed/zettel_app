from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import re
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from zettel.hashing import file_sha256
from zettel.web import create_app

from tests.fakes import FakeVectorIndex, deleted
from tests.web_support import login, wait_for_job


def _seed_failed_asset(client) -> None:
    """``retry_assets`` is the cheapest job, and it refuses with nothing to retry (#314)."""
    db = client.app.state.service.db()
    try:
        db.upsert_source("@Asset2024", "Asset2024", "Fonte", ["A"], 2024, "h", "/a.md", "md")
        db.upsert_asset("asset-1", "@Asset2024", "90_Assets/a.png", "img", status="failed")
    finally:
        db.close()


def _ready_biblio_review(client, tmp_path, csrf, *, allow_incomplete=False):
    """Create a completed preparation without racing the background worker."""
    from zettel.bibliography import BibliographicMetadata, format_abnt
    from zettel.harvester.prepared import save_snapshot

    svc = client.app.state.service
    path = tmp_path / "inbox" / "obra.md"
    path.parent.mkdir(exist_ok=True)
    path.write_text("# Obra\n\nTexto da obra.", encoding="utf-8")
    job_id = "a" * 32
    biblio = BibliographicMetadata(
        document_type="livro",
        confidence=0.9,
        title="Obra",
        authors=["Ana Silva"],
        year=2024,
        place="São Paulo",
        publisher="Editora",
        provenance={"authors": "user", "title": "llm"},
    )
    payload = {
        "selected_file": str(path.resolve()),
        "checksum": file_sha256(path),
        "skip_biblio": allow_incomplete,
        "duplicate_action": "skip",
        "skip_paging": False,
        "content_start_file": None,
        "content_start_book": None,
        "dump_dir": None,
        "extraction_dump_dir": None,
    }
    db = svc.db()
    try:
        assert db.create_web_job(job_id, "prepare_harvest", payload)
        db.create_web_harvest_review(job_id, payload["checksum"])
        db.update_web_job(
            job_id,
            state="succeeded",
            result={
                "selected_file": str(path.resolve()),
                "checksum": payload["checksum"],
                "biblio": biblio.model_dump(),
                "abnt_reference": format_abnt(biblio),
            },
            finished=True,
        )
    finally:
        db.close()
    save_snapshot(
        svc.cfg,
        job_id,
        checksum=payload["checksum"],
        text="# Obra\n\nTexto da obra.",
        metadata={"title": "Obra"},
    )
    return job_id, path


def test_document_review_confirms_edits_and_preserves_options(web_client, monkeypatch):
    client, tmp_path = web_client
    csrf = login(client)
    job_id, path = _ready_biblio_review(client, tmp_path, csrf)
    page = client.get(f"/documents/review/{job_id}")
    assert page.status_code == 200
    assert "Confiança da inferência: 90%" in page.text
    assert "Ana Silva" in page.text and "SILVA, Ana" in page.text
    assert "usuário" in page.text and "modelo" in page.text
    assert "book_editors" in page.text and "chapter_title" in page.text
    from zettel.web.jobs import continue_href

    db = client.app.state.service.db()
    try:
        assert continue_href(db.get_web_job(job_id)) == f"/documents/review/{job_id}"
    finally:
        db.close()

    data = {
        "csrf": csrf,
        "document_type": "livro",
        "title": "Obra corrigida",
        "authors": "Ana Silva\nBruno Costa",
        "year": "2024",
        "place": "Rio de Janeiro",
        "publisher": "Nova Editora",
        "subtitle": "Complemento",
        "isbn": "978-0-123",
        "abnt_reference": "Referência personalizada.",
    }
    preview = client.post(f"/documents/review/{job_id}/preview", data=data)
    assert preview.status_code == 200
    assert "Obra corrigida: Complemento" in preview.json()["abnt_reference"]
    assert preview.json()["missing"] == []

    captured = []
    monkeypatch.setattr(
        client.app.state.service,
        "submit_review",
        lambda review_id, payload: captured.append((review_id, payload)) or "harvest-job",
    )
    result = client.post(
        f"/documents/review/{job_id}",
        data={**data, "decision": "confirm"},
        follow_redirects=False,
    )
    assert result.status_code == 303
    assert result.headers["location"] == "/jobs/harvest-job"
    assert captured[0][0] == job_id
    assert captured[0][1]["selected_file"] == str(path.resolve())
    assert captured[0][1]["duplicate_action"] == "skip"
    assert captured[0][1]["biblio"]["authors"] == ["Ana Silva", "Bruno Costa"]
    assert captured[0][1]["biblio"]["subtitle"] == "Complemento"
    assert captured[0][1]["abnt_reference"] == "Referência personalizada."


def test_review_rejects_missing_and_requires_ack_when_allowed(web_client, monkeypatch):
    client, tmp_path = web_client
    csrf = login(client)
    job_id, _ = _ready_biblio_review(client, tmp_path, csrf, allow_incomplete=True)
    submitted = []
    monkeypatch.setattr(
        client.app.state.service,
        "submit_review",
        lambda *args: submitted.append(args) or "queued",
    )
    data = {
        "csrf": csrf,
        "decision": "confirm",
        "document_type": "livro",
        "title": "Sem local",
        "authors": "Ana Silva",
        "year": "2024",
    }
    denied = client.post(f"/documents/review/{job_id}", data=data)
    assert denied.status_code == 400
    assert "Confirme explicitamente" in denied.text
    assert not submitted
    accepted = client.post(
        f"/documents/review/{job_id}",
        data={**data, "incomplete_ack": "1"},
        follow_redirects=False,
    )
    assert accepted.status_code == 303
    assert submitted[0][1]["biblio"]["publisher"] is None


def test_review_cancel_stale_file_and_session_isolation(web_client, monkeypatch):
    client, tmp_path = web_client
    csrf = login(client)
    job_id, path = _ready_biblio_review(client, tmp_path, csrf)
    monkeypatch.setattr(
        client.app.state.service,
        "submit_review",
        lambda *args: pytest.fail("Não deve iniciar o harvest"),
    )
    assert (
        client.post(
            f"/documents/review/{job_id}",
            data={"csrf": "wrong", "decision": "cancel"},
        ).status_code
        == 403
    )
    path.write_text("# Outra obra", encoding="utf-8")
    assert client.get(f"/documents/review/{job_id}").status_code == 409
    assert (
        client.post(
            f"/documents/review/{job_id}",
            data={"csrf": csrf, "decision": "confirm"},
        ).status_code
        == 409
    )
    path.write_text("# Obra\n\nTexto da obra.", encoding="utf-8")
    assert (
        client.post(
            f"/documents/review/{job_id}",
            data={"csrf": csrf, "decision": "cancel"},
            follow_redirects=False,
        ).status_code
        == 303
    )
    assert client.get(f"/documents/review/{job_id}").status_code == 409
    db = client.app.state.service.db()
    try:
        assert db.list_sources() == []
    finally:
        db.close()


def test_review_queue_is_single_use(web_client):
    client, tmp_path = web_client
    csrf = login(client)
    job_id, path = _ready_biblio_review(client, tmp_path, csrf)
    db = client.app.state.service.db()
    try:
        assert not db.queue_web_harvest_review("f" * 32, "c" * 32, {})
        assert db.queue_web_harvest_review(
            job_id,
            "c" * 32,
            {"selected_file": str(path), "review_id": job_id},
        )
        assert not db.queue_web_harvest_review(job_id, "d" * 32, {})
        assert db.get_web_harvest_review(job_id)["harvest_job_id"] == "c" * 32
        db.update_web_job("c" * 32, state="failed", error_message="Interrompido")
        assert db.queue_web_harvest_review(
            job_id,
            "d" * 32,
            {"selected_file": str(path), "review_id": job_id},
        )
        assert db.get_web_harvest_review(job_id)["harvest_job_id"] == "d" * 32
    finally:
        db.close()


def test_review_recovers_confirmed_fields_after_failure(web_client):
    client, tmp_path = web_client
    csrf = login(client)
    job_id, path = _ready_biblio_review(client, tmp_path, csrf)
    db = client.app.state.service.db()
    try:
        payload = {
            "review_id": job_id,
            "selected_file": str(path),
            "biblio": {
                "document_type": "livro",
                "title": "Título confirmado",
                "authors": ["Ana Silva"],
                "year": 2024,
                "place": "SP",
                "publisher": "Editora",
                "confidence": 0.9,
            },
            "abnt_reference": "Referência confirmada.",
        }
        assert db.queue_web_harvest_review(job_id, "c" * 32, payload)
        db.update_web_job("c" * 32, state="failed")
    finally:
        db.close()
    page = client.get(f"/documents/review/{job_id}")
    assert page.status_code == 200
    assert 'value="Título confirmado"' in page.text
    assert "Referência confirmada." in page.text
    assert "tentar novamente" in page.text


def test_confirmed_harvest_is_requeued_after_restart(tmp_path):
    from zettel.state import StateDB

    db = StateDB(tmp_path / "restart.db")
    try:
        db.create_web_job("a" * 32, "harvest", {"review_id": "b" * 32})
        assert db.claim_web_job("a" * 32)
        assert db.recover_web_jobs() == 1
        assert db.get_web_job("a" * 32)["state"] == "queued"
    finally:
        db.close()


def test_prepare_stages_images_and_cancel_removes_them(tmp_path, monkeypatch):
    from zettel.bibliography import BibliographicMetadata
    from zettel.config import AppConfig
    from zettel.harvester.prepared import asset_stage_path, remove_preparation
    from zettel.state import StateDB
    from zettel.web_app import WebWorker

    cfg = AppConfig(
        inbox_path=tmp_path / "inbox",
        cache_path=tmp_path / "cache",
        vault_path=tmp_path / "vault",
    )
    cfg.images.enabled = True
    cfg.inbox_path.mkdir()
    (cfg.inbox_path / "image.png").write_bytes(b"imagem")
    path = cfg.inbox_path / "obra.md"
    path.write_text("# Obra\n\nTexto da obra ![figura](image.png)", encoding="utf-8")
    monkeypatch.setattr(
        "zettel.bibliography.build_bibliographic_metadata",
        lambda *args: BibliographicMetadata(title="Obra", confidence=0.9),
    )
    job_id = "f" * 32
    db = StateDB(tmp_path / "staged.db")
    try:
        db.create_web_job(job_id, "prepare_harvest", {})

        class Progress:
            job_id = "f" * 32

            def emit(self, event):
                pass

        WebWorker._dispatch(
            cfg,
            db,
            Progress(),
            "prepare_harvest",
            {"selected_file": str(path), "checksum": file_sha256(path)},
        )
        assert list((asset_stage_path(cfg, job_id) / "90_Assets").glob("img-*.png"))
        assert not (cfg.vault_path / "90_Assets").exists()
        remove_preparation(cfg, job_id)
        assert not asset_stage_path(cfg, job_id).exists()
    finally:
        db.close()


def test_confirmed_worker_reuses_preparation_and_passes_file_options(tmp_path, monkeypatch):
    from zettel.bibliography import BibliographicMetadata
    from zettel.config import AppConfig
    from zettel.harvester.pipeline import HarvestOutcome
    from zettel.harvester.prepared import save_snapshot
    from zettel.state import StateDB
    from zettel.web_app import JobProgress, WebWorker

    cfg = AppConfig(
        inbox_path=tmp_path / "inbox",
        cache_path=tmp_path / "cache",
        vault_path=tmp_path / "vault",
    )
    cfg.inbox_path.mkdir()
    path = cfg.inbox_path / "obra.md"
    path.write_text("# Obra", encoding="utf-8")
    review_id, harvest_id = "e" * 32, "f" * 32
    biblio = BibliographicMetadata(
        document_type="relatorio",
        confidence=0.9,
        title="Obra revista",
        year=2024,
        institution="USP",
    )
    captured = {}

    def fake_harvest(*args, **kwargs):
        captured.update(kwargs)
        return HarvestOutcome(source_ids=["@Obra2024"])

    monkeypatch.setattr("zettel.index.VectorIndex", lambda **kwargs: object())
    monkeypatch.setattr("zettel.harvester.run_harvest", fake_harvest)
    db = StateDB(tmp_path / "worker.db")
    try:
        db.create_web_job(review_id, "prepare_harvest", {})
        db.create_web_harvest_review(review_id, file_sha256(path))
        db.update_web_job(
            review_id,
            state="succeeded",
            result={"selected_file": str(path.resolve())},
        )
        save_snapshot(
            cfg,
            review_id,
            checksum=file_sha256(path),
            text="Texto preparado",
            metadata={"title": "Obra"},
        )
        payload = {
            "selected_file": str(path.resolve()),
            "review_id": review_id,
            "biblio": biblio.model_dump(),
            "abnt_reference": "Referência final.",
            "duplicate_action": "continue",
            "skip_paging": True,
            "dump_dir": None,
            "extraction_dump_dir": None,
        }
        assert db.queue_web_harvest_review(review_id, harvest_id, payload)
        result = WebWorker._dispatch(
            cfg,
            db,
            JobProgress(db, harvest_id),
            "harvest",
            payload,
        )
        assert result["sources"] == ["@Obra2024"]
        assert captured["prepared"]["text"] == "Texto preparado"
        assert captured["prepared"]["biblio"]["title"] == "Obra revista"
        assert captured["prepared"]["abnt_reference"] == "Referência final."
        assert captured["duplicate_action"] == "continue"
        assert captured["skip_paging"] is True
        path.write_text("# Alterado", encoding="utf-8")
        with pytest.raises(Exception, match="arquivo mudou"):
            WebWorker._dispatch(
                cfg,
                db,
                JobProgress(db, harvest_id),
                "harvest",
                payload,
            )
    finally:
        db.close()


@pytest.mark.parametrize("extension", [".pdf", ".md", ".txt"])
def test_preparation_stores_snapshot_without_source(web_client, monkeypatch, extension):
    from zettel.bibliography import BibliographicMetadata
    from zettel.harvester.prepared import load_snapshot
    from zettel.web_app import WebWorker

    client, tmp_path = web_client
    login(client)
    path = tmp_path / "inbox" / f"obra{extension}"
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(b"arquivo")
    captured = []
    monkeypatch.setattr(
        "zettel.harvester.extract.extract_text",
        lambda cfg, file_path, origin: (
            captured.append(origin) or ("Texto da obra", {"title": "Obra"})
        ),
    )
    monkeypatch.setattr(
        "zettel.bibliography.build_bibliographic_metadata",
        lambda *args: BibliographicMetadata(
            document_type="relatorio",
            confidence=0.8,
            title="Obra",
            year=2024,
            institution="USP",
        ),
    )
    svc = client.app.state.service
    job_id = "b" * 32
    db = svc.db()
    try:
        assert db.create_web_job(
            job_id,
            "prepare_harvest",
            {"selected_file": str(path), "checksum": file_sha256(path)},
        )

        class Progress:
            job_id = "b" * 32

            def emit(self, event):
                pass

        result = WebWorker._dispatch(
            svc.cfg,
            db,
            Progress(),
            "prepare_harvest",
            {"selected_file": str(path), "checksum": file_sha256(path)},
        )
        assert result["biblio"]["title"] == "Obra"
        assert load_snapshot(svc.cfg, job_id)["text"] == "Texto da obra"
        assert db.get_web_harvest_review(job_id)["state"] == "ready"
        assert db.list_sources() == []
        assert captured == ["pdf" if extension == ".pdf" else "md"]
    finally:
        db.close()


def test_favicon_serves_brand_mark(web_client):
    client, _ = web_client
    icon = client.get("/favicon.ico")
    assert icon.status_code == 200
    assert icon.headers["content-type"].startswith("image/svg+xml")
    assert b"#da5a3b" in icon.content
    assert 'rel="icon" href="/static/favicon.svg"' in client.get("/login").text


def test_authentication_and_csrf_protect_mutations(web_client):
    client, _ = web_client
    assert client.get("/", follow_redirects=False).status_code == 303
    login_page = client.get("/login")
    token = re.search(r'name="login_csrf" value="([^"]+)"', login_page.text).group(1)
    assert (
        client.post(
            "/login",
            data={"instance_secret": "wrong", "login_csrf": token},
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/login",
            data={"instance_secret": "web-test-secret", "login_csrf": "wrong"},
        ).status_code
        == 403
    )
    csrf = login(client)
    _seed_failed_asset(client)
    assert client.post("/pipeline/retry_assets", data={"csrf": "wrong"}).status_code == 403
    assert (
        client.post(
            "/pipeline/retry_assets",
            data={"csrf": csrf},
            follow_redirects=False,
        ).status_code
        == 303
    )


def _login_token(page) -> str:
    return re.search(r'name="login_csrf" value="([^"]+)"', page.text).group(1)


def test_login_page_carries_nothing_computed_from_the_secret(web_client):
    """An HMAC of a known message under the secret is an offline brute-force oracle (#305)."""
    client, _ = web_client
    first = _login_token(client.get("/login"))
    second = _login_token(client.get("/login"))
    assert first != second
    oracle = hmac.new(b"web-test-secret", b"login", hashlib.sha256).hexdigest()
    assert oracle not in {first, second}


def test_session_cookie_is_not_keyed_by_the_secret(web_client):
    client, _ = web_client
    login(client)
    body, tag = client.cookies["zettel_session"].rsplit(".", 1)
    assert tag != hmac.new(b"web-test-secret", body.encode(), hashlib.sha256).hexdigest()


def test_login_with_non_ascii_secret_is_refused_not_crashed(web_client):
    client, _ = web_client
    token = _login_token(client.get("/login"))
    response = client.post("/login", data={"instance_secret": "senhação", "login_csrf": token})
    assert response.status_code == 401


def test_repeated_login_failures_are_throttled(web_client):
    client, _ = web_client
    page = client.get("/login")
    for _ in range(5):
        page = client.post(
            "/login", data={"instance_secret": "wrong", "login_csrf": _login_token(page)}
        )
        assert page.status_code == 401
    locked = client.post(
        "/login",
        data={"instance_secret": "web-test-secret", "login_csrf": _login_token(page)},
        follow_redirects=False,
    )
    assert locked.status_code == 429
    assert int(locked.headers["retry-after"]) >= 1
    assert "zettel_session" not in client.cookies


def test_logout_revokes_a_copied_session_cookie(web_client):
    client, _ = web_client
    csrf = login(client)
    stolen = client.cookies["zettel_session"]
    assert client.post("/logout", data={"csrf": csrf}, follow_redirects=False).status_code == 303
    client.cookies.set("zettel_session", stolen)
    assert client.get("/", follow_redirects=False).status_code == 303


def test_session_survives_a_server_restart(web_client):
    client, tmp_path = web_client
    login(client)
    cookie = client.cookies["zettel_session"]
    with TestClient(create_app(tmp_path / "config.yaml")) as restarted:
        restarted.cookies.set("zettel_session", cookie)
        assert restarted.get("/", follow_redirects=False).status_code == 200


def test_settings_warns_about_a_short_session_secret(web_client):
    client, _ = web_client
    login(client)
    assert "SESSION_SECRET curto" in client.get("/settings").text


def test_upload_rejects_traversal_and_collisions(web_client):
    client, tmp_path = web_client
    csrf = login(client)
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


def test_documents_empty_state_displays_options_without_queueing(web_client, monkeypatch):
    client, _ = web_client
    csrf = login(client)
    page = client.get("/documents")
    assert page.status_code == 200
    for text in (
        "Configurar processamento",
        "Permitir bibliografia incompleta",
        "Paginação do PDF",
        "Salvar diagnóstico dos chunks",
        "Salvar Markdown extraído",
        "Envie um arquivo primeiro",
    ):
        assert text in page.text
    assert 'id="selected-document" name="selected_file" required disabled' in page.text
    assert 'id="document-start" class="primary" type="submit" disabled' in page.text
    assert all(f'<option value="{mode}">' in page.text for mode in ("auto", "manual", "first"))
    monkeypatch.setattr(
        client.app.state.service,
        "submit",
        lambda *args: pytest.fail("Não deve enfileirar sem documento selecionado"),
    )
    response = client.post("/documents/harvest", data={"csrf": csrf, "duplicate_action": "skip"})
    assert response.status_code == 400
    assert "Selecione um documento" in response.text


def test_queued_harvest_without_a_file_cannot_scan_the_inbox():
    from zettel.web_app import UserFacingError, WebWorker

    class Progress:
        def emit(self, event):
            pass

    with pytest.raises(UserFacingError, match="Selecione um documento"):
        WebWorker._dispatch(None, None, Progress(), "harvest", {})


def test_document_options_queue_only_selected_file(web_client, monkeypatch):
    client, tmp_path = web_client
    csrf = login(client)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    pdf = inbox / "livro.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    other = inbox / "outro.md"
    other.write_text("# Outro", encoding="utf-8")
    page = client.get("/documents")
    assert 'value="livro.pdf"' in page.text and 'value="outro.md"' in page.text
    assert 'data-pdf="1"' in page.text and 'data-pdf="0"' in page.text
    assert 'id="document-page-inputs" disabled' in page.text
    assert 'id="document-start" class="primary" type="submit" disabled' in page.text

    captured = []
    monkeypatch.setattr(
        client.app.state.service,
        "submit",
        lambda operation, payload: captured.append((operation, payload)) or "selected-job",
    )
    manual = client.post(
        "/documents/harvest",
        data={
            "csrf": csrf,
            "selected_file": "livro.pdf",
            "duplicate_action": "continue",
            "skip_biblio": "1",
            "paging_mode": "manual",
            "content_start_file": "8",
            "content_start_book": "13",
            "dump_chunks": "1",
            "dump_extraction": "1",
        },
        follow_redirects=False,
    )
    assert manual.status_code == 303
    operation, payload = captured[-1]
    assert operation == "prepare_harvest"
    assert payload["checksum"] == file_sha256(pdf)
    assert "session_hash" not in payload
    assert {k: v for k, v in payload.items() if k != "checksum"} == {
        "selected_file": str(pdf.resolve()),
        "duplicate_action": "continue",
        "skip_biblio": True,
        "skip_paging": False,
        "content_start_file": 8,
        "content_start_book": 13,
        "dump_dir": str(tmp_path / "cache" / "chunk-dumps"),
        "extraction_dump_dir": str(tmp_path / "cache" / "extraction-dumps"),
    }
    first = client.post(
        "/documents/harvest",
        data={
            "csrf": csrf,
            "selected_file": "livro.pdf",
            "duplicate_action": "abort",
            "paging_mode": "first",
        },
        follow_redirects=False,
    )
    assert first.status_code == 303
    assert captured[-1][1]["skip_paging"] is True
    assert captured[-1][1]["content_start_file"] is None
    assert captured[-1][1]["content_start_book"] is None
    assert captured[-1][1]["duplicate_action"] == "abort"
    default = client.post(
        "/documents/harvest",
        data={"csrf": csrf, "selected_file": "outro.md"},
        follow_redirects=False,
    )
    assert default.status_code == 303
    assert captured[-1][1]["selected_file"] == str(other.resolve())
    assert captured[-1][1]["duplicate_action"] == "skip"
    assert captured[-1][1]["skip_paging"] is False


def test_document_options_reject_conflicts_and_invalid_selection(web_client, monkeypatch):
    client, tmp_path = web_client
    csrf = login(client)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "p.pdf").write_bytes(b"%PDF")
    (inbox / "m.txt").write_text("Texto", encoding="utf-8")
    monkeypatch.setattr(
        client.app.state.service,
        "submit",
        lambda *args: pytest.fail("Opções inválidas não devem enfileirar"),
    )
    cases = [
        ({"selected_file": ""}, 400),
        ({"selected_file": "missing.pdf"}, 404),
        ({"selected_file": "../m.txt"}, 400),
        ({"selected_file": "m.txt", "paging_mode": "first"}, 400),
        (
            {
                "selected_file": "m.txt",
                "paging_mode": "manual",
                "content_start_file": "2",
                "content_start_book": "1",
            },
            400,
        ),
        ({"selected_file": "p.pdf", "duplicate_action": "invalid"}, 400),
        ({"selected_file": "p.pdf", "paging_mode": "invalid"}, 400),
        ({"selected_file": "p.pdf", "skip_biblio": "false"}, 400),
        ({"selected_file": "p.pdf", "paging_mode": "manual"}, 400),
        (
            {
                "selected_file": "p.pdf",
                "paging_mode": "manual",
                "content_start_file": "0",
                "content_start_book": "1",
            },
            400,
        ),
        (
            {
                "selected_file": "p.pdf",
                "paging_mode": "manual",
                "content_start_file": "2",
                "content_start_book": "-1",
            },
            400,
        ),
        (
            {
                "selected_file": "p.pdf",
                "paging_mode": "manual",
                "skip_paging": "on",
                "content_start_file": "2",
                "content_start_book": "1",
            },
            400,
        ),
        ({"selected_file": "p.pdf", "paging_mode": "first", "content_start_file": "2"}, 400),
        ({"selected_file": "p.pdf", "paging_mode": "auto", "content_start_book": "1"}, 400),
    ]
    for data, expected in cases:
        response = client.post("/documents/harvest", data={"csrf": csrf, **data})
        assert response.status_code == expected, data


def test_upload_keeps_pending_list_on_errors_and_rejects_invalid_files(web_client):
    client, tmp_path = web_client
    csrf = login(client)
    for name, content in (("text.txt", b"text"), ("scan.pdf", b"%PDF")):
        response = client.post(
            "/documents/upload",
            data={"csrf": csrf},
            files={"file": (name, content, "application/octet-stream")},
            follow_redirects=False,
        )
        assert response.status_code == 303
    for name, content, expected in (
        ("bad.exe", b"unsafe", 400),
        ("empty.md", b"", 400),
        ("huge.pdf", b"X" * (25 * 1024 * 1024 + 1), 413),
        ("text.txt", b"overwrite", 409),
    ):
        response = client.post(
            "/documents/upload",
            data={"csrf": csrf},
            files={"file": (name, content, "application/octet-stream")},
        )
        assert response.status_code == expected
        assert 'value="text.txt"' in response.text
        assert 'value="scan.pdf"' in response.text
        assert "Configurar processamento" in response.text
    assert (tmp_path / "inbox" / "text.txt").read_bytes() == b"text"


def test_navigation_and_retry_job_flow(web_client):
    client, _ = web_client
    csrf = login(client)
    _seed_failed_asset(client)
    home = client.get("/")
    assert 'id="theme-toggle"' in home.text
    # Scripts are static files under the CSP (#315); the page only links them.
    assert '<script src="/static/theme-init.js">' in home.text
    assert "zettel-theme" in client.get("/static/theme-init.js").text
    for path, text in [
        ("/", "Bom trabalho"),
        ("/documents", "Documentos"),
        ("/pipeline", "Pré-requisitos"),
        ("/review", "Revisão humana"),
        ("/notes", "Notas / MOCs"),
        ("/notes/new", "Criar notas"),
        ("/studio", "Consultar"),
        ("/runs", "Execuções"),
        ("/settings", "SQLite FTS5"),
    ]:
        response = client.get(path)
        assert response.status_code == 200
        assert text in response.text

    response = client.post(
        "/pipeline/retry_assets",
        data={"csrf": csrf},
        follow_redirects=False,
    )
    job_id = response.headers["location"].rsplit("/", 1)[-1]
    payload = wait_for_job(client, job_id)
    assert payload["job"]["state"] == "succeeded"
    assert payload["job"]["result"] == {"assets_reset": 1}
    assert payload["events"]
    detail = client.get(f"/jobs/{job_id}")
    assert 'id="job-state"' in detail.text
    assert "succeeded" in detail.text
    assert 'id="job-result"' in detail.text
    assert "Concluído" in detail.text


def test_pipeline_blocks_extract_without_harvest_output(web_client):
    client, _ = web_client
    csrf = login(client)
    response = client.post("/pipeline/extract", data={"csrf": csrf})
    assert response.status_code == 409
    assert "Não há chunks pendentes" in response.text


def test_unknown_details_do_not_expose_arbitrary_files(web_client):
    client, _ = web_client
    login(client)
    assert client.get("/sources/not-found").status_code == 404
    assert client.get("/notes/not-found").status_code == 404
    assert client.get("/mocs/not-found").status_code == 404


def test_notes_catalog_search_filters_and_pagination(web_client):
    client, _ = web_client
    login(client)
    db = client.app.state.service.db()
    try:
        for source_id, citekey, author in (
            ("@Ana2024", "Ana2024", "Ana Silva"),
            ("@Beto2024", "Beto2024", "Beto Costa"),
        ):
            db.upsert_source(
                source_id,
                citekey,
                f"Livro de {author}",
                [author],
                2024,
                "",
                f"/tmp/{citekey}.md",
                "md",
            )
        for index in range(14):
            db.upsert_note(
                f"note-{index:02}",
                "@Ana2024" if index % 2 == 0 else "@Beto2024",
                None,
                title=f"Ideia {index:02}",
                body="Evidência única" if index == 0 else "Outros argumentos",
                origin="manual" if index % 2 == 0 else "pipeline",
            )
        db.upsert_moc("mapa-1", "Mapa geral", body="Mapa com evidência única")
    finally:
        db.close()

    first = client.get("/notes")
    assert first.status_code == 200
    assert "15 itens encontrados" in first.text
    assert "Página 1 de 2" in first.text
    assert 'rel="next"' in first.text
    second = client.get("/notes?page=2")
    assert "Página 2 de 2" in second.text
    assert 'rel="prev"' in second.text
    assert "3 itens encontrados" not in second.text  # total, not page count
    sorted_second = client.get("/notes?kind=ZTL&sort=title&page=2")
    assert "Página 2 de 2" in sorted_second.text
    assert "kind=ZTL&amp;sort=title&amp;page=1" in sorted_second.text

    search = client.get("/notes", params={"q": "evidencia"})
    assert "2 itens encontrados" in search.text
    assert "Ideia 00" in search.text and "Mapa geral" in search.text
    assert "Ideia 01" not in search.text
    assert "Nenhuma nota corresponde" in client.get("/notes?q=%25").text

    filtered = client.get(
        "/notes",
        params={
            "kind": "ZTL",
            "source_id": "@Ana2024",
            "author": "Ana Silva",
            "origin": "manual",
            "sort": "title",
        },
    )
    assert "7 itens encontrados" in filtered.text
    assert "Mapa geral" not in filtered.text
    assert 'value="@Ana2024" selected' in filtered.text
    assert 'value="Ana Silva" selected' in filtered.text
    assert "Autor: Ana Silva" in filtered.text
    assert "Fonte: Livro de Ana Silva" in filtered.text
    assert (
        "0 itens encontrados"
        in client.get("/notes", params={"kind": "MOC", "author": "Ana Silva"}).text
    )
    assert "1 item encontrado" in client.get("/notes?kind=MOC").text
    assert "Página 1 de 2" in client.get("/notes?page=invalid").text
    assert "Página 2 de 2" in client.get("/notes?page=999999").text


def _seed_ranked_notes(db):
    db.upsert_note(
        "note-a",
        None,
        None,
        title="Ética aplicada",
        body="Uma nota longa sobre deveres que cita Kant uma vez " + "e segue outro assunto " * 40,
    )
    db.upsert_note("note-b", None, None, title="Kant e a crítica", body="Kant, Kant e a CRÍTICA.")
    db.upsert_note("note-c", None, None, title="Hume", body="Ceticismo sem relação.")
    db.upsert_moc("mapa-kant", "Kant", body="Mapa")


def test_notes_search_ranks_by_relevance_and_can_sort_by_date(web_client):
    """With ``q`` the list is BM25-ranked from FTS5; a MOC whose topic or body
    matches comes first, as the door to that subject (#312)."""
    client, _ = web_client
    login(client)
    db = client.app.state.service.db()
    try:
        _seed_ranked_notes(db)
        db.conn.execute(
            "UPDATE notes SET updated_at='2020-01-01T00:00:00+00:00' WHERE note_id='note-b'"
        )
        db.conn.commit()
    finally:
        db.close()

    ranked = client.get("/notes", params={"q": "kant"}).text
    assert "3 itens encontrados" in ranked
    assert ranked.index("Kant e a crítica") < ranked.index("Ética aplicada")
    assert ranked.index(">Kant<") < ranked.index("Kant e a crítica")
    assert '<option value="" selected>' in ranked

    by_date = client.get("/notes", params={"q": "kant", "sort": "recent", "kind": "ZTL"}).text
    assert by_date.index("Ética aplicada") < by_date.index("Kant e a crítica")
    assert "Hume" not in ranked and "Hume" not in by_date


def test_notes_search_folds_accents_and_neutralizes_fts_operators(web_client):
    client, _ = web_client
    login(client)
    db = client.app.state.service.db()
    try:
        _seed_ranked_notes(db)
    finally:
        db.close()

    assert "Kant e a crítica" in client.get("/notes", params={"q": "critica"}).text
    assert "Kant e a crítica" in client.get("/notes", params={"q": "CRÍTICA"}).text
    for raw in ('"kant', "kant OR", "NOT kant", "kant*", "title:kant", "kant NEAR("):
        page = client.get("/notes", params={"q": raw, "kind": "ZTL"})
        assert page.status_code == 200, raw
        assert "Kant e a crítica" in page.text, raw
    # A quoted prefix stays a literal term: "kan" is not "kant".
    assert "Nenhuma nota corresponde" in client.get("/notes", params={"q": "kan*"}).text


def test_notes_search_does_not_fold_every_note_in_python(tmp_path):
    """Notes are matched by the FTS5 index, not by a Python UDF per row (#312)."""
    from zettel.search_terms import fold
    from zettel.state import StateDB

    db = StateDB(tmp_path / "state.db")
    try:
        _seed_ranked_notes(db)
        for index in range(30):
            db.upsert_note(f"filler-{index}", None, None, title=f"Nota {index}", body="texto")
        calls = []

        def counting_fold(text):
            calls.append(text)
            return fold(text)

        db.conn.create_function("zfold", 1, counting_fold, deterministic=True)
        rows, total = db.search_catalog(query="kant")
    finally:
        db.close()
    assert total == 3
    assert [row["item_id"] for row in rows][:2] == ["mapa-kant", "note-b"]
    assert len(calls) <= 4  # the MOC rows only, title and body


def test_note_markdown_copy_and_download_use_vault_files_only(web_client):
    client, tmp_path = web_client
    permanent = tmp_path / "vault" / "30_Permanent"
    mocs = tmp_path / "vault" / "40_MOCs"
    permanent.mkdir(parents=True)
    mocs.mkdir()
    note_path = permanent / "ZTL - nota.md"
    moc_path = mocs / "MOC - mapa.md"
    original = "---\ntitle: Nota\n---\n\n# Versão atual\n"
    # Bytes, not text: the endpoint serves the file verbatim, and write_text
    # would turn "\n" into "\r\n" on Windows.
    note_path.write_bytes(original.encode("utf-8"))
    moc_path.write_bytes(b"# Mapa atual\n")
    outside = tmp_path / "privado.md"
    outside.write_text("NÃO EXPOR", encoding="utf-8")
    # Windows needs a privilege to create symlinks; without it the "symlink" row
    # points at a missing file and still has to 404.
    with contextlib.suppress(OSError):
        (permanent / "link.md").symlink_to(outside)
    db = client.app.state.service.db()
    try:
        db.upsert_note("safe", None, str(note_path), title="Nota", body="Conteúdo antigo")
        db.upsert_moc("safe-moc", "Mapa", path=str(moc_path), body="Conteúdo antigo")
        db.upsert_note("outside", None, str(outside), title="Fora")
        db.upsert_note("symlink", None, str(permanent / "link.md"), title="Link")
        db.upsert_note("wrong-folder", None, str(moc_path), title="Errado")
        db.upsert_note("missing", None, str(permanent / "missing.md"), title="Sumiu")
    finally:
        db.close()
    assert client.get("/notes/safe/markdown", follow_redirects=False).status_code == 303
    assert client.get("/mocs/safe-moc/markdown", follow_redirects=False).status_code == 303

    login(client)
    page = client.get("/notes")
    detail = client.get("/notes/safe")
    moc_detail = client.get("/mocs/safe-moc")
    assert 'data-url="/notes/safe/markdown"' in page.text
    assert 'href="/notes/safe/markdown?download=1"' in detail.text
    assert 'data-url="/mocs/safe-moc/markdown"' in moc_detail.text
    raw = client.get("/notes/safe/markdown")
    assert raw.status_code == 200
    assert raw.text == original
    assert raw.headers["content-type"].startswith("text/markdown")
    note_path.write_bytes(b"# Editado no vault\n")
    assert client.get("/notes/safe/markdown").text == "# Editado no vault\n"
    assert client.get("/mocs/safe-moc/markdown").text == "# Mapa atual\n"
    download = client.get("/notes/safe/markdown?download=1")
    assert download.content == b"# Editado no vault\n"
    assert "attachment" in download.headers["content-disposition"]
    assert "ZTL%20-%20nota.md" in download.headers["content-disposition"]
    assert (
        "attachment"
        in client.get("/mocs/safe-moc/markdown?download=1").headers["content-disposition"]
    )
    for note_id in ("outside", "symlink", "wrong-folder", "missing", "not-found"):
        assert client.get(f"/notes/{note_id}/markdown").status_code == 404
        assert client.get(f"/notes/{note_id}/markdown?download=1").status_code == 404


def test_nested_inbox_file_can_be_selected_for_harvest(web_client, monkeypatch):
    client, tmp_path = web_client
    csrf = login(client)
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
    assert captured["operation"] == "prepare_harvest"
    assert captured["payload"]["selected_file"] == str(nested.resolve())
    assert captured["payload"]["dump_dir"] == str(tmp_path / "cache" / "chunk-dumps")
    assert captured["payload"]["extraction_dump_dir"] == str(
        tmp_path / "cache" / "extraction-dumps"
    )


def test_manual_source_document_type_is_a_dropdown_of_abnt_types(web_client):
    from zettel.bibliography import DOCUMENT_TYPE_LABELS

    client, _ = web_client
    login(client)
    response = client.get("/notes/new")
    assert '<select name="document_type">' in response.text
    assert 'name="document_type" placeholder' not in response.text
    for value, label in DOCUMENT_TYPE_LABELS.items():
        assert f'<option value="{value}"' in response.text
        assert label in response.text


def test_manual_source_lists_bibliography_options_before_reference(web_client):
    client, _ = web_client
    login(client)
    response = client.get("/notes/new")
    assert response.status_code == 200
    page = response.text
    assert page.index("<h2>O essencial</h2>") < page.index("<h2>Mais dados bibliográficos</h2>")
    assert page.index("<h2>Mais dados bibliográficos</h2>") < page.index("<h2>Referência</h2>")
    for field in (
        "title",
        "citekey",
        "authors",
        "year",
        "publisher",
        "edition",
        "place",
        "document_type",
        "doi",
        "url",
        "journal",
        "institution",
        "pages",
        "abnt_reference",
        "biblio_sample",
    ):
        assert len(re.findall(rf'<(?:input|textarea|select)\b[^>]*\bname="{field}"', page)) == 1
    assert "<details" not in page


def test_manual_source_scaffold_can_be_created_without_overwrite(web_client):
    client, tmp_path = web_client
    csrf = login(client)
    response = client.post(
        "/notes/new",
        data={
            "csrf": csrf,
            "note_type": "SRC",
            "title": "Thinking Fast",
            "citekey": "Kahneman2011",
            "authors": "Daniel Kahneman",
            "year": "2011",
            "document_type": "book",
        },
    )
    assert response.status_code == 201
    assert "Nota criada" in response.text
    created = list((tmp_path / "vault" / "10_Sources").glob("*.md"))
    assert len(created) == 1
    duplicate = client.post(
        "/notes/new",
        data={
            "csrf": csrf,
            "note_type": "SRC",
            "title": "Thinking Fast",
            "citekey": "Kahneman2011",
        },
    )
    assert duplicate.status_code == 400


def test_manual_ztl_uses_single_title_as_thesis(web_client):
    client, tmp_path = web_client
    csrf = login(client)
    response = client.post(
        "/notes/new",
        data={
            "csrf": csrf,
            "note_type": "ZTL",
            "title": "Heurísticas reduzem esforço cognitivo",
        },
    )
    assert response.status_code == 201
    note = next((tmp_path / "vault" / "30_Permanent").glob("*.md"))
    content = note.read_text(encoding="utf-8")
    assert "title: Heurísticas reduzem esforço cognitivo" in content
    assert "> **Tese**: Heurísticas reduzem esforço cognitivo" in content
    page = client.get("/notes/new")
    assert 'name="thesis"' not in page.text
    assert '<script src="/static/manual-notes.js">' in page.text
    assert "Tese / título da ideia" in client.get("/static/manual-notes.js").text


@pytest.mark.parametrize("source_id", ["../../outside", r"..\\outside", "@not-known"])
def test_manual_lit_rejects_forged_source_ids(web_client, source_id):
    client, tmp_path = web_client
    csrf = login(client)
    response = client.post(
        "/notes/new",
        data={
            "csrf": csrf,
            "note_type": "LIT",
            "title": "Tentativa",
            "source_id": source_id,
            "granular": "1",
        },
    )
    assert response.status_code == 400
    assert not (tmp_path / "outside").exists()


def test_harvest_rejects_absolute_and_parent_paths(web_client):
    client, tmp_path = web_client
    csrf = login(client)
    invalid = [str((tmp_path / "outside.txt").resolve()), "../outside.txt"]
    for selected in invalid:
        response = client.post(
            "/documents/harvest",
            data={"csrf": csrf, "selected_file": selected, "duplicate_action": "skip"},
        )
        assert response.status_code == 400


def test_documents_hide_completed_file_but_show_changed_copy(web_client, monkeypatch):
    client, tmp_path = web_client
    csrf = login(client)
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
            str(completed.resolve()),
            checksum,
            "md",
            source_id="@completed",
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
            str(incomplete.resolve()),
            incomplete_checksum,
            "md",
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
    assert captured["operation"] == "prepare_harvest"
    assert captured["payload"]["selected_file"] == str(incomplete.resolve())

    completed.write_text("# Conteúdo alterado", encoding="utf-8")
    refreshed = client.get("/documents")
    assert 'value="completed.md"' in refreshed.text


def test_note_and_moc_details_render_sanitized_markdown(web_client):
    client, _ = web_client
    login(client)
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
            "note-markdown",
            None,
            None,
            title="Nota formatada",
            body=markdown,
        )
        db.upsert_note(
            "note-target",
            None,
            None,
            title="Nota relacionada",
            body="# Destino",
        )
        db.upsert_note(
            "note-target-2",
            None,
            None,
            title="Outra nota relacionada",
            body="# Outro destino",
        )
        db.upsert_note(
            "note-target-3",
            None,
            None,
            title="Nota de apoio",
            body="# Apoio",
        )
        db.upsert_note_connection(
            "note-markdown",
            "note-target",
            "extends",
            "Amplia o assunto",
        )
        db.upsert_note_connection(
            "note-markdown",
            "note-target-2",
            "extends",
            "Amplia outro aspecto",
        )
        db.upsert_note_connection(
            "note-markdown",
            "note-target-3",
            "supports",
            "Sustenta a tese",
        )
        db.upsert_moc(
            "moc-markdown",
            "Mapa formatado",
            body="## Seção\n\n`código`",
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


def test_note_detail_renders_tex_formulas_as_mathml(web_client):
    client, _ = web_client
    login(client)
    db = client.app.state.service.db()
    try:
        db.upsert_note(
            "note-math",
            None,
            None,
            title="Série estacionária",
            body=(
                "Para isolar os componentes estocásticos, subtrai-se o efeito "
                "da série $Z_t$. A primeira diferença é "
                "$\\nabla Z_t = Z_t - Z_{t-1}$."
            ),
        )
    finally:
        db.close()

    page = client.get("/notes/note-math")
    assert page.status_code == 200
    assert "<math" in page.text
    assert "<msub>" in page.text
    assert "<em>" not in page.text
    assert "$Z_t$" not in page.text


def test_review_draft_renders_tex_in_thesis(web_client):
    client, _ = web_client
    login(client)
    db = client.app.state.service.db()
    try:
        db.upsert_source("@Book2024", "Book2024", "Livro", ["Autor"], 2024, "h", "/x.md", "md")
        db.upsert_chapter("@Book2024::ch000", "@Book2024", "Cap", "ch")
        db.upsert_chunk(
            "@Book2024::ch000::math",
            "@Book2024",
            "@Book2024::ch000",
            "texto",
            "ck",
            status="awaiting_review",
            chunk_index=0,
            summary_json=json.dumps({"summary": "A série $Z_t$ é estacionária."}),
        )
        db.upsert_concept(
            "c-math",
            "@Book2024",
            "@Book2024::ch000::math",
            candidate_json=json.dumps(
                {
                    "thesis": (
                        r"A primeira diferença $\nabla Z_t = Z_t - Z_{t-1}$ remove a tendência."
                    ),
                    "definition": r"Operador $\nabla$ de grau $d$.",
                }
            ),
            status="awaiting_review",
        )
    finally:
        db.close()

    page = client.get("/review")
    assert page.status_code == 200
    assert "<math" in page.text
    assert "<msub>" in page.text
    assert "$Z_t$" not in page.text


def test_documents_can_queue_full_pipeline(web_client, monkeypatch):
    client, _ = web_client
    csrf = login(client)
    captured = {}
    service = client.app.state.service

    monkeypatch.setattr("zettel.web.documents.blocker", lambda cfg, operation: None)

    def fake_submit(operation, payload):
        captured.update(operation=operation, payload=payload)
        return "run-all-job"

    monkeypatch.setattr(service, "submit", fake_submit)
    page = client.get("/documents")
    assert "Executar pipeline completo" in page.text

    response = client.post(
        "/documents/run-all",
        data={"csrf": csrf},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/jobs/run-all-job"
    assert captured == {
        "operation": "run_all",
        "payload": {
            "duplicate_action": "skip",
            "skip_biblio": False,
            "skip_paging": False,
            "auto_approve": False,
        },
    }
    assert "Aprovar automaticamente acima do limiar" in page.text
    assert "nao foi validado" in page.text

    opted = client.post(
        "/documents/run-all",
        data={"csrf": csrf, "auto_approve": "1"},
        follow_redirects=False,
    )
    assert opted.status_code == 303
    assert captured["payload"]["auto_approve"] is True


def test_rejected_queue_shows_filter_origin(web_client):
    import json

    client, _ = web_client
    login(client)
    db = client.app.state.service.db()
    try:
        db.upsert_source("@Book2024", "Book2024", "Livro", ["Autor"], 2024, "h", "/x.md", "md")
        db.upsert_chapter("@Book2024::ch000", "@Book2024", "Cap", "ch")
        db.upsert_chunk(
            "@Book2024::ch000::filt",
            "@Book2024",
            "@Book2024::ch000",
            "TRECHO_DO_FILTRO",
            "ckf",
            status="rejected",
            summary_json=json.dumps(
                {
                    "chunk_status": "accepted",
                    "rejection_category": "filtered",
                    "candidates": [],
                    "rejected_candidates": [
                        {
                            "thesis": "TESE_DO_FILTRO",
                            "anchor_quote": "ANCORA_DO_FILTRO",
                            "reason": "anchor_quote_words=40 > 37 (teto tolerado)",
                        }
                    ],
                }
            ),
        )
    finally:
        db.close()

    page = client.get("/review?queue=rejected")
    assert page.status_code == 200
    assert "filtro" in page.text
    assert "TESE_DO_FILTRO" in page.text
    assert "ANCORA_DO_FILTRO" in page.text
    assert "teto tolerado" in page.text
    assert "TRECHO_DO_FILTRO" in page.text


def _seed_source(client, source_id="@Kahneman2011", citekey="Kahneman2011", title="Thinking Fast"):
    db = client.app.state.service.db()
    try:
        db.upsert_source(
            source_id,
            citekey,
            title,
            ["Daniel Kahneman"],
            2011,
            "h",
            "/p",
            "md",
        )
        db.conn.execute(
            "UPDATE sources SET extracted_text=?, lit_body=? WHERE source_id=?",
            ("SENTINEL_EXTRACTED_TEXT", "SENTINEL_LIT_BODY", source_id),
        )
        db.conn.commit()
    finally:
        db.close()


def _seed_literature_chunk(
    client,
    *,
    source_id="@Kahneman2011",
    chunk_index=1,
    section="Sistema 1",
    text="SENTINEL_CHUNK_TEXT",
    path=None,
):
    db = client.app.state.service.db()
    try:
        db.upsert_chapter(f"{source_id}::ch000", source_id, "Manual", "ch")
        chunk_id = f"{source_id}::manual::{chunk_index:04d}"
        db.upsert_chunk(
            chunk_id,
            source_id,
            f"{source_id}::ch000",
            text,
            "ck",
            locator="p. 20",
            section_path=section,
            chunk_index=chunk_index,
            page_in_book=20,
            literature_note_path=str(path or f"/vault/{chunk_id}.md"),
        )
        return chunk_id
    finally:
        db.close()


def test_manual_form_markup_contract(web_client):
    """The JS reads data-types / data-required-for; these tests do not execute JS."""
    client, _ = web_client
    login(client)
    page = client.get("/notes/new")
    assert page.status_code == 200
    assert "for-lit for-ztl" not in page.text
    assert 'data-types="' in page.text
    assert "data-required-for" in page.text
    assert "pelo menos 3 letras" in page.text
    assert re.search(r"\srequired(\s|=|>)", page.text) is None
    assert 'name="thesis"' not in page.text
    assert '<script src="/static/manual-notes.js">' in page.text
    assert "Tese / título da ideia" in client.get("/static/manual-notes.js").text
    assert 'id="biblio-format"' in page.text
    assert 'id="biblio-enrich"' in page.text
    assert 'name="biblio_sample"' in page.text
    assert 'name="lit_excerpt"' in page.text
    assert 'name="lit_summary"' in page.text
    assert 'name="lit_concepts"' in page.text
    assert 'name="lit_candidate"' in page.text
    assert "Escreva a nota" in page.text


def test_biblio_enrich_button_follows_harvest_phase(web_client, monkeypatch):
    """All-phase llm_ready can be false (e.g. missing Gemini) while harvest works."""
    client, _ = web_client
    monkeypatch.setattr("zettel.web.manual._connect_llm_ok", lambda cfg: False)
    monkeypatch.setattr("zettel.web.manual._biblio_llm_ok", lambda cfg: True)
    login(client)
    page = client.get("/notes/new")
    match = re.search(r'<button[^>]*id="biblio-enrich"[^>]*>', page.text)
    assert match
    assert "disabled" not in match.group(0)
    assert "data-locked" not in match.group(0)
    assert "biblio-llm-note" not in page.text


def test_from_lit_llm_checkbox_follows_connect_phase(web_client, monkeypatch):
    """Connect can be ready while extract/ask (Gemini) are not."""
    client, _ = web_client
    monkeypatch.setattr("zettel.web.manual._connect_llm_ok", lambda cfg: True)
    monkeypatch.setattr("zettel.web.manual._biblio_llm_ok", lambda cfg: False)
    login(client)
    page = client.get("/notes/new")
    match = re.search(r'<input[^>]*id="use-llm"[^>]*>', page.text)
    assert match
    assert "disabled" not in match.group(0)
    assert "data-locked" not in match.group(0)
    assert "A fase connect não tem credencial" not in page.text


def test_biblio_preview_requires_session(web_client):
    client, _ = web_client
    response = client.post("/notes/new/biblio-preview")
    assert response.status_code == 401
    assert response.json() == {"error": "unauthorized"}


def test_biblio_preview_rejects_bad_csrf(web_client):
    client, _ = web_client
    login(client)
    response = client.post(
        "/notes/new/biblio-preview",
        data={
            "csrf": "wrong",
            "document_type": "livro",
            "title": "O Capital",
        },
    )
    assert response.status_code == 403
    assert response.json() == {"error": "csrf"}


def test_biblio_preview_formats_abnt_without_writing_vault(web_client):
    client, tmp_path = web_client
    csrf = login(client)
    response = client.post(
        "/notes/new/biblio-preview",
        data={
            "csrf": csrf,
            "title": "O Capital",
            "authors": "Karl Marx",
            "year": "2013",
            "document_type": "livro",
            "place": "Sao Paulo",
            "publisher": "Boitempo",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["abnt_reference"].startswith("MARX, Karl.")
    assert "Sao Paulo: Boitempo, 2013." in body["abnt_reference"]
    assert body["enriched"] is False
    assert body["publisher"] == "Boitempo"
    assert not list((tmp_path / "vault").rglob("*.md"))


def test_biblio_preview_requires_document_type_without_llm(web_client):
    client, _ = web_client
    csrf = login(client)
    response = client.post(
        "/notes/new/biblio-preview",
        data={
            "csrf": csrf,
            "title": "O Capital",
            "authors": "Karl Marx",
            "year": "2013",
        },
    )
    assert response.status_code == 400
    assert "tipo de documento" in response.json()["error"].lower()


def test_biblio_preview_refuses_llm_without_evidence(web_client):
    client, _ = web_client
    csrf = login(client)
    response = client.post(
        "/notes/new/biblio-preview",
        data={
            "csrf": csrf,
            "enrich": "1",
            "document_type": "livro",
            "title": "O Capital",
        },
    )
    assert response.status_code == 400
    assert "DOI" in response.json()["error"]


def test_biblio_preview_enrich_without_llm_is_conflict(web_client, monkeypatch):
    client, tmp_path = web_client
    csrf = login(client)
    monkeypatch.setattr("zettel.web.manual._biblio_llm_ok", lambda cfg: False)
    response = client.post(
        "/notes/new/biblio-preview",
        data={
            "csrf": csrf,
            "enrich": "1",
            "document_type": "livro",
            "title": "O Capital",
            "doi": "https://doi.org/10.1234/x",
        },
    )
    assert response.status_code == 409
    assert "credencial" in response.json()["error"].lower()
    assert not list((tmp_path / "vault").rglob("*.md"))


def test_biblio_preview_enrich_fills_empty_fields_without_writing(web_client, monkeypatch):
    client, tmp_path = web_client
    csrf = login(client)
    monkeypatch.setattr("zettel.web.manual._biblio_llm_ok", lambda cfg: True)

    def fake_enrich(
        cfg, db, seed, text_sample, filename, *, prefer_seed=False, raise_on_error=False
    ):
        assert prefer_seed is True
        assert filename == "manual-src"
        assert "10.1234/x" in text_sample
        seed.publisher = seed.publisher or "Boitempo"
        seed.place = "Sao Paulo"
        return seed

    monkeypatch.setattr("zettel.bibliography.enrich_with_llm", fake_enrich)
    response = client.post(
        "/notes/new/biblio-preview",
        data={
            "csrf": csrf,
            "enrich": "1",
            "title": "O Capital",
            "authors": "Karl Marx",
            "year": "2013",
            "document_type": "livro",
            "doi": "https://doi.org/10.1234/x",
            "publisher": "Boitempo",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["enriched"] is True
    assert body["publisher"] == "Boitempo"
    assert body["place"] == "Sao Paulo"
    assert "MARX, Karl." in body["abnt_reference"]
    assert not list((tmp_path / "vault").rglob("*.md"))


def test_manual_source_keeps_edited_abnt_reference(web_client):
    client, tmp_path = web_client
    csrf = login(client)
    response = client.post(
        "/notes/new",
        data={
            "csrf": csrf,
            "note_type": "SRC",
            "title": "O Capital",
            "citekey": "Marx2013",
            "authors": "Karl Marx",
            "year": "2013",
            "document_type": "livro",
            "abnt_reference": "MARX, Karl. O Capital. Sao Paulo: Boitempo, 2013.",
            "publisher": "Boitempo",
            "biblio_sample": "nao deve ir para a nota",
        },
    )
    assert response.status_code == 201
    created = list((tmp_path / "vault" / "10_Sources").glob("*.md"))
    assert len(created) == 1
    content = created[0].read_text(encoding="utf-8")
    assert "MARX, Karl. O Capital. Sao Paulo: Boitempo, 2013." in content
    assert "nao deve ir para a nota" not in content


def test_manual_source_offers_sync_then_create_lit(web_client):
    client, _ = web_client
    csrf = login(client)
    response = client.post(
        "/notes/new",
        data={
            "csrf": csrf,
            "note_type": "SRC",
            "title": "O Capital",
            "citekey": "Marx2013",
        },
    )
    assert response.status_code == 201
    assert "Sincronizar e criar LIT" in response.text
    assert 'action="/pipeline/sync"' in response.text
    assert 'value="/notes/new?type=LIT&amp;source_id=%40Marx2013"' in response.text


def test_manual_form_preserves_values_after_validation_error(web_client):
    client, _ = web_client
    csrf = login(client)
    response = client.post(
        "/notes/new",
        data={
            "csrf": csrf,
            "note_type": "LIT",
            "granular": "1",
            "title": "Sistema 1 & heurísticas",
            "chunk_index": "7",
            "page_number": "42",
            "lit_excerpt": "Trecho <original>",
            "lit_summary": "Resumo interpretativo",
            "lit_concepts": "decisão\nheurística",
            "lit_candidate": "Decisões rápidas usam atalhos.",
            "force": "1",
        },
    )
    assert response.status_code == 400
    assert 'value="Sistema 1 &amp; heurísticas"' in response.text
    assert 'name="chunk_index" min="1" inputmode="numeric" value="7"' in response.text
    assert (
        'name="page_number" type="number" min="1" inputmode="numeric" value="42"' in response.text
    )
    assert "Trecho &lt;original&gt;" in response.text
    assert "Resumo interpretativo" in response.text
    assert "decisão\nheurística" in response.text
    assert "Decisões rápidas usam atalhos." in response.text
    assert 'id="force-overwrite" checked' in response.text


def test_manual_lit_granular_creates_chunk_file(web_client):
    client, tmp_path = web_client
    csrf = login(client)
    _seed_source(client)
    response = client.post(
        "/notes/new",
        data={
            "csrf": csrf,
            "note_type": "LIT",
            "title": "Sistema 1",
            "source_id": "@Kahneman2011",
            "granular": "1",
            "chunk_index": "1",
            "page_number": "20",
        },
    )
    assert response.status_code == 201
    assert "Próximo passo" in response.text
    created = list((tmp_path / "vault" / "20_Literature" / "Kahneman2011").glob("*.md"))
    assert len(created) == 1
    assert "p020" in created[0].name
    content = created[0].read_text(encoding="utf-8")
    assert "chunk_id:" in content
    assert "@Kahneman2011::manual::0001" in content
    assert "Sistema 1" in content


def test_manual_lit_granular_writes_body(web_client):
    client, tmp_path = web_client
    csrf = login(client)
    _seed_source(client)
    response = client.post(
        "/notes/new",
        data={
            "csrf": csrf,
            "note_type": "LIT",
            "title": "Sistema 1",
            "source_id": "@Kahneman2011",
            "granular": "1",
            "chunk_index": "1",
            "page_number": "20",
            "lit_excerpt": "O sistema 1 opera automatica e rapidamente.",
            "lit_summary": "Pensar rapido e o modo padrao, sem esforco deliberado.",
            "lit_concepts": "sistema 1\nheuristica",
            "lit_candidate": "O sistema 1 e o modo padrao da mente.",
        },
    )
    assert response.status_code == 201
    created = list((tmp_path / "vault" / "20_Literature" / "Kahneman2011").glob("*.md"))
    assert len(created) == 1
    content = created[0].read_text(encoding="utf-8")
    assert "O sistema 1 opera automatica e rapidamente." in content
    assert "Pensar rapido e o modo padrao, sem esforco deliberado." in content
    assert "#sistema 1" in content
    assert "#heuristica" in content
    assert "O sistema 1 e o modo padrao da mente." in content
    assert "_Preencha o resumo._" not in content
    assert "_Cole o trecho da fonte aqui._" not in content


_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)


def _asset_files(tmp_path: Path) -> list[Path]:
    root = tmp_path / "vault" / "90_Assets"
    return list(root.glob("img-*")) if root.is_dir() else []


def test_manual_note_form_accepts_an_image_file(web_client):
    client, _ = web_client
    login(client)
    page = client.get("/notes/new").text
    assert 'enctype="multipart/form-data"' in page
    assert 'name="images"' in page
    assert "LIT_GRANULAR ZTL_BLANK ZTL_FROM_LIT" in page


def test_manual_lit_granular_stores_uploaded_image(web_client):
    client, tmp_path = web_client
    csrf = login(client)
    _seed_source(client)
    assert client.app.state.service.cfg.images.enabled is False
    response = client.post(
        "/notes/new",
        data={
            "csrf": csrf,
            "note_type": "LIT",
            "title": "Sistema 1",
            "source_id": "@Kahneman2011",
            "granular": "1",
            "chunk_index": "1",
            "page_number": "20",
        },
        files={"images": ("figura.png", _PNG, "image/png")},
    )
    assert response.status_code == 201
    created = list((tmp_path / "vault" / "20_Literature" / "Kahneman2011").glob("*.md"))
    assert len(created) == 1
    content = created[0].read_text(encoding="utf-8")
    assert "## Imagens Relacionadas" in content
    assert "_Nenhuma._" not in content
    assert "90_Assets/img-" in content
    files = _asset_files(tmp_path)
    assert len(files) == 1
    assert files[0].read_bytes() == _PNG
    db = client.app.state.service.db()
    try:
        assets = db.get_assets_for_source("@Kahneman2011")
        assert len(assets) == 1
        assert assets[0]["status"] == "pending"
        assert assets[0]["path"] in content
        from zettel.assets import register_note_images

        register_note_images(client.app.state.service.cfg, db, "@Kahneman2011", created[0])
        assert len(db.get_assets_for_source("@Kahneman2011")) == 1
    finally:
        db.close()


def test_manual_ztl_blank_stores_uploaded_image(web_client):
    client, tmp_path = web_client
    csrf = login(client)
    _seed_source(client)
    response = client.post(
        "/notes/new",
        data={
            "csrf": csrf,
            "note_type": "ZTL",
            "title": "Heurísticas como atalhos",
            "source_id": "@Kahneman2011",
            "ztl_origin": "blank",
        },
        files={"images": ("figura.png", _PNG, "image/png")},
    )
    assert response.status_code == 201
    created = list((tmp_path / "vault" / "30_Permanent").glob("*.md"))
    assert len(created) == 1
    content = created[0].read_text(encoding="utf-8")
    assert "## Figuras" in content
    assert "![[90_Assets/img-" in content
    db = client.app.state.service.db()
    try:
        assets = db.get_assets_for_source("@Kahneman2011")
        assert len(assets) == 1
        assert assets[0]["status"] == "pending"
    finally:
        db.close()


@pytest.mark.parametrize(
    ("data", "filename", "body", "snippet"),
    [
        (
            {"note_type": "SRC", "title": "O Capital", "citekey": "Marx2013"},
            "figura.png",
            _PNG,
            "não aceitam imagem",
        ),
        (
            {
                "note_type": "LIT",
                "title": "Índice",
                "source_id": "@Kahneman2011",
                "granular": "",
            },
            "figura.png",
            _PNG,
            "não aceitam imagem",
        ),
        (
            {"note_type": "ZTL", "title": "Uma tese", "ztl_origin": "blank"},
            "figura.png",
            _PNG,
            "anexar uma imagem",
        ),
        (
            {
                "note_type": "LIT",
                "title": "Sistema 1",
                "source_id": "@Kahneman2011",
                "granular": "1",
            },
            "notas.txt",
            b"nao e imagem",
            "PNG",
        ),
    ],
)
def test_manual_image_upload_is_refused(web_client, data, filename, body, snippet):
    client, tmp_path = web_client
    csrf = login(client)
    _seed_source(client)
    response = client.post(
        "/notes/new",
        data={"csrf": csrf, **data},
        files={"images": (filename, body, "application/octet-stream")},
    )
    assert response.status_code == 400
    assert snippet in response.text
    assert _asset_files(tmp_path) == []


def test_manual_image_upload_rejects_too_many_and_empty(web_client):
    from io import BytesIO

    from zettel.web.manual_images import read_image_uploads

    class _Upload:
        def __init__(self, name: str, data: bytes):
            self.filename = name
            self.file = BytesIO(data)

    class _Form:
        def __init__(self, items):
            self._items = items

        def getlist(self, name: str):
            assert name == "images"
            return self._items

    with pytest.raises(ValueError, match="no máximo 6"):
        read_image_uploads(_Form([_Upload(f"f{i}.png", _PNG) for i in range(7)]))
    with pytest.raises(ValueError, match="vazia"):
        read_image_uploads(_Form([_Upload("vazia.png", b"")]))
    with pytest.raises(ValueError, match="8 MB"):
        read_image_uploads(_Form([_Upload("grande.png", b"x" * (8 * 1024 * 1024 + 1))]))


def test_manual_lit_index_collides_without_force(web_client):
    client, _tmp_path = web_client
    csrf = login(client)
    _seed_source(client)
    first = client.post(
        "/notes/new",
        data={
            "csrf": csrf,
            "note_type": "SRC",
            "title": "Thinking Fast",
            "citekey": "Kahneman2011",
        },
    )
    assert first.status_code == 201
    collision = client.post(
        "/notes/new",
        data={
            "csrf": csrf,
            "note_type": "LIT",
            "title": "Thinking Fast",
            "source_id": "@Kahneman2011",
            "granular": "",
        },
    )
    assert collision.status_code == 400
    forced = client.post(
        "/notes/new",
        data={
            "csrf": csrf,
            "note_type": "LIT",
            "title": "Thinking Fast",
            "source_id": "@Kahneman2011",
            "granular": "",
            "force": "1",
        },
    )
    assert forced.status_code == 201


def test_manual_ztl_renders_missing_src_warning(web_client):
    client, _ = web_client
    csrf = login(client)
    _seed_source(client, source_id="@Ghost2020", citekey="Ghost2020", title="Fantasma")
    response = client.post(
        "/notes/new",
        data={
            "csrf": csrf,
            "note_type": "ZTL",
            "title": "Uma tese sem SRC no vault",
            "source_id": "@Ghost2020",
        },
    )
    assert response.status_code == 201
    assert "SRC nao encontrada" in response.text


def test_pickers_require_session_and_omit_payloads(web_client):
    client, tmp_path = web_client
    anonymous = client.get("/api/pickers/sources")
    assert anonymous.status_code == 401
    assert anonymous.json() == {"error": "unauthorized"}
    login(client)
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
    assert sources.json()["items"][0]["id"] == "@Kahneman2011"
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
    assert items[0]["id"] == chunk_id
    assert items[0]["source_id"] == "@Kahneman2011"
    assert items[0]["id"] != items[0]["source_id"]
    other = client.get(
        "/api/pickers/literature",
        params={"q": "Sistema", "source_id": "@Other2010"},
    )
    assert other.json()["items"] == []


@pytest.mark.parametrize("query", ["%", "_", '" OR 1=1 --', "NEAR(a b)", "a*", "-x"])
def test_pickers_treat_metacharacters_literally(web_client, query):
    client, _ = web_client
    login(client)
    _seed_source(client)
    response = client.get("/api/pickers/sources", params={"q": query})
    assert response.status_code == 200
    if query == "%":
        assert response.json()["items"] == []


def test_pickers_are_accent_insensitive_and_clamp_limit(web_client):
    client, _ = web_client
    login(client)
    _seed_source(client, source_id="@Funcao2020", citekey="Funcao2020", title="Função cognitiva")
    folded = client.get("/api/pickers/sources", params={"q": "funcao"})
    assert folded.json()["items"][0]["source_id"] == "@Funcao2020"
    clamped = client.get("/api/pickers/sources", params={"q": "", "limit": 999})
    assert clamped.status_code == 200
    empty = client.get("/api/pickers/sources", params={"q": ""})
    assert empty.json()["items"][0]["source_id"] == "@Funcao2020"


def test_from_lit_unknown_chunk_is_rejected(web_client):
    client, _ = web_client
    csrf = login(client)
    response = client.post(
        "/notes/new",
        data={
            "csrf": csrf,
            "note_type": "ZTL",
            "ztl_origin": "from_lit",
            "from_lit": "@nope::manual::0001",
        },
    )
    assert response.status_code == 400
    assert "LIT granular" in response.text


def test_from_lit_source_id_is_not_a_chunk(web_client):
    """A literature pick that submitted source_id (citekey) is not a valid chunk."""
    client, _ = web_client
    csrf = login(client)
    _seed_source(client)
    response = client.post(
        "/notes/new",
        data={
            "csrf": csrf,
            "note_type": "ZTL",
            "ztl_origin": "from_lit",
            "from_lit": "@Kahneman2011",
        },
    )
    assert response.status_code == 400
    assert "LIT granular" in response.text


def test_from_lit_chunk_id_enqueues(web_client, monkeypatch):
    client, tmp_path = web_client
    csrf = login(client)
    _seed_source(client)
    lit_dir = tmp_path / "vault" / "20_Literature" / "Kahneman2011"
    lit_dir.mkdir(parents=True)
    path = lit_dir / "LIT - Kahneman2011 - p020 - sistema-1-0001.md"
    path.write_text(
        "---\ntype: literature\nchunk_id: '@Kahneman2011::manual::0001'\n"
        "source_id: '@Kahneman2011'\n---\n## Resumo\n\nHeurísticas guiam o julgamento.\n",
        encoding="utf-8",
    )
    chunk_id = _seed_literature_chunk(client, path=path)
    captured = {}

    def fake_submit(operation, payload):
        captured.update(operation=operation, payload=payload)
        return "from-lit-job"

    monkeypatch.setattr(client.app.state.service, "submit", fake_submit)
    response = client.post(
        "/notes/new",
        data={
            "csrf": csrf,
            "note_type": "ZTL",
            "ztl_origin": "from_lit",
            "from_lit": chunk_id,
            "lit_thesis": "Heurísticas guiam o julgamento.",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert captured["operation"] == "manual-ztl-from-lit"
    assert captured["payload"]["ref"] == chunk_id


def test_from_lit_upload_is_stored_and_passed_to_the_job(web_client, monkeypatch):
    client, tmp_path = web_client
    csrf = login(client)
    _seed_source(client)
    lit_dir = tmp_path / "vault" / "20_Literature" / "Kahneman2011"
    lit_dir.mkdir(parents=True)
    path = lit_dir / "LIT - Kahneman2011 - p020 - sistema-1-0001.md"
    path.write_text(
        "---\ntype: literature\nchunk_id: '@Kahneman2011::manual::0001'\n"
        "source_id: '@Kahneman2011'\n---\n## Resumo\n\nHeurísticas guiam o julgamento.\n",
        encoding="utf-8",
    )
    chunk_id = _seed_literature_chunk(client, path=path)
    captured = {}

    def fake_submit(operation, payload):
        captured.update(operation=operation, payload=payload)
        return "from-lit-job"

    monkeypatch.setattr(client.app.state.service, "submit", fake_submit)
    response = client.post(
        "/notes/new",
        data={
            "csrf": csrf,
            "note_type": "ZTL",
            "ztl_origin": "from_lit",
            "from_lit": chunk_id,
            "lit_thesis": "Heurísticas guiam o julgamento.",
        },
        files={"images": ("figura.png", _PNG, "image/png")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    paths = captured["payload"]["image_paths"]
    assert len(paths) == 1
    assert paths[0].startswith("90_Assets/img-")
    assert (tmp_path / "vault" / paths[0]).read_bytes() == _PNG
    assert _PNG not in json.dumps(captured["payload"]).encode()


@pytest.mark.parametrize(
    "path",
    [
        "../../etc/passwd",
        "/etc/passwd",
        r"C:\Windows\win.ini",
        "30_Permanent/other.md",
        "20_Literature/../../secret.md",
    ],
)
def test_from_lit_path_cannot_escape_literature_dir(web_client, path):
    client, tmp_path = web_client
    csrf = login(client)
    response = client.post(
        "/notes/new",
        data={
            "csrf": csrf,
            "note_type": "ZTL",
            "ztl_origin": "from_lit",
            "from_lit_path": path,
        },
    )
    assert response.status_code == 400
    assert not (tmp_path / "secret.md").exists()
    assert not list((tmp_path / "vault" / "30_Permanent").glob("*.md"))


def test_from_lit_index_note_is_rejected(web_client):
    client, tmp_path = web_client
    csrf = login(client)
    _seed_source(client)
    index = tmp_path / "vault" / "20_Literature" / "LIT - Kahneman2011 - index.md"
    index.parent.mkdir(parents=True, exist_ok=True)
    index.write_text("---\ntype: literature_index\n---\n# Index\n", encoding="utf-8")
    response = client.post(
        "/notes/new",
        data={
            "csrf": csrf,
            "note_type": "ZTL",
            "ztl_origin": "from_lit",
            "from_lit_path": "20_Literature/LIT - Kahneman2011 - index.md",
        },
    )
    assert response.status_code == 400


def test_from_lit_llm_without_sqlite_source_is_conflict(web_client):
    client, tmp_path = web_client
    csrf = login(client)
    lit_dir = tmp_path / "vault" / "20_Literature" / "Orphan2020"
    lit_dir.mkdir(parents=True)
    lit = lit_dir / "LIT - Orphan2020 - p001 - tema-0001.md"
    lit.write_text(
        "---\ntype: literature\nchunk_id: '@Orphan2020::manual::0001'\n"
        "source_id: '@Orphan2020'\n---\n## Resumo\n\nUma tese real o suficiente.\n",
        encoding="utf-8",
    )
    response = client.post(
        "/notes/new",
        data={
            "csrf": csrf,
            "note_type": "ZTL",
            "ztl_origin": "from_lit",
            "from_lit_path": "20_Literature/Orphan2020/LIT - Orphan2020 - p001 - tema-0001.md",
            "use_llm": "1",
            "lit_thesis": "Uma tese",
        },
    )
    assert response.status_code == 409
    assert "Pipeline" in response.text


def test_from_lit_scaffold_without_thesis_is_rejected(web_client):
    client, tmp_path = web_client
    csrf = login(client)
    _seed_source(client)
    lit_dir = tmp_path / "vault" / "20_Literature" / "Kahneman2011"
    lit_dir.mkdir(parents=True)
    lit = lit_dir / "LIT - Kahneman2011 - p001 - tema-0001.md"
    lit.write_text(
        "---\ntype: literature\nchunk_id: '@Kahneman2011::manual::0001'\n"
        "source_id: '@Kahneman2011'\n---\n## Resumo\n\n_Preencha o resumo._\n",
        encoding="utf-8",
    )
    response = client.post(
        "/notes/new",
        data={
            "csrf": csrf,
            "note_type": "ZTL",
            "ztl_origin": "from_lit",
            "from_lit_path": "20_Literature/Kahneman2011/LIT - Kahneman2011 - p001 - tema-0001.md",
        },
    )
    assert response.status_code == 400


def test_from_lit_enqueues_ref_thesis_and_force(web_client, monkeypatch):
    client, tmp_path = web_client
    csrf = login(client)
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
            "csrf": csrf,
            "note_type": "ZTL",
            "ztl_origin": "from_lit",
            "from_lit_path": rel,
            "lit_thesis": "Heurísticas guiam o julgamento.",
            "force": "1",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert captured["operation"] == "manual-ztl-from-lit"
    assert "Kahneman2011" in captured["payload"]["ref"].replace("\\", "/")
    assert captured["payload"]["thesis"] == "Heurísticas guiam o julgamento."
    assert captured["payload"]["force"] is True


def test_review_rejected_queue_shows_full_text_and_requeues_only_extract(web_client):
    import json

    client, _ = web_client
    csrf = login(client)
    long_text = "TRECHO_COMPLETO_" + ("palavra " * 80)
    long_reason = "MOTIVO_COMPLETO_" + ("porque " * 30)
    extract_id = "@Book2024::ch000::rej00"
    reviewer_id = "@Book2024::ch000::rev"
    draft_id = "@Book2024::ch000::draft"
    db = client.app.state.service.db()
    try:
        db.upsert_source(
            "@Book2024",
            "Book2024",
            "Livro",
            ["Autor"],
            2024,
            "h",
            "/x.md",
            "md",
        )
        db.upsert_chapter("@Book2024::ch000", "@Book2024", "Cap", "ch")
        db.upsert_chunk(
            extract_id,
            "@Book2024",
            "@Book2024::ch000",
            long_text,
            "ck0",
            status="rejected",
            section_path="Referencias",
            chunk_index=0,
            page_in_book=4,
            summary_json=json.dumps(
                {
                    "chunk_status": "rejected",
                    "rejection_category": "structural",
                    "rejection_reason": long_reason,
                    "candidates": [],
                }
            ),
        )
        for index in range(1, 11):
            text = "PAGINA_DOIS_UNICO" if index == 10 else f"curto {index}"
            db.upsert_chunk(
                f"@Book2024::ch000::rej{index:02d}",
                "@Book2024",
                "@Book2024::ch000",
                text,
                f"ck{index}",
                status="rejected",
                chunk_index=index,
                summary_json=json.dumps(
                    {
                        "rejection_category": "structural",
                        "rejection_reason": "curto",
                        "candidates": [],
                    }
                ),
            )
        db.upsert_chunk(
            reviewer_id,
            "@Book2024",
            "@Book2024::ch000",
            "texto do revisor",
            "ckr",
            status="rejected",
            chunk_index=20,
            summary_json=json.dumps(
                {
                    "candidates": [{"thesis": "TESE_DO_REVISOR"}],
                    "rejection_reason": "revisor",
                }
            ),
        )
        db.upsert_chunk(
            draft_id,
            "@Book2024",
            "@Book2024::ch000",
            "texto do draft",
            "ckd",
            status="awaiting_review",
            chunk_index=21,
            summary_json=json.dumps({"summary": "RESUMO_DRAFT", "candidates": []}),
        )
    finally:
        db.close()

    rejected = client.get("/review?queue=rejected")
    assert rejected.status_code == 200
    assert long_text in rejected.text
    assert long_reason in rejected.text
    assert "PAGINA_DOIS_UNICO" not in rejected.text
    assert "TESE_DO_REVISOR" not in rejected.text
    assert "RESUMO_DRAFT" not in rejected.text
    assert "Reenfileirar para o extract" in rejected.text
    assert "Processar mesmo assim" in rejected.text
    assert "Apagar do banco" in rejected.text
    assert 'href="?queue=rejected&amp;source_id=&amp;confidence=&amp;page=2"' in rejected.text
    assert "Página 1 de 2" in rejected.text

    page_two = client.get("/review?queue=rejected&page=2")
    assert "PAGINA_DOIS_UNICO" in page_two.text
    assert long_text not in page_two.text
    assert "Página 2 de 2" in page_two.text
    beyond = client.get("/review?queue=rejected&page=9")
    assert "Página 2 de 2" in beyond.text
    assert "PAGINA_DOIS_UNICO" in beyond.text

    drafts = client.get("/review")
    assert "RESUMO_DRAFT" in drafts.text
    assert long_text not in drafts.text
    assert "Aprovar selecionados" in drafts.text

    invalid = client.post("/review/action", data={"csrf": csrf, "action": "nope"})
    assert invalid.status_code == 400

    response = client.post(
        "/review/action",
        data={
            "csrf": csrf,
            "action": "requeue",
            "chunk_ids": [extract_id, reviewer_id, draft_id],
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    job_id = response.headers["location"].rsplit("/", 1)[-1]
    payload = wait_for_job(client, job_id)
    assert payload["job"]["state"] == "succeeded"
    assert payload["job"]["result"]["requeued"] == 1
    assert payload["job"]["result"]["skipped"] == 2

    db = client.app.state.service.db()
    try:
        assert db.get_chunk(extract_id)["status"] == "pending"
        assert db.get_chunk(reviewer_id)["status"] == "rejected"
        assert db.get_chunk(draft_id)["status"] == "awaiting_review"
    finally:
        db.close()

    again = client.get("/review?queue=rejected")
    assert long_text not in again.text


def test_review_force_marks_only_extract_rejections(web_client):
    import json

    client, _ = web_client
    csrf = login(client)
    extract_id = "@Book2024::ch000::rej"
    reviewer_id = "@Book2024::ch000::rev"
    db = client.app.state.service.db()
    try:
        db.upsert_source("@Book2024", "Book2024", "Livro", ["Autor"], 2024, "h", "/x.md", "md")
        db.upsert_chapter("@Book2024::ch000", "@Book2024", "Cap", "ch")
        db.upsert_chunk(
            extract_id,
            "@Book2024",
            "@Book2024::ch000",
            "texto rejeitado pelo extract",
            "ck",
            status="rejected",
            chunk_index=0,
            summary_json=json.dumps(
                {
                    "rejection_category": "structural",
                    "rejection_reason": "motivo",
                    "candidates": [],
                }
            ),
        )
        db.upsert_chunk(
            reviewer_id,
            "@Book2024",
            "@Book2024::ch000",
            "texto do revisor",
            "ckr",
            status="rejected",
            chunk_index=1,
            summary_json=json.dumps({"candidates": [{"thesis": "TESE_DO_REVISOR"}]}),
        )
    finally:
        db.close()

    response = client.post(
        "/review/action",
        data={
            "csrf": csrf,
            "action": "force",
            "chunk_ids": [extract_id, reviewer_id],
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    job_id = response.headers["location"].rsplit("/", 1)[-1]
    payload = wait_for_job(client, job_id)
    assert payload["job"]["state"] == "succeeded"
    assert payload["job"]["result"]["forced"] == 1
    assert payload["job"]["result"]["skipped"] == 1

    db = client.app.state.service.db()
    try:
        forced = db.get_chunk(extract_id)
        assert forced["status"] == "pending"
        assert json.loads(forced["summary_json"])["force_extract"] is True
        assert db.get_chunk(reviewer_id)["status"] == "rejected"
    finally:
        db.close()


def test_review_discard_deletes_only_extract_rejections(web_client, monkeypatch):
    import json

    client, _ = web_client
    csrf = login(client)
    index = FakeVectorIndex()
    monkeypatch.setattr("zettel.index.VectorIndex", lambda **kwargs: index)
    extract_id = "@Book2024::ch000::rej"
    reviewer_id = "@Book2024::ch000::rev"
    db = client.app.state.service.db()
    try:
        db.upsert_source("@Book2024", "Book2024", "Livro", ["Autor"], 2024, "h", "/x.md", "md")
        db.upsert_chapter("@Book2024::ch000", "@Book2024", "Cap", "ch")
        db.upsert_chunk(
            extract_id,
            "@Book2024",
            "@Book2024::ch000",
            "texto rejeitado pelo extract",
            "ck",
            status="rejected",
            chunk_index=0,
            summary_json=json.dumps(
                {
                    "rejection_category": "structural",
                    "rejection_reason": "referencias",
                    "candidates": [],
                }
            ),
        )
        db.upsert_chunk(
            reviewer_id,
            "@Book2024",
            "@Book2024::ch000",
            "texto do revisor",
            "ckr",
            status="rejected",
            chunk_index=1,
            summary_json=json.dumps({"candidates": [{"thesis": "TESE_DO_REVISOR"}]}),
        )
    finally:
        db.close()

    response = client.post(
        "/review/action",
        data={"csrf": csrf, "action": "discard", "chunk_ids": [extract_id, reviewer_id]},
        follow_redirects=False,
    )
    assert response.status_code == 303
    job_id = response.headers["location"].rsplit("/", 1)[-1]
    payload = wait_for_job(client, job_id)
    assert payload["job"]["state"] == "succeeded"
    assert payload["job"]["result"]["discarded"] == 1
    assert payload["job"]["result"]["skipped"] == 1
    assert deleted(index, "chunks") == [extract_id]

    db = client.app.state.service.db()
    try:
        assert db.get_chunk(extract_id) is None
        assert db.get_chunk(reviewer_id)["status"] == "rejected"
    finally:
        db.close()


def test_review_page_shows_candidates_and_approves_one(web_client, monkeypatch):
    monkeypatch.setattr("zettel.review.finalize_approved_concepts", lambda *a, **k: None)
    monkeypatch.setattr("zettel.index.VectorIndex", lambda **kwargs: object())
    import json

    client, _ = web_client
    csrf = login(client)
    chunk_id = "@Book2024::ch000::abc"
    source = "O modelo usa atencao para ligar tokens distantes neste paragrafo longo."
    db = client.app.state.service.db()
    try:
        db.upsert_source("@Book2024", "Book2024", "Livro", ["Autor"], 2024, "h", "/x.md", "md")
        db.upsert_chapter("@Book2024::ch000", "@Book2024", "Cap", "ch")
        db.upsert_chunk(
            chunk_id,
            "@Book2024",
            "@Book2024::ch000",
            source,
            "ck",
            status="awaiting_review",
            chunk_index=0,
            summary_json=json.dumps(
                {
                    "summary": "RESUMO_DO_CANDIDATO",
                    "candidates": [
                        {"thesis": "Atencao liga tokens"},
                        {"thesis": "Outra tese do mesmo chunk"},
                    ],
                    "rejected_candidates": [
                        {"thesis": "Termo periferico", "reason": "so define o termo"}
                    ],
                }
            ),
        )
        db.upsert_concept(
            "c-keep",
            "@Book2024",
            chunk_id,
            candidate_json=json.dumps(
                {
                    "thesis": "Atencao liga tokens",
                    "definition": "Pesa outras posicoes da sequencia.",
                    "intuition": "Como consultar o resto da frase.",
                    "limits": "Nao cria informacao nova.",
                    "relevance_score": 4,
                    "anchor_quote": "atencao para ligar tokens distantes",
                }
            ),
            status="awaiting_review",
        )
        db.upsert_concept(
            "c-drop",
            "@Book2024",
            chunk_id,
            candidate_json=json.dumps(
                {
                    "thesis": "Outra tese do mesmo chunk",
                    "definition": "Uma segunda ideia que o revisor pode recusar.",
                    "relevance_score": 3,
                    "anchor_quote": "paragrafo longo",
                }
            ),
            status="awaiting_review",
        )
    finally:
        db.close()

    page = client.get("/review")
    assert page.status_code == 200
    assert "Como consultar o resto da frase." in page.text
    assert "Nao cria informacao nova." in page.text
    assert "so define o termo" in page.text
    assert "<mark>atencao para ligar tokens distantes</mark>" in page.text
    assert "O modelo usa " in page.text
    assert "<mark>paragrafo longo</mark>" in page.text
    assert 'name="concept_ids"' in page.text

    response = client.post(
        "/review/action",
        data={"csrf": csrf, "action": "approve", "concept_ids": ["c-keep"]},
        follow_redirects=False,
    )
    assert response.status_code == 303
    job_id = response.headers["location"].rsplit("/", 1)[-1]
    payload = wait_for_job(client, job_id)
    assert payload["job"]["state"] == "succeeded"
    assert payload["job"]["result"]["concepts_approved"] == 1

    db = client.app.state.service.db()
    try:
        assert db.get_concept("c-keep")["status"] in {"extracted", "approved", "dedupe_pending"}
        assert db.get_concept("c-drop")["status"] == "awaiting_review"
        assert db.get_chunk(chunk_id)["status"] == "awaiting_review"
        events = db.list_review_events(chunk_id)
        assert [(event["decision"], event["mode"], event["surface"]) for event in events] == [
            ("approve", "web_batch", "web")
        ]
    finally:
        db.close()


def test_duplicate_queue_keeps_and_discards_with_a_shadow_label(web_client):
    client, _ = web_client
    csrf = login(client)
    db = client.app.state.service.db()
    try:
        db.upsert_source("@Book2024", "Book2024", "Livro", ["Autor"], 2024, "h", "/x.md", "md")
        db.upsert_chapter("@Book2024::ch000", "@Book2024", "Cap", "ch")
        db.upsert_chunk(
            "@Book2024::ch000::abc",
            "@Book2024",
            "@Book2024::ch000",
            "texto do chunk",
            "ck",
            status="awaiting_review",
            chunk_index=0,
        )
        for concept_id, thesis in (
            ("dup-keep", "Tese para manter"),
            ("dup-drop", "Tese para descartar"),
        ):
            db.upsert_concept(
                concept_id,
                "@Book2024",
                "@Book2024::ch000::abc",
                candidate_json=json.dumps(
                    {"thesis": thesis, "definition": "Definicao do conceito"}
                ),
                status="extracted",
            )
            db.set_concept_dedupe(
                concept_id, "dedupe_pending", {"reason": "repete a nota", "duplicate_of": ""}
            )
            db.record_decision_shadow(
                site="dedupe",
                subject_id=concept_id,
                state_checksum=concept_id,
                model="test",
                state={"candidate": {"thesis": thesis}},
                baseline={"decision": "link"},
                jev=None,
                latency_ms=1,
                input_tokens=1,
            )
    finally:
        db.close()

    page = client.get("/review?queue=duplicates")
    assert page.status_code == 200
    assert "Tese para manter" in page.text
    assert "repete a nota" in page.text
    assert "Duplicatas (2)" in page.text
    home = client.get("/")
    assert "Duplicatas pendentes" in home.text

    refused = client.post(
        "/review/action",
        data={"csrf": "wrong", "action": "keep_duplicate", "concept_ids": ["dup-keep"]},
    )
    assert refused.status_code == 403

    response = client.post(
        "/review/action",
        data={"csrf": csrf, "action": "keep_duplicate", "concept_ids": ["dup-keep", "missing"]},
        follow_redirects=False,
    )
    assert response.status_code == 303
    payload = wait_for_job(client, response.headers["location"].rsplit("/", 1)[-1])
    assert payload["job"]["state"] == "succeeded"
    assert payload["job"]["result"]["kept"] == 1
    assert payload["job"]["result"]["skipped"] == 1

    response = client.post(
        "/review/action",
        data={"csrf": csrf, "action": "discard_duplicate", "concept_ids": ["dup-drop"]},
        follow_redirects=False,
    )
    payload = wait_for_job(client, response.headers["location"].rsplit("/", 1)[-1])
    assert payload["job"]["state"] == "succeeded"
    assert payload["job"]["result"]["discarded"] == 1

    db = client.app.state.service.db()
    try:
        kept = db.get_concept("dup-keep")
        assert kept["status"] == "approved"
        assert json.loads(kept["dedupe_json"])["override"] is True
        assert db.get_concept("dup-drop")["status"] == "duplicate"
        labels = {
            row["subject_id"]: row["human"]["verdict"] for row in db.list_decision_shadow("dedupe")
        }
        assert labels == {"dup-keep": "not_ignore", "dup-drop": "ignore"}
    finally:
        db.close()


def test_review_links_queue_accepts_a_proposed_edge(web_client):
    client, _tmp_path = web_client
    csrf = login(client)
    cfg = client.app.state.service.cfg
    db = client.app.state.service.db()
    try:
        root = cfg.vault_path
        for note_id in ("A", "B"):
            path = root / "30_Permanent" / f"ZTL - {note_id} - nota.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"> **Tese**: tese {note_id}\n", encoding="utf-8")
            db.upsert_note(
                note_id,
                "@S",
                str(path),
                title=f"Titulo {note_id}",
                body=f"> **Tese**: tese {note_id}\n\n## Definição\n\ndefinicao {note_id}\n",
                provenance_json=json.dumps(
                    {
                        "citation": f"(AUTOR, 2024, p. {note_id})",
                        "source_locator": f"p. {note_id}",
                        "anchor_quote": f"ancora literal {note_id}",
                    }
                ),
            )
        db.upsert_note_connection("A", "B", "supports", "proposta", origin="llm", status="proposed")
    finally:
        db.close()

    page = client.get("/review?queue=links")
    assert page.status_code == 200
    assert "Aceitar" in page.text
    assert "tese A" in page.text and "tese B" in page.text
    assert "definicao A" in page.text and "definicao B" in page.text
    assert "Nota de origem" in page.text and "Nota de destino" in page.text
    assert "Justificativa da relação (supports):" in page.text and "proposta" in page.text
    assert "ancora literal A" in page.text and "(AUTOR, 2024, p. A)" in page.text
    assert 'class="edge-retype"' in page.text and 'class="review-card"' in page.text
    assert "data-select-all" in page.text and "Selecionar todas" in page.text
    response = client.post(
        "/review/links",
        data={
            "csrf": csrf,
            "source_note_id": "A",
            "target_note_id": "B",
            "relation_type": "supports",
            "action": "accept",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    db = client.app.state.service.db()
    try:
        row = db.list_outgoing_connections("A")[0]
        assert row["status"] == "accepted"
        assert db.list_review_events()[-1]["surface"] == "web"
    finally:
        db.close()


def test_config_is_read_once_until_the_yaml_changes(web_client, monkeypatch):
    """Re-reading config.yaml cost 54 ms on every access (#307)."""
    import os

    import zettel.web_app as web_app_module

    client, tmp_path = web_client
    login(client)
    calls = []
    real = web_app_module.load_config
    monkeypatch.setattr(
        web_app_module, "load_config", lambda path=None: calls.append(path) or real(path)
    )
    for path in ("/runs", "/settings", "/pipeline", "/runs"):
        assert client.get(path).status_code == 200
    assert calls == []
    config = tmp_path / "config.yaml"
    stamp = config.stat().st_mtime_ns + 1_000_000_000
    os.utime(config, ns=(stamp, stamp))
    assert client.get("/runs").status_code == 200
    assert len(calls) == 1


def test_idle_worker_does_not_poll_the_database(web_client, monkeypatch):
    """An idle worker reopened state.db twice a second (#307); a submit wakes it."""
    import zettel.web_app as web_app_module

    client, _ = web_client
    csrf = login(client)
    _seed_failed_asset(client)
    time.sleep(0.6)  # let the worker settle into its wait
    opened = []
    real = web_app_module.StateDB
    monkeypatch.setattr(web_app_module, "StateDB", lambda path: opened.append(path) or real(path))
    time.sleep(1.6)
    assert opened == []
    response = client.post("/pipeline/retry_assets", data={"csrf": csrf}, follow_redirects=False)
    job_id = response.headers["location"].rsplit("/", 1)[1]
    assert wait_for_job(client, job_id)["job"]["state"] == "succeeded"


def test_job_detail_lists_every_run_and_its_cost(web_client, monkeypatch):
    """A multi-phase job showed only its last run, and none after a pause (#311)."""
    from zettel.web_app import WebWorker

    client, _ = web_client
    csrf = login(client)
    _seed_failed_asset(client)

    def fake_dispatch(cfg, db, progress, operation, payload, **_kwargs):
        for phase in ("harvest", "extract"):
            run_id = db.start_run(phase)
            db.finish_run(run_id, usage={"cost_usd_total": 0.25})
        return {}

    monkeypatch.setattr(WebWorker, "_dispatch", staticmethod(fake_dispatch))
    response = client.post("/pipeline/retry_assets", data={"csrf": csrf}, follow_redirects=False)
    job_id = response.headers["location"].rsplit("/", 1)[1]
    wait_for_job(client, job_id)
    job = client.app.state.service.job(job_id)
    assert [run["pipeline_signature"] for run in job["runs"]] == ["harvest", "extract"]
    assert job["cost_usd_total"] == pytest.approx(0.5)
    page = client.get(f"/jobs/{job_id}").text
    assert "harvest" in page and "extract" in page


def test_failed_connect_job_shows_error_and_result(web_client):
    """A Prompt 2 failure used to render succeeded with only `{notes: []}`."""
    client, _ = web_client
    login(client)
    job_id = "e" * 32
    db = client.app.state.service.db()
    try:
        assert db.create_web_job(job_id, "connect", {})
        assert db.claim_web_job(job_id)
        db.update_web_job(
            job_id,
            state="failed",
            phase="failed",
            message="1 nota(s) falharam: temperature: Extra inputs are not permitted",
            result={
                "kind": "connect",
                "notes": [],
                "rejected": 0,
                "failed": 1,
                "skipped": 0,
                "errors": ["temperature: Extra inputs are not permitted"],
            },
            error_message="1 nota(s) falharam: temperature: Extra inputs are not permitted",
            finished=True,
        )
    finally:
        db.close()
    page = client.get(f"/jobs/{job_id}").text
    assert "1 nota(s) falharam" in page
    assert "temperature" in page
    assert "Falhas" in page
    assert 'id="job-error"' in page
    assert 'id="job-result"' in page
    assert '"kind": "connect"' in page
    assert "hidden" in page.split('id="job-continue"', 1)[1][:80]


def test_run_all_form_names_the_files_it_will_ingest_without_review(web_client):
    """run_all harvests the whole inbox; the form has to say so (#310)."""
    client, tmp_path = web_client
    login(client)
    inbox = tmp_path / "inbox"
    inbox.mkdir(exist_ok=True)
    (inbox / "artigo.md").write_text("# Texto\n", encoding="utf-8")
    page = client.get("/documents").text
    assert "sem revisão bibliográfica" in page
    assert "artigo.md" in page.split("sem revisão bibliográfica", 1)[1]


def _seed_pipeline_work(client, monkeypatch):
    """One pending chunk and one connect candidate, so both estimates have work."""
    from zettel import connector

    db = client.app.state.service.db()
    try:
        db.upsert_source("@Book2024", "Book2024", "Livro", ["Autor"], 2024, "h", "/x.md", "md")
        db.upsert_chapter("@Book2024::ch000", "@Book2024", "Cap", "ch")
        db.upsert_chunk(
            "@Book2024::ch000::p1", "@Book2024", "@Book2024::ch000", "Texto", "ck", chunk_index=0
        )
    finally:
        db.close()
    monkeypatch.setattr(connector, "load_approved_candidates", lambda db: [{"candidate": None}])


def test_pipeline_page_shows_the_extract_and_connect_preflight(web_client, monkeypatch):
    """The CLI asks before paying; the web showed no estimate for its costliest phases (#310)."""
    from zettel import preflight

    client, _ = web_client
    login(client)

    def fake(phase, cost):
        return preflight.PreflightEstimate(
            phase=phase,
            provider="openai",
            model=f"modelo-{phase}",
            items=3,
            item_label="item(ns)",
            input_tokens=10,
            output_tokens=5,
            cost_usd=cost,
        )

    monkeypatch.setattr(preflight, "estimate_extract", lambda cfg, db: fake("extract", 0.1234))
    monkeypatch.setattr(
        preflight, "estimate_connect", lambda cfg, db, candidates: fake("connect", 0.5678)
    )
    _seed_pipeline_work(client, monkeypatch)
    page = client.get("/pipeline").text
    assert "0.1234" in page and "modelo-extract" in page
    assert "0.5678" in page and "modelo-connect" in page


def _seed_review_drafts(client, confidences):
    """Drafts with the given (chunk suffix, confidence, has_defect) and their concepts."""
    import json

    db = client.app.state.service.db()
    try:
        db.upsert_source("@Book2024", "Book2024", "Livro", ["Autor"], 2024, "h", "/x.md", "md")
        db.upsert_chapter("@Book2024::ch000", "@Book2024", "Cap", "ch")
        for index, (suffix, confidence, defect) in enumerate(confidences):
            candidate = {"thesis": f"Tese {suffix}", "intuition": "i", "limits": "l"}
            if defect:
                candidate.pop("limits")
            db.upsert_chunk(
                f"@Book2024::ch000::{suffix}",
                "@Book2024",
                "@Book2024::ch000",
                f"Texto {suffix}",
                f"ck{suffix}",
                status="awaiting_review",
                chunk_index=index,
                review_confidence=confidence,
                summary_json=json.dumps({"candidates": [candidate]}),
            )
            db.upsert_concept(
                f"c-{suffix}",
                "@Book2024",
                f"@Book2024::ch000::{suffix}",
                candidate_json=json.dumps(candidate),
                status="awaiting_review",
            )
    finally:
        db.close()


def test_review_drafts_follow_the_cli_queue_order(web_client):
    """Defects first, then ascending confidence, as zettel review does (#268, #309)."""
    client, _ = web_client
    login(client)
    _seed_review_drafts(
        client, [("aaa", 0.9, False), ("bbb", 0.5, False), ("ccc", 0.95, True), ("ddd", 0.7, False)]
    )
    page = client.get("/review").text
    order = sorted(("aaa", "bbb", "ccc", "ddd"), key=page.index)
    assert order == ["ccc", "bbb", "ddd", "aaa"]


def test_review_queries_do_not_grow_with_the_queue(web_client):
    """Every draft was enriched before slicing 20 in Python (#309)."""
    from zettel.state import StateDB

    client, _ = web_client
    login(client)

    def count_queries(path):
        statements = []
        real_init = StateDB.__init__

        def traced(self, db_path):
            real_init(self, db_path)
            self.conn.set_trace_callback(statements.append)

        StateDB.__init__ = traced
        try:
            assert client.get(path).status_code == 200
        finally:
            StateDB.__init__ = real_init
        return len([s for s in statements if s.lstrip().upper().startswith("SELECT")])

    _seed_review_drafts(client, [(f"a{n:02d}", 0.5, False) for n in range(25)])
    small = count_queries("/review")
    _seed_review_drafts(client, [(f"b{n:02d}", 0.5, False) for n in range(60)])
    assert count_queries("/review") == small


def _seed_proposed_edges(client, count):
    db = client.app.state.service.db()
    try:
        db.upsert_source("@A2020", "A2020", "A", ["A"], 2020, "ha", "/a.md", "md")
        db.upsert_source("@B2021", "B2021", "B", ["B"], 2021, "hb", "/b.md", "md")
        for n in range(count):
            for side, source in (("s", "@A2020"), ("t", "@A2020" if n % 2 else "@B2021")):
                db.upsert_note(
                    f"{side}{n:03d}",
                    source,
                    None,
                    title=f"Nota {side}{n:03d}",
                    body=f"## Tese\n\nTese {side}{n:03d}.\n",
                )
            db.upsert_note_connection(
                f"s{n:03d}",
                f"t{n:03d}",
                "supports" if n % 3 else "contradicts",
                origin="llm",
                status="proposed",
            )
    finally:
        db.close()


def test_link_queue_filters_by_relation_and_scope(web_client):
    client, _ = web_client
    login(client)
    _seed_proposed_edges(client, 6)
    contradicts = client.get("/review?queue=links&relation_type=contradicts").text
    assert "s000" in contradicts and "s003" in contradicts and "s001" not in contradicts
    intra = client.get("/review?queue=links&scope=intra").text
    assert "s001" in intra and "s000" not in intra


def test_link_queue_decides_a_batch_as_one_job(web_client):
    """One click per edge does not scale to what connect proposes (#309)."""
    client, _ = web_client
    csrf = login(client)
    _seed_proposed_edges(client, 3)
    response = client.post(
        "/review/links/batch",
        data={
            "csrf": csrf,
            "action": "accept",
            "edges": ["s000|t000|contradicts", "s001|t001|supports"],
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    job_id = response.headers["location"].rsplit("/", 1)[1]
    wait_for_job(client, job_id)
    job = client.app.state.service.job(job_id)
    assert job["state"] == "succeeded"
    assert job["result"]["decided"] == 2
    db = client.app.state.service.db()
    try:
        remaining = {
            (e["source_note_id"], e["target_note_id"]) for e in db.list_proposed_connections()
        }
    finally:
        db.close()
    assert remaining == {("s002", "t002")}


def test_pipeline_page_survives_an_estimate_that_cannot_be_built(web_client, monkeypatch):
    """A missing few-shot file must not take the Pipeline page down with it."""
    from zettel import preflight
    from zettel.domain_examples import DomainExamplesLoadError

    client, _ = web_client
    login(client)

    def broken(*_args, **_kwargs):
        raise DomainExamplesLoadError("Arquivo de exemplos de dominio nao encontrado")

    monkeypatch.setattr(preflight, "estimate_extract", broken)
    _seed_pipeline_work(client, monkeypatch)
    page = client.get("/pipeline")
    assert page.status_code == 200
    assert "Pré-voo indisponível" in page.text


def test_enqueue_refuses_while_the_cli_holds_the_lock(web_client):
    """The web and the CLI used to write the same stores at once (#308)."""
    from zettel.oplock import OperationLock

    client, tmp_path = web_client
    csrf = login(client)
    _seed_failed_asset(client)
    held = OperationLock(tmp_path / "zettel.lock")
    held.acquire("connect", "cli")
    try:
        response = client.post("/pipeline/retry_assets", data={"csrf": csrf})
    finally:
        held.release()
    assert response.status_code == 409
    assert "connect" in response.text and "CLI" in response.text


def test_single_edge_decision_respects_the_lock(web_client):
    from zettel.oplock import OperationLock

    client, tmp_path = web_client
    csrf = login(client)
    _seed_proposed_edges(client, 1)
    held = OperationLock(tmp_path / "zettel.lock")
    held.acquire("garden", "cli")
    try:
        response = client.post(
            "/review/links",
            data={
                "csrf": csrf,
                "source_note_id": "s000",
                "target_note_id": "t000",
                "relation_type": "contradicts",
                "action": "accept",
            },
            follow_redirects=False,
        )
    finally:
        held.release()
    assert response.status_code == 409
    db = client.app.state.service.db()
    try:
        assert len(db.list_proposed_connections()) == 1
    finally:
        db.close()


def test_worker_waits_for_the_cli_instead_of_failing(web_client):
    from zettel.oplock import OperationLock

    client, tmp_path = web_client
    login(client)
    held = OperationLock(tmp_path / "zettel.lock")
    held.acquire("connect", "cli")
    try:
        job_id = client.app.state.service.submit("retry_assets", {})
        deadline = time.monotonic() + 30
        while "connect" not in (client.app.state.service.job(job_id)["message"] or ""):
            assert time.monotonic() < deadline, "the worker never met the CLI's lock"
            time.sleep(0.05)
        assert client.app.state.service.job(job_id)["state"] == "queued"
    finally:
        held.release()
    client.app.state.service.worker._wake.set()
    assert wait_for_job(client, job_id)["job"]["state"] == "succeeded"


def test_a_failed_job_shows_a_reference_the_log_also_has(web_client, monkeypatch, caplog):
    """An unknown error reaches the browser only as a correlation id (#315)."""
    import logging

    from zettel.web_app import WebWorker

    client, _ = web_client
    csrf = login(client)
    _seed_failed_asset(client)

    def failing_dispatch(cfg, db, progress, operation, payload, **_kwargs):
        raise RuntimeError(r"database is locked: D:\projetos\zettel_app\data\state.db")

    monkeypatch.setattr(WebWorker, "_dispatch", staticmethod(failing_dispatch))
    with caplog.at_level(logging.ERROR, logger="zettel.web_app"):
        response = client.post(
            "/pipeline/retry_assets", data={"csrf": csrf}, follow_redirects=False
        )
        job_id = response.headers["location"].rsplit("/", 1)[1]
        assert wait_for_job(client, job_id)["job"]["state"] == "failed"
    job = client.app.state.service.job(job_id)
    page = client.get(f"/jobs/{job_id}").text
    assert "projetos" not in job["error_message"] and "projetos" not in page
    ref = re.search(r"ref\. ([0-9a-f]{8})", job["error_message"]).group(1)
    assert any(ref in record.getMessage() for record in caplog.records)


def test_upload_is_written_beside_and_renamed_into_the_inbox(web_client, monkeypatch):
    """A half-written file never sits under its final name (#315)."""
    import os

    client, tmp_path = web_client
    csrf = login(client)
    inbox = tmp_path / "inbox"
    seen = []
    real_link = os.link

    def watching_link(src, dst):
        seen.append((Path(src).name, Path(dst).exists()))
        real_link(src, dst)

    monkeypatch.setattr("zettel.web.documents.os.link", watching_link)
    response = client.post(
        "/documents/upload",
        data={"csrf": csrf},
        files={"file": ("obra.md", b"# Obra\n", "text/markdown")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert seen == [(".obra.md.part", False)]
    assert (inbox / "obra.md").read_bytes() == b"# Obra\n"
    assert sorted(p.name for p in inbox.iterdir()) == ["obra.md"]

    def taken(src, dst):
        raise FileExistsError(dst)

    monkeypatch.setattr("zettel.web.documents.os.link", taken)
    race = client.post(
        "/documents/upload",
        data={"csrf": csrf},
        files={"file": ("outra.md", b"x", "text/markdown")},
    )
    assert race.status_code == 409
    assert sorted(p.name for p in inbox.iterdir()) == ["obra.md"]


def test_a_bibliographic_review_survives_a_new_login(web_client):
    """The preparation (minutes of Docling) belongs to the instance, not the session (#315)."""
    client, tmp_path = web_client
    csrf = login(client)
    review_id, _ = _ready_biblio_review(client, tmp_path, csrf)
    assert client.get(f"/documents/review/{review_id}").status_code == 200

    client.post("/logout", data={"csrf": csrf})  # rotates the key: every session ends
    login(client)
    page = client.get(f"/documents/review/{review_id}")
    assert page.status_code == 200
    assert "Obra" in page.text
