"""Shared LLM helpers — provider instantiation, call, prompt loading, JSON extraction.

Centralizes functions that were previously duplicated verbatim across
extractor.py, connector.py and gardener.py.

Prompt layout for provider prefix caching:
  SystemMessage(stable instructions) + HumanMessage(per-call payload)
via ``<!-- zettel:user -->`` in prompt files (see ``load_prompt_parts``).
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

USER_SPLIT_MARKER = "<!-- zettel:user -->"

# OpenAI-compatible chat APIs (gateways / local servers).
_OPENAI_COMPAT_PROVIDERS = frozenset(
    {
        "openai",
        "openrouter",
        "opencode",
        "azure",
        "compatible",
        "deepseek",
    }
)
_CHAT_PROVIDERS = _OPENAI_COMPAT_PROVIDERS | frozenset({"anthropic", "ollama", "gemini"})


class LLMUnavailableError(RuntimeError):
    """Provider down, unreachable, or not installable. Not a content/parse failure."""

    def __init__(self, reason: str, *, phase: str | None = None) -> None:
        self.reason = reason
        self.phase = phase
        where = f" ({phase})" if phase else ""
        super().__init__(
            f"LLM indisponível{where}: {reason}. "
            "Itens pendentes não foram marcados failed. "
            "Verifique o provider e rode de novo."
        )


_UNAVAILABLE_CLASS_MARKERS = (
    "apiconnection",
    "authenticationerror",
    "connecterror",
    "connectionerror",
    "connecttimeout",
    "internalservererror",
    "notfounderror",
    "permissiondenied",
    "ratelimit",
    "serviceunavailable",
    "timeout",
)

_UNAVAILABLE_MSG_MARKERS = (
    "401",
    "403",
    "404",
    "429",
    "502",
    "503",
    "504",
    "connection refused",
    "connect error",
    "connection reset",
    "connecttimeout",
    "getaddrinfo",
    "incorrect api key",
    "invalid api key",
    "max retries exceeded",
    "model not found",
    "name or service not known",
    "nodename nor servname",
    "rate limit",
    "rate_limit",
    "timed out",
    "timeout",
    "unauthorized",
)

_REASON_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        ("connection refused", "connecterror", "connect error", "connectionerror"),
        "connection refused",
    ),
    (("timed out", "timeout", "connecttimeout"), "timeout"),
    (("429", "ratelimit", "rate limit", "rate_limit"), "rate limit esgotado"),
    (
        ("401", "unauthorized", "authentication", "invalid api key", "incorrect api key"),
        "autenticação recusada",
    ),
    (("403", "permissiondenied", "permission denied"), "acesso recusado"),
    (
        ("502", "503", "504", "service unavailable", "internal server error"),
        "servidor indisponível",
    ),
    (("model not found", "not found: model"), "modelo não encontrado"),
    (("connection reset", "reset by peer"), "connection reset"),
    (("name or service not known", "getaddrinfo", "nodename nor servname"), "host inacessível"),
)


def _walk_exception_chain(exc: BaseException) -> list[BaseException]:
    seen: set[int] = set()
    stack: list[BaseException] = [exc]
    out: list[BaseException] = []
    while stack:
        cur = stack.pop()
        if id(cur) in seen:
            continue
        seen.add(id(cur))
        out.append(cur)
        if cur.__cause__ is not None:
            stack.append(cur.__cause__)
        if cur.__context__ is not None and cur.__context__ is not cur.__cause__:
            stack.append(cur.__context__)
    return out


def is_llm_unavailable(exc: BaseException) -> bool:
    """True for transport, auth, or provider-down errors (not parse/validation)."""
    if isinstance(exc, (LLMUnavailableError, ImportError)):
        return True
    for cur in _walk_exception_chain(exc):
        if isinstance(cur, (ConnectionError, TimeoutError, OSError)):
            return True
        name = type(cur).__name__.lower()
        if any(marker in name for marker in _UNAVAILABLE_CLASS_MARKERS):
            return True
        msg = str(cur).lower()
        if any(marker in msg for marker in _UNAVAILABLE_MSG_MARKERS):
            return True
    return False


def _unavailable_reason(exc: BaseException) -> str:
    if isinstance(exc, ImportError):
        pkg = getattr(exc, "name", None) or "do provider"
        return f"pacote {pkg} não instalado"
    for cur in _walk_exception_chain(exc):
        blob = f"{type(cur).__name__.lower()} {cur}".lower()
        for markers, reason in _REASON_RULES:
            if any(marker in blob for marker in markers):
                return reason
    return "falha de conexão ou autenticação"


def _raise_unavailable(exc: BaseException, *, phase: str | None = None) -> None:
    if isinstance(exc, LLMUnavailableError):
        raise exc
    raise LLMUnavailableError(_unavailable_reason(exc), phase=phase) from exc


class _RetryingChatModel:
    """Honours ``max_retries`` when the client has no such knob (ChatOllama)."""

    def __init__(self, inner: Any, max_retries: int) -> None:
        self._inner = inner
        self._max_retries = max(0, int(max_retries))

    def invoke(self, messages: Any, **kwargs: Any) -> Any:
        attempts = self._max_retries + 1
        for attempt in range(attempts):
            try:
                return self._inner.invoke(messages, **kwargs)
            except Exception as e:
                if not is_llm_unavailable(e) or attempt >= self._max_retries:
                    raise
        raise RuntimeError("retry loop exhausted")  # pragma: no cover

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


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


_ANTHROPIC_THINKING_BUDGET = {
    "minimal": 1024,
    "low": 2048,
    "medium": 4096,
    "high": 8192,
}
_ANTHROPIC_DEFAULT_BUDGET = 4096


def _thinking_client_kwargs(provider: str, thinking: Any) -> dict[str, Any]:
    """Vendor kwargs for ``llm.<phase>.thinking``. ``None`` omits the parameter."""
    if thinking is None:
        return {}

    off = thinking is False or thinking == 0
    level = thinking if isinstance(thinking, str) else None
    is_budget = isinstance(thinking, int) and not isinstance(thinking, bool) and thinking > 0
    budget = thinking if is_budget else None

    if provider == "gemini":
        if off:
            return {"thinking_budget": 0}
        if level:
            return {"reasoning_effort": level}
        if budget:
            return {"thinking_budget": budget}
        return {}

    if is_openai_compatible(provider):
        if off:
            return {"reasoning_effort": "none"}
        if level:
            return {"reasoning_effort": level}
        if budget:
            return {"reasoning_effort": "high"}
        return {"reasoning_effort": "medium"}

    if provider == "ollama":
        return {"reasoning": not off}

    if provider == "anthropic":
        if off:
            return {"thinking": {"type": "disabled"}}
        tokens = (
            budget
            if budget is not None
            else (_ANTHROPIC_THINKING_BUDGET[level] if level else _ANTHROPIC_DEFAULT_BUDGET)
        )
        return {"thinking": {"type": "enabled", "budget_tokens": tokens}}

    return {}


def get_llm(
    cfg: Any,
    phase: str,
    *,
    temperature: float | None = None,
    max_retries: int | None = None,
) -> Any:
    """Instantiate the LLM configured for ``phase``.

    Identity (provider, model, base_url, thinking) comes from ``llm.<phase>``.
    Sampling knobs come from ``cfg.llm`` unless ``temperature`` / ``max_retries``
    are passed (article node temps; vision sets ``max_retries=0`` because assets
    owns 429 pacing) or the phase declares its own ``llm.<phase>.temperature``
    (lower priority than the explicit kwarg, higher than the global default).
    ``thinking: null`` is omitted so the vendor default applies.
    """
    from zettel.config import effective_temperature, llm_phase

    spec = llm_phase(cfg, phase)
    temp = effective_temperature(cfg, spec) if temperature is None else temperature
    retries = cfg.llm.max_retries if max_retries is None else max_retries
    provider = normalize_llm_provider(spec.provider)
    base_url = spec.base_url
    top_p = getattr(cfg.llm, "top_p", 1)
    thinking_kwargs = _thinking_client_kwargs(provider, spec.thinking)

    try:
        if is_openai_compatible(provider):
            from langchain_openai import ChatOpenAI

            kwargs: dict[str, Any] = {
                "model": spec.model,
                "temperature": temp,
                "top_p": top_p,
                "max_retries": retries,
                **thinking_kwargs,
            }
            if base_url:
                kwargs["base_url"] = base_url
            if provider == "deepseek":
                key = os.environ.get("DEEPSEEK_API_KEY")
                if not key:
                    raise RuntimeError(
                        "Sem DEEPSEEK_API_KEY no ambiente (.env). "
                        "Necessaria quando llm.<fase>.provider e deepseek."
                    )
                kwargs["api_key"] = key
            return ChatOpenAI(**kwargs)

        if provider == "anthropic":
            from langchain_anthropic import ChatAnthropic

            return ChatAnthropic(
                model=spec.model,
                temperature=temp,
                top_p=top_p,
                max_retries=retries,
                **thinking_kwargs,
            )

        if provider == "ollama":
            from langchain_ollama import ChatOllama

            kwargs = {
                "model": spec.model,
                "temperature": temp,
                "top_p": top_p,
                **thinking_kwargs,
            }
            if base_url:
                kwargs["base_url"] = base_url
            # ChatOllama has no max_retries field; wrap invoke instead.
            return _RetryingChatModel(ChatOllama(**kwargs), retries)

        if provider == "gemini":
            from langchain_google_genai import ChatGoogleGenerativeAI

            return ChatGoogleGenerativeAI(
                model=spec.model,
                temperature=temp,
                top_p=top_p,
                max_retries=retries,
                **thinking_kwargs,
            )
    except LLMUnavailableError:
        raise
    except ImportError as e:
        _raise_unavailable(e, phase=phase)
    except Exception as e:
        if is_llm_unavailable(e):
            _raise_unavailable(e, phase=phase)
        raise

    raise ValueError(f"LLM provider não suportado: {spec.provider}")


def _resolve_model_name(llm: Any, model: str | None) -> str:
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
        data.get("cache_creation_input_tokens") or data.get("cache_write_tokens")
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
        completion = token_usage.get("completion_tokens") or token_usage.get("output_tokens") or 0
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
    label: str | None = None,
    step: int | None = None,
    total: int | None = None,
    model: str | None = None,
    provider: str | None = None,
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
            logger.debug("LLM [%d/%d] %s", step, total, label)
        elif step is not None:
            logger.debug("LLM [%d] %s", step, label)
        else:
            logger.debug("LLM %s", label)

    messages: list[Any] = []
    if system:
        messages.append(SystemMessage(content=system))
    messages.append(HumanMessage(content=user_text))
    messages, invoke_kwargs = apply_prompt_cache_hints(
        provider,
        messages,
        enabled=prompt_cache,
    )

    try:
        response = llm.invoke(messages, **invoke_kwargs)
    except LLMUnavailableError:
        raise
    except Exception as e:
        if is_llm_unavailable(e):
            _raise_unavailable(e)
        raise
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
    "chapter_summary.md",
    "source_summary.md",
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
    if text.startswith(("{", "[")):
        return text
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1:
        return text[start : end + 1]
    raise ValueError("Nenhum JSON encontrado na resposta do LLM")


# LaTeX commands whose first letter collides with a JSON escape that is legitimate in
# prose: \t (tab), \n (newline), \r (carriage return). Only these, followed by a non-letter,
# are read as LaTeX. Deliberately absent: "ne" and "not" (a newline followed by the words
# "e" or "not" is ordinary text), "tr" and "rm" (too short to tell apart).
_TAB_NEWLINE_CR_COMMANDS = {
    "t": (
        "theta",
        "tau",
        "times",
        "text",
        "textbf",
        "textit",
        "textrm",
        "texttt",
        "textsf",
        "top",
        "to",
        "tilde",
        "tfrac",
        "triangle",
        "triangleq",
        "tan",
        "tanh",
        "therefore",
    ),
    "n": (
        "nabla",
        "neq",
        "nu",
        "notin",
        "newline",
        "nonumber",
        "nexists",
        "neg",
        "nmid",
        "nleq",
        "ngeq",
        "nsubseteq",
    ),
    "r": (
        "rho",
        "right",
        "rightarrow",
        "rightharpoonup",
        "rangle",
        "rbrace",
        "rbrack",
        "rfloor",
        "rceil",
        "rvert",
        "rVert",
    ),
}
_HEX = frozenset("0123456789abcdefABCDEF")


def repair_latex_escapes(json_text: str) -> str:
    """Double the backslashes of LaTeX that an LLM copied into JSON without escaping.

    With formula enrichment (#178) chunks carry LaTeX, and models copy it into their
    JSON answers as ``\\mathbf`` instead of ``\\\\mathbf``. Two failures follow:

    * an escape JSON does not define (``\\m``, ``\\s``, ``\\{``) raises ``Invalid \\escape``
      and the whole answer is lost;
    * an escape JSON *does* define parses without error into the wrong text -- ``\\frac``
      becomes a form feed plus ``rac``, ``\\top`` a tab plus ``op``, ``\\nabla`` a newline plus
      ``abla``.

    Rules, applied left to right:

    * ``\\\\``, ``\\"`` and ``\\/`` are kept; ``\\u`` with four hex digits is kept.
    * ``\\b`` or ``\\f`` followed by a letter is LaTeX: a backspace or form feed never belongs in
      prose.
    * ``\\t``, ``\\n``, ``\\r`` are LaTeX only when they start a known command followed by a
      non-letter; otherwise they stay tab, newline, carriage return.
    * any other backslash is not a JSON escape at all, so it can only be LaTeX.

    Idempotent: an already escaped ``\\\\frac`` is left alone.
    """
    out: list[str] = []
    i, n = 0, len(json_text)
    while i < n:
        ch = json_text[i]
        if ch != "\\" or i + 1 >= n:
            out.append(ch)
            i += 1
            continue
        nxt = json_text[i + 1]
        rest = json_text[i + 1 :]
        if nxt in '\\"/':
            out.append(json_text[i : i + 2])
            i += 2
        elif nxt == "u" and len(rest) >= 5 and all(c in _HEX for c in rest[1:5]):
            out.append(json_text[i : i + 6])
            i += 6
        elif nxt in "bf":
            if len(rest) > 1 and rest[1].isalpha():
                out.append("\\\\")
                i += 1
            else:
                out.append(json_text[i : i + 2])
                i += 2
        elif nxt in _TAB_NEWLINE_CR_COMMANDS:
            if _starts_latex_command(rest, _TAB_NEWLINE_CR_COMMANDS[nxt]):
                out.append("\\\\")
                i += 1
            else:
                out.append(json_text[i : i + 2])
                i += 2
        else:
            out.append("\\\\")
            i += 1
    return "".join(out)


def _starts_latex_command(text: str, commands: tuple[str, ...]) -> bool:
    for command in commands:
        if text.startswith(command):
            after = text[len(command) : len(command) + 1]
            if not after or not after.isalpha():
                return True
    return False


def parse_llm_json(text: str) -> Any:
    """The JSON object or array in an LLM answer, with unescaped LaTeX repaired first."""
    return json.loads(repair_latex_escapes(extract_json(text)))
