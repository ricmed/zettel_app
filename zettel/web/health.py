"""LLM credential readiness, shared by documents / pipeline / settings / manual."""

from __future__ import annotations

import os
from typing import Any

_PROVIDER_ENV = {
    "openai": ("OPENAI_API_KEY",),
    "openrouter": ("OPENROUTER_API_KEY", "OPENAI_API_KEY"),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "gemini": ("GOOGLE_API_KEY",),
}


def llm_phase_ready(cfg: Any, phase: str) -> bool:
    """True when the configured provider for ``phase`` has a credential (or needs none)."""
    from zettel.config import llm_phase
    from zettel.llm import normalize_llm_provider

    provider = normalize_llm_provider(llm_phase(cfg, phase).provider)
    required = _PROVIDER_ENV.get(provider)
    if required is None:
        return True
    return any(os.getenv(name) for name in required)


def llm_ready(cfg: Any) -> bool:
    from zettel.config import LLM_PHASES

    return all(llm_phase_ready(cfg, phase) for phase in LLM_PHASES)


def llm_phase_rows(cfg: Any) -> list[dict[str, str]]:
    from zettel.config import LLM_PHASES, llm_phase

    rows = []
    for phase in LLM_PHASES:
        spec = llm_phase(cfg, phase)
        rows.append({"phase": phase, "provider": spec.provider, "model": spec.model})
    return rows
