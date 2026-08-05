"""Shared LLM helpers — provider instantiation, call, prompt loading, JSON extraction.

Centralizes functions that were previously duplicated verbatim across
extractor.py, connector.py and gardener.py.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


def clip_text(text: str, max_len: int = 72) -> str:
    """One-line preview for progress logs (collapses whitespace)."""
    one = " ".join((text or "").split())
    if len(one) <= max_len:
        return one
    return one[: max_len - 3].rstrip() + "..."


def get_llm(cfg: Any, temperature: float | None = None) -> Any:
    """Instantiate the configured LLM from AppConfig.

    ``temperature`` overrides ``cfg.llm.temperature`` when provided (used by
    article enricher/judge/personality nodes that need cooler or warmer draws).
    """
    temp = cfg.llm.temperature if temperature is None else temperature
    if cfg.llm.provider == "openai":
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(
            model=cfg.llm.model,
            temperature=temp,
            max_retries=cfg.llm.max_retries,
        )
    elif cfg.llm.provider == "anthropic":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(
            model=cfg.llm.model,
            temperature=temp,
            max_retries=cfg.llm.max_retries,
        )
    elif cfg.llm.provider == "ollama":
        from langchain_ollama import ChatOllama
        return ChatOllama(
            model=cfg.llm.model,
            temperature=temp,
        )
    else:
        raise ValueError(f"LLM provider não suportado: {cfg.llm.provider}")


def _resolve_model_name(llm: Any, model: Optional[str]) -> str:
    if model:
        return model
    for attr in ("model", "model_name", "model_id"):
        val = getattr(llm, attr, None)
        if isinstance(val, str) and val.strip():
            return val
    return ""


def _extract_usage(response: Any) -> tuple[int, int]:
    """Return (prompt_tokens, completion_tokens) from a LangChain AIMessage."""
    usage = getattr(response, "usage_metadata", None) or {}
    if isinstance(usage, dict) and usage:
        prompt = usage.get("input_tokens")
        completion = usage.get("output_tokens")
        if prompt is None:
            prompt = usage.get("prompt_tokens", 0)
        if completion is None:
            completion = usage.get("completion_tokens", 0)
        return int(prompt or 0), int(completion or 0)

    meta = getattr(response, "response_metadata", None) or {}
    if not isinstance(meta, dict):
        return 0, 0
    token_usage = meta.get("token_usage") or meta.get("usage") or {}
    if isinstance(token_usage, dict):
        prompt = token_usage.get("prompt_tokens") or token_usage.get("input_tokens") or 0
        completion = (
            token_usage.get("completion_tokens")
            or token_usage.get("output_tokens")
            or 0
        )
        return int(prompt or 0), int(completion or 0)
    return 0, 0


def call_llm(
    llm: Any,
    prompt: str,
    *,
    label: Optional[str] = None,
    step: Optional[int] = None,
    total: Optional[int] = None,
    model: Optional[str] = None,
    provider: Optional[str] = None,
) -> str:
    """Call the LLM with a single human message and return the response text.

    Optional ``label`` / ``step`` / ``total`` emit an INFO line before the HTTP
    call so opaque client logs (``HTTP Request: POST .../chat/completions``)
    can be correlated with pipeline stages.

    Records token usage and estimated USD cost on the active ``CostTracker``.
    """
    from langchain_core.messages import HumanMessage

    from zettel.pricing import estimate_llm_cost
    from zettel.usage import record_llm

    if label:
        if step is not None and total is not None:
            logger.info("LLM [%d/%d] %s", step, total, label)
        elif step is not None:
            logger.info("LLM [%d] %s", step, label)
        else:
            logger.info("LLM %s", label)

    response = llm.invoke([HumanMessage(content=prompt)])
    content = response.content
    if not isinstance(content, str):
        content = str(content)

    model_name = _resolve_model_name(llm, model)
    tokens_in, tokens_out = _extract_usage(response)
    cost = estimate_llm_cost(
        model_name, tokens_in, tokens_out, provider=provider,
    )
    record_llm(
        model=model_name or "unknown",
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=cost,
        label=label or "",
        step=step,
        total=total,
    )
    return content


def load_prompt(path: Path) -> str:
    """Load a prompt template from a file path."""
    if not path.exists():
        raise FileNotFoundError(f"Prompt não encontrado: {path}")
    return path.read_text(encoding="utf-8")


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
