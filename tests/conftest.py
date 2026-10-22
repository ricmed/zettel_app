"""Suite-wide guards: nothing a test does leaves the machine (#324)."""

import ipaddress
import os
import socket

# Before anything imports litellm: its import fetches the price map from GitHub
# unless told to use the copy bundled with the installed version.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

import pytest

from tests.fakes import OFFLINE_EMBEDDING_PROVIDER, token_hash_ef_builder


class NetworkBlockedInTests(RuntimeError):
    """A test tried to reach a host outside the machine."""


def _is_loopback(host: object) -> bool:
    if not isinstance(host, str):
        return False  # AF_UNIX paths and the like never leave the machine either
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Refuse every connection outside loopback.

    Loopback stays open: asyncio's self-pipe on Windows and the web TestClient
    use it. Blocking ``connect`` rather than socket creation is what keeps them
    working; ``create_connection`` and every HTTP client go through ``connect``.
    """
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def _check(address) -> None:
        host = address[0] if isinstance(address, tuple) else address
        if isinstance(address, tuple) and not _is_loopback(host):
            raise NetworkBlockedInTests(f"rede bloqueada nos testes: {host}")

    def connect(self, address):
        _check(address)
        return real_connect(self, address)

    def connect_ex(self, address):
        _check(address)
        return real_connect_ex(self, address)

    def getaddrinfo(host, *args, **kwargs):
        if host is not None and not _is_loopback(host):
            raise NetworkBlockedInTests(f"rede bloqueada nos testes: {host}")
        return real_getaddrinfo(host, *args, **kwargs)

    real_getaddrinfo = socket.getaddrinfo
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


@pytest.fixture(autouse=True)
def _no_decision_model_network(monkeypatch):
    """No test reaches the decision model's API (ADR-055).

    Building the SDK client is the only door to the network; it is replaced by a
    refusal the client treats as "unavailable", so a test that forgets to inject
    a fake still fails open instead of calling out. Tests that need answers patch
    ``zettel.decision.client.get_decision_client`` with a fake.
    """
    from zettel.decision import client

    def _refuse(_dcfg):
        raise client.DecisionUnavailable("rede bloqueada nos testes")

    monkeypatch.setattr(client, "_build_sdk_client", _refuse)
    client._CLIENTS.clear()
    yield
    client._CLIENTS.clear()


@pytest.fixture
def web_client(tmp_path, monkeypatch):
    """``(client, tmp_path)`` on a fresh web instance; log in with ``web_support.login``."""
    from tests.web_support import web_test_client

    with web_test_client(tmp_path, monkeypatch) as client:
        yield client, tmp_path


@pytest.fixture
def token_hash_embedding(monkeypatch) -> str:
    """Register the offline embedding and return its provider name for ``VectorIndex``."""
    from zettel import index

    monkeypatch.setitem(index._EF_BUILDERS, OFFLINE_EMBEDDING_PROVIDER, token_hash_ef_builder)
    return OFFLINE_EMBEDDING_PROVIDER
