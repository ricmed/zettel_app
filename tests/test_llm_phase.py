"""Per-phase LLM identity: llm_phase + get_llm factory."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from zettel.config import (
    AppConfig,
    LLMConfig,
    LLMPhaseConfig,
    effective_temperature,
    llm_phase,
)
from zettel.llm import get_llm, is_supported_llm_provider


def test_llm_phase_rejects_unknown():
    with pytest.raises(ValueError, match="desconhecida"):
        llm_phase(AppConfig(), "nope")


def test_llm_phase_returns_named_spec():
    cfg = AppConfig()
    cfg.llm.extract = LLMPhaseConfig(
        provider="ollama",
        model="qwen3.5:4b",
        base_url="http://localhost:11434",
    )
    spec = llm_phase(cfg, "extract")
    assert spec.provider == "ollama"
    assert spec.model == "qwen3.5:4b"
    assert spec.base_url == "http://localhost:11434"
    assert llm_phase(cfg, "connect").model == "gpt-4o-mini"


def test_llm_config_rejects_global_identity():
    with pytest.raises(ValidationError):
        LLMConfig(provider="openai", model="gpt-4o-mini")


def test_is_supported_llm_provider():
    assert is_supported_llm_provider("OpenAI")
    assert is_supported_llm_provider("openrouter")
    assert is_supported_llm_provider("ollama")
    assert not is_supported_llm_provider("acme")


def test_get_llm_uses_phase_identity(monkeypatch):
    langchain_openai = pytest.importorskip("langchain_openai")
    captured: dict = {}

    class FakeChat:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(langchain_openai, "ChatOpenAI", FakeChat)

    cfg = AppConfig()
    cfg.llm.connect = LLMPhaseConfig(
        provider="openai",
        model="gpt-4o",
        base_url="https://example/v1",
    )
    get_llm(cfg, "connect", temperature=0.3, max_retries=0)
    assert captured["model"] == "gpt-4o"
    assert captured["base_url"] == "https://example/v1"
    assert captured["temperature"] == 0.3
    assert captured["max_retries"] == 0


def test_get_llm_rejects_unsupported_provider():
    cfg = AppConfig()
    cfg.llm.ask = LLMPhaseConfig(provider="acme", model="x")
    with pytest.raises(ValueError, match="não suportado"):
        get_llm(cfg, "ask")


# ── #60: temperature per phase ────────────────────────────────────────


def test_effective_temperature_inherits_global_when_phase_unset():
    cfg = AppConfig()
    cfg.llm.temperature = 0.25
    spec = llm_phase(cfg, "connect")  # default LLMPhaseConfig: temperature=None
    assert spec.temperature is None
    assert effective_temperature(cfg, spec) == 0.25


def test_effective_temperature_phase_override_wins():
    cfg = AppConfig()
    cfg.llm.temperature = 0.25
    cfg.llm.extract = LLMPhaseConfig(
        provider="gemini", model="gemini-3.5-flash-lite", temperature=0.0
    )
    spec = llm_phase(cfg, "extract")
    assert effective_temperature(cfg, spec) == 0.0
    # Other phases are untouched.
    assert effective_temperature(cfg, llm_phase(cfg, "connect")) == 0.25


def test_get_llm_uses_phase_temperature_when_not_overridden_by_kwarg(monkeypatch):
    langchain_openai = pytest.importorskip("langchain_openai")
    captured: dict = {}

    class FakeChat:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(langchain_openai, "ChatOpenAI", FakeChat)

    cfg = AppConfig()
    cfg.llm.temperature = 0.25
    cfg.llm.extract = LLMPhaseConfig(provider="openai", model="gpt-4o-mini", temperature=0.0)
    get_llm(cfg, "extract")
    assert captured["temperature"] == 0.0


def test_get_llm_explicit_kwarg_still_wins_over_phase_temperature(monkeypatch):
    langchain_openai = pytest.importorskip("langchain_openai")
    captured: dict = {}

    class FakeChat:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(langchain_openai, "ChatOpenAI", FakeChat)

    cfg = AppConfig()
    cfg.llm.article = LLMPhaseConfig(provider="openai", model="gpt-4o-mini", temperature=0.0)
    get_llm(cfg, "article", temperature=0.8)
    assert captured["temperature"] == 0.8
