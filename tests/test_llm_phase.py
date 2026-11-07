"""Per-phase LLM identity: llm_phase + get_llm factory."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from zettel.config import (
    AppConfig,
    LLMConfig,
    LLMPhaseConfig,
    anthropic_omits_sampling,
    checksum_sampling,
    effective_temperature,
    gemini_omits_sampling,
    llm_phase,
    thinking_checksum_token,
)
from zettel.llm import (
    _thinking_client_kwargs,
    get_llm,
    is_supported_llm_provider,
)


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
    assert is_supported_llm_provider("deepseek")
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


def test_get_llm_deepseek_uses_deepseek_api_key(monkeypatch):
    langchain_openai = pytest.importorskip("langchain_openai")
    captured: dict = {}

    class FakeChat:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(langchain_openai, "ChatOpenAI", FakeChat)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-test")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    cfg = AppConfig()
    cfg.llm.ask = LLMPhaseConfig(
        provider="deepseek",
        model="deepseek-v4-flash",
        base_url="https://api.deepseek.com",
    )
    get_llm(cfg, "ask")
    assert captured["model"] == "deepseek-v4-flash"
    assert captured["base_url"] == "https://api.deepseek.com"
    assert captured["api_key"] == "sk-deepseek-test"


def test_get_llm_deepseek_fails_without_key(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    cfg = AppConfig()
    cfg.llm.ask = LLMPhaseConfig(
        provider="deepseek",
        model="deepseek-v4-flash",
        base_url="https://api.deepseek.com",
    )
    with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY"):
        get_llm(cfg, "ask")


def test_llm_phase_ready_deepseek_requires_own_key(monkeypatch):
    from zettel.web.health import llm_phase_ready

    cfg = AppConfig()
    cfg.llm.ask = LLMPhaseConfig(provider="deepseek", model="deepseek-v4-flash")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")
    assert llm_phase_ready(cfg, "ask") is False
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-test")
    assert llm_phase_ready(cfg, "ask") is True


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


# ── thinking per phase ────────────────────────────────────────────────


def test_thinking_checksum_token_canonical():
    assert thinking_checksum_token(None) == ""
    assert thinking_checksum_token(False) == "false"
    assert thinking_checksum_token(True) == "true"
    assert thinking_checksum_token("high") == "high"
    assert thinking_checksum_token(1024) == "1024"
    assert thinking_checksum_token(0) == "0"


def test_llm_phase_thinking_rejects_unknown_and_negative():
    with pytest.raises(ValidationError):
        LLMPhaseConfig(thinking="off")
    with pytest.raises(ValidationError):
        LLMPhaseConfig(thinking=-1)


def test_llm_phase_thinking_accepts_bool_level_budget():
    assert LLMPhaseConfig(thinking=False).thinking is False
    assert LLMPhaseConfig(thinking=True).thinking is True
    assert LLMPhaseConfig(thinking="minimal").thinking == "minimal"
    assert LLMPhaseConfig(thinking=0).thinking == 0
    assert LLMPhaseConfig(thinking=1024).thinking == 1024
    assert LLMPhaseConfig().thinking is None


def test_llm_phase_thinking_none_is_off():
    spec = LLMPhaseConfig(thinking="none")
    assert spec.thinking is False
    assert thinking_checksum_token(spec.thinking) == "false"


def test_thinking_client_kwargs_null_omits():
    assert _thinking_client_kwargs("gemini", None) == {}
    assert _thinking_client_kwargs("openai", None) == {}
    assert _thinking_client_kwargs("ollama", None) == {}
    assert _thinking_client_kwargs("anthropic", None) == {}


def test_thinking_client_kwargs_gemini_uses_only_thinking_level():
    assert _thinking_client_kwargs("gemini", False) == {"thinking_level": "minimal"}
    assert _thinking_client_kwargs("gemini", 0) == {"thinking_level": "minimal"}
    assert _thinking_client_kwargs("gemini", True) == {}
    assert _thinking_client_kwargs("gemini", "high") == {"thinking_level": "high"}


def test_gemini_thinking_budget_is_refused_at_config_load():
    with pytest.raises(ValidationError, match="orcamento"):
        LLMPhaseConfig(provider="gemini", model="gemini-3.8-flash", thinking=1024)
    # Zero is "off", not a budget; other providers keep the budget.
    assert LLMPhaseConfig(provider="gemini", model="gemini-3.8-flash", thinking=0).thinking == 0
    assert LLMPhaseConfig(provider="anthropic", model="claude-haiku-4-5", thinking=1024)


def test_gemini_omits_sampling_from_version_3():
    assert gemini_omits_sampling("gemini-3.5-flash-lite")
    assert gemini_omits_sampling("models/gemini-3.8-flash")
    assert gemini_omits_sampling("gemini-10-pro")
    assert not gemini_omits_sampling("gemini-2.5-flash")
    assert not gemini_omits_sampling("gpt-4o-mini")


def test_checksum_sampling_is_empty_when_no_sampling_is_sent():
    cfg = AppConfig()
    cfg.llm.temperature = 0.2
    cfg.llm.top_p = 0.5
    cfg.llm.extract = LLMPhaseConfig(
        provider="gemini", model="gemini-3.5-flash-lite", temperature=0.1
    )
    cfg.llm.connect = LLMPhaseConfig(provider="anthropic", model="claude-haiku-5-5")
    cfg.llm.review = LLMPhaseConfig(provider="openai", model="gpt-4o-mini")
    assert checksum_sampling(cfg, llm_phase(cfg, "extract")) == (None, None)
    assert checksum_sampling(cfg, llm_phase(cfg, "connect"), 0.7) == (None, None)
    assert checksum_sampling(cfg, llm_phase(cfg, "review")) == (0.2, 0.5)
    assert checksum_sampling(cfg, llm_phase(cfg, "review"), 0.7) == (0.7, 0.5)


def test_cache_key_ignores_temperature_of_a_model_without_sampling():
    from zettel.extractor import prompt1_call_checksum

    cfg = AppConfig()
    cfg.llm.extract = LLMPhaseConfig(
        provider="gemini", model="gemini-3.5-flash-lite", temperature=0.1
    )
    chunk = {"chunk_checksum": "abc"}
    before = prompt1_call_checksum(cfg, "prompt", chunk, "")
    cfg.llm.extract.temperature = 0.9
    cfg.llm.top_p = 0.3
    assert prompt1_call_checksum(cfg, "prompt", chunk, "") == before


def test_thinking_client_kwargs_openai_compat():
    assert _thinking_client_kwargs("openai", False) == {"reasoning_effort": "none"}
    assert _thinking_client_kwargs("deepseek", "low") == {"reasoning_effort": "low"}
    assert _thinking_client_kwargs("openai", True) == {"reasoning_effort": "medium"}
    assert _thinking_client_kwargs("openai", 2048) == {"reasoning_effort": "high"}


def test_thinking_client_kwargs_ollama_and_anthropic():
    assert _thinking_client_kwargs("ollama", False) == {"reasoning": False}
    assert _thinking_client_kwargs("ollama", True) == {"reasoning": True}
    assert _thinking_client_kwargs("ollama", "high") == {"reasoning": True}
    assert _thinking_client_kwargs("anthropic", False) == {"thinking": {"type": "disabled"}}
    assert _thinking_client_kwargs("anthropic", True) == {
        "thinking": {"type": "enabled", "budget_tokens": 4096}
    }
    assert _thinking_client_kwargs("anthropic", "low") == {
        "thinking": {"type": "enabled", "budget_tokens": 2048}
    }
    assert _thinking_client_kwargs("anthropic", 3000) == {
        "thinking": {"type": "enabled", "budget_tokens": 3000}
    }


def test_anthropic_omits_sampling_by_family():
    assert anthropic_omits_sampling("claude-haiku-5-5")
    assert anthropic_omits_sampling("claude-haiku-5-5-20261001")
    assert anthropic_omits_sampling("Claude-Sonnet-5-5")
    assert anthropic_omits_sampling("claude-opus-5-5")
    assert anthropic_omits_sampling("claude-opus-4-7")
    assert anthropic_omits_sampling("claude-fable-5")
    assert not anthropic_omits_sampling("claude-haiku-4-5")
    assert not anthropic_omits_sampling("claude-sonnet-4-6")
    assert not anthropic_omits_sampling("claude-opus-4-6")


def test_thinking_false_turns_thinking_off_where_the_model_allows_it():
    """Haiku 5.5 accepts {type: disabled} at low effort; Opus 5.5 / Sonnet 5.5 / Fable never."""
    off = {"thinking": {"type": "disabled"}, "output_config": {"effort": "low"}}
    effort_only = {"output_config": {"effort": "low"}}
    for model in ("claude-haiku-5-5", "claude-opus-5", "claude-opus-4-8", "claude-sonnet-5"):
        assert _thinking_client_kwargs("anthropic", False, model=model) == off, model
    for model in ("claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1", "claude-mythos-5-1"):
        assert _thinking_client_kwargs("anthropic", False, model=model) == effort_only, model
    # "low" keeps thinking on at low effort: the wire the connect phase had before.
    assert _thinking_client_kwargs("anthropic", "low", model="claude-haiku-5-5") == {
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "low"},
    }


def test_thinking_client_kwargs_anthropic_adaptive_family():
    assert _thinking_client_kwargs("anthropic", False, model="claude-haiku-5-5") == {
        "thinking": {"type": "disabled"},
        "output_config": {"effort": "low"},
    }
    assert _thinking_client_kwargs("anthropic", True, model="claude-haiku-5-5") == {
        "thinking": {"type": "adaptive"}
    }
    assert _thinking_client_kwargs("anthropic", "high", model="claude-haiku-5-5") == {
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "high"},
    }
    assert _thinking_client_kwargs("anthropic", "minimal", model="claude-haiku-5-5") == {
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "low"},
    }
    assert _thinking_client_kwargs("anthropic", 3000, model="claude-haiku-5-5") == {
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "medium"},
    }
    assert _thinking_client_kwargs("anthropic", False, model="claude-haiku-4-5") == {
        "thinking": {"type": "disabled"}
    }


def test_get_llm_omits_thinking_when_null(monkeypatch):
    langchain_openai = pytest.importorskip("langchain_openai")
    captured: dict = {}

    class FakeChat:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(langchain_openai, "ChatOpenAI", FakeChat)
    cfg = AppConfig()
    cfg.llm.extract = LLMPhaseConfig(provider="openai", model="gpt-4o-mini")
    get_llm(cfg, "extract")
    assert "reasoning_effort" not in captured
    assert "thinking_budget" not in captured
    assert "reasoning" not in captured


def test_get_llm_openai_thinking_false(monkeypatch):
    langchain_openai = pytest.importorskip("langchain_openai")
    captured: dict = {}

    class FakeChat:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(langchain_openai, "ChatOpenAI", FakeChat)
    cfg = AppConfig()
    cfg.llm.extract = LLMPhaseConfig(provider="openai", model="gpt-4o-mini", thinking=False)
    get_llm(cfg, "extract")
    assert captured["reasoning_effort"] == "none"


def test_get_llm_openai_thinking_none_keeps_sampling(monkeypatch):
    langchain_openai = pytest.importorskip("langchain_openai")
    captured: dict = {}

    class FakeChat:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(langchain_openai, "ChatOpenAI", FakeChat)
    cfg = AppConfig()
    cfg.llm.temperature = 0.2
    cfg.llm.top_p = 0.5
    cfg.llm.harvest = LLMPhaseConfig(provider="openai", model="gpt-6-luna", thinking="none")
    get_llm(cfg, "harvest")
    assert captured["reasoning_effort"] == "none"
    assert captured["temperature"] == 0.2
    assert captured["top_p"] == 0.5


def test_get_llm_gemini_thinking_false(monkeypatch):
    langchain_google_genai = pytest.importorskip("langchain_google_genai")
    captured: dict = {}

    class FakeChat:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(langchain_google_genai, "ChatGoogleGenerativeAI", FakeChat)
    cfg = AppConfig()
    cfg.llm.connect = LLMPhaseConfig(
        provider="gemini", model="gemini-3.1-flash-lite", thinking=False
    )
    get_llm(cfg, "connect")
    assert captured["thinking_level"] == "minimal"
    assert "thinking_budget" not in captured
    assert "include_thoughts" not in captured


def test_get_llm_gemini_thinking_level(monkeypatch):
    langchain_google_genai = pytest.importorskip("langchain_google_genai")
    captured: dict = {}

    class FakeChat:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(langchain_google_genai, "ChatGoogleGenerativeAI", FakeChat)
    cfg = AppConfig()
    cfg.llm.connect = LLMPhaseConfig(
        provider="gemini", model="gemini-3.1-flash-lite", thinking="high"
    )
    get_llm(cfg, "connect")
    assert captured["thinking_level"] == "high"
    assert "thinking_budget" not in captured


def test_get_llm_gemini_3_sends_no_sampling(monkeypatch):
    langchain_google_genai = pytest.importorskip("langchain_google_genai")
    captured: dict = {}

    class FakeChat:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(langchain_google_genai, "ChatGoogleGenerativeAI", FakeChat)
    cfg = AppConfig()
    cfg.llm.top_p = 0.5
    cfg.llm.extract = LLMPhaseConfig(provider="gemini", model="gemini-3.8-flash", temperature=0.1)
    get_llm(cfg, "extract")
    for knob in ("temperature", "top_p", "top_k", "thinking_budget"):
        assert knob not in captured


def test_get_llm_ollama_thinking_false(monkeypatch):
    langchain_ollama = pytest.importorskip("langchain_ollama")
    captured: dict = {}

    class FakeChat:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(langchain_ollama, "ChatOllama", FakeChat)
    cfg = AppConfig()
    cfg.llm.extract = LLMPhaseConfig(
        provider="ollama",
        model="qwen",
        base_url="http://localhost:11434",
        thinking=False,
    )
    get_llm(cfg, "extract")
    assert captured["reasoning"] is False
    assert "max_retries" not in captured


def _patch_chat_anthropic(monkeypatch) -> dict:
    langchain_anthropic = pytest.importorskip("langchain_anthropic")
    captured: dict = {}

    class FakeChat:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(langchain_anthropic, "ChatAnthropic", FakeChat)
    return captured


def test_get_llm_anthropic_haiku_55_omits_sampling(monkeypatch):
    captured = _patch_chat_anthropic(monkeypatch)
    cfg = AppConfig()
    cfg.llm.temperature = 0.2
    cfg.llm.top_p = 0.5
    cfg.llm.connect = LLMPhaseConfig(
        provider="anthropic",
        model="claude-haiku-5-5",
        temperature=0.15,
        thinking=False,
    )
    get_llm(cfg, "connect")
    assert captured["model"] == "claude-haiku-5-5"
    assert "temperature" not in captured
    assert "top_p" not in captured
    assert captured["output_config"] == {"effort": "low"}
    assert captured["thinking"] == {"type": "disabled"}


def test_get_llm_anthropic_haiku_45_keeps_sampling(monkeypatch):
    captured = _patch_chat_anthropic(monkeypatch)
    cfg = AppConfig()
    cfg.llm.top_p = 0.5
    cfg.llm.connect = LLMPhaseConfig(
        provider="anthropic",
        model="claude-haiku-4-5",
        temperature=0.15,
        thinking=False,
    )
    get_llm(cfg, "connect")
    assert captured["temperature"] == 0.15
    assert captured["top_p"] == 0.5
    assert captured["thinking"] == {"type": "disabled"}


def test_get_llm_anthropic_haiku_55_thinking_high(monkeypatch):
    captured = _patch_chat_anthropic(monkeypatch)
    cfg = AppConfig()
    cfg.llm.connect = LLMPhaseConfig(
        provider="anthropic",
        model="claude-haiku-5-5",
        thinking="high",
    )
    get_llm(cfg, "connect")
    assert captured["thinking"] == {"type": "adaptive"}
    assert captured["output_config"] == {"effort": "high"}
    assert "temperature" not in captured
