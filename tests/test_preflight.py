"""Pre-flight LLM cost estimate for extract / connect / article (ADR-036).

The estimators are pure: they read SQLite and config, never call an LLM and
never prompt. The confirmation lives in the CLI, so `run_*` (and therefore the
web worker and every test calling them directly) is untouched.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from zettel.config import AppConfig, LLMConfig, LLMPhaseConfig
from zettel.preflight import (
    estimate_article,
    estimate_connect,
    estimate_extract,
    estimate_tokens,
)
from zettel.schemas import PermanentNoteCandidate
from zettel.state import StateDB


@pytest.fixture
def cfg(tmp_path: Path) -> AppConfig:
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "literature_note.md").write_text("p" * 400, encoding="utf-8")
    (prompts / "permanent_note.md").write_text("p" * 800, encoding="utf-8")
    return AppConfig(
        prompts_path=prompts,
        llm=LLMConfig(
            extract=LLMPhaseConfig(provider="openai", model="gpt-4o-mini"),
            connect=LLMPhaseConfig(provider="openai", model="gpt-4o-mini"),
            article=LLMPhaseConfig(provider="openai", model="gpt-4o-mini"),
        ),
    )


@pytest.fixture
def db(tmp_path: Path):
    database = StateDB(tmp_path / "state.db")
    database.upsert_source(
        "@Autor2020",
        citekey="Autor2020",
        title="Livro",
        authors=["A"],
        year=2020,
        file_checksum="c",
        origin_path="/x.pdf",
        origin_type="pdf",
    )
    database.upsert_chapter("@Autor2020::ch000", "@Autor2020", "Cap", "chk")
    yield database
    database.close()


def _pending_chunks(db: StateDB, count: int, chars: int = 4000) -> None:
    for i in range(count):
        db.upsert_chunk(
            f"@Autor2020::ch000::c{i:04d}",
            "@Autor2020",
            "@Autor2020::ch000",
            "x" * chars,
            f"chk{i}",
        )


def _candidates(count: int) -> list[dict]:
    return [
        {
            "concept_id": f"c{i}",
            "source_id": "@Autor2020",
            "chunk_id": "chunk",
            "candidate": PermanentNoteCandidate(
                thesis="t" * 100,
                definition="d" * 400,
            ),
        }
        for i in range(count)
    ]


# ── Shared contract ────────────────────────────────────────────────────


def test_token_estimator_is_chars_over_four():
    assert estimate_tokens("x" * 400) == 100
    assert estimate_tokens("") == 0


def test_estimators_use_the_phase_model(cfg, db):
    cfg.llm.extract = LLMPhaseConfig(provider="anthropic", model="claude-haiku-4-5-20251001")
    _pending_chunks(db, 1)
    est = estimate_extract(cfg, db)
    assert (est.provider, est.model) == ("anthropic", "claude-haiku-4-5-20251001")


def test_unknown_or_local_model_costs_zero(cfg, db):
    cfg.llm.extract = LLMPhaseConfig(provider="ollama", model="qwen3:4b")
    _pending_chunks(db, 5)
    est = estimate_extract(cfg, db)
    assert est.input_tokens > 0
    assert est.cost_usd == 0.0


# ── extract ────────────────────────────────────────────────────────────


def test_extract_counts_pending_chunks_and_prompt_overhead(cfg, db):
    _pending_chunks(db, 3, chars=4000)
    est = estimate_extract(cfg, db)
    assert est.items == 3
    # 3 x (4000/4 chunk tokens + 400/4 prompt tokens)
    assert est.input_tokens == 3 * (1000 + 100)
    assert est.output_tokens == 3 * cfg.extraction.preflight_output_tokens_per_chunk


def test_extract_output_target_is_configurable(cfg, db):
    _pending_chunks(db, 2)
    cfg.extraction.preflight_output_tokens_per_chunk = 50
    assert estimate_extract(cfg, db).output_tokens == 100


def test_extract_with_nothing_pending_reports_no_work(cfg, db):
    est = estimate_extract(cfg, db)
    assert est.items == 0
    assert est.has_work is False
    assert est.cost_usd == 0.0


def test_extract_flags_what_it_does_not_count(cfg, db):
    _pending_chunks(db, 1)
    caveats = " ".join(estimate_extract(cfg, db).caveats)
    assert "cache" in caveats.lower()
    assert "imagens" in caveats.lower()


# ── connect ────────────────────────────────────────────────────────────


def test_connect_counts_candidates_and_rag_context(cfg, db):
    cfg.linking.topk = 5
    cfg.retrieval.graph_expansion.enabled = True
    cfg.retrieval.graph_expansion.max_neighbors = 10
    est = estimate_connect(cfg, db, _candidates(2))
    assert est.items == 2
    # Context is 15 notes x 250 chars / 4 = 937 tokens per candidate.
    assert "15 nota(s)" in " ".join(est.caveats)
    assert est.input_tokens > 2 * 937
    assert est.output_tokens == 2 * cfg.linking.preflight_output_tokens_per_note


def test_connect_context_shrinks_without_graph_expansion(cfg, db):
    cfg.retrieval.graph_expansion.enabled = True
    with_graph = estimate_connect(cfg, db, _candidates(1)).input_tokens
    cfg.retrieval.graph_expansion.enabled = False
    without_graph = estimate_connect(cfg, db, _candidates(1)).input_tokens
    assert without_graph < with_graph


def test_connect_with_no_candidates_reports_no_work(cfg, db):
    est = estimate_connect(cfg, db, [])
    assert est.items == 0
    assert est.input_tokens == 0


# ── article ────────────────────────────────────────────────────────────


def test_article_is_a_floor_derived_from_the_graph_shape(cfg):
    art = cfg.retrieval.article
    est = estimate_article(cfg)
    # enrich + outline + one draft per section + assemble + judge ceiling
    assert est.items == 1 + 1 + art.max_sections + 1 + art.max_judge_iterations
    assert est.input_tokens > 0
    assert est.output_tokens > 0


def test_article_says_it_is_a_floor(cfg):
    assert any("piso" in c.lower() for c in estimate_article(cfg).caveats)


def test_article_scales_with_its_knobs(cfg):
    small = estimate_article(cfg).output_tokens
    cfg.retrieval.article.max_sections *= 2
    assert estimate_article(cfg).output_tokens > small


# ── The gate never calls an LLM ────────────────────────────────────────


def test_estimating_does_not_touch_the_llm(cfg, db, monkeypatch):
    import zettel.llm as llm_module

    def _boom(*args, **kwargs):
        raise AssertionError("pre-voo nao pode chamar LLM")

    monkeypatch.setattr(llm_module, "call_llm", _boom)
    monkeypatch.setattr(llm_module, "get_llm", _boom)
    _pending_chunks(db, 2)
    estimate_extract(cfg, db)
    estimate_connect(cfg, db, _candidates(2))
    estimate_article(cfg)


def test_gate_passes_through_with_yes(cfg, db):
    from zettel.cli.deps import preflight_gate

    _pending_chunks(db, 1)
    # No exception and no prompt: --yes short-circuits before the confirm.
    preflight_gate(estimate_extract(cfg, db), yes=True)


def test_gate_aborts_without_calling_anything(cfg, db, monkeypatch):
    import typer
    from zettel.cli.deps import preflight_gate

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr(typer, "confirm", lambda *a, **k: False)
    _pending_chunks(db, 1)
    with pytest.raises(typer.Exit) as exc:
        preflight_gate(estimate_extract(cfg, db), yes=False)
    assert exc.value.exit_code == 1


def test_gate_does_not_block_a_non_tty(cfg, db, monkeypatch):
    import typer
    from zettel.cli.deps import preflight_gate

    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr(
        typer,
        "confirm",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("nao deve perguntar")),
    )
    _pending_chunks(db, 1)
    preflight_gate(estimate_extract(cfg, db), yes=False)
