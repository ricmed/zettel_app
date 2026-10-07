"""Suite-wide guards."""

import pytest


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
