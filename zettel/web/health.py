"""LLM and embedding credential readiness, shared by documents / pipeline / settings / manual.

The per-phase rule lives in ``zettel.credentials`` so ``zettel doctor`` judges a phase
exactly as the web does.
"""

from __future__ import annotations

from typing import Any

from zettel.credentials import embedding_ready, llm_phase_ready

__all__ = ["embedding_ready", "llm_phase_ready", "llm_phase_rows", "llm_ready"]


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
