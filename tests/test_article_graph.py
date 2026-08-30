"""Tests for the LangGraph article pipeline."""

from __future__ import annotations

import json
from pathlib import Path

import zettel.article as article_mod
from zettel.article_graph import run_article_graph
from zettel.config import AppConfig
from zettel.retrieval import NoteSearchResult, RetrievedNote
from zettel.state import StateDB

NOTE_A = "01KZ6QKMSE8K4MWBDQ97N40F5Y"


class FakeIndex:
    def query_similar_notes(self, *a, **k):
        return []

    def find_similar_chunks(self, *a, **k):
        return []


def test_graph_judge_reject_then_approve(tmp_path, monkeypatch):
    db = StateDB(tmp_path / "g.db")
    vault = tmp_path / "vault"
    vault.mkdir()
    db.upsert_source(
        source_id="@S1",
        citekey="S1",
        title="Livro Teste",
        authors=["Ana Silva"],
        year=2024,
        file_checksum="x",
        origin_path="x.pdf",
        origin_type="pdf",
        abnt_reference="SILVA, Ana. Livro Teste. 2024.",
        document_type="livro",
    )
    db.upsert_note(
        NOTE_A,
        source_id="@S1",
        path=f"30_Permanent/ZTL - {NOTE_A}.md",
        title="Conceito A",
        body="Definicao do conceito A.",
    )

    hits = [
        RetrievedNote(
            note_id=NOTE_A,
            score=0.9,
            title="Conceito A",
            document="Definicao do conceito A.",
            metadata={"source_id": "@S1"},
            passed_floor=True,
        )
    ]
    monkeypatch.setattr(
        "zettel.retrieval.Retriever.search_notes",
        lambda self, *a, **k: NoteSearchResult(hits=hits, candidates=hits),
    )

    enrich = json.dumps({"queries": ["conceito A"]})
    outline = json.dumps(
        {
            "title": "Artigo A",
            "thesis": "Tese.",
            "style_notes": "",
            "sections": [
                {
                    "heading": "Intro",
                    "goal": "g",
                    "note_ids": [NOTE_A],
                    "figure_asset_ids": [],
                }
            ],
        }
    )
    section = (
        "## Intro\n\n"
        "Como observa Ana Silva em *Livro Teste*, o conceito importa.\n"
        "<!-- cites: @S1 -->\n"
    )
    judge_reject = json.dumps(
        {
            "fidelity": 5,
            "coverage": 5,
            "references": 5,
            "naturalness": 5,
            "average": 5.0,
            "verdict": "REJECTED",
            "feedback": "Ampliar a introducao.",
        }
    )
    section2 = (
        "## Intro\n\n"
        "Como observa Ana Silva em *Livro Teste*, o conceito importa e foi ampliado.\n"
        "<!-- cites: @S1 -->\n"
    )
    judge_ok = json.dumps(
        {
            "fidelity": 9,
            "coverage": 9,
            "references": 9,
            "naturalness": 8,
            "average": 8.75,
            "verdict": "APPROVED",
            "feedback": "ok",
        }
    )
    # enrich, outline, draft1, judge reject, draft2, judge ok
    # personality neutral = no call
    responses = [enrich, outline, section, judge_reject, section2, judge_ok]

    monkeypatch.setattr(
        article_mod, "call_llm", lambda llm, prompt, **kwargs: responses.pop(0)
    )
    monkeypatch.setattr(
        article_mod, "get_llm", lambda *a, **k: object()
    )

    root = Path(__file__).resolve().parents[1]
    cfg = AppConfig(vault_path=vault, prompts_path=root / "prompts")
    cfg.retrieval.article.personalities_path = root / "config" / "personalities.yaml"
    cfg.retrieval.article.judge_min_score = 7.0
    cfg.retrieval.article.max_judge_iterations = 3

    result = run_article_graph(
        cfg, db, FakeIndex(), "conceito A",
        style="blog",
        personality="neutral",
        skip_context_review=True,
        skip_judge=False,
        approve_outline=lambda o: ("approve", None),
        max_judge_iterations=3,
    )
    assert "Artigo A" in result.body
    assert "ampliado" in result.body or "conceito" in result.body.lower()
    assert result.frontmatter.get("judge_verdict") == "APPROVED"
    db.close()


def test_graph_context_enrich_loop(tmp_path, monkeypatch):
    db = StateDB(tmp_path / "g2.db")
    vault = tmp_path / "vault"
    vault.mkdir()
    db.upsert_source(
        source_id="@S1",
        citekey="S1",
        title="T",
        authors=["Ana"],
        year=2024,
        file_checksum="c",
        origin_path="p",
        origin_type="pdf",
    )
    db.upsert_note(NOTE_A, "@S1", "p.md", "Nota A", body="corpo A")

    hit = RetrievedNote(
        note_id=NOTE_A, score=0.9, title="Nota A",
        document="corpo A", metadata={"source_id": "@S1"}, passed_floor=True,
    )
    monkeypatch.setattr(
        "zettel.retrieval.Retriever.search_notes",
        lambda self, *a, **k: NoteSearchResult(hits=[hit], candidates=[hit]),
    )

    calls = {"n": 0}

    def context_cb(state):
        calls["n"] += 1
        if calls["n"] == 1:
            return {
                "context_decision": "enrich",
                "extra_queries": ["segunda query"],
            }
        return {"context_decision": "approve", "extra_queries": []}

    enrich1 = json.dumps({"queries": ["primeira"]})
    # second enrich path uses extras only (no LLM) when executed_queries set
    outline = json.dumps(
        {
            "title": "T",
            "thesis": "t",
            "sections": [
                {"heading": "H", "goal": "g", "note_ids": [NOTE_A], "figure_asset_ids": []}
            ],
        }
    )
    section = "## H\n\nTexto.\n<!-- cites: -->\n"
    responses = [enrich1, outline, section]

    monkeypatch.setattr(
        article_mod, "call_llm", lambda llm, prompt, **kwargs: responses.pop(0)
    )
    monkeypatch.setattr(
        article_mod, "get_llm", lambda *a, **k: object()
    )

    root = Path(__file__).resolve().parents[1]
    cfg = AppConfig(vault_path=vault, prompts_path=root / "prompts")
    cfg.retrieval.article.personalities_path = root / "config" / "personalities.yaml"

    result = run_article_graph(
        cfg, db, FakeIndex(), "tema",
        style="blog",
        personality="neutral",
        skip_judge=True,
        context_callback=context_cb,
        approve_outline=lambda o: ("approve", None),
    )
    assert calls["n"] >= 2
    assert result.body
    db.close()


class _FakeInterrupt:
    def __init__(self, value: dict):
        self.value = value


def test_hitl_handler_receives_interrupt_payload(tmp_path, monkeypatch):
    """run_article_graph must unwrap __interrupt__[0].value before calling hitl_handler."""
    db = StateDB(tmp_path / "hitl.db")
    vault = tmp_path / "vault"
    vault.mkdir()

    received: list[dict] = []

    class FakeCompiled:
        def __init__(self):
            self._n = 0

        def invoke(self, input_or_cmd, config=None):
            self._n += 1
            if self._n == 1:
                return {
                    "__interrupt__": [
                        _FakeInterrupt(
                            {
                                "type": "context_review",
                                "notes": [{"note_id": "N1", "title": "T", "score": 0.9}],
                                "executed_queries": ["tema"],
                            }
                        )
                    ]
                }
            return {
                "final_body": "# Ok\n\nTexto.\n",
                "frontmatter": {"title": "Ok"},
                "warnings": [],
                "llm_called": False,
                "used_note_ids": [],
                "cited_source_ids": [],
                "no_evidence": False,
                "aborted": False,
            }

    class FakeBuilder:
        def compile(self, checkpointer=None):
            return FakeCompiled()

    monkeypatch.setattr(
        "zettel.article_graph.build_article_graph", lambda: FakeBuilder()
    )

    def hitl(payload: dict) -> dict:
        received.append(payload)
        return {"context_decision": "approve", "extra_queries": []}

    cfg = AppConfig(vault_path=vault, prompts_path=Path("prompts"))
    result = run_article_graph(
        cfg, db, FakeIndex(), "tema",
        style="blog",
        personality="neutral",
        skip_context_review=False,
        skip_judge=True,
        hitl_handler=hitl,
    )

    assert len(received) == 1
    assert received[0]["type"] == "context_review"
    assert received[0]["executed_queries"] == ["tema"]
    assert "Ok" in result.body
    db.close()
