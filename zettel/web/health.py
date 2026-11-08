"""LLM and embedding credential readiness, shared by documents / pipeline / settings / manual.

The rules live in ``zettel.credentials`` so ``zettel doctor`` judges a phase exactly
as the web does; these wrappers only keep the import lazy, as the web package requires.
"""

from __future__ import annotations

from typing import Any


def llm_phase_ready(cfg: Any, phase: str) -> bool:
    """True when the configured provider for ``phase`` has a credential (or needs none)."""
    from zettel.credentials import llm_phase_ready as _llm_phase_ready

    return _llm_phase_ready(cfg, phase)


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
