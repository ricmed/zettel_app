"""LLM credential readiness, shared by documents / pipeline / settings / manual."""

from __future__ import annotations

import os
from typing import Any


def llm_ready(cfg: Any) -> bool:
    from zettel.config import LLM_PHASES, llm_phase
    from zettel.llm import normalize_llm_provider

    env_names = {
        "openai": ("OPENAI_API_KEY",),
        "openrouter": ("OPENROUTER_API_KEY", "OPENAI_API_KEY"),
        "anthropic": ("ANTHROPIC_API_KEY",),
        "gemini": ("GOOGLE_API_KEY",),
    }
    for phase in LLM_PHASES:
        provider = normalize_llm_provider(llm_phase(cfg, phase).provider)
        required = env_names.get(provider)
        if required is not None and not any(os.getenv(name) for name in required):
            return False
    return True


def llm_phase_rows(cfg: Any) -> list[dict[str, str]]:
    from zettel.config import LLM_PHASES, llm_phase

    rows = []
    for phase in LLM_PHASES:
        spec = llm_phase(cfg, phase)
        rows.append({"phase": phase, "provider": spec.provider, "model": spec.model})
    return rows
