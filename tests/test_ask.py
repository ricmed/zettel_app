"""Tests for the `ask` command (QA over the vault)."""

import pytest

import zettel.ask as ask_mod
from zettel.ask import AskResult, AskSource, build_ask_note_body, run_ask, save_ask_note
from zettel.config import AppConfig
from zettel.retrieval import NoteSearchResult, RetrievedNote
from zettel.state import StateDB


@pytest.fixture
def db(tmp_path):
    db = StateDB(tmp_path / "ask.db")
    yield db
    db.close()


class FakeIndex:
    def query_similar_notes(self, *a, **k):
        return []

    def find_similar_chunks(self, *a, **k):
        return []


def test_run_ask_empty_vault_no_llm(db, monkeypatch):
    """No retrieval hits -> deterministic 'no evidence' answer, no LLM call."""
    called = {"n": 0}

    def _boom(*a, **k):
        called["n"] += 1
        raise AssertionError("LLM nao deveria ser chamado")

    monkeypatch.setattr(ask_mod, "call_llm", _boom)
    monkeypatch.setattr(ask_mod, "get_llm", lambda cfg: object())

    result = run_ask(AppConfig(), db, FakeIndex(), "o que e um grafo?")
    assert result.answer == ask_mod._NO_EVIDENCE
    assert result.llm_called is False
    assert called["n"] == 0


def test_run_ask_passes_wikilinks_to_prompt(db, tmp_path, monkeypatch):
    """The context handed to the LLM must contain the exact citation wikilinks."""
    db.upsert_note(
        "01HZZZ", "@S",
        "/vault/30_Permanent/ZTL - 01HZZZ - grafos-de-conhecimento.md",
        "Grafos de Conhecimento",
        body="Um grafo conecta conceitos por arestas tipadas.",
    )

    captured = {}

    def _fake_search(self, query, topk=None, mode=None, expand_graph=None):
        hit = RetrievedNote(
            note_id="01HZZZ", score=0.9, title="Grafos de Conhecimento",
            document="Um grafo conecta conceitos por arestas tipadas.", hop=0,
        )
        return NoteSearchResult(hits=[hit], candidates=[hit])

    def _fake_llm_call(llm, prompt="", **kwargs):
        # Prefer explicit user=; legacy positional `prompt` still works.
        captured["prompt"] = kwargs.get("user") or prompt
        captured["system"] = kwargs.get("system")
        return "Resposta baseada em [[ZTL - 01HZZZ - grafos-de-conhecimento]]."

    monkeypatch.setattr("zettel.retrieval.Retriever.search_notes", _fake_search)
    monkeypatch.setattr(ask_mod, "call_llm", _fake_llm_call)
    monkeypatch.setattr(ask_mod, "get_llm", lambda cfg: object())

    cfg = AppConfig()
    cfg.prompts_path = tmp_path  # write a minimal prompt template
    (tmp_path / "ask.md").write_text(
        "Lang {language}\nPergunta {question}\nContexto:\n{context_notes}\n",
        encoding="utf-8",
    )

    result = run_ask(cfg, db, FakeIndex(), "o que e um grafo?")
    assert result.llm_called is True
    # The exact wikilink for the note must be in the context sent to the LLM.
    assert "[[ZTL - 01HZZZ - grafos-de-conhecimento]]" in captured["prompt"]
    assert len(result.sources) == 1
    assert result.sources[0].origin == "busca"


def test_run_ask_below_floor_shows_candidates_but_no_llm_call(db, monkeypatch):
    """Everything below the relevance floor: no LLM call, but candidates surfaced."""
    called = {"n": 0}

    def _boom(*a, **k):
        called["n"] += 1
        raise AssertionError("LLM nao deveria ser chamado")

    def _fake_search(self, query, topk=None, mode=None, expand_graph=None):
        rejected = RetrievedNote(
            note_id="n1", score=0.03, title="Nota Irrelevante",
            document="assunto sem relacao", hop=0, passed_floor=False,
        )
        return NoteSearchResult(hits=[], candidates=[rejected])

    monkeypatch.setattr("zettel.retrieval.Retriever.search_notes", _fake_search)
    monkeypatch.setattr(ask_mod, "call_llm", _boom)
    monkeypatch.setattr(ask_mod, "get_llm", lambda cfg: object())

    result = run_ask(AppConfig(), db, FakeIndex(), "pergunta fora do tema")
    assert result.answer == ask_mod._NO_EVIDENCE
    assert result.llm_called is False
    assert called["n"] == 0
    assert result.sources == []
    # Raw candidates still surfaced for transparency, marked as not used.
    assert len(result.candidates) == 1
    assert result.candidates[0].note_id == "n1"
    assert result.candidates[0].passed_floor is False


def test_build_ask_note_body_provenance():
    result = AskResult(
        question="o que e RAG?",
        answer="RAG combina recuperacao e geracao [[ZTL - 01ABC - rag]].",
        sources=[
            AskSource(
                note_id="01ABC", title="RAG",
                wiki_link="[[ZTL - 01ABC - rag]]", rrf_score=0.42, hop=0,
                origin="busca", source_id="@Paper2024", vector_similarity=0.84,
            ),
            AskSource(
                note_id="01DEF", title="Embeddings",
                wiki_link="[[ZTL - 01DEF - embeddings]]", rrf_score=0.1, hop=1,
                origin="conexao depends_on a partir de [[ZTL - 01ABC]]",
                source_id="@Paper2024",
            ),
        ],
        mode="hybrid", graph_expansion=True, llm_model="gpt-4o-mini",
    )
    meta, body = build_ask_note_body(result)
    assert meta["type"] == "ask_answer"
    assert meta["question"] == "o que e RAG?"
    assert meta["retrieval_mode"] == "hybrid"
    # Body carries the answer and a provenance section for every source.
    assert "## Fontes consultadas" in body
    assert "[[ZTL - 01ABC - rag]]" in body
    assert "[[ZTL - 01DEF - embeddings]]" in body
    assert "conexao depends_on" in body
    assert "@Paper2024" in body
    # Vector similarity is included when available, and omitted otherwise.
    assert "similaridade: 0.84" in body


def test_save_ask_note_default_location(tmp_path):
    result = AskResult(question="pergunta teste", answer="resposta", sources=[])
    path = save_ask_note(result, tmp_path)
    assert path.exists()
    assert path.parent.name == "00_Inbox"
    assert path.name.startswith("ASK - ")
    content = path.read_text(encoding="utf-8")
    assert "type: ask_answer" in content
    assert "# Pergunta" in content
