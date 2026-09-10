"""Fail-fast when the configured LLM is unreachable (ADR-045)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError
from zettel.config import AppConfig, HarvestConfig, LLMPhaseConfig
from zettel.llm import (
    LLMUnavailableError,
    call_llm,
    get_llm,
    is_llm_unavailable,
)
from zettel.retrieval import NoteSearchResult
from zettel.schemas import PermanentNoteCandidate
from zettel.state import StateDB

from tests.test_harvester_dedup import FakeVectorIndex


def test_is_llm_unavailable_connection_and_http():
    assert is_llm_unavailable(ConnectionError("connection refused"))
    assert is_llm_unavailable(TimeoutError("timed out"))
    assert is_llm_unavailable(RuntimeError("Error code: 503 - overloaded"))
    assert is_llm_unavailable(ImportError("No module named 'langchain_ollama'"))
    assert is_llm_unavailable(LLMUnavailableError("timeout"))


def test_is_llm_unavailable_ignores_parse_and_schema():
    class _M(BaseModel):
        x: int

    with pytest.raises(ValidationError) as exc:
        _M(x="no")
    assert not is_llm_unavailable(exc.value)
    assert not is_llm_unavailable(ValueError("Nenhum JSON encontrado na resposta do LLM"))


def test_call_llm_wraps_connection_error():
    class _Down:
        model = "gpt-4o-mini"

        def invoke(self, _messages, **_kwargs):
            raise ConnectionError("connection refused")

    with pytest.raises(LLMUnavailableError, match="connection refused") as exc:
        call_llm(_Down(), "hello")
    assert "não foram marcados failed" in str(exc.value)


def test_get_llm_import_error_is_unavailable(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def _import(name, *args, **kwargs):
        if name == "langchain_anthropic":
            raise ImportError("No module named 'langchain_anthropic'", name="langchain_anthropic")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _import)
    cfg = AppConfig()
    cfg.llm.extract = LLMPhaseConfig(provider="anthropic", model="claude-x")
    with pytest.raises(LLMUnavailableError, match="langchain_anthropic"):
        get_llm(cfg, "extract")


def test_get_llm_ollama_retries_then_raises(monkeypatch):
    langchain_ollama = pytest.importorskip("langchain_ollama")
    calls = {"n": 0}

    class FakeChat:
        model = "qwen"

        def __init__(self, **kwargs):
            assert "max_retries" not in kwargs

        def invoke(self, _messages, **_kwargs):
            calls["n"] += 1
            raise ConnectionError("connection refused")

    monkeypatch.setattr(langchain_ollama, "ChatOllama", FakeChat)
    cfg = AppConfig()
    cfg.llm.max_retries = 2
    cfg.llm.extract = LLMPhaseConfig(
        provider="ollama",
        model="qwen",
        base_url="http://localhost:11434",
    )
    llm = get_llm(cfg, "extract")
    with pytest.raises(LLMUnavailableError):
        call_llm(llm, "hello")
    assert calls["n"] == 3


def _two_pending_chunks(tmp_path: Path) -> tuple[AppConfig, StateDB]:
    cfg = AppConfig(
        vault_path=tmp_path / "vault",
        cache_path=tmp_path / "cache",
        state_db_path=tmp_path / "state.db",
        chroma_path=tmp_path / "chroma",
        prompts_path=Path(__file__).resolve().parents[1] / "prompts",
    )
    (cfg.vault_path / "00_Inbox" / "Review").mkdir(parents=True)
    db = StateDB(cfg.state_db_path)
    db.upsert_source("@Book2024", "Book2024", "Livro", ["Autor"], 2024, "h", "/x.pdf", "pdf")
    db.upsert_chapter("@Book2024::ch000", "@Book2024", "Ch1", "chh")
    for i, cid in enumerate(("aaa", "bbb")):
        db.upsert_chunk(
            f"@Book2024::ch000::{cid}",
            "@Book2024",
            "@Book2024::ch000",
            f"texto do chunk {cid} com conteudo suficiente",
            f"ck{cid}",
            locator="Ch1",
            chunk_index=i,
            status="pending",
        )
    return cfg, db


def test_extract_aborts_without_marking_failed(tmp_path, monkeypatch):
    from zettel.extractor import run_extract

    cfg, db = _two_pending_chunks(tmp_path)
    monkeypatch.setattr("zettel.extractor.get_llm", lambda *a, **k: object())

    def _boom(*_a, **_k):
        raise LLMUnavailableError("connection refused")

    monkeypatch.setattr("zettel.extractor.call_llm", _boom)
    try:
        with pytest.raises(LLMUnavailableError):
            run_extract(cfg, db, FakeVectorIndex())
        assert db.get_chunk("@Book2024::ch000::aaa")["status"] == "pending"
        assert db.get_chunk("@Book2024::ch000::bbb")["status"] == "pending"
        assert db.get_last_run()["status"] == "failed"
    finally:
        db.close()


def _approved_concepts(tmp_path: Path) -> tuple[AppConfig, StateDB, list[dict]]:
    cfg = AppConfig(
        vault_path=tmp_path / "vault",
        cache_path=tmp_path / "cache",
        state_db_path=tmp_path / "state.db",
        chroma_path=tmp_path / "chroma",
        prompts_path=Path(__file__).resolve().parents[1] / "prompts",
    )
    (cfg.vault_path / "30_Permanent").mkdir(parents=True)
    db = StateDB(cfg.state_db_path)
    db.upsert_source("@Book2024", "Book2024", "Livro", ["Autor"], 2024, "h", "/x.pdf", "pdf")
    db.upsert_chapter("@Book2024::ch000", "@Book2024", "Ch1", "chh")
    db.upsert_chunk(
        "@Book2024::ch000::aaa",
        "@Book2024",
        "@Book2024::ch000",
        "texto",
        "ck",
        status="persisted",
    )
    candidates = []
    for i, cid in enumerate(("c1", "c2")):
        cand = PermanentNoteCandidate(
            thesis=f"Tese declarativa numero {i} sobre um conceito atomico",
            definition="Definicao autonoma com palavras suficientes para o schema.",
        )
        concept_id = f"@Book2024::concept::{cid}"
        db.upsert_concept(
            concept_id,
            "@Book2024",
            "@Book2024::ch000::aaa",
            candidate_json=cand.model_dump_json(),
            status="approved",
        )
        candidates.append(
            {
                "concept_id": concept_id,
                "source_id": "@Book2024",
                "chunk_id": "@Book2024::ch000::aaa",
                "candidate": cand,
            }
        )
    return cfg, db, candidates


def test_connect_aborts_leaving_concepts_approved(tmp_path, monkeypatch):
    from zettel.connector import run_connect

    cfg, db, candidates = _approved_concepts(tmp_path)
    monkeypatch.setattr("zettel.connector.get_llm", lambda *a, **k: object())
    monkeypatch.setattr(
        "zettel.connector._load_connect_taxonomy",
        lambda *a, **k: ({}, {}),
    )
    monkeypatch.setattr(
        "zettel.retrieval.Retriever.search_notes",
        lambda *a, **k: NoteSearchResult(hits=[], candidates=[]),
    )

    def _boom(*_a, **_k):
        raise LLMUnavailableError("connection refused")

    monkeypatch.setattr("zettel.connector.call_llm", _boom)
    try:
        with pytest.raises(LLMUnavailableError):
            run_connect(cfg, db, FakeVectorIndex(), candidates)
        assert db.get_concept("@Book2024::concept::c1")["status"] == "approved"
        assert db.get_concept("@Book2024::concept::c1")["note_id"] is None
        assert db.get_concept("@Book2024::concept::c2")["status"] == "approved"
        assert db.get_last_run()["status"] == "failed"
        assert list((cfg.vault_path / "30_Permanent").glob("*.md")) == []
    finally:
        db.close()


def test_harvest_aborts_without_writing_src(tmp_path, monkeypatch):
    from zettel.harvester import run_harvest

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    body = (
        "---\ntitle: Doc\nauthors: [Autor]\nyear: 2020\n---\n\n"
        "# Titulo\n\nUm paragrafo com conteudo suficiente para virar chunk.\n"
    )
    (inbox / "primeiro.md").write_text(body, encoding="utf-8")
    (inbox / "segundo.md").write_text(body.replace("Doc", "Outro"), encoding="utf-8")

    cfg = AppConfig(
        vault_path=tmp_path / "vault",
        inbox_path=inbox,
        harvest=HarvestConfig(biblio_llm_enabled=True),
    )
    (cfg.vault_path / "10_Sources").mkdir(parents=True)
    (cfg.vault_path / "20_Literature").mkdir(parents=True)

    def _boom(*_a, **_k):
        raise LLMUnavailableError("connection refused")

    monkeypatch.setattr("zettel.bibliography.enrich_with_llm", _boom)
    db = StateDB(tmp_path / "state.db")
    try:
        with pytest.raises(LLMUnavailableError):
            run_harvest(
                cfg,
                db,
                FakeVectorIndex(),
                interactive=False,
                duplicate_action="skip",
                skip_paging=True,
            )
        assert db.get_stats()["sources"] == 0
        assert list((cfg.vault_path / "10_Sources").glob("*.md")) == []
        assert db.get_last_run()["status"] == "failed"
    finally:
        db.close()
