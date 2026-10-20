"""What each web operation needs before it may run (#314).

``blocker`` is the single decision: the page disables a button and shows the
reason it returns, and the handler answers 409 with that same reason, so a
button is enabled exactly when the server accepts the submit. An operation
needs the credential of each LLM phase it calls, not of all nine, and the
embedding credential when it searches or writes the vector index.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from zettel.web.health import embedding_ready, llm_phase_ready

PIPELINE_OPERATIONS = (
    "extract",
    "connect",
    "garden",
    "garden_hubs",
    "sync",
    "retry_chunks",
    "retry_assets",
)

_PHASES = {
    "extract": ("extract",),
    "connect": ("connect",),
    "garden": ("garden",),
    "garden_hubs": ("garden",),
    # harvest (bibliography), extract, review (dedupe), connect, garden.
    "run_all": ("harvest", "extract", "review", "connect", "garden"),
    "ask": ("ask",),
    "article": ("article",),
    "summarize": ("summarize",),
}

# Search (connect's strict Retriever, ask, article, catalog), note and summary
# upserts (sync, summarize) and the garden's label embeddings.
_EMBEDDING = {"connect", "garden", "sync", "run_all", "ask", "article", "summarize", "catalog"}

# Operation -> (state key that must be positive, message when it is not).
_WORK = {
    "extract": ("chunks_pending", "Não há chunks pendentes. Execute um harvest válido primeiro."),
    "connect": ("connect_queue", "Nenhum candidato aprovado aguardando connect."),
    "garden": ("notes", "Não há notas permanentes para jardinagem."),
    "garden_hubs": ("notes", "Não há notas permanentes para jardinagem."),
    "retry_chunks": ("chunks_failed", "Não há chunks com falha para reprocessar."),
    "retry_assets": ("assets_failed", "Não há assets com falha para reprocessar."),
}


def llm_phases(cfg: Any, operation: str) -> tuple[str, ...]:
    """The LLM phases ``operation`` calls; vision only when images are on."""
    phases = _PHASES.get(operation, ())
    if operation in {"extract", "run_all"} and cfg.images.enabled:
        phases = (*phases, "images")
    return phases


def pipeline_state(db: Any) -> dict[str, int]:
    """The counts ``blocker`` reads, in one place for the page and the handler."""
    from zettel.connector import load_approved_candidates

    stats = db.get_stats()
    return {
        "chunks_pending": stats["chunks_pending"],
        "chunks_failed": stats["chunks_failed"],
        "notes": stats["notes"],
        "assets_failed": stats["assets_failed"],
        # What the connect job will process: approved and resumable `connecting`.
        "connect_queue": len(load_approved_candidates(db)),
    }


def blocker(cfg: Any, operation: str, state: Mapping[str, int] | None = None) -> str | None:
    """Why ``operation`` cannot run now, or ``None``. Nothing to do is said first."""
    work = _WORK.get(operation)
    if work and state is not None and not state.get(work[0]):
        return work[1]
    missing = [phase for phase in llm_phases(cfg, operation) if not llm_phase_ready(cfg, phase)]
    if missing:
        names = ", ".join(missing)
        verb = "não tem" if len(missing) == 1 else "não têm"
        noun = "A fase" if len(missing) == 1 else "As fases"
        return f"{noun} {names} {verb} credencial de LLM. Verifique Configuração / saúde."
    if operation in _EMBEDDING and not embedding_ready(cfg):
        return (
            "Sem credencial de embedding: esta operação usa o índice vetorial. "
            "Verifique Configuração / saúde."
        )
    return None
