"""Pre-flight token/cost estimate for the commands that spend real LLM money.

`extract`, `connect` and `article` can each fire dozens of calls. The cost was
only visible afterwards, in the `runs` row. These are **pure functions**: they
read SQLite and config, never call an LLM, and never prompt. The CLI renders the
estimate and asks; the web worker and any test calling `run_*` directly are
unaffected.

Every number here is an estimate. Tokens are ``chars // 4`` (the estimator the
cost layer already uses) and the price comes from LiteLLM's public map, so an
unknown or local model reports $0 — the same convention as the rest of the app.
The SQLite response cache is deliberately **not** discounted: a hit is free, so
the estimate is an upper bound rather than a promise.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from zettel.config import AppConfig, llm_phase
from zettel.pricing import estimate_llm_cost
from zettel.state import StateDB

logger = logging.getLogger(__name__)

# `_build_rag_context` renders each retrieved note as a wikilink plus a 150-char
# snippet and its tags, so a context entry costs far less than a whole note.
RAG_CHARS_PER_NOTE = 250


def estimate_tokens(text: str) -> int:
    """Chars/4 — the same rough estimator `pricing.estimate_embed_tokens` uses."""
    return max(0, len(text or "")) // 4


@dataclass(frozen=True)
class PreflightEstimate:
    """What one phase is about to spend, before it spends it."""

    phase: str
    provider: str
    model: str
    items: int
    item_label: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    caveats: list[str] = field(default_factory=list)

    @property
    def has_work(self) -> bool:
        return self.items > 0


def _prompt_tokens(cfg: AppConfig, filename: str) -> int:
    """Token overhead of a prompt template, read once."""
    path = Path(cfg.prompts_path) / filename
    try:
        return estimate_tokens(path.read_text(encoding="utf-8"))
    except OSError:
        logger.debug("Prompt %s indisponivel para o pre-voo; overhead = 0", path)
        return 0


def _build(
    cfg: AppConfig,
    phase: str,
    *,
    items: int,
    item_label: str,
    input_tokens: int,
    output_tokens: int,
    caveats: list[str] | None = None,
) -> PreflightEstimate:
    spec = llm_phase(cfg, phase)
    return PreflightEstimate(
        phase=phase,
        provider=spec.provider,
        model=spec.model,
        items=items,
        item_label=item_label,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=estimate_llm_cost(
            spec.model, input_tokens, output_tokens, provider=spec.provider,
        ),
        caveats=caveats or [],
    )


# ── Per-phase estimators ───────────────────────────────────────────────


def estimate_extract(cfg: AppConfig, db: StateDB) -> PreflightEstimate:
    """One Prompt 1 call per pending chunk."""
    chunks = db.get_pending_chunks()
    overhead = _prompt_tokens(cfg, "literature_note.md")
    input_tokens = sum(estimate_tokens(c.get("text") or "") for c in chunks)
    input_tokens += overhead * len(chunks)
    return _build(
        cfg, "extract",
        items=len(chunks),
        item_label="chunk(s) pendente(s)",
        input_tokens=input_tokens,
        output_tokens=len(chunks) * cfg.extraction.preflight_output_tokens_per_chunk,
        caveats=[
            "Imagens pendentes (llm.images) nao entram nesta conta.",
            "Hits do cache SQLite nao estao descontados.",
        ],
    )


def estimate_connect(
    cfg: AppConfig, db: StateDB, candidates: list[dict],
) -> PreflightEstimate:
    """One Prompt 2 call per approved candidate, plus its RAG context."""
    graph_cfg = cfg.retrieval.graph_expansion
    context_notes = cfg.linking.topk + (
        graph_cfg.max_neighbors if graph_cfg.enabled else 0
    )
    context_tokens = estimate_tokens("x" * (context_notes * RAG_CHARS_PER_NOTE))
    overhead = _prompt_tokens(cfg, "permanent_note.md")

    input_tokens = 0
    for item in candidates:
        cand = item.get("candidate")
        text = " ".join(
            str(getattr(cand, attr, "") or "")
            for attr in ("thesis", "definition", "intuition", "limits")
        )
        input_tokens += estimate_tokens(text) + context_tokens + overhead
    return _build(
        cfg, "connect",
        items=len(candidates),
        item_label="conceito(s) aprovado(s)",
        input_tokens=input_tokens,
        output_tokens=len(candidates) * cfg.linking.preflight_output_tokens_per_note,
        caveats=[
            f"Contexto RAG estimado em {context_notes} nota(s) por conceito.",
            "Hits do cache SQLite nao estao descontados.",
        ],
    )


def estimate_article(cfg: AppConfig) -> PreflightEstimate:
    """A **floor**: the calls the graph makes even if nothing loops.

    enrich + outline + one draft per section + assemble + the judge ceiling. HITL
    revisions and personality rewrites push the real number up, never down.
    """
    art = cfg.retrieval.article
    calls = 1 + 1 + art.max_sections + 1 + art.max_judge_iterations
    context_tokens = estimate_tokens("x" * (art.max_context_notes * art.max_chars_per_note))
    section_tokens = estimate_tokens("x" * art.chars_per_section_draft)

    input_tokens = context_tokens + art.max_sections * (context_tokens // 2)
    input_tokens += art.max_judge_iterations * section_tokens * art.max_sections
    output_tokens = art.max_sections * section_tokens + art.max_judge_iterations * 500
    return _build(
        cfg, "article",
        items=calls,
        item_label="chamada(s) (piso)",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        caveats=[
            "Piso: revisoes no HITL e ciclos do juiz podem aumentar.",
            "A reescrita de personalidade nao entra (depende do perfil escolhido).",
        ],
    )
