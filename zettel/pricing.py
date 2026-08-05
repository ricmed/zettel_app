"""LLM/embedding cost estimation via LiteLLM's public price map (calculator only).

Does not route completions through LiteLLM — only ``cost_per_token``.
Prices refresh when the ``litellm`` package is upgraded.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_warned_models: set[str] = set()


def estimate_embed_tokens(text: str) -> int:
    """Rough token estimate for embedding billing (chars/4, min 1)."""
    if not text:
        return 0
    return max(1, len(text) // 4)


def _is_local_model(model: str) -> bool:
    m = (model or "").strip().lower()
    if not m:
        return True
    return (
        m.startswith("ollama/")
        or m.startswith("ollama:")
        or "/" not in m and ":" in m  # e.g. qwen3.5:4b
        or m in {"sentence-transformers", "default"}
    )


def _normalize_model(model: str) -> str:
    """Strip provider prefixes LiteLLM sometimes needs inverted."""
    m = (model or "").strip()
    if m.startswith("openai/"):
        return m[len("openai/"):]
    if m.startswith("anthropic/"):
        return m
    return m


def estimate_llm_cost(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    *,
    provider: str | None = None,
) -> float:
    """USD cost for a chat completion from LiteLLM pricing; 0 if unknown/local."""
    if provider in ("ollama",) or _is_local_model(model):
        return 0.0
    prompt_tokens = max(0, int(prompt_tokens or 0))
    completion_tokens = max(0, int(completion_tokens or 0))
    if prompt_tokens == 0 and completion_tokens == 0:
        return 0.0

    name = _normalize_model(model)
    try:
        import litellm

        prompt_cost, completion_cost = litellm.cost_per_token(
            model=name,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
        return float(prompt_cost or 0.0) + float(completion_cost or 0.0)
    except Exception as exc:
        _warn_once(name, exc)
        return 0.0


def estimate_embed_cost(
    model: str,
    tokens: int,
    *,
    provider: str | None = None,
) -> float:
    """USD cost for embedding ``tokens`` of ``model``; 0 if unknown/local."""
    if provider in ("ollama", "sentence-transformers") or _is_local_model(model):
        return 0.0
    if tokens <= 0:
        return 0.0
    name = _normalize_model(model)
    try:
        import litellm

        prompt_cost, _ = litellm.cost_per_token(
            model=name,
            prompt_tokens=int(tokens),
            completion_tokens=0,
        )
        return float(prompt_cost or 0.0)
    except Exception as exc:
        _warn_once(name, exc)
        return 0.0


def _warn_once(model: str, exc: Any) -> None:
    if model in _warned_models:
        return
    _warned_models.add(model)
    logger.warning(
        "Preco LiteLLM indisponivel para modelo %r (%s) — custo registrado como 0",
        model, exc,
    )


def reset_price_warnings() -> None:
    """Clear one-shot warning set (tests)."""
    _warned_models.clear()
