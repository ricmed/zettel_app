"""Shared LLM helpers — provider instantiation, call, prompt loading, JSON extraction.

Centralizes functions that were previously duplicated verbatim across
extractor.py, connector.py and gardener.py.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def get_llm(cfg: Any) -> Any:
    """Instantiate the configured LLM from AppConfig."""
    if cfg.llm.provider == "openai":
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(
            model=cfg.llm.model,
            temperature=cfg.llm.temperature,
            max_retries=cfg.llm.max_retries,
        )
    elif cfg.llm.provider == "anthropic":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(
            model=cfg.llm.model,
            temperature=cfg.llm.temperature,
            max_retries=cfg.llm.max_retries,
        )
    elif cfg.llm.provider == "ollama":
        from langchain_ollama import ChatOllama
        return ChatOllama(
            model=cfg.llm.model,
            temperature=cfg.llm.temperature,
        )
    else:
        raise ValueError(f"LLM provider não suportado: {cfg.llm.provider}")


def call_llm(llm: Any, prompt: str) -> str:
    """Call the LLM with a single human message and return the response text."""
    from langchain_core.messages import HumanMessage
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
