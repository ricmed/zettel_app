"""LLM and embedding credential readiness, shared by documents / pipeline / settings / manual."""

from __future__ import annotations

from typing import Any


def llm_phase_ready(cfg: Any, phase: str) -> bool:
    """True when the configured provider for ``phase`` has a credential (or needs none)."""
    from zettel.config import llm_phase
    from zettel.credentials import LLM_PROVIDER_ENV, has_credential
    from zettel.llm import normalize_llm_provider

    provider = normalize_llm_provider(llm_phase(cfg, phase).provider)
    return has_credential(LLM_PROVIDER_ENV.get(provider))


def llm_ready(cfg: Any) -> bool:
    from zettel.config import LLM_PHASES

    return all(llm_phase_ready(cfg, phase) for phase in LLM_PHASES)


def embedding_ready(cfg: Any) -> bool:
    from zettel.credentials import embedding_ready as _embedding_ready

    return _embedding_ready(cfg)


def llm_phase_rows(cfg: Any) -> list[dict[str, str]]:
    from zettel.config import LLM_PHASES, llm_phase

    rows = []
    for phase in LLM_PHASES:
        spec = llm_phase(cfg, phase)
        rows.append({"phase": phase, "provider": spec.provider, "model": spec.model})
    return rows
