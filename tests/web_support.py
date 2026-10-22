"""Shared helpers for the web tests: a client on a throwaway instance, login, job waits.

A helper module, not a test file (#327): four web test files used to carry their
own copy of the client fixture, the login and a polling loop.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SECRET = "web-test-secret"  # noqa: S105 -- the throwaway test instance's login
PROMPTS = Path(__file__).resolve().parents[1] / "prompts"


def write_web_config(tmp_path: Path) -> Path:
    """A YAML that keeps every path of the instance inside ``tmp_path``."""
    config = tmp_path / "config.yaml"
    config.write_text(
        "\n".join(
            [
                f"vault_path: {tmp_path / 'vault'}",
                f"inbox_path: {tmp_path / 'inbox'}",
                f"chroma_path: {tmp_path / 'chroma'}",
                f"state_db_path: {tmp_path / 'state.db'}",
                f"cache_path: {tmp_path / 'cache'}",
                f"prompts_path: {PROMPTS}",
                "images:",
                "  enabled: false",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "vault").mkdir(exist_ok=True)
    return config


@contextmanager
def web_test_client(tmp_path: Path, monkeypatch: Any, **client_kwargs: Any) -> Iterator[Any]:
    """A ``TestClient`` on a fresh instance; ``client_kwargs`` go to ``TestClient``."""
    from fastapi.testclient import TestClient
    from zettel.web import create_app

    config = write_web_config(tmp_path)
    monkeypatch.setenv("SESSION_SECRET", SECRET)
    with TestClient(create_app(config), **client_kwargs) as client:
        yield client


def login(client: Any) -> str:
    """Log in with the instance secret and return the page's CSRF token."""
    page = client.get("/login")
    token = re.search(r'name="login_csrf" value="([^"]+)"', page.text).group(1)
    response = client.post(
        "/login",
        data={"instance_secret": SECRET, "login_csrf": token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    match = re.search(r'name="csrf" value="([^"]+)"', client.get("/").text)
    assert match
    return match.group(1)


def wait_for_job(
    client: Any, job_id: str, states: set[str] | None = None, timeout: float = 30.0
) -> dict:
    """The job's API payload once it reaches ``states`` (default: left queued/running).

    Waits on the condition with a generous deadline instead of a fixed number of
    short sleeps, so a loaded CI machine slows the test down instead of failing it.
    """
    deadline = time.monotonic() + timeout
    while True:
        payload = client.get(f"/api/jobs/{job_id}").json()
        state = payload["job"]["state"]
        done = state in states if states else state not in {"queued", "running"}
        if done:
            return payload
        assert time.monotonic() < deadline, f"job {job_id} still {state}"
        time.sleep(0.05)
