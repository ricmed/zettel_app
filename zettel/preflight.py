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
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from zettel.config import AppConfig, llm_phase
from zettel.note_content import note_content
from zettel.pricing import estimate_llm_cost
from zettel.state import StateDB

logger = logging.getLogger(__name__)

# `connector.context.build_rag_context` renders each retrieved note as a header
# line (id, wikilink, tags or relation) plus its content up to
# `linking.rag_note_chars` (#212).
RAG_HEADER_CHARS = 120


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


_UNCOUNTED = (
    "Hits do cache SQLite nao estao descontados.",
    "Cache de prompt do provedor nao esta descontado.",
    "Tokens de thinking nao entram nesta conta.",
)


def _filled_overhead(cfg: AppConfig, filename: str, extra: dict[str, str] | None = None) -> int:
    """Tokens of one call's prompt after placeholders and few-shots are filled.

    The per-item payload (chunk text, RAG, chapter text) is counted by the
    caller. Here the user template is filled with empty leftovers, so a
    ``{chunk_text}`` slot is not counted twice.
    """
    path = Path(cfg.prompts_path) / filename
    try:
        from zettel.llm import fill_template, load_prompt_parts

        parts = load_prompt_parts(path)
    except OSError:
        logger.debug("Prompt %s indisponivel para o pre-voo; overhead = 0", path)
        return 0
    mapping: dict[str, str] = {"language": cfg.language, "domain": cfg.domain.name}
    if extra:
        mapping.update(extra)
    system = fill_template(parts.system, mapping) if parts.system else ""
    user = fill_template(parts.user_template, mapping)
    text = f"{system}\n{user}" if system else user
    return estimate_tokens(text)


def _example_fields(cfg: AppConfig, prompt_name: str) -> dict[str, str]:
    from zettel.domain_examples import load_domain_examples, render_for_prompt

    try:
        return render_for_prompt(load_domain_examples(cfg.domain.examples_path), prompt_name)
    except OSError:
        logger.debug("Exemplos de dominio indisponiveis para o pre-voo")
        return {}


def _output_tokens_per_call(db: StateDB, phase: str, fallback: int) -> tuple[int, str]:
    """Mean completion tokens of past runs of ``phase``, else the config target.

    ``runs`` has no model column, so the mean is every finished run of the phase
    in this database. The note says so instead of pretending to filter.
    """
    from zettel.usage import pipeline_phase_name

    total_out = 0
    total_calls = 0
    for row in db.get_recent_runs(limit=200):
        if pipeline_phase_name(row.get("pipeline_signature")) != phase:
            continue
        if row.get("status") not in ("completed", "failed"):
            continue
        calls = int(row.get("llm_calls") or 0)
        if calls <= 0:
            continue
        total_out += int(row.get("tokens_completion") or 0)
        total_calls += calls
    if not total_calls:
        return fallback, "Saida: alvo do config (sem execucoes anteriores desta fase)."
    mean = max(1, total_out // total_calls)
    return mean, (
        "Saida: media das execucoes anteriores desta fase. O registro de runs nao guarda o modelo."
    )


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
            spec.model,
            input_tokens,
            output_tokens,
            provider=spec.provider,
        ),
        caveats=caveats or [],
    )


# ── Per-phase estimators ───────────────────────────────────────────────


def estimate_extract(cfg: AppConfig, db: StateDB) -> PreflightEstimate:
    """One Prompt 1 call per pending chunk."""
    chunks = db.get_chunks_by_status("pending")
    overhead = _filled_overhead(cfg, "literature_note.md", _example_fields(cfg, "literature_note"))
    input_tokens = sum(estimate_tokens(c.get("text") or "") for c in chunks)
    input_tokens += overhead * len(chunks)
    per_call, output_note = _output_tokens_per_call(
        db, "extract", cfg.extraction.preflight_output_tokens_per_chunk
    )
    return _build(
        cfg,
        "extract",
        items=len(chunks),
        item_label="chunk(s) pendente(s)",
        input_tokens=input_tokens,
        output_tokens=len(chunks) * per_call,
        caveats=[
            "Imagens pendentes (llm.images) nao entram nesta conta.",
            output_note,
            *_UNCOUNTED,
        ],
    )


def rag_note_chars(db: StateDB, cap: int) -> int:
    """Mean characters one neighbour adds to the RAG context: its content, capped.

    An empty vault has no mean, so the cap stands in as the upper bound.
    """
    notes = db.list_notes()
    if not notes:
        return cap
    lengths = [
        min(len(note_content(n.get("title") or "", n.get("body") or "")), cap) for n in notes
    ]
    return sum(lengths) // len(lengths)


def estimate_reconnect(cfg: AppConfig, db: StateDB, notes: list[dict]) -> PreflightEstimate:
    """One Prompt 2 call per permanent note whose thesis or definition can be linked."""
    from zettel.connector.reconnect import candidate_from_note

    candidates = [
        {"candidate": cand} for note in notes if (cand := candidate_from_note(note)) is not None
    ]
    estimate = estimate_connect(cfg, db, candidates)
    return replace(
        estimate,
        phase="reconnect",
        item_label="nota(s) permanente(s)",
    )


def estimate_connect(
    cfg: AppConfig,
    db: StateDB,
    candidates: list[dict],
) -> PreflightEstimate:
    """One Prompt 2 call per approved candidate, or two when ``prompt2_mode`` is split."""
    graph_cfg = cfg.linking.graph_expansion
    context_notes = cfg.linking.topk
    if graph_cfg.enabled:
        context_notes += graph_cfg.max_neighbors
    if cfg.linking.distant_analogy_topk > 0:
        context_notes += cfg.linking.distant_analogy_topk
    per_note = RAG_HEADER_CHARS + rag_note_chars(db, cfg.linking.rag_note_chars)
    context_tokens = estimate_tokens("x" * (context_notes * per_note))
    calls = 2 if cfg.linking.prompt2_mode == "split" else 1
    if calls == 2:
        overhead = _filled_overhead(cfg, "note_write.md") + _filled_overhead(cfg, "note_link.md")
    else:
        overhead = _filled_overhead(
            cfg, "permanent_note.md", _example_fields(cfg, "permanent_note")
        )

    input_tokens = 0
    for item in candidates:
        cand = item.get("candidate")
        text = " ".join(
            str(getattr(cand, attr, "") or "") for attr in ("thesis", "definition", "limits")
        )
        input_tokens += estimate_tokens(text) + context_tokens + overhead
    per_call, output_note = _output_tokens_per_call(
        db, "connect", cfg.linking.preflight_output_tokens_per_note
    )
    caveats = [
        (
            f"Contexto RAG estimado em {context_notes} nota(s) por conceito "
            f"(topk + grafo + analogias distantes)."
        ),
        "O guard PT-BR nao entra nesta conta.",
        output_note,
        *_UNCOUNTED,
    ]
    if calls == 2:
        caveats.insert(0, "Modo split: uma chamada de escrita e uma de ligacao por conceito.")
    return _build(
        cfg,
        "connect",
        items=len(candidates),
        item_label="conceito(s) aprovado(s)",
        input_tokens=input_tokens,
        output_tokens=len(candidates) * per_call * calls,
        caveats=caveats,
    )


def estimate_summarize(
    cfg: AppConfig, db: StateDB, source_id: str | None = None
) -> PreflightEstimate:
    """One call per stale chapter (more when a chapter map-reduces), plus one
    reduce per source whose chapters changed.

    A chapter whose ``summary_checksum`` still matches its ``chapter_checksum``
    costs nothing and is not counted, so re-running on a settled vault estimates
    zero work.
    """
    chapters = db.get_chapters_needing_summary(source_id)
    overhead = _filled_overhead(cfg, "chapter_summary.md")
    budget = cfg.summarize.max_input_chars

    calls = 0
    input_tokens = 0
    map_reduced = 0
    for chapter in chapters:
        text = "\n\n".join(
            (c.get("text") or "") for c in db.get_chunks_for_chapter(chapter["chapter_id"])
        )
        if not text.strip():
            continue
        # Over budget the chapter is split, and the reduce pass re-reads the
        # partial summaries — so the text is paid for once plus a small tail.
        parts = max(1, -(-len(text) // budget))
        chapter_calls = parts + (1 if parts > 1 else 0)
        if parts > 1:
            map_reduced += 1
        calls += chapter_calls
        input_tokens += estimate_tokens(text) + overhead * chapter_calls

    source_ids = {c["source_id"] for c in chapters}
    if source_ids:
        reduce_overhead = _filled_overhead(cfg, "source_summary.md")
        per_chapter_summary = cfg.summarize.preflight_output_tokens_per_chapter
        for sid in source_ids:
            n = len(db.get_chapters_for_source(sid))
            input_tokens += reduce_overhead + n * per_chapter_summary
        calls += len(source_ids)

    caveats = [
        "Capitulo com resumo atualizado (checksum igual) nao entra na conta.",
        *_UNCOUNTED,
    ]
    if map_reduced:
        caveats.append(f"{map_reduced} capitulo(s) acima do orcamento: vao por map-reduce.")
    per_call, output_note = _output_tokens_per_call(
        db, "summarize", cfg.summarize.preflight_output_tokens_per_chapter
    )
    caveats.append(output_note)
    return _build(
        cfg,
        "summarize",
        items=calls,
        item_label="chamada(s)",
        input_tokens=input_tokens,
        output_tokens=calls * per_call,
        caveats=caveats,
    )


# Output target for one garden call when no previous run exists. Not a cap.
GARDEN_OUTPUT_TOKENS_FALLBACK = 800
# A MOC prompt lists each note as "- **N1**: <title>"; the alias and markup.
_GARDEN_LINE_OVERHEAD_CHARS = 12


def estimate_garden(cfg: AppConfig, db: StateDB, report: Any) -> PreflightEstimate:
    """One call per planned MOC, sub-MOC, incremental update or MOC of MOCs (``garden --plan``).

    ``report`` is the planned ``GardenReport``. The per-call overhead is the
    filled prompt (taxonomy included for a creation); each entry note costs its
    title. A MOC of MOCs costs one summary call (#296).
    The decision shadow (Jev) and the label embeddings are not in this figure.
    """
    from zettel.taxonomy import resolve_allowed_topics

    hubs = report.pipeline == "garden_hubs"
    create_prompt = "moc_hub_generation.md" if hubs else "moc_generation.md"
    update_prompt = "moc_hub_incremental.md" if hubs else "moc_incremental.md"
    try:
        allowed, detail = resolve_allowed_topics(
            cfg.gardener.topics_path, cfg.gardener.allowed_topics, strict=False
        )
    except Exception:
        allowed, detail = [], ""
    create_overhead = _filled_overhead(
        cfg,
        create_prompt,
        {
            "allowed_topics_section": "\n".join(f"- {t}" for t in allowed),
            "taxonomy_detail": detail,
        },
    )
    update_overhead = _filled_overhead(cfg, update_prompt)
    mean_title = db.mean_permanent_title_chars() or 60
    per_note = estimate_tokens("x" * (mean_title + _GARDEN_LINE_OVERHEAD_CHARS))

    summary_overhead = _filled_overhead(cfg, "moc_summary_refresh.md")
    cap = cfg.gardener.max_entries_per_moc

    calls = 0
    input_tokens = 0
    for item in report.actions:
        sent = min(item.size, cap) * per_note  # only entry notes reach the prompt (ADR-060)
        if item.action in ("created", "submoc"):
            calls += 1
            input_tokens += create_overhead + sent
        elif item.action in ("incremental", "home"):
            calls += 1
            input_tokens += update_overhead + sent
        elif item.action == "moc_of_mocs":
            calls += 1
            input_tokens += summary_overhead
    per_call, output_note = _output_tokens_per_call(db, "garden", GARDEN_OUTPUT_TOKENS_FALLBACK)
    return _build(
        cfg,
        "garden",
        items=calls,
        item_label="chamada(s)",
        input_tokens=input_tokens,
        output_tokens=calls * per_call,
        caveats=[
            "A camada de decisao em shadow (Jev) e o embedding dos rotulos nao entram na conta.",
            (
                "Cada chamada conta ate max_entries_per_moc notas; um cluster que o MOC da "
                "categoria nao aceitar vira sub-MOC e custa uma segunda chamada."
            ),
            output_note,
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
        cfg,
        "article",
        items=calls,
        item_label="chamada(s) (piso)",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        caveats=[
            "Piso: revisoes no HITL e ciclos do juiz podem aumentar.",
            "A reescrita de personalidade nao entra (depende do perfil escolhido).",
            "O piso nao inclui o texto dos prompts de artigo.",
            *_UNCOUNTED,
        ],
    )
