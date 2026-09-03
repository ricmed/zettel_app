"""Shared LLM helpers — provider instantiation, call, prompt loading, JSON extraction.

Centralizes functions that were previously duplicated verbatim across
extractor.py, connector.py and gardener.py.

Prompt layout for provider prefix caching:
  SystemMessage(stable instructions) + HumanMessage(per-call payload)
via ``<!-- zettel:user -->`` in prompt files (see ``load_prompt_parts``).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

USER_SPLIT_MARKER = "<!-- zettel:user -->"

# OpenAI-compatible chat APIs (gateways / local servers).
_OPENAI_COMPAT_PROVIDERS = frozenset({
    "openai",
    "openrouter",
    "opencode",
    "azure",
    "compatible",
})
_CHAT_PROVIDERS = _OPENAI_COMPAT_PROVIDERS | frozenset({"anthropic", "ollama", "gemini"})


def _message_text(content: Any) -> str:
    """Plain text from ``AIMessage.content`` (str, or Gemini 3+ list of blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        joined = "\n".join(t for t in texts if t)
        if joined:
            return joined
        strs = [b for b in content if isinstance(b, str) and b]
        if strs:
            return "\n".join(strs)
    return "" if content is None else str(content)


def clip_text(text: str, max_len: int = 72) -> str:
    """One-line preview for progress logs (collapses whitespace)."""
    one = " ".join((text or "").split())
    if len(one) <= max_len:
        return one
    return one[: max_len - 3].rstrip() + "..."


def normalize_llm_provider(provider: str | None) -> str:
    """Normalize provider aliases for branching (lowercase strip)."""
    return (provider or "").strip().lower()


def is_openai_compatible(provider: str | None) -> bool:
    return normalize_llm_provider(provider) in _OPENAI_COMPAT_PROVIDERS


def is_supported_llm_provider(provider: str | None) -> bool:
    return normalize_llm_provider(provider) in _CHAT_PROVIDERS


def get_llm(
    cfg: Any,
    phase: str,
    *,
    temperature: float | None = None,
    max_retries: int | None = None,
) -> Any:
    """Instantiate the LLM configured for ``phase``.

    Identity (provider, model, base_url) comes from ``llm.<phase>``. Sampling
    knobs come from ``cfg.llm`` unless ``temperature`` / ``max_retries`` are
    passed (article node temps; vision sets ``max_retries=0`` because assets
    owns 429 pacing) or the phase declares its own ``llm.<phase>.temperature``
    (lower priority than the explicit kwarg, higher than the global default).
    """
    from zettel.config import effective_temperature, llm_phase

    spec = llm_phase(cfg, phase)
    temp = effective_temperature(cfg, spec) if temperature is None else temperature
    retries = cfg.llm.max_retries if max_retries is None else max_retries
    provider = normalize_llm_provider(spec.provider)
    base_url = spec.base_url
    top_p = getattr(cfg.llm, "top_p", 1)

    if is_openai_compatible(provider):
        from langchain_openai import ChatOpenAI
        kwargs: dict[str, Any] = {
            "model": spec.model,
            "temperature": temp,
            "top_p": top_p,
            "max_retries": retries,
        }
        if base_url:
            kwargs["base_url"] = base_url
        return ChatOpenAI(**kwargs)

    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(
            model=spec.model,
            temperature=temp,
            top_p=top_p,
            max_retries=retries,
        )

    if provider == "ollama":
        from langchain_ollama import ChatOllama
        kwargs = {
            "model": spec.model,
            "temperature": temp,
            "top_p": top_p,
        }
        if base_url:
            kwargs["base_url"] = base_url
        return ChatOllama(**kwargs)

    if provider == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI
        return ChatGoogleGenerativeAI(
            model=spec.model,
            temperature=temp,
            top_p=top_p,
            max_retries=retries,
        )

    raise ValueError(f"LLM provider não suportado: {spec.provider}")


def _resolve_model_name(llm: Any, model: Optional[str]) -> str:
    if model:
        return model
    for attr in ("model", "model_name", "model_id"):
        val = getattr(llm, attr, None)
        if isinstance(val, str) and val.strip():
            return val
    return ""


@dataclass(frozen=True)
class TokenUsage:
    """Token counts from a provider response (provider prompt-cache fields included)."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _extract_cache_tokens_from_mapping(data: dict[str, Any]) -> tuple[int, int]:
    """Best-effort cache_read / cache_write from nested usage dicts."""
    cache_read = 0
    cache_write = 0

    details = (
        data.get("input_token_details")
        or data.get("prompt_tokens_details")
        or data.get("input_tokens_details")
        or {}
    )
    if isinstance(details, dict):
        cache_read = _as_int(
            details.get("cache_read")
            or details.get("cache_read_input_tokens")
            or details.get("cached_tokens")
            or details.get("cache_read_tokens")
        )
        cache_write = _as_int(
            details.get("cache_creation")
            or details.get("cache_write")
            or details.get("cache_creation_input_tokens")
            or details.get("cache_write_tokens")
            or details.get("ephemeral_5m_input_tokens")
            or details.get("ephemeral_1h_input_tokens")
        )
        # Anthropic sometimes splits ephemeral TTLs; sum if both present without cache_creation.
        if not cache_write:
            e5 = _as_int(details.get("ephemeral_5m_input_tokens"))
            e1 = _as_int(details.get("ephemeral_1h_input_tokens"))
            cache_write = e5 + e1

    cache_read = cache_read or _as_int(
        data.get("cache_read_input_tokens")
        or data.get("cached_tokens")
        or data.get("cached_content_token_count")
        or data.get("total_cached_tokens")
    )
    cache_write = cache_write or _as_int(
        data.get("cache_creation_input_tokens")
        or data.get("cache_write_tokens")
    )
    return cache_read, cache_write


def _extract_usage(response: Any) -> TokenUsage:
    """Return token usage (incl. provider prompt-cache counts) from a LangChain AIMessage."""
    usage = getattr(response, "usage_metadata", None) or {}
    if isinstance(usage, dict) and usage:
        prompt = usage.get("input_tokens")
        completion = usage.get("output_tokens")
        if prompt is None:
            prompt = usage.get("prompt_tokens", 0)
        if completion is None:
            completion = usage.get("completion_tokens", 0)
        cache_read, cache_write = _extract_cache_tokens_from_mapping(usage)
        return TokenUsage(
            prompt_tokens=_as_int(prompt),
            completion_tokens=_as_int(completion),
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
        )

    meta = getattr(response, "response_metadata", None) or {}
    if not isinstance(meta, dict):
        return TokenUsage()
    token_usage = meta.get("token_usage") or meta.get("usage") or {}
    if isinstance(token_usage, dict):
        prompt = token_usage.get("prompt_tokens") or token_usage.get("input_tokens") or 0
        completion = (
            token_usage.get("completion_tokens")
            or token_usage.get("output_tokens")
            or 0
        )
        cache_read, cache_write = _extract_cache_tokens_from_mapping(token_usage)
        # OpenAI often nests prompt_tokens_details under token_usage.
        if not cache_read and not cache_write:
            cache_read, cache_write = _extract_cache_tokens_from_mapping(meta)
        return TokenUsage(
            prompt_tokens=_as_int(prompt),
            completion_tokens=_as_int(completion),
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
        )
    return TokenUsage()


def apply_prompt_cache_hints(
    provider: str | None,
    messages: list[Any],
    *,
    enabled: bool = True,
) -> tuple[list[Any], dict[str, Any]]:
    """Attach provider-specific prompt-cache hints; no-op for most providers.

    Returns ``(messages, invoke_kwargs)``. Only Anthropic gets explicit
    ``cache_control`` on the system message content blocks.
    """
    invoke_kwargs: dict[str, Any] = {}
    if not enabled or not messages:
        return messages, invoke_kwargs

    if normalize_llm_provider(provider) != "anthropic":
        return messages, invoke_kwargs

    from langchain_core.messages import SystemMessage

    out: list[Any] = []
    for msg in messages:
        if isinstance(msg, SystemMessage):
            content = msg.content
            if isinstance(content, str):
                out.append(
                    SystemMessage(
                        content=[
                            {
                                "type": "text",
                                "text": content,
                                "cache_control": {"type": "ephemeral"},
                            }
                        ]
                    )
                )
            elif isinstance(content, list) and content:
                blocks = []
                for i, block in enumerate(content):
                    if isinstance(block, dict):
                        b = dict(block)
                        if i == len(content) - 1 and b.get("type") == "text":
                            b["cache_control"] = {"type": "ephemeral"}
                        blocks.append(b)
                    else:
                        blocks.append(block)
                out.append(SystemMessage(content=blocks))
            else:
                out.append(msg)
        else:
            out.append(msg)
    return out, invoke_kwargs


def call_llm(
    llm: Any,
    prompt: str = "",
    *,
    system: str | None = None,
    user: str | None = None,
    label: Optional[str] = None,
    step: Optional[int] = None,
    total: Optional[int] = None,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    prompt_cache: bool = True,
) -> str:
    """Call the LLM and return the response text.

    Prefer ``system`` (stable instructions) + ``user``/``prompt`` (per-call
    payload) so providers can reuse the prefix (prompt caching). Legacy callers
    may pass a single filled blob as ``prompt`` with no ``system``.

    Optional ``label`` / ``step`` / ``total`` emit an INFO line before the HTTP
    call so opaque client logs can be correlated with pipeline stages.

    Records token usage and estimated USD cost on the active ``CostTracker``.
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    from zettel.pricing import estimate_llm_cost
    from zettel.usage import record_llm

    user_text = user if user is not None else prompt
    if not user_text and not system:
        raise ValueError("call_llm requires a user/prompt message")

    if label:
        if step is not None and total is not None:
            logger.info("LLM [%d/%d] %s", step, total, label)
        elif step is not None:
            logger.info("LLM [%d] %s", step, label)
        else:
            logger.info("LLM %s", label)

    messages: list[Any] = []
    if system:
        messages.append(SystemMessage(content=system))
    messages.append(HumanMessage(content=user_text))
    messages, invoke_kwargs = apply_prompt_cache_hints(
        provider, messages, enabled=prompt_cache,
    )

    response = llm.invoke(messages, **invoke_kwargs)
    content = _message_text(response.content)

    model_name = _resolve_model_name(llm, model)
    usage = _extract_usage(response)
    cost = estimate_llm_cost(
        model_name,
        usage.prompt_tokens,
        usage.completion_tokens,
        provider=provider,
    )
    record_llm(
        model=model_name or "unknown",
        tokens_in=usage.prompt_tokens,
        tokens_out=usage.completion_tokens,
        cost_usd=cost,
        label=label or "",
        step=step,
        total=total,
        cache_read_tokens=usage.cache_read_tokens,
        cache_write_tokens=usage.cache_write_tokens,
    )
    return content


@dataclass(frozen=True)
class PromptParts:
    """Split prompt template: stable system + per-call user template."""

    system: str
    user_template: str
    has_split: bool = False

    @property
    def full_template(self) -> str:
        """Reconstruct a single template string (for hashing / legacy)."""
        if self.has_split and self.system:
            return f"{self.system}\n\n{USER_SPLIT_MARKER}\n\n{self.user_template}"
        return self.user_template or self.system


def split_prompt_text(text: str) -> PromptParts:
    """Split raw prompt text on ``<!-- zettel:user -->``."""
    if USER_SPLIT_MARKER not in text:
        logger.debug(
            "Prompt sem marcador %s — enviando tudo como HumanMessage",
            USER_SPLIT_MARKER,
        )
        return PromptParts(system="", user_template=text.strip(), has_split=False)

    system, _, user = text.partition(USER_SPLIT_MARKER)
    return PromptParts(
        system=system.strip(),
        user_template=user.strip(),
        has_split=True,
    )


# Every prompt template the pipeline loads at runtime, relative to
# ``cfg.prompts_path``. Kept here, next to ``load_prompt_parts`` (the function that
# actually reads them), so there is one list instead of one per consumer: `zettel
# doctor` checks that each file exists before a run fails mid-pipeline on a
# ``FileNotFoundError``, and ``tests/test_prompts.py`` locks each template against
# the code that fills it.
#
# ``article_anti_ai.md`` is here too even though it is never loaded on its own: it
# is a fragment injected into the section prompts through ``{anti_ai}``, so a
# missing file breaks article generation exactly like a missing top-level prompt.
#
# Adding a prompt file means adding it here.
REQUIRED_PROMPTS: tuple[str, ...] = (
    "literature_note.md",
    "permanent_note.md",
    "dedupe_decision.md",
    "moc_generation.md",
    "moc_incremental.md",
    "moc_hub_generation.md",
    "moc_hub_incremental.md",
    "ptbr_guard.md",
    "image_description.md",
    "ask.md",
    "bibliographic_metadata.md",
    "article_outline.md",
    "article_section_blog.md",
    "article_section_academic.md",
    "article_anti_ai.md",
    "article_query_enrich.md",
    "article_personality.md",
    "article_judge.md",
)


def load_prompt(path: Path) -> str:
    """Load a prompt template from a file path (full text, including marker)."""
    if not path.exists():
        raise FileNotFoundError(f"Prompt não encontrado: {path}")
    return path.read_text(encoding="utf-8")


def load_prompt_parts(path: Path) -> PromptParts:
    """Load and split a prompt file into system + user template parts."""
    return split_prompt_text(load_prompt(path))


def fill_template(text: str, mapping: dict[str, Any]) -> str:
    """Replace ``{key}`` placeholders; missing keys become empty string."""
    result = text
    for key, value in mapping.items():
        result = result.replace("{" + key + "}", "" if value is None else str(value))
    return result


def extract_json(text: str) -> str:
    """Extract a JSON object or array from text that may include markdown code blocks."""
    match = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    text = text.strip()
    if text.startswith("{") or text.startswith("["):
        return text
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1:
        return text[start: end + 1]
    raise ValueError("Nenhum JSON encontrado na resposta do LLM")
