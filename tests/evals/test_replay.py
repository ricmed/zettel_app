"""Offline replay: deterministic, network-free, and honest about its contract."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zettel.evals.replay import RESULT_SCHEMA_VERSION, main, render, replay
from zettel.evals.score import Verdict

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG = REPO_ROOT / "evals" / "configs" / "current-ask.yaml"


@pytest.fixture
def result() -> dict:
    return replay(CONFIG)


def test_the_shipped_fixture_exercises_every_verdict(result):
    counts = result["counts"]
    assert counts[Verdict.OK.value] == 2            # one hit + one no-evidence
    assert counts[Verdict.ROUTING_MISS.value] == 1
    assert counts[Verdict.FLOOR_REJECT.value] == 1
    assert counts[Verdict.ANSWER_FAIL.value] == 1


def test_replay_is_deterministic():
    """Two runs must diff to nothing — no timestamps, no run-id churn."""
    assert render(replay(CONFIG)) == render(replay(CONFIG))


def test_result_carries_its_manifest_and_identity(result):
    assert result["schema_version"] == RESULT_SCHEMA_VERSION
    assert result["runner"] == "replay"
    assert result["manifest"]["condition"] == "current_ask"
    assert len(result["manifest_identity"]) == 64


def test_committed_result_matches_the_fixture():
    """`evals/results/` is the checked-in baseline; drift should show up in a diff."""
    committed = json.loads(
        (REPO_ROOT / "evals" / "results" / "current-ask.json").read_text(encoding="utf-8")
    )
    fresh = replay(CONFIG)
    assert committed["counts"] == fresh["counts"]
    assert committed["manifest_identity"] == fresh["manifest_identity"]


def test_replay_makes_no_network_call(monkeypatch):
    import socket

    def _boom(*args, **kwargs):
        raise AssertionError("replay nao pode abrir rede")

    monkeypatch.setattr(socket.socket, "connect", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)
    replay(CONFIG)


def test_replay_never_touches_the_llm(monkeypatch):
    import zettel.llm as llm_module

    def _boom(*args, **kwargs):
        raise AssertionError("replay nao pode chamar LLM")

    monkeypatch.setattr(llm_module, "call_llm", _boom)
    monkeypatch.setattr(llm_module, "get_llm", _boom)
    replay(CONFIG)


def test_cli_writes_stable_json(tmp_path: Path):
    out = tmp_path / "run.json"
    assert main([str(CONFIG), "--out", str(out)]) == 0
    first = out.read_text(encoding="utf-8")
    main([str(CONFIG), "--out", str(out)])
    assert out.read_text(encoding="utf-8") == first


def test_ask_result_converts_into_a_trajectory():
    """The bridge to a future live runner uses only the public AskResult surface."""
    from zettel.ask import AskResult, AskSource
    from zettel.evals.replay import trajectory_from_ask_result

    used = AskSource(
        note_id="N1", title="T", wiki_link="[[T]]", rrf_score=0.5, hop=0,
        origin="busca", passed_floor=True, floor_reason="similaridade 0.9 >= piso 0.7",
    )
    rejected = AskSource(
        note_id="N2", title="U", wiki_link="[[U]]", rrf_score=0.2, hop=0,
        origin="busca", passed_floor=False, floor_reason="similaridade 0.4 abaixo do piso (0.70)",
    )
    result = AskResult(
        question="pergunta", answer="resposta",
        sources=[used], candidates=[used, rejected], llm_called=True,
    )
    traj = trajectory_from_ask_result("q1", result)
    assert traj.hit_ids == ["N1"]
    assert traj.candidate_ids == ["N1", "N2"]
    assert traj.passed_floor == {"N1": True, "N2": False}
    assert "abaixo do piso" in traj.floor_reasons["N2"]
    assert traj.llm_called is True


def test_evals_are_not_imported_by_the_production_path():
    """Nothing in harvest/extract/connect/ask may depend on the eval package."""
    import subprocess
    import sys

    probe = (
        "import sys; "
        "import zettel.harvester, zettel.extractor, zettel.connector, zettel.ask; "
        "print(any(m.startswith('zettel.evals') for m in sys.modules))"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True, text=True, cwd=REPO_ROOT, check=True,
    )
    assert out.stdout.strip() == "False"
