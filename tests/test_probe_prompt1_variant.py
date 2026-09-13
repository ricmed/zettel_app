"""Tests for the offline Prompt 1 re-run over the gold set (issue #177).

No LLM is called: the model is injected, and a fully recorded run must never reach it.
The properties pinned are the ones that would silently corrupt a before/after
comparison -- a recording reused for a different prompt or few-shot set, a regenerated
key that loses its sampling strata, and a flip counted as noise when it was a model
change.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from probe_prompt1_variant import (
    Verdict,
    apply_extract_override,
    call_id,
    collect,
    compare,
    narrative_losses,
    parse_thinking_arg,
    regenerate_key,
    verdict_from_output,
)

SETTINGS = {"provider": "openai", "model": "gpt-4o-mini", "temperature": 0.1}


def _entry(item_id, verdict, category="", stratum="contested"):
    return {
        "item_id": item_id,
        "chunk_id": f"c-{item_id}",
        "source_id": "@S",
        "llm_verdict": verdict,
        "llm_category": category,
        "sampling_stratum": stratum,
    }


# -- call identity -------------------------------------------------------


def test_call_id_changes_when_the_few_shots_change():
    """The production cache key hashes the template only; this identity must not."""
    base = call_id("sistema com exemplo A", "chunk", SETTINGS)
    assert call_id("sistema com exemplo B", "chunk", SETTINGS) != base
    assert call_id("sistema com exemplo A", "outro chunk", SETTINGS) != base
    assert call_id("sistema com exemplo A", "chunk", {**SETTINGS, "temperature": 0.0}) != base
    assert call_id("sistema com exemplo A", "chunk", dict(SETTINGS)) == base


# -- model override (#181) -----------------------------------------------


def test_override_swaps_only_the_extract_identity():
    import pytest
    from zettel.config import AppConfig, llm_phase

    cfg = AppConfig()
    before_extract = cfg.llm.extract.model_dump()
    before_connect = cfg.llm.connect.model_dump()

    run = apply_extract_override(cfg, provider="gemini", model="gemini-x", temperature=0.0)

    assert llm_phase(run, "extract").provider == "gemini"
    assert llm_phase(run, "extract").model == "gemini-x"
    assert llm_phase(run, "extract").temperature == 0.0
    assert run.llm.connect.model_dump() == before_connect  # other phases untouched
    assert run.extraction == cfg.extraction  # filters untouched
    assert cfg.llm.extract.model_dump() == before_extract  # original not mutated

    with pytest.raises(ValueError):
        apply_extract_override(cfg, thinking="banana")


def test_no_override_returns_the_production_config_itself():
    from zettel.config import AppConfig

    cfg = AppConfig()
    assert apply_extract_override(cfg) is cfg


def test_override_changes_the_recording_identity():
    """A run of another model must never reuse the production recording."""
    production = call_id("sistema", "chunk", {**SETTINGS, "model": "gpt-4o-mini"})
    other = call_id("sistema", "chunk", {**SETTINGS, "provider": "gemini", "model": "gemini-x"})
    assert production != other


def test_thinking_arg_matches_what_the_validator_accepts():
    assert parse_thinking_arg(None) is None
    assert parse_thinking_arg("false") is False
    assert parse_thinking_arg("True") is True
    assert parse_thinking_arg("2048") == 2048
    assert parse_thinking_arg("low") == "low"


# -- verdict -------------------------------------------------------------


def test_has_content_mirrors_the_extractor():
    assert Verdict("accepted", "", 2, 1).has_content
    assert not Verdict("accepted", "", 2, 0).has_content  # filter dropped everything
    assert not Verdict("rejected", "narrative", 0, 0).has_content


def test_verdict_uses_the_pipeline_filter(monkeypatch):
    from zettel import extractor

    monkeypatch.setattr(extractor, "_filter_candidates", lambda cands, cfg, text: (cands[:1], []))
    output = SimpleNamespace(chunk_status="accepted", rejection_category="", candidates=["a", "b"])
    assert verdict_from_output(output, cfg=None, chunk_text="") == Verdict("accepted", "", 2, 1)


# -- recording -----------------------------------------------------------


def test_matching_recording_is_reused_without_calling_the_model():
    def model(_item):
        raise AssertionError("gravacao valida nao pode gastar chamada")

    recorded = {"c1": {"call_id": "x", "verdict": Verdict("rejected", "narrative", 0, 0).__dict__}}
    verdicts, failures = collect(
        [{"chunk_id": "c1", "call_id": "x"}], recorded, model, lambda *_: None, lambda *_: None
    )
    assert verdicts["c1"].chunk_status == "rejected"
    assert failures == {}


def test_recording_for_a_different_call_is_not_reused():
    calls, saved = [], []
    recorded = {
        "c1": {"call_id": "prompt-antigo", "verdict": Verdict("rejected", "", 0, 0).__dict__}
    }
    verdicts, _ = collect(
        [{"chunk_id": "c1", "call_id": "prompt-novo"}],
        recorded,
        lambda item: calls.append(item["chunk_id"]) or "resposta",
        lambda item, resp: Verdict("accepted", "", 1, 1),
        lambda chunk_id, cid, verdict, resp: saved.append(cid),
    )
    assert calls == ["c1"]
    assert saved == ["prompt-novo"]
    assert verdicts["c1"].chunk_status == "accepted"


def test_parse_failure_is_reported_and_not_recorded():
    saved = []

    def bad_parse(_item, _resp):
        raise ValueError("json quebrado")

    verdicts, failures = collect(
        [{"chunk_id": "c1", "call_id": "x"}],
        {},
        lambda _item: "lixo",
        bad_parse,
        lambda *args: saved.append(args),
    )
    assert verdicts == {} and saved == []
    assert "c1" in failures


# -- regenerated key -----------------------------------------------------


def test_regenerated_key_keeps_strata_and_population_and_drops_failures():
    original = {
        "population": {"contested": 50, "accepted": 474},
        "seed": 0,
        "items": [
            _entry("G1", "rejected", "narrative", stratum="contested"),
            _entry("G2", "accepted", stratum="accepted"),
            _entry("G3", "rejected", "narrative", stratum="contested"),
        ],
    }
    verdicts = {
        "c-G1": Verdict("accepted", "", 1, 1),  # moved from rejected to accepted
        "c-G2": Verdict("accepted", "", 1, 1),
        # c-G3 failed to parse
    }
    regenerated = regenerate_key(original, verdicts, {"label": "teste"})

    assert regenerated["population"] == original["population"]
    assert regenerated["n_items"] == 2
    by_id = {e["item_id"]: e for e in regenerated["items"]}
    assert set(by_id) == {"G1", "G2"}
    assert by_id["G1"]["llm_verdict"] == "accepted"
    assert by_id["G1"]["sampling_stratum"] == "contested"  # the stratum it was DRAWN from
    assert regenerated["regenerated"]["label"] == "teste"


# -- comparison ----------------------------------------------------------


def test_flips_are_split_by_how_the_record_was_made():
    original = {
        "items": [
            _entry("G1", "rejected", "narrative"),
            _entry("G2", "accepted"),
            _entry("G3", "rejected", "structural"),
        ]
    }
    verdicts = {
        "c-G1": Verdict("accepted", "", 1, 1),  # flip, same config -> noise
        "c-G2": Verdict("accepted", "", 1, 1),  # stable
        "c-G3": Verdict("accepted", "", 1, 1),  # flip, old config -> model change
    }
    same_config = {"c-G1": True, "c-G2": True, "c-G3": False}
    result = compare(original, verdicts, same_config)

    assert result["groups"]["mesma_config"] == {"n": 2, "flips": 1}
    assert result["groups"]["config_antiga"] == {"n": 1, "flips": 1}
    assert {f["item_id"] for f in result["flips"]} == {"G1", "G3"}


def test_narrative_losses_count_only_human_keeps_still_rejected():
    key = {
        "items": [
            _entry("G1", "rejected", "narrative"),  # human keeps, still rejected -> loss
            _entry("G2", "accepted"),  # human keeps, now accepted -> recovered
            _entry("G3", "rejected", "narrative"),  # human discards -> not a loss
        ]
    }
    labels = [
        SimpleNamespace(item_id="G1", verdict="keep"),
        SimpleNamespace(item_id="G2", verdict="keep"),
        SimpleNamespace(item_id="G3", verdict="discard"),
    ]
    result = narrative_losses(key, labels, narrative_items={"G1", "G2", "G3"})
    assert result == {"human_keeps": 2, "still_rejected": 1, "items": ["G1"]}
