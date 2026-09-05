"""Tests for prompt split, System+Human call_llm, and provider cache hints."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from langchain_core.messages import HumanMessage, SystemMessage
from zettel.llm import (
    TokenUsage,
    _extract_usage,
    apply_prompt_cache_hints,
    call_llm,
    fill_template,
    split_prompt_text,
)
from zettel.usage import begin_run, get_tracker, reset


def setup_function() -> None:
    reset()


def teardown_function() -> None:
    reset()


def test_split_prompt_with_marker():
    text = "SYSTEM RULES\n\n<!-- zettel:user -->\n\nUser {x}\n"
    parts = split_prompt_text(text)
    assert parts.has_split
    assert parts.system == "SYSTEM RULES"
    assert parts.user_template == "User {x}"
    assert "<!-- zettel:user -->" in parts.full_template


def test_split_prompt_without_marker_is_legacy_user_only():
    parts = split_prompt_text("all in one {blob}")
    assert not parts.has_split
    assert parts.system == ""
    assert parts.user_template == "all in one {blob}"


def test_fill_template_replaces_keys():
    assert fill_template("a={a} b={b}", {"a": 1, "b": "x"}) == "a=1 b=x"


class _CapturingLLM:
    model = "gpt-4o-mini"
    last_messages = None
    last_kwargs = None

    def invoke(self, messages, **kwargs):
        self.last_messages = messages
        self.last_kwargs = kwargs
        return SimpleNamespace(
            content="ok",
            usage_metadata={
                "input_tokens": 100,
                "output_tokens": 5,
                "input_token_details": {"cache_read": 80, "cache_creation": 20},
            },
            response_metadata={},
        )


def test_call_llm_sends_system_and_human():
    begin_run(1)
    llm = _CapturingLLM()
    with patch("zettel.pricing.estimate_llm_cost", return_value=0.01):
        text = call_llm(
            llm,
            "user payload",
            system="stable system",
            label="t",
            provider="openai",
        )
    assert text == "ok"
    assert len(llm.last_messages) == 2
    assert isinstance(llm.last_messages[0], SystemMessage)
    assert llm.last_messages[0].content == "stable system"
    assert isinstance(llm.last_messages[1], HumanMessage)
    assert llm.last_messages[1].content == "user payload"
    s = get_tracker().summary()
    assert s.prompt_cache_read_tokens == 80
    assert s.prompt_cache_write_tokens == 20


def test_apply_prompt_cache_hints_anthropic_only():
    msgs = [
        SystemMessage(content="sys"),
        HumanMessage(content="user"),
    ]
    out, kwargs = apply_prompt_cache_hints("anthropic", msgs, enabled=True)
    assert kwargs == {}
    assert isinstance(out[0].content, list)
    assert out[0].content[0]["cache_control"] == {"type": "ephemeral"}

    out2, _ = apply_prompt_cache_hints("openai", msgs, enabled=True)
    assert out2[0].content == "sys"

    out3, _ = apply_prompt_cache_hints("ollama", msgs, enabled=True)
    assert out3[0].content == "sys"

    out4, _ = apply_prompt_cache_hints("anthropic", msgs, enabled=False)
    assert out4[0].content == "sys"


def test_extract_usage_openai_cached_tokens():
    response = SimpleNamespace(
        usage_metadata={},
        response_metadata={
            "token_usage": {
                "prompt_tokens": 50,
                "completion_tokens": 7,
                "prompt_tokens_details": {"cached_tokens": 40},
            }
        },
    )
    # usage_metadata empty dict is falsy for `if usage` — force path via empty then meta
    response.usage_metadata = None
    usage = _extract_usage(response)
    assert usage == TokenUsage(
        prompt_tokens=50,
        completion_tokens=7,
        cache_read_tokens=40,
        cache_write_tokens=0,
    )


def test_get_llm_openai_compatible_aliases():
    from zettel.llm import is_openai_compatible, normalize_llm_provider

    assert is_openai_compatible("openrouter")
    assert is_openai_compatible("opencode")
    assert is_openai_compatible("OpenAI")
    assert normalize_llm_provider(" Gemini ") == "gemini"
    assert not is_openai_compatible("anthropic")


def test_message_text_uses_gemini_text_blocks():
    from zettel.llm import _message_text

    blocks = [
        {"type": "thinking", "thinking": "vou montar o json"},
        {"type": "text", "text": '{"chunk_status": "accepted"}'},
    ]
    assert _message_text(blocks) == '{"chunk_status": "accepted"}'
    assert _message_text("plain") == "plain"


def test_call_llm_uses_gemini_text_block():
    begin_run(1)

    class _GeminiShape:
        model = "gemini-3.5-flash-lite"

        def invoke(self, messages, **kwargs):
            return SimpleNamespace(
                content=[
                    {"type": "thinking", "thinking": "rascunho"},
                    {"type": "text", "text": '{"hi": 1}'},
                ],
                usage_metadata={"input_tokens": 1, "output_tokens": 1},
                response_metadata={},
            )

    with patch("zettel.pricing.estimate_llm_cost", return_value=0.0):
        text = call_llm(_GeminiShape(), "user", provider="gemini")
    assert text == '{"hi": 1}'
