"""Tests for extractor candidate filtering."""

from zettel.config import AppConfig, ExtractionConfig
from zettel.extractor import _filter_candidates
from zettel.schemas import PermanentNoteCandidate


def _make_candidate(**overrides) -> PermanentNoteCandidate:
    """Helper to build a valid candidate with sensible defaults."""
    defaults = {
        "thesis": "Gradient descent converge mais rapido com learning rate adaptativo",
        "definition": (
            "O algoritmo de gradient descent ajusta os pesos do modelo iterativamente "
            "na direcao oposta ao gradiente da funcao de perda, e taxas adaptativas "
            "como Adam aceleram a convergencia em superficies nao convexas"
        ),
        "intuition": "Como descer uma montanha ajustando o tamanho do passo",
        "limits": "Pode divergir com learning rates muito altos",
        "anchor_quote": "adaptive learning rates converge faster in practice than fixed schedules",
        "source_locator": "p.42",
        "tags": ["otimizacao", "deep-learning"],
        "relevance_score": 4,
    }
    defaults.update(overrides)
    return PermanentNoteCandidate(**defaults)


def _make_config(**overrides) -> AppConfig:
    """Helper to build an AppConfig with custom extraction settings."""
    ext_kwargs = {
        "min_relevance_score": 3,
        "min_thesis_words": 5,
        "require_anchor_quote": True,
        "min_definition_words": 10,
    }
    ext_kwargs.update(overrides)
    return AppConfig(extraction=ExtractionConfig(**ext_kwargs))


def test_filter_candidates_by_relevance():
    """Candidates below relevance threshold are rejected."""
    cfg = _make_config(min_relevance_score=3)
    candidates = [
        _make_candidate(relevance_score=2),
        _make_candidate(relevance_score=3),
    ]
    approved, rejected = _filter_candidates(candidates, cfg)
    assert len(approved) == 1
    assert len(rejected) == 1
    cand, reason = rejected[0]
    assert cand.relevance_score == 2
    assert "relevance_score" in reason


def test_filter_candidates_by_thesis_length():
    """Candidates with thesis shorter than min_thesis_words are rejected."""
    cfg = _make_config(min_thesis_words=5)
    candidates = [
        _make_candidate(thesis="Curta demais"),  # 2 words
        _make_candidate(thesis="Esta tese tem palavras suficientes para passar"),
    ]
    approved, rejected = _filter_candidates(candidates, cfg)
    assert len(approved) == 1
    assert len(rejected) == 1


def test_filter_candidates_by_definition_length():
    """Candidates with definition shorter than min_definition_words are rejected."""
    cfg = _make_config(min_definition_words=10)
    candidates = [
        _make_candidate(definition="Muito curta"),  # 2 words
        _make_candidate(),  # default definition has >10 words
    ]
    approved, rejected = _filter_candidates(candidates, cfg)
    assert len(approved) == 1
    assert len(rejected) == 1


def test_filter_candidates_require_anchor():
    """Candidates without anchor_quote are rejected when required."""
    cfg = _make_config(require_anchor_quote=True)
    candidates = [
        _make_candidate(anchor_quote=""),
        _make_candidate(anchor_quote="   "),  # whitespace only
        _make_candidate(),  # default has anchor_quote
    ]
    approved, rejected = _filter_candidates(candidates, cfg)
    assert len(approved) == 1
    assert len(rejected) == 2


def test_filter_candidates_anchor_not_required():
    """Candidates without anchor_quote pass when not required."""
    cfg = _make_config(require_anchor_quote=False)
    candidates = [
        _make_candidate(anchor_quote=""),
    ]
    approved, rejected = _filter_candidates(candidates, cfg)
    assert len(approved) == 1
    assert len(rejected) == 0


def test_filter_candidates_all_pass():
    """Valid candidates pass all checks."""
    cfg = _make_config()
    candidates = [
        _make_candidate(relevance_score=4),
        _make_candidate(relevance_score=5),
    ]
    approved, rejected = _filter_candidates(candidates, cfg)
    assert len(approved) == 2
    assert len(rejected) == 0


def test_write_literature_draft_records_llm_model(tmp_path):
    """Draft frontmatter gets llm_model from the caller, not a missing spec."""
    from zettel.extractor import _write_literature_draft
    from zettel.schemas import LiteratureChunkOutput
    from zettel.state import StateDB
    from zettel.vault import parse_frontmatter

    cfg = AppConfig(
        vault_path=tmp_path / "vault",
        cache_path=tmp_path / "cache",
        state_db_path=tmp_path / "state.db",
        chroma_path=tmp_path / "chroma",
        prompts_path=tmp_path / "prompts",
    )
    (cfg.vault_path / "00_Inbox" / "Review").mkdir(parents=True)
    db = StateDB(cfg.state_db_path)
    db.upsert_source(
        "@Book2024", "Book2024", "Livro", ["Autor"], 2024,
        "h", "/x.pdf", "pdf",
    )
    db.upsert_chapter("@Book2024::ch000", "@Book2024", "Ch1", "chh")
    db.upsert_chunk(
        "@Book2024::ch000::abc", "@Book2024", "@Book2024::ch000",
        "texto do chunk com conteudo suficiente", "ck",
        chunk_index=0, page_in_file=1, page_in_book=145,
        status="pending",
    )
    chunk_row = db.get_chunk("@Book2024::ch000::abc")
    output = LiteratureChunkOutput(
        chunk_status="accepted",
        rejection_reason="",
        rejection_category="",
        summary="Resumo do trecho",
        key_concepts=["conceito"],
        candidates=[],
    )
    path = _write_literature_draft(
        cfg, db, chunk_row, output, "01HTESTLITID00000000000000",
        0.8, 12, candidates=[], llm_model="gpt-4o-mini",
    )
    assert path is not None and path.is_file()
    meta, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
    assert meta["llm_model"] == "gpt-4o-mini"
    db.close()


def test_process_chunk_payload_carries_section_language_and_domain(tmp_path, monkeypatch):
    """Prompt 1 gets the section path, the formatted locator, and config identity.

    `section_path` used to be sent as `chapter_title` filled with `locator`, so the
    model saw the citation string twice and never the heading trail.
    """
    import json
    from pathlib import Path

    from zettel.extractor import _process_chunk
    from zettel.llm import load_prompt_parts
    from zettel.state import StateDB

    cfg = AppConfig(
        vault_path=tmp_path / "vault",
        cache_path=tmp_path / "cache",
        state_db_path=tmp_path / "state.db",
        chroma_path=tmp_path / "chroma",
        prompts_path=Path(__file__).resolve().parents[1] / "prompts",
    )
    cfg.language = "pt-BR"
    cfg.gardener.domain = "Ciencia de Dados"
    (cfg.vault_path / "00_Inbox" / "Review").mkdir(parents=True)

    db = StateDB(cfg.state_db_path)
    db.upsert_source("@Book2024", "Book2024", "Livro", ["Autor"], 2024, "h", "/x.pdf", "pdf")
    db.upsert_chapter("@Book2024::ch000", "@Book2024", "Ch1", "chh")
    db.upsert_chunk(
        "@Book2024::ch000::abc", "@Book2024", "@Book2024::ch000",
        "texto do chunk com conteudo suficiente", "ck",
        locator="Ch1", section_path="3 Retrieval > 3.2 Reranking",
        chunk_index=0, page_in_file=12, page_in_book=145, status="pending",
    )
    chunk_row = db.get_chunk("@Book2024::ch000::abc")

    captured: dict[str, str] = {}

    def fake_call_llm(llm, user, system=None, **kwargs):
        captured["user"] = user
        captured["system"] = system or ""
        return json.dumps({
            "chunk_status": "rejected",
            "rejection_reason": "sem conceito",
            "rejection_category": "narrative",
            "summary": "Trecho de transicao.",
            "key_concepts": [],
            "candidates": [],
        })

    monkeypatch.setattr("zettel.extractor.call_llm", fake_call_llm)

    prompt_parts = load_prompt_parts(cfg.prompts_path / "literature_note.md")
    _process_chunk(cfg, db, None, object(), chunk_row, prompt_parts, "prompthash")
    db.close()

    assert "3 Retrieval > 3.2 Reranking" in captured["user"]
    assert "p.145" in captured["user"]
    assert "pt-BR" in captured["system"]
    assert "Ciencia de Dados" in captured["system"]
    # No orphan placeholder survived fill_template on either half.
    for half in captured.values():
        assert "{language}" not in half and "{domain}" not in half
        assert "{section_path}" not in half and "{chunk_text}" not in half


def _process_chunk_test_setup(tmp_path):
    from pathlib import Path

    from zettel.state import StateDB

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
    db.upsert_chunk(
        "@Book2024::ch000::abc", "@Book2024", "@Book2024::ch000",
        "texto do chunk com conteudo suficiente", "ck",
        chunk_index=0, status="pending",
    )
    return cfg, db, db.get_chunk("@Book2024::ch000::abc")


def test_process_chunk_persists_rejection_taxonomy(tmp_path, monkeypatch):
    """Chunk-level rejection_category/rejection_reason survive into summary_json."""
    import json

    from zettel.extractor import _process_chunk
    from zettel.llm import load_prompt_parts

    cfg, db, chunk_row = _process_chunk_test_setup(tmp_path)

    def fake_call_llm(llm, user, system=None, **kwargs):
        return json.dumps({
            "chunk_status": "rejected",
            "rejection_reason": "trecho e so uma referencia bibliografica",
            "rejection_category": "structural",
            "summary": "Trecho estrutural.",
            "key_concepts": [],
            "candidates": [],
        })

    monkeypatch.setattr("zettel.extractor.call_llm", fake_call_llm)
    prompt_parts = load_prompt_parts(cfg.prompts_path / "literature_note.md")
    _process_chunk(cfg, db, None, object(), chunk_row, prompt_parts, "prompthash")

    persisted = json.loads(db.get_chunk("@Book2024::ch000::abc")["summary_json"])
    assert persisted["rejection_category"] == "structural"
    assert persisted["rejection_reason"] == "trecho e so uma referencia bibliografica"
    assert persisted["rejected_candidates"] == []
    db.close()


def test_process_chunk_normalizes_unknown_rejection_category(tmp_path, monkeypatch):
    """An out-of-vocabulary category from the LLM doesn't crash the parse."""
    import json

    from zettel.extractor import _process_chunk
    from zettel.llm import load_prompt_parts

    cfg, db, chunk_row = _process_chunk_test_setup(tmp_path)

    def fake_call_llm(llm, user, system=None, **kwargs):
        return json.dumps({
            "chunk_status": "rejected",
            "rejection_reason": "motivo qualquer",
            "rejection_category": "categoria-inventada-pelo-llm",
            "summary": "Trecho.",
            "key_concepts": [],
            "candidates": [],
        })

    monkeypatch.setattr("zettel.extractor.call_llm", fake_call_llm)
    prompt_parts = load_prompt_parts(cfg.prompts_path / "literature_note.md")
    _process_chunk(cfg, db, None, object(), chunk_row, prompt_parts, "prompthash")

    persisted = json.loads(db.get_chunk("@Book2024::ch000::abc")["summary_json"])
    assert persisted["rejection_category"] == ""
    db.close()


def test_process_chunk_persists_rejected_candidates_with_reason(tmp_path, monkeypatch):
    """A candidate dropped by the deterministic filter is recorded with its reason."""
    import json

    from zettel.extractor import _process_chunk
    from zettel.llm import load_prompt_parts

    cfg, db, chunk_row = _process_chunk_test_setup(tmp_path)

    low_relevance_candidate = {
        "thesis": "Uma tese qualquer com palavras suficientes para passar no filtro de tamanho",
        "definition": (
            "Uma definicao qualquer com bastante texto explicativo sobre o tema tratado aqui"
        ),
        "anchor_quote": "",
        "relevance_score": 1,
    }

    def fake_call_llm(llm, user, system=None, **kwargs):
        return json.dumps({
            "chunk_status": "accepted",
            "rejection_reason": "",
            "rejection_category": "",
            "summary": "Resumo com conteudo suficiente para pontuar bem no calculo de confianca.",
            "key_concepts": ["conceito"],
            "candidates": [low_relevance_candidate],
        })

    monkeypatch.setattr("zettel.extractor.call_llm", fake_call_llm)
    prompt_parts = load_prompt_parts(cfg.prompts_path / "literature_note.md")
    _process_chunk(cfg, db, None, object(), chunk_row, prompt_parts, "prompthash")

    persisted = json.loads(db.get_chunk("@Book2024::ch000::abc")["summary_json"])
    assert persisted["candidates"] == []
    assert len(persisted["rejected_candidates"]) == 1
    rejected = persisted["rejected_candidates"][0]
    assert rejected["thesis"] == low_relevance_candidate["thesis"]
    assert "relevance_score" in rejected["reason"]
    db.close()
