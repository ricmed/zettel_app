"""The suite never leaves the machine (#324).

CI has no secrets, but it has a network: a test that downloads a public file
passes there and slows every run. These pin the three guards in
``tests/conftest.py``: no connection outside loopback, LiteLLM's bundled price
map instead of the one on GitHub, and a local embedding instead of Chroma's
ONNX model.
"""

from __future__ import annotations

import math
import socket

import pytest


def test_a_test_cannot_open_a_connection_outside_the_machine():
    from tests.conftest import NetworkBlockedInTests

    with pytest.raises(NetworkBlockedInTests, match=r"example\.com"):
        socket.create_connection(("example.com", 443), timeout=1)


def test_loopback_stays_open():
    """asyncio's self-pipe on Windows and the web TestClient need 127.0.0.1."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    try:
        client = socket.create_connection(server.getsockname(), timeout=1)
        client.close()
    finally:
        server.close()


def test_litellm_prices_come_from_its_bundled_map():
    import litellm  # noqa: F401  (the cost map loads on import)
    from litellm.litellm_core_utils.get_model_cost_map import get_model_cost_map_source_info

    assert get_model_cost_map_source_info()["source"] == "local"


def test_token_hash_embedding_is_deterministic_unit_and_fixed_size(tmp_path, token_hash_embedding):
    from zettel.index import VectorIndex

    idx = VectorIndex(tmp_path / "chroma", token_hash_embedding, "m")
    first, second = idx.embed_texts(["grafos de conhecimento", "grafos de conhecimento"])
    assert first == second
    assert len(first) == 64
    assert math.isclose(math.sqrt(sum(x * x for x in first)), 1.0, rel_tol=1e-6)
