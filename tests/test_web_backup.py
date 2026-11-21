"""Backup and restore on the web: the passphrase stays out of the job row."""

from __future__ import annotations

import io
import json
import sqlite3
import time
from pathlib import Path

import pytest
from zettel.backup.crypto import MAGIC
from zettel.state import StateDB
from zettel.web.backup import spool_upload
from zettel.web_app import UserFacingError

from tests.web_support import login, wait_for_job, web_test_client

_PHRASE = "senha-de-teste-xyz"
_RIGHT = "senha-certa-xyz"
_WRONG = "senha-errada-xyz"


def _archive(tmp_path: Path, passphrase: str) -> Path:
    from zettel.backup.archive import create_backup
    from zettel.config import env_file_path, load_config

    cfg = load_config(tmp_path / "config.yaml")
    dest = tmp_path / "fora.zettelbak"
    db = StateDB(cfg.state_db_path)
    try:
        create_backup(cfg, db, dest, passphrase, env_path=env_file_path(tmp_path / "config.yaml"))
    finally:
        db.close()
    return dest


def _user_version(path: Path) -> int:
    conn = sqlite3.connect(path)
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


def _set_user_version(path: Path, version: int) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(f"PRAGMA user_version={version}")
        conn.commit()
    finally:
        conn.close()


def _wait_row(path: Path, job_id: str) -> dict:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        db = StateDB(path)
        try:
            job = db.get_web_job(job_id)
        finally:
            db.close()
        if job and job["state"] in {"succeeded", "failed"}:
            return job
        time.sleep(0.05)
    raise AssertionError(job_id)


def test_spool_upload_stops_at_the_cap(tmp_path: Path):
    dest = tmp_path / "parcial.bin"
    with pytest.raises(UserFacingError, match="10 GB"):
        spool_upload(io.BytesIO(b"abcdefghijklmnopqrstuvwxyz"), dest, 8)
    assert not dest.exists()


def test_backup_page_requires_login_csrf_and_a_passphrase(tmp_path, monkeypatch):
    with web_test_client(tmp_path, monkeypatch) as client:
        anonymous = client.get("/backup", follow_redirects=False)
        assert anonymous.status_code == 303
        assert anonymous.headers["location"] == "/login"
        posted = client.post("/backup", data={"passphrase": _PHRASE}, follow_redirects=False)
        assert posted.status_code == 303
        assert posted.headers["location"] == "/login"

        csrf = login(client)
        page = client.get("/backup")
        assert page.status_code == 200
        assert 'type="password"' in page.text
        assert 'autocomplete="off"' in page.text
        assert 'name="include_chroma" value="1" checked' in page.text
        assert "entrar de novo" in page.text
        assert 'href="/backup"' in client.get("/").text
        assert 'href="/backup"' in client.get("/settings").text

        refused = client.post("/backup", data={"csrf": "nao", "passphrase": _PHRASE})
        assert refused.status_code == 403
        empty = client.post("/backup", data={"csrf": csrf, "passphrase": ""})
        assert empty.status_code == 400
        db = StateDB(tmp_path / "state.db")
        try:
            assert db.list_web_jobs() == []
        finally:
            db.close()


def test_a_second_backup_is_refused_while_the_queue_is_busy(tmp_path, monkeypatch):
    with web_test_client(tmp_path, monkeypatch) as client:
        csrf = login(client)
        db = StateDB(tmp_path / "state.db")
        try:
            assert db.create_web_job("ocupado", "sync", {})
            assert db.claim_web_job("ocupado")
        finally:
            db.close()
        response = client.post(
            "/backup",
            data={"csrf": csrf, "passphrase": _PHRASE, "include_chroma": "1"},
        )
        assert response.status_code == 409


def test_backup_download_hides_the_passphrase_and_is_single_use(tmp_path, monkeypatch):
    with web_test_client(tmp_path, monkeypatch) as client:
        csrf = login(client)
        response = client.post(
            "/backup",
            data={"csrf": csrf, "passphrase": _PHRASE, "include_chroma": "1"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        job_id = response.headers["location"].rsplit("/", 1)[-1]
        payload = wait_for_job(client, job_id, {"succeeded", "failed"})
        assert payload["job"]["state"] == "succeeded", payload["job"].get("error_message")
        dumped = json.dumps(payload, ensure_ascii=False)
        assert _PHRASE not in dumped
        assert payload["job"]["payload"] == {"include_chroma": True, "include_inbox": False}
        download = client.get(f"/backup/arquivo/{job_id}")
        assert download.status_code == 200
        assert download.content.startswith(MAGIC)
        assert list((tmp_path / "vault").rglob("*.zettelbak")) == []
        missing = client.get(f"/backup/arquivo/{job_id}")
        assert missing.status_code == 404


def test_restore_with_the_wrong_passphrase_does_not_replace_the_database(tmp_path, monkeypatch):
    with web_test_client(tmp_path, monkeypatch) as client:
        csrf = login(client)
        archive = _archive(tmp_path, _RIGHT)
        _set_user_version(tmp_path / "state.db", 7)
        response = client.post(
            "/backup/restaurar",
            data={"csrf": csrf, "passphrase": _WRONG, "confirmar": "restaurar"},
            files={"arquivo": ("fora.zettelbak", archive.read_bytes(), "application/octet-stream")},
            follow_redirects=False,
        )
        assert response.status_code == 303
        job_id = response.headers["location"].rsplit("/", 1)[-1]
        payload = wait_for_job(client, job_id, {"succeeded", "failed"})
        assert payload["job"]["state"] == "failed"
        assert payload["job"]["error_message"] == "senha incorreta ou arquivo danificado"
        dumped = json.dumps(payload, ensure_ascii=False)
        assert _RIGHT not in dumped
        assert _WRONG not in dumped
        assert payload["job"]["payload"] == {"force_env": False, "rotate_session": False}
        assert _user_version(tmp_path / "state.db") == 7


def test_restore_clears_the_session_and_brings_the_database_back(tmp_path, monkeypatch):
    with web_test_client(tmp_path, monkeypatch) as client:
        csrf = login(client)
        archive = _archive(tmp_path, _RIGHT)
        client.post("/logout", data={"csrf": csrf}, follow_redirects=False)
        csrf = login(client)
        _set_user_version(tmp_path / "state.db", 7)
        response = client.post(
            "/backup/restaurar",
            data={"csrf": csrf, "passphrase": _RIGHT, "confirmar": "restaurar"},
            files={"arquivo": ("fora.zettelbak", archive.read_bytes(), "application/octet-stream")},
            follow_redirects=False,
        )
        assert response.status_code == 303
        job_id = response.headers["location"].rsplit("/", 1)[-1]
        job = _wait_row(tmp_path / "state.db", job_id)
        assert job["state"] == "succeeded", job.get("error_message")
        assert _RIGHT not in json.dumps(job["payload"], ensure_ascii=False)
        assert _user_version(tmp_path / "state.db") == 0
        home = client.get("/", follow_redirects=False)
        assert home.status_code == 303
        assert home.headers["location"] == "/login"


def test_a_body_over_26_mb_is_accepted_only_on_restore(tmp_path, monkeypatch):
    from zettel.web.hardening import MAX_BODY_BYTES

    blob = b"x" * (MAX_BODY_BYTES + 1)
    with web_test_client(tmp_path, monkeypatch) as client:
        csrf = login(client)
        refused = client.post(
            "/backup",
            data={"csrf": csrf, "passphrase": _PHRASE, "pad": blob.decode("ascii")},
        )
        assert refused.status_code == 413
        accepted = client.post(
            "/backup/restaurar",
            data={"csrf": csrf, "passphrase": _PHRASE, "confirmar": "nao"},
            files={"arquivo": ("grande.zettelbak", blob, "application/octet-stream")},
        )
        assert accepted.status_code == 400
