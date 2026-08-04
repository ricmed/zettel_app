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


def call_llm(
    llm: Any,
    prompt: str,
    *,
    label: Optional[str] = None,
    step: Optional[int] = None,
    total: Optional[int] = None,
) -> str:
    """Call the LLM with a single human message and return the response text.

    Optional ``label`` / ``step`` / ``total`` emit an INFO line before the HTTP
    call so opaque client logs (``HTTP Request: POST .../chat/completions``)
    can be correlated with pipeline stages.
    """
    from langchain_core.messages import HumanMessage

    if label:
        if step is not None and total is not None:
            logger.info("LLM [%d/%d] %s", step, total, label)
        elif step is not None:
            logger.info("LLM [%d] %s", step, label)
        else:
            logger.info("LLM %s", label)

    response = llm.invoke([HumanMessage(content=prompt)])
    return response.content


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
